"""Offline South-India gazetteer for place suggestions.

Nominatim/OSM knows POIs (bus stands, temples, hospitals) but its coverage of
Indian towns and villages is uneven, so a district's main places could answer
"No places found". This module indexes the GeoNames settlements exported by
``backend/tools/build_places.py`` and searches them locally: instant, offline,
and complete for the southern states.

Data: GeoNames (https://www.geonames.org/), CC BY 4.0. See
``backend/data/places_south_india.json.gz``.
"""

import gzip
import json
import math
import os
import re
import threading
import unicodedata
from functools import lru_cache

from .placenames import meaningful_tokens, normalize_place_query

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "places_south_india.json.gz")

# Specificity of the GeoNames feature code. A district centroid (ADM2) must
# never outrank the actual town it is named after, and a population figure is
# only meaningful for populated places.
_CODE_SCORE = {
    "PPLC": 100, "PPLA": 99, "PPLA2": 97, "PPLA3": 93, "PPLA4": 89,
    "PPL": 86, "PPLX": 70, "PPLL": 66,
    "ADM3": 45, "ADM2": 36, "ADM1": 26,
}
_POPULATED = {"PPLC", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPL", "PPLX", "PPLL"}

_TOKEN_RE = re.compile(r"[0-9a-z]+")

_lock = threading.Lock()
_entries = None
_buckets = None


def _fold(text):
    """Lowercase, accent-insensitive key ("Kāraikkudi" -> "karaikkudi")."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _ascii_text(text):
    """Display-safe ASCII ("Rāmanāthapuram" -> "Ramanathapuram")."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


@lru_cache(maxsize=200000)
def _squash(text):
    """Collapse doubled letters: "karaikkudi" and "karaikudi" agree."""
    return re.sub(r"(.)\1+", r"\1", text)


def _load(path=DATA_PATH):
    global _entries, _buckets
    with _lock:
        if _entries is not None:
            return _entries
        if not os.path.exists(path):
            _entries, _buckets = [], {}
            return _entries
        with gzip.open(path, "rt", encoding="utf-8") as src:
            rows = json.load(src)

        entries = []
        for name, ascii_name, alternates, district, state, lat, lng, pop, code in rows:
            # GeoNames often carries local diacritics in `name`; the ASCII
            # spelling is what users type and what we want to display.
            display = ascii_name if ascii_name else name
            fold_name = _fold(display)
            alt_folds = tuple({_fold(alt) for alt in alternates
                               if _fold(alt) and _fold(alt) != fold_name})
            tokens = set()
            for value in (name, display, *alternates):
                for tok in _TOKEN_RE.findall(_fold(value)):
                    if len(tok) > 1:
                        tokens.add(tok)
            entries.append((display, district, state, lat, lng, pop, code,
                            fold_name, alt_folds, frozenset(tokens)))

        # Two-letter bucket per token: a 119k-row scan per keystroke is ~180 ms,
        # while a bucket narrows the candidates to a few thousand.
        buckets = {}
        for idx, entry in enumerate(entries):
            for key in {tok[:2] for tok in entry[9] if len(tok) >= 2}:
                buckets.setdefault(key, []).append(idx)

        _entries, _buckets = entries, buckets
        return _entries


def reload(path=DATA_PATH):
    """Drop the cached index (tests / dataset refresh)."""
    global _entries, _buckets
    with _lock:
        _entries = None
        _buckets = None
    return _load(path)


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


def _match_score(entry, q, tokens):
    (_, _district, _state, _lat, _lng, pop, code,
     fold_name, alt_folds, name_tokens) = entry
    sq_name = _squash(fold_name)
    sq_q = _squash(q)
    sq_tokens = [_squash(tok) for tok in tokens]

    if fold_name == q:
        base = 1000
    elif sq_name == sq_q:
        # Same name with doubled letters dropped ("Karaikudi"/"Karaikkudi").
        base = 995
    elif q in alt_folds or sq_q in {_squash(alt) for alt in alt_folds}:
        base = 960
    elif fold_name.startswith(q) or sq_name.startswith(sq_q):
        base = 870
    elif any(alt.startswith(q) or _squash(alt).startswith(sq_q) for alt in alt_folds):
        base = 830
    elif len(tokens) == 1 and any(
            tok.startswith(tokens[0]) or _squash(tok).startswith(sq_tokens[0])
            for tok in name_tokens):
        base = 790
    else:
        matched = 0
        for sq_token in sq_tokens:
            if any(_squash(name_tok).startswith(sq_token) for name_tok in name_tokens):
                matched += 1
        if matched == len(tokens):
            base = 730
        elif matched and len(tokens) <= 2 and any(
                _squash(tok).startswith(sq_tokens[0]) for tok in name_tokens):
            # One known word ("Thanjavur someunknownspot") still beats no
            # suggestion; the list shows the town with a "relaxed" note. The
            # town must be the word that matched, not a stray alternate.
            base = 340
        else:
            return None

    score = base + _CODE_SCORE.get(code, 0)
    if code in _POPULATED and pop:
        score += min(30, math.log10(pop + 1) * 6)
    return score


def _place_dict(entry, score):
    display, district, state, lat, lng = entry[:5]
    display = _ascii_text(display)
    district = _ascii_text(district)
    state = _ascii_text(state)
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
        "lat": lat,
        "lng": lng,
        "_score": score,
    }


def _candidates(tokens):
    prefixes = {tok[:2] for tok in tokens if len(tok) >= 2}
    if not prefixes or len(prefixes) < len(tokens):
        # A one-letter token cannot be narrowed by the two-letter buckets.
        return range(len(_entries))
    # Look in every token's bucket, so a partial match on the second word
    # ("chennai central" -> "Chennai") is still found.
    ids = set()
    for prefix in prefixes:
        ids.update(_buckets.get(prefix, ()))
    return ids


def search_places(query, limit=8):
    """Ranked local suggestions for ``query`` (empty list when no dataset)."""
    entries = _load()
    if not entries:
        return []
    q = _fold(" ".join(normalize_place_query(query).split()))
    tokens = meaningful_tokens(q)
    if not tokens:
        return []
    q_nospace = " ".join(tokens)

    ranked = []
    for idx in _candidates(tokens):
        entry = entries[idx]
        score = _match_score(entry, q_nospace, tokens)
        if score is not None:
            ranked.append((score, entry))
    if not ranked and len(q_nospace) >= 4:
        # Last resort for spelling variants the correction table does not know
        # ("Karaikudi" vs "Karaikkudi"): one edit away, same opening letters.
        for idx in _buckets.get(q_nospace[:2], ()):
            entry = entries[idx]
            if not _edit_distance_at_most_one(q_nospace, entry[7]):
                continue
            bonus = 0
            if entry[6] in _POPULATED and entry[5]:
                bonus = min(30, math.log10(entry[5] + 1) * 6)
            ranked.append((500 + bonus, entry))

    ranked.sort(key=lambda pair: (-pair[0], len(pair[1][7]), pair[1][0].casefold()))

    # The gazetteer has one row per feature (town, district, taluk) and often
    # several spellings. Show the best row per actual place.
    seen = set()
    kept = []
    results = []
    for score, entry in ranked:
        name_key = _squash(entry[7])
        key = (name_key, _fold(entry[1]))
        if key in seen:
            continue
        if any(other_key == name_key
               and abs(entry[3] - other[3]) <= 0.05
               and abs(entry[4] - other[4]) <= 0.05
               for other_key, other in kept):
            continue
        seen.add(key)
        kept.append((name_key, entry))
        results.append(_place_dict(entry, score))
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
