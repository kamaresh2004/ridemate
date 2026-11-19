"""Offline all-India gazetteer for place suggestions.

Nominatim/OSM knows POIs (bus stands, temples, hospitals) but its coverage of
Indian towns and villages is uneven, and it is often slow or unreachable from
the API host, so a district's main places could answer "No places found". This
module indexes the GeoNames settlements exported by
``backend/tools/build_places.py`` and searches them locally: instant, offline,
and complete for every state, district and taluk in India.

Data: GeoNames (https://www.geonames.org/), CC BY 4.0. See
``backend/data/places_india.bin.gz`` and the builder for the packed layout.

The index is stored as ``array``/``bytes`` slices rather than Python objects on
purpose: the whole country is ~557k rows and the API runs on a 512 MB instance,
where one tuple per row (the old South-India format) would not fit.
"""

import array
import gzip
import json
import logging
import math
import os
import re
import struct
import threading
import unicodedata
from functools import lru_cache

from .placenames import meaningful_tokens, normalize_place_query

log = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "places_india.bin.gz")

MAGIC = b"RMPL2"
BUCKET_PREFIX = 3
_SECTION_TYPES = {"u8": "B", "u16": "H", "u32": "I", "i32": "i"}

# Specificity of the GeoNames feature code. A district centroid (ADM2) must
# never outrank the actual town it is named after, and a population figure is
# only meaningful for populated places.
_CODE_SCORE = {
    "PPLC": 100, "PPLA": 99, "PPLA2": 97, "PPLA3": 93, "PPLA4": 89,
    "PPL": 86, "PPLX": 70, "PPLL": 66,
    "ADM3": 45, "ADM2": 36, "ADM1": 26,
}
_POPULATED = {"PPLC", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPL", "PPLX", "PPLL"}

_lock = threading.Lock()
_index = None


def _fold(text):
    """Lowercase, accent-insensitive key ("Kāraikkudi" -> "karaikkudi")."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _ascii_text(text):
    """Display-safe ASCII ("Rāmanāthapuram" -> "Ramanathapuram")."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


@lru_cache(maxsize=32768)
def _squash(text):
    """Collapse doubled letters: "karaikkudi" and "karaikudi" agree.

    The cache is deliberately small: at ~250 bytes per entry a 200k cache is
    50 MB of RSS, which the all-India dataset cannot spare.
    """
    return re.sub(r"(.)\1+", r"\1", text)


class _Index:
    """Packed gazetteer: text blobs + parallel arrays, no per-row objects."""

    __slots__ = (
        "n", "names", "name_off", "folds", "fold_off", "alts", "alt_off",
        "toks", "tok_off", "district_ids", "state_ids", "lat", "lng", "pop",
        "codes", "states", "districts", "code_names", "buckets",
        "bucket_ids", "bucket_ids_off",
    )


def _empty_index():
    idx = _Index()
    idx.n = 0
    idx.names = idx.folds = idx.alts = idx.toks = b""
    for name in ("name_off", "fold_off", "alt_off", "tok_off"):
        setattr(idx, name, array.array("I", [0]))
    idx.district_ids = array.array("H")
    idx.state_ids = array.array("H")
    idx.lat = array.array("i")
    idx.lng = array.array("i")
    idx.pop = array.array("I")
    idx.codes = array.array("B")
    idx.states = []
    idx.districts = []
    idx.code_names = ()
    idx.buckets = {}
    idx.bucket_ids = array.array("I")
    idx.bucket_ids_off = array.array("I", [0])
    return idx


def _read(path):
    with gzip.open(path, "rb") as src:
        if src.read(len(MAGIC)) != MAGIC:
            raise ValueError("not a gazetteer file")
        raw_len = src.read(4)
        if len(raw_len) != 4:
            raise ValueError("truncated header")
        header = json.loads(src.read(struct.unpack("<I", raw_len)[0]).decode("utf-8"))
        sections = {}
        for name, kind, count in header["sections"]:
            if kind == "bytes":
                sections[name] = src.read(count)
                continue
            typecode = _SECTION_TYPES[kind]
            raw = src.read(count * array.array(typecode).itemsize)
            values = array.array(typecode)
            values.frombytes(raw)
            sections[name] = values

    idx = _Index()
    idx.n = int(header["n"])
    for name in ("names", "name_off", "folds", "fold_off", "alts", "alt_off",
                 "toks", "tok_off", "district_ids", "state_ids", "lat", "lng",
                 "pop", "codes"):
        setattr(idx, name, sections[name])
    idx.states = header["states"]
    idx.districts = header["districts"]
    idx.code_names = tuple(header["code_names"])

    key_off = sections["bucket_key_off"]
    keys = sections["bucket_keys"]
    ids_off = sections["bucket_ids_off"]
    buckets = {}
    for k in range(len(key_off) - 1):
        key = keys[key_off[k]:key_off[k + 1] - 1].decode("utf-8")
        buckets[key] = (ids_off[k], ids_off[k + 1])
    idx.buckets = buckets
    idx.bucket_ids = sections["bucket_ids"]
    idx.bucket_ids_off = ids_off
    return idx


def _load(path=DATA_PATH):
    """Load (once) and return the index; empty when the data file is missing."""
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        if not os.path.exists(path):
            _index = _empty_index()
            return _index
        try:
            _index = _read(path)
        except Exception:  # corrupt or half-copied file must not 500 the API
            log.exception("offline gazetteer %s could not be loaded", path)
            _index = _empty_index()
        return _index


def reload(path=DATA_PATH):
    """Drop the cached index (tests / dataset refresh)."""
    global _index
    with _lock:
        _index = None
    return _load(path)


def dataset_size():
    """Rows in the gazetteer (0 when the data file is absent)."""
    return _load().n


def states():
    """State/UT names covered by the gazetteer."""
    return {name for name in _load().states if name}


def districts():
    """District names covered by the gazetteer."""
    return {name for name in _load().districts if name}


def _text(blob, offsets, idx):
    return blob[offsets[idx]:offsets[idx + 1] - 1].decode("utf-8")


def _edit_distance_at_most_one(a, b):
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    served = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if served:
            return False
        served = True
        if la == lb:
            i += 1
            j += 1
        else:
            j += 1
    return True


def _match_score(index, idx, q, tokens, sq_q, sq_tokens):
    fold = _text(index.folds, index.fold_off, idx)
    code = index.code_names[index.codes[idx]]
    sq_name = _squash(fold)

    if fold == q:
        base = 1000
    elif sq_name == sq_q:
        # Same name with doubled letters dropped ("Karaikudi"/"Karaikkudi").
        base = 995
    else:
        alt_text = _text(index.alts, index.alt_off, idx)
        alt_folds = tuple(part for part in alt_text.split("\x1f") if part)
        if q in alt_folds or sq_q in {_squash(alt) for alt in alt_folds}:
            base = 960
        elif fold.startswith(q) or sq_name.startswith(sq_q):
            base = 870
        elif any(alt.startswith(q) or _squash(alt).startswith(sq_q)
                 for alt in alt_folds):
            base = 830
        else:
            name_tokens = _text(index.toks, index.tok_off, idx).split()
            if len(tokens) == 1 and any(
                    tok.startswith(tokens[0]) or _squash(tok).startswith(sq_tokens[0])
                    for tok in name_tokens):
                base = 790
            else:
                matched = 0
                for sq_token in sq_tokens:
                    if any(_squash(name_tok).startswith(sq_token)
                           for name_tok in name_tokens):
                        matched += 1
                if matched == len(tokens):
                    base = 730
                elif matched and len(tokens) <= 2 and any(
                        _squash(tok).startswith(sq_tokens[0]) for tok in name_tokens):
                    # One known word ("Thanjavur someunknownspot") still beats no
                    # suggestion; the list shows the town with a "relaxed" note.
                    base = 340
                else:
                    return None

    score = base + _CODE_SCORE.get(code, 0)
    population = index.pop[idx]
    if code in _POPULATED and population:
        score += min(30, math.log10(population + 1) * 6)
    return score


def _place_dict(index, idx, score):
    display = _ascii_text(_text(index.names, index.name_off, idx))
    district = _ascii_text(index.districts[index.district_ids[idx]])
    state = _ascii_text(index.states[index.state_ids[idx]])
    parts = [display]
    for value in (district, state, "India"):
        if value and value.casefold() not in {p.casefold() for p in parts}:
            parts.append(value)
    short = [display]
    if district and district.casefold() != display.casefold():
        short.append(district)
    elif state and state.casefold() != display.casefold():
        short.append(state)
    return {
        "label": ", ".join(short),
        "address": ", ".join(parts),
        "lat": index.lat[idx] / 100000.0,
        "lng": index.lng[idx] / 100000.0,
        "_score": score,
    }


def _candidates(index, tokens):
    keys = {tok[:BUCKET_PREFIX] if len(tok) > BUCKET_PREFIX else tok
            for tok in tokens if len(tok) > 1}
    if not keys:
        # Only single-letter words: nothing useful can be bucketed.
        return range(index.n)
    # Look in every token's bucket, so a partial match on the second word
    # ("chennai central" -> "Chennai") is still found.
    ids = set()
    buckets = index.buckets
    bucket_ids = index.bucket_ids
    for key in keys:
        span = buckets.get(key)
        if span:
            ids.update(bucket_ids[span[0]:span[1]])
    return ids


def search_places(query, limit=8):
    """Ranked local suggestions for ``query`` (empty list when no dataset)."""
    index = _load()
    if not index.n:
        return []
    q = _fold(" ".join(normalize_place_query(query).split()))
    if len(q) < 2:
        return []
    tokens = meaningful_tokens(q)
    if not tokens:
        return []
    q_nospace = " ".join(tokens)
    sq_q = _squash(q_nospace)
    sq_tokens = [_squash(tok) for tok in tokens]

    ranked = []
    for idx in _candidates(index, tokens):
        score = _match_score(index, idx, q_nospace, tokens, sq_q, sq_tokens)
        if score is not None:
            ranked.append((score, idx))
    if not ranked and len(q_nospace) >= 4:
        # Last resort for spelling variants the correction table does not know
        # ("Karaikudi" vs "Karaikkudi"): one edit away, same opening letters.
        span = index.buckets.get(q_nospace[:BUCKET_PREFIX])
        if span:
            for idx in index.bucket_ids[span[0]:span[1]]:
                if not _edit_distance_at_most_one(
                        q_nospace, _text(index.folds, index.fold_off, idx)):
                    continue
                bonus = 0
                if (index.code_names[index.codes[idx]] in _POPULATED
                        and index.pop[idx]):
                    bonus = min(30, math.log10(index.pop[idx] + 1) * 6)
                ranked.append((500 + bonus, idx))

    folds, fold_off = index.folds, index.fold_off
    ranked.sort(key=lambda pair: (-pair[0],
                                  fold_off[pair[1] + 1] - fold_off[pair[1]],
                                  folds[fold_off[pair[1]]:fold_off[pair[1] + 1] - 1]))

    # The gazetteer has one row per feature (town, district, taluk) and often
    # several spellings. Show the best row per actual place.
    seen = set()
    kept = []
    results = []
    for score, idx in ranked:
        name_key = _squash(_text(folds, fold_off, idx))
        district = index.districts[index.district_ids[idx]]
        key = (name_key, district.casefold())
        if key in seen:
            continue
        lat = index.lat[idx] / 100000.0
        lng = index.lng[idx] / 100000.0
        if any(other_key == name_key
               and abs(lat - other_lat) <= 0.05
               and abs(lng - other_lng) <= 0.05
               for other_key, other_lat, other_lng in kept):
            continue
        seen.add(key)
        kept.append((name_key, lat, lng))
        results.append(_place_dict(index, idx, score))
        if len(results) >= limit:
            break
    return results


def has_confident_match(query, results):
    """True when the top local hit *is* what was typed.

    Used to skip the upstream POI search: for an exact town name the local
    answer is authoritative and instant, and Nominatim would only repeat it.
    """
    if not results:
        return False
    q = _fold(" ".join(normalize_place_query(query).split()))
    tokens = meaningful_tokens(q)
    if len(tokens) != 1:
        return False
    top = results[0]
    name = _fold(top["label"].split(",")[0])
    return name == q or results[0]["_score"] >= 790
