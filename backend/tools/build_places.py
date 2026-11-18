"""Build the offline South-India place gazetteer used by the autocomplete.

Source: GeoNames (https://download.geonames.org/export/dump/), CC BY 4.0.
The dump is filtered to the southern states/UTs, settlements and administrative
divisions only, and packed as gzip JSON so the API can suggest towns even when
Nominatim/OSM (used for POIs like bus stands) has no record of them.

Usage (from the repo root):

    python -m backend.tools.build_places --download
    python -m backend.tools.build_places --source C:\\path\\to\\geonames

The output defaults to backend/data/places_south_india.json.gz. Regenerate it
only when refreshing the gazetteer: search works offline once it exists.
"""

import argparse
import gzip
import io
import json
import os
import sys
import tempfile
import urllib.request
import zipfile

BASE = "https://download.geonames.org/export/dump/"
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "places_south_india.json.gz")

# Andhra Pradesh, Karnataka, Kerala, Tamil Nadu, Telangana, Puducherry,
# Lakshadweep, Andaman & Nicobar (the conventional South India set).
SOUTH_ADMIN1 = {"01", "02", "13", "14", "19", "22", "25", "40"}

# Populated places (P) and administrative divisions (A). Farm/abandoned/house
# rows are noise in a pickup field, and mountains/roads/hotels are left to
# Nominatim, which has the POIs this dataset lacks.
KEEP_FEATURE_CODES = {
    "PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5", "PPLC", "PPLX", "PPLL",
    "ADM1", "ADM2", "ADM3",
}

# "New" / "old" are filler at query time but part of real names here, so they
# are kept in the data; only obvious non-name alternates are dropped.
MAX_ALTERNATES = 8


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


CODE_RANK = {
    "PPLC": 9, "PPLA": 8, "PPLA2": 7, "PPLA3": 6, "PPLA4": 5, "PPLA5": 4,
    "PPL": 3, "PPLX": 2, "PPLL": 1, "ADM2": 1, "ADM3": 0, "ADM1": 0,
}


def build(source_dir, out_path):
    admin1 = read_admin1(os.path.join(source_dir, "admin1CodesASCII.txt"))
    admin2 = read_admin2(os.path.join(source_dir, "admin2Codes.txt"))

    places = {}
    with io.open(os.path.join(source_dir, "IN.txt"), encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 19 or p[8] != "IN" or p[10] not in SOUTH_ADMIN1:
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
            current = places.get(dedupe)
            if current is None:
                places[dedupe] = row
                continue
            current_key = (current[7], CODE_RANK.get(current[8], 0))
            row_key = (population, CODE_RANK.get(code, 0))
            if row_key > current_key:
                places[dedupe] = row

    rows = list(places.values())

    # Biggest settlements first: stable ordering for equal scores, and the
    # front of the list is what a bare prefix like "ka" will surface.
    rows.sort(key=lambda r: (-r[7], r[0].casefold()))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as out:
        json.dump(rows, out, ensure_ascii=False, separators=(",", ":"))
    sys.stderr.write(
        f"wrote {len(rows)} places -> {out_path} "
        f"({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)\n")


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
