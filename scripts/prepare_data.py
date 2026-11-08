#!/usr/bin/env python3
"""
Turn the VPD GeoDASH crime CSV into a small data.json that the website loads.
Version 3: keeps every year in the data, so the website can show one year or all years,
and stores the points as one list per field, which makes the file much smaller to download.

Usage (run from the repository root):
    python scripts/prepare_data.py                       # uses every .csv in data/raw/ (subfolders too)
    python scripts/prepare_data.py data/raw/myfile.csv   # or point to specific files
    python scripts/prepare_data.py --from-year 2016      # skip years before 2016
    python scripts/prepare_data.py --out web/data.json

What it does:
  1. Reads the VPD CSV (columns: TYPE, YEAR, MONTH, DAY, HOUR, MINUTE,
     HUNDRED_BLOCK, NEIGHBOURHOOD, X, Y)
  2. Skips blank rows, drops rows with no location (VPD sets X and Y to 0 or
     leaves them empty for privacy) and rows that fall outside Vancouver; offences
     against a person among those are still counted by neighbourhood and year
  3. Converts X/Y from UTM zone 10N metres to latitude/longitude
  4. Keeps the year and date of every incident, so the website can filter by year
     and compare a part-year (like 2026 so far) with the same months of the year before
  5. Renames VPD crime types to plain-language labels
  6. Writes web/data.json and prints a summary you can use to check the app
     (points sorted by place and stored column by column, see columns() below)

Only the Python standard library is needed.
"""

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_OUT = REPO_ROOT / "web" / "data.json"

# VPD type name -> label shown in the app. Unknown types keep their VPD name.
TYPE_LABELS = {
    "Theft of Bicycle": "Bike theft",
    "Theft from Vehicle": "Theft from cars",
    "Theft of Vehicle": "Car theft",
    "Break and Enter Residential/Other": "Home break-ins",
    "Break and Enter Commercial": "Business break-ins",
    "Other Theft": "Other theft",
    "Mischief": "Mischief and vandalism",
    "Offence Against a Person": "Offences against a person",
    "Homicide": "Homicide",
    "Vehicle Collision or Pedestrian Struck (with Injury)": "Traffic collisions with injury",
    "Vehicle Collision or Pedestrian Struck (with Fatality)": "Fatal traffic collisions",
}

# VPD randomizes these locations across several blocks, so the app flags them as area-level only.
BLURRED_TYPES = {"Offence Against a Person", "Homicide"}


def plain(text):
    """VPD's type and neighbourhood names end up in the page's HTML, so refuse anything that could become markup."""
    if any(c in text for c in "<>\"'`"):
        sys.exit(f"Unexpected characters in VPD text, not publishing: {text!r}")
    return text


# Rough box around the City of Vancouver; anything outside is treated as bad data.
LAT_RANGE = (49.19, 49.32)
LNG_RANGE = (-123.28, -123.00)



# --- UTM zone 10N (NAD83 / GRS80) to latitude/longitude ----------------------
# Standard inverse transverse Mercator series (USGS, Snyder 1987).
# Accurate to well under a metre for Vancouver, far finer than VPD's location offsets.
_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_LON0 = math.radians(-123.0)  # central meridian of zone 10
_E1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))


def utm10n_to_latlng(easting, northing):
    x = easting - 500000.0
    m = northing / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    phi1 = (
        mu
        + (3 * _E1 / 2 - 27 * _E1**3 / 32) * math.sin(2 * mu)
        + (21 * _E1**2 / 16 - 55 * _E1**4 / 32) * math.sin(4 * mu)
        + (151 * _E1**3 / 96) * math.sin(6 * mu)
        + (1097 * _E1**4 / 512) * math.sin(8 * mu)
    )
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = _EP2 * cos1**2
    t1 = tan1**2
    n1 = _A / math.sqrt(1 - _E2 * sin1**2)
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1**2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _EP2 - 3 * c1**2) * d**6 / 720
    )
    lng = _LON0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _EP2 + 24 * t1**2) * d**5 / 120
    ) / cos1
    return math.degrees(lat), math.degrees(lng)


# --- Helpers ------------------------------------------------------------------
def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    f = to_float(value)
    return None if f is None else int(f)


def row_date(row):
    y, m, d = to_int(row.get("YEAR")), to_int(row.get("MONTH")), to_int(row.get("DAY"))
    if not (y and m and d):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def read_rows(paths):
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            reader.fieldnames = [name.strip().upper() for name in reader.fieldnames]
            missing = {"TYPE", "YEAR", "MONTH", "DAY", "X", "Y"} - set(reader.fieldnames)
            if missing:
                sys.exit(f"{path.name} is missing expected columns: {', '.join(sorted(missing))}")
            for row in reader:
                yield path, row


def find_default_csvs():
    csvs = sorted(DEFAULT_RAW_DIR.rglob("*.csv"))
    if not csvs:
        sys.exit(
            f"No CSV found in {DEFAULT_RAW_DIR}.\n"
            "Download the crime data from https://geodash.vpd.ca/opendata/ "
            "and unzip it into data/raw/."
        )
    return csvs


def columns(points):
    """The points as one list per field, which compresses far better than one list per point.

    Latitude and longitude become whole numbers of 1/10,000 degree (the same 4 decimals as before)
    counted from the south-west corner of LAT_RANGE/LNG_RANGE, so they're short and never negative.
    Rows are sorted by place first: neighbouring rows then share most of their digits, and that
    roughly halves the compressed size. The app doesn't depend on the order of the points.
    """
    lat0, lng0 = round(LAT_RANGE[0] * 1e4), round(LNG_RANGE[0] * 1e4)
    points = sorted(points)
    return {
        "lat0": lat0,   # in 1/10,000 degree: a point's latitude is (lat0 + lat[i]) / 10000
        "lng0": lng0,
        "lat": [round(p[0] * 1e4) - lat0 for p in points],
        "lng": [round(p[1] * 1e4) - lng0 for p in points],
        "type": [p[2] for p in points],
        "hour": [p[3] for p in points],
        "year": [p[4] for p in points],
        "md": [p[5] for p in points],
    }


# --- Main ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="*", help="VPD crime CSV file(s) (default: every .csv in data/raw/)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Where to write data.json (default: web/data.json)")
    parser.add_argument("--from-year", type=int, help="Skip years before this one, e.g. 2016")
    parser.add_argument("--to-year", type=int, help="Skip years after this one")
    args = parser.parse_args()

    src = [Path(p) for p in args.csv] if args.csv else find_default_csvs()
    out = Path(args.out)
    print("Reading:")
    for path in src:
        print(f"  {path}")

    def wanted(d):
        return (args.from_year is None or d.year >= args.from_year) and (args.to_year is None or d.year <= args.to_year)

    stats = Counter()
    type_index = {}
    types = []
    points = []
    hood_sums = defaultdict(lambda: [0.0, 0.0, 0])
    by_year = Counter()
    by_type = Counter()
    no_location_by_year = Counter()
    violent_by_hood = defaultdict(Counter)   # neighbourhood -> year -> offences against a person (no map location)
    years_in_file = defaultdict(set)
    latest = None

    for path, row in read_rows(src):
        if not any((v or "").strip() for v in row.values()):
            stats["skipped: blank row"] += 1
            continue
        stats["rows read"] += 1
        d = row_date(row)
        if d is None:
            stats["skipped: bad date"] += 1
            continue
        years_in_file[d.year].add(path.name)
        if not wanted(d):
            stats["skipped: outside the chosen years"] += 1
            continue
        if latest is None or d > latest:
            latest = d

        vpd_type = plain((row.get("TYPE") or "").strip() or "Unknown")
        x, y = to_float(row.get("X")), to_float(row.get("Y"))
        if not x or not y:
            stats["skipped: no location (privacy)"] += 1
            no_location_by_year[d.year] += 1
            hood = plain((row.get("NEIGHBOURHOOD") or "").strip())
            if vpd_type in BLURRED_TYPES and hood:
                violent_by_hood[hood][d.year] += 1
            continue

        lat, lng = utm10n_to_latlng(x, y)
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
            stats["skipped: outside Vancouver"] += 1
            continue

        if vpd_type not in type_index:
            type_index[vpd_type] = len(types)
            types.append(vpd_type)
        hour = to_int(row.get("HOUR"))
        hour = hour if hour is not None and 0 <= hour <= 23 else -1

        # 4 decimals is about 10 m, finer than VPD's hundred-block offsets, and keeps the file small
        points.append([round(lat, 4), round(lng, 4), type_index[vpd_type], hour, d.year, d.month * 100 + d.day])
        stats["kept: shown on the map"] += 1
        by_year[d.year] += 1
        by_type[vpd_type] += 1

        hood = plain((row.get("NEIGHBOURHOOD") or "").strip())
        if hood:
            s = hood_sums[hood]
            s[0] += lat
            s[1] += lng
            s[2] += 1

    if latest is None:
        sys.exit("Couldn't find any incidents with valid dates in the chosen years.")

    # Same year in more than one file means incidents would be counted twice
    doubled = {yr: files for yr, files in years_in_file.items() if len(files) > 1}
    if doubled:
        print("\nWarning: these years appear in more than one file, so incidents may be counted twice:")
        for yr in sorted(doubled):
            print(f"  {yr}: {', '.join(sorted(doubled[yr]))}")
        print("Keep only one copy of each year in data/raw/ and run this again.")

    years = sorted(by_year)
    missing = [yr for yr in range(years[0], years[-1] + 1) if yr not in by_year]
    if missing:
        print(f"\nWarning: no incidents at all for {', '.join(map(str, missing))}. "
              "That year is probably missing from the CSV. Download it from GeoDASH, put it in data/raw/ and run this again.")
    neighbourhoods = sorted(
        ({"name": name, "lat": round(s[0] / s[2], 5), "lng": round(s[1] / s[2], 5)} for name, s in hood_sums.items()),
        key=lambda h: h["name"],
    )

    data = {
        "version": 3,
        "source": "Vancouver Police Department, GeoDASH Open Data (https://geodash.vpd.ca/opendata/)",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "latestDate": latest.isoformat(),
        "years": years,
        # The latest year is only partly covered unless the data runs to December 31
        "partialYear": latest.year if (latest.month, latest.day) != (12, 31) else None,
        "noLocationByYear": {str(yr): no_location_by_year[yr] for yr in years},
        # Offences against a person (and homicides) have no map location, but VPD still names their neighbourhood,
        # so the app can count them for the neighbourhood a clicked spot is in
        "violentByHood": {h: {str(yr): n for yr, n in sorted(c.items())} for h, c in sorted(violent_by_hood.items())},
        "types": [
            {"label": TYPE_LABELS.get(t, t), "vpd": t, "blurred": t in BLURRED_TYPES} for t in types
        ],
        "neighbourhoods": neighbourhoods,
        # One list per field; point i is lat[i], lng[i], type index, hour (-1 if unknown), year,
        # month*100 + day (e.g. 1002 = Oct 2). See columns() above.
        "points": columns(points),
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))

    # Summary: use these numbers to check the app shows the same totals.
    print(f"\nYears in the data: {years[0]} to {years[-1]}, latest incident {latest.isoformat()}")
    if data["partialYear"]:
        print(f"{data['partialYear']} is a part year (January 1 to {latest.strftime('%B %d').replace(' 0', ' ')})\n")
    for key in sorted(stats):
        print(f"  {key:<40} {stats[key]:>9,}")
    print("\nIncidents on the map, by year:")
    for yr in years:
        print(f"  {yr:<40} {by_year[yr]:>9,}")
    print("\nIncidents on the map (all years), by type:")
    for t, n in by_type.most_common():
        print(f"  {TYPE_LABELS.get(t, t):<40} {n:>9,}")
    size_mb = out.stat().st_size / 1_000_000
    print(f"\nWrote {out} ({size_mb:.1f} MB, {len(points):,} points)")
    if size_mb > 50:
        print("That's big for GitHub. Use --from-year to drop older years if the page loads slowly.")


if __name__ == "__main__":
    main()
