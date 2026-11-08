#!/usr/bin/env python3
"""
Download the City of Vancouver's local-area boundaries and write web/boundaries.json.

The app uses them to tell which neighbourhood a clicked spot is in, so it can show that
neighbourhood's offences against a person (VPD gives those no map location, only a neighbourhood).
The boundaries rarely change, so this is run by hand, not by the weekly data update:

    python scripts/boundaries.py

Source: City of Vancouver Open Data, "Local area boundary", under the Open Government
Licence - Vancouver (https://opendata.vancouver.ca/pages/licence/).
Only the Python standard library is needed.
"""

import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from prepare_data import plain  # noqa: E402  (refuses names that could become page markup)

OUT = REPO_ROOT / "web" / "boundaries.json"
URL = "https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/local-area-boundary/exports/geojson"
# The City and VPD name one area differently; the app shows VPD's names, since VPD's counts use them.
# VPD's other two neighbourhoods have no City area: Stanley Park lies outside every City area, and
# Musqueam sits inside Dunbar-Southlands (the app tells those two apart, see hoodAt in app.js).
CITY_TO_VPD = {"Downtown": "Central Business District"}


def rounded(ring):
    # 5 decimals is about 1 m, far finer than VPD's hundred-block offsets
    return [[round(lng, 5), round(lat, 5)] for lng, lat, *_ in ring]


def convert(city):
    features = []
    for f in city["features"]:
        geom = f["geometry"]
        polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        name = plain(f["properties"]["name"].strip())
        features.append({
            "type": "Feature",
            "properties": {"name": CITY_TO_VPD.get(name, name)},
            "geometry": {"type": "MultiPolygon", "coordinates": [[rounded(r) for r in p] for p in polygons]},
        })
    features.sort(key=lambda f: f["properties"]["name"])
    return {
        "type": "FeatureCollection",
        "source": "City of Vancouver Open Data, Local area boundary (Open Government Licence - Vancouver)",
        "features": features,
    }


def main():
    request = urllib.request.Request(URL, headers={"User-Agent": "VanScene boundaries (+https://github.com/YazidNasser/VanScene)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        city = json.load(response)
    data = convert(city)
    if len(data["features"]) < 20:
        sys.exit(f"Only {len(data['features'])} areas in the download; expected 22. web/boundaries.json is unchanged.")
    OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT} ({len(data['features'])} areas, {OUT.stat().st_size / 1000:.0f} KB)")


if __name__ == "__main__":
    main()
