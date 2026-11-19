"""Build the offline all-India place gazetteer used by the autocomplete.

Source: GeoNames (https://download.geonames.org/export/dump/), CC BY 4.0.

The dump is filtered to India's settlements and administrative divisions --
states (ADM1), districts (ADM2), taluks/tehsils (ADM3) and populated places
(PPL*) -- and packed as a compact binary of text blobs + arrays. The old JSON
format stored one Python tuple (with two sets) per row, which cost ~1 KB per
row: fine for the 119k South-India rows, fatal for the 557k all-India ones on
Render's 512 MB free instance. Packed arrays hold the same data in ~65 MB.

Usage (from the repo root):

    python -m backend.tools.build_places --download
    python -m backend.tools.build_places --source C:\\path\\to\\geonames

The output defaults to backend/data/places_india.bin.gz. Regenerate it only
when refreshing the gazetteer: search works offline once it exists.
"""

import argparse
import array
import gzip
import io
import json
import os
import re
import struct
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile

BASE = "https://download.geonames.org/export/dump/"
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "places_india.bin.gz")

MAGIC = b"RMPL2"

# Settlements plus the administrative rows that give every district and taluk a
# name to match, even when GeoNames has no town with the same name.
KEEP_FEATURE_CODES = {
    "PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5", "PPLC", "PPLX", "PPLL",
    "ADM1", "ADM2", "ADM3",
}

# "New" / "old" are filler at query time but part of real names here, so they
# are kept in the data; only obvious non-name alternates are dropped.
MAX_ALTERNATES = 8

CODE_TABLE = ("PPLC", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5",
              "PPL", "PPLX", "PPLL", "ADM1", "ADM2", "ADM3")
CODE_RANK = {
    "PPLC": 9, "PPLA": 8, "PPLA2": 7, "PPLA3": 6, "PPLA4": 5, "PPLA5": 4,
    "PPL": 3, "PPLX": 2, "PPLL": 1, "ADM2": 1, "ADM3": 0, "ADM1": 0,
}

# Bucket keys are the first three letters of each token ("pat" for Patna), so a
# keystroke only scores the rows sharing that prefix. Shorter tokens are their
# own bucket key.
BUCKET_PREFIX = 3

# Same tokenization as backend.places: words only, so "(New)" and "Sultanpur,"
# index as "new"/"sultanpur" instead of never matching.
_TOKEN_RE = re.compile(r"[0-9a-z]+")


def _fold(text):
    """Lowercase, accent-insensitive key ("Kāraikkudi" -> "karaikkudi")."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def download(source_dir, files):
    """Download the GeoNames files into source_dir (idempotent)."""
    os.makedirs(source_dir, exist_ok=True)
    for name in files:
        target = os.path.join(source_dir, name)
        if os.path.exists(target) and os.path.getsize(target) > 0:
            continue
        url = BASE + name
        sys.stderr.write(f"downloading {url}\n")
        with urllib.request.urlopen(url, timeout=600) as resp, open(target, "wb") as out:
            out.write(resp.read())


def read_admin1(path):
    names = {}
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0].startswith("IN."):
                names[parts[0].split(".")[1]] = parts[1]
    return names


def read_admin2(path):
    names = {}
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0].startswith("IN."):
                _, admin1, admin2 = parts[0].split(".")[:3]
                names[(admin1, admin2)] = parts[1]
    return names


def pick_alternates(raw, name, ascii_name):
    """Keep a few ASCII alternate names (Tanjore, Trichy, Ooty, ...)."""
    out = []
    seen = {name.casefold(), ascii_name.casefold()}
    for alt in (raw or "").split(","):
        alt = alt.strip()
        if not alt or not alt.isascii() or not (2 <= len(alt) <= 40):
            continue
        if any(ch.isdigit() for ch in alt):
            continue
        key = alt.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(alt)
        if len(out) >= MAX_ALTERNATES:
            break
    return out


def rows_from_dump(source_dir):
    """Yield one compact row per place, all states and UTs of India."""
    admin1 = read_admin1(os.path.join(source_dir, "admin1CodesASCII.txt"))
    admin2 = read_admin2(os.path.join(source_dir, "admin2Codes.txt"))
    seen = {}
    with io.open(os.path.join(source_dir, "IN.txt"), encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 19 or p[8] != "IN":
                continue
            if p[6] not in ("P", "A") or p[7] not in KEEP_FEATURE_CODES:
                continue
            name = (p[1] or "").strip()
            ascii_name = (p[2] or name).strip()
            if len(name) < 2 or name.isdigit():
                continue
            try:
                lat = round(float(p[4]), 5)
                lng = round(float(p[5]), 5)
                population = int(p[14] or 0)
            except (TypeError, ValueError):
                continue
            code = p[7]
            district = admin2.get((p[10], p[11]), "") if p[11] else ""
            state = admin1.get(p[10], "")
            # The same place is often both PPL and PPLA* or an ADM row. Merge
            # rows within ~2 km that share a name, keeping the most specific
            # code and the larger population.
            dedupe = (name.casefold(), round(lat, 2), round(lng, 2))
            row = [name, ascii_name, pick_alternates(p[3], name, ascii_name),
                   district, state, lat, lng, population, code]
            current = seen.get(dedupe)
            if current is None:
                seen[dedupe] = row
                continue
            current_key = (current[7], CODE_RANK.get(current[8], 0))
            row_key = (population, CODE_RANK.get(code, 0))
            if row_key > current_key:
                seen[dedupe] = row
    rows = list(seen.values())
    # Biggest settlements first: stable ordering for equal scores, and the
    # front of the list is what a bare prefix like "ka" will surface first.
    rows.sort(key=lambda r: (-r[7], r[1].casefold()))
    return rows


def _blob(values):
    """Pack variable-length text: blob + start offset per value, '\\n'-separated."""
    blob = bytearray()
    offsets = array.array("I", [0])
    for value in values:
        blob += value.encode("utf-8")
        blob += b"\n"
        offsets.append(len(blob))
    return bytes(blob), offsets


def _pack(rows):
    """Turn the rows into the section list written to disk."""
    code_ids = {code: idx for idx, code in enumerate(CODE_TABLE)}
    # Index 0 is the empty name: a row whose adm1/adm2 code has no entry must
    # show no district, not the alphabetically first one.
    states = [""] + sorted({r[4] for r in rows if r[4]})
    state_ids = {name: idx for idx, name in enumerate(states)}
    districts = [""] + sorted({r[3] for r in rows if r[3]})
    district_ids = {name: idx for idx, name in enumerate(districts)}

    names, name_off = _blob([r[1] for r in rows])
    folds, fold_off = _blob([_fold(r[1]) for r in rows])
    alts, alt_off = _blob(["\x1f".join(sorted({_fold(alt) for alt in r[2]}
                                              - {_fold(r[1])}))
                           for r in rows])

    token_texts = []
    bucket_keys = []
    for r in rows:
        tokens = set()
        for value in (r[0], r[1], *r[2]):
            tokens.update(_TOKEN_RE.findall(_fold(value)))
        cleaned = " ".join(sorted(tok for tok in tokens if len(tok) > 1))
        token_texts.append(cleaned)
        bucket_keys.append({tok[:BUCKET_PREFIX] for tok in cleaned.split()})
    toks, tok_off = _blob(token_texts)

    buckets = {}
    for idx, keys in enumerate(bucket_keys):
        for key in keys:
            ids = buckets.get(key)
            if ids is None:
                buckets[key] = array.array("I", [idx])
            else:
                ids.append(idx)
    ordered = sorted(buckets)
    bucket_keys_blob, bucket_key_off = _blob(ordered)
    bucket_ids = array.array("I")
    bucket_ids_off = array.array("I", [0])
    for key in ordered:
        bucket_ids.extend(buckets[key])
        bucket_ids_off.append(len(bucket_ids))

    lat = array.array("i", [int(round(r[5] * 100000)) for r in rows])
    lng = array.array("i", [int(round(r[6] * 100000)) for r in rows])
    pop = array.array("I", [max(0, int(r[7])) for r in rows])
    codes = array.array("B", [code_ids[r[8]] for r in rows])
    d_ids = array.array("H", [district_ids.get(r[3], 0) for r in rows])
    s_ids = array.array("H", [state_ids.get(r[4], 0) for r in rows])

    payload = [
        ("names", "bytes", names),
        ("name_off", "u32", name_off),
        ("folds", "bytes", folds),
        ("fold_off", "u32", fold_off),
        ("alts", "bytes", alts),
        ("alt_off", "u32", alt_off),
        ("toks", "bytes", toks),
        ("tok_off", "u32", tok_off),
        ("district_ids", "u16", d_ids),
        ("state_ids", "u16", s_ids),
        ("lat", "i32", lat),
        ("lng", "i32", lng),
        ("pop", "u32", pop),
        ("codes", "u8", codes),
        ("bucket_keys", "bytes", bucket_keys_blob),
        ("bucket_key_off", "u32", bucket_key_off),
        ("bucket_ids", "u32", bucket_ids),
        ("bucket_ids_off", "u32", bucket_ids_off),
    ]
    header = {
        "v": 2,
        "n": len(rows),
        "states": states,
        "districts": districts,
        "code_names": list(CODE_TABLE),
        "sections": [[name, kind,
                      len(value) if kind == "bytes" else len(value)]
                     for name, kind, value in payload],
    }
    return header, payload


def _write(out_path, header, payload):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    raw_header = json.dumps(header, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8")
    with gzip.open(out_path, "wb", compresslevel=9) as out:
        out.write(MAGIC)
        out.write(struct.pack("<I", len(raw_header)))
        out.write(raw_header)
        for _name, _kind, value in payload:
            out.write(value.tobytes() if hasattr(value, "tobytes") else value)


def build(source_dir, out_path):
    rows = rows_from_dump(source_dir)
    header, payload = _pack(rows)
    _write(out_path, header, payload)
    sys.stderr.write(
        f"wrote {len(rows)} places -> {out_path} "
        f"({os.path.getsize(out_path) / 1024 / 1024:.1f} MB, "
        f"{len(header['states'])} states/UTs, "
        f"{len(header['districts'])} districts)\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="directory holding IN.txt/admin1/admin2 files")
    ap.add_argument("--download", action="store_true",
                    help="download the GeoNames files to a temp directory first")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    source = args.source
    if args.download or not source:
        source = source or os.path.join(tempfile.gettempdir(), "geonames-in")
        download(source, ["IN.zip", "admin1CodesASCII.txt", "admin2Codes.txt"])
        in_txt = os.path.join(source, "IN.txt")
        if not os.path.exists(in_txt):
            with zipfile.ZipFile(os.path.join(source, "IN.zip")) as z:
                z.extract("IN.txt", source)

    build(source, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
