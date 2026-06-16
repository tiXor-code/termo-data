"""One-time / occasional fetch of ALL named Bucharest streets -> static/streets.json.

CMTEB only names a subset of the streets each punct termic serves, so the
outage data alone can't answer "is street X bad?" for an un-named street. This
pulls the full named-street universe from OpenStreetMap (Overpass) so every
street is searchable; publish.py then infers a serving PT geographically.

Streets are normalized with the SAME logic as the outage parser
(parse_street_line) and the OSM full-word type word ("Strada", "Splaiul") is
canonicalized to CMTEB's abbreviation ("str", "spl") so an OSM street that
CMTEB also named merges to the same slug instead of duplicating.

Output: [{name, type, norm, sector, lat, lon}] (one per (norm,type,sector)).
Data (c) OpenStreetMap contributors, ODbL - attribution in README / metodologie.

Usage: uv run python scripts/fetch_streets.py [out_path]   (default static/streets.json)
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import pipeline

from pipeline.parse import parse_street_line  # noqa: E402
from pipeline.publish import load_sector_polygons, sector_of_point  # noqa: E402

QUERY = """
[out:json][timeout:240];
area["name"="București"]["admin_level"="4"]->.buc;
way(area.buc)["highway"]["name"];
out geom;
"""

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# highway= values that are not addressable streets
SKIP_HIGHWAY = {
    "footway", "path", "steps", "cycleway", "bridleway", "track", "service",
    "bus_guideway", "platform", "construction", "proposed", "corridor",
    "raceway", "escape", "rest_area", "services", "elevator",
}

# OSM full-word street types -> CMTEB abbreviation (what's already in the db).
# parse_street_line folds the prefix; we re-map full words to the short form.
TYPE_CANON = {
    "strada": "str", "bulevardul": "bld", "b-dul": "bld", "bdul": "bld",
    "soseaua": "sos", "calea": "cal", "splaiul": "spl", "intrarea": "intr",
    "aleea": "al", "drumul": "drm", "piata": "pta", "prelungirea": "prel",
}


def fetch(query: str) -> dict:
    last_err = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(
                url, data=query.encode(), method="POST",
                headers={"User-Agent": "termo-data/1.0 (street universe, one-off)"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001 - try next mirror
            last_err = e
            print(f"WARN {url}: {e}", file=sys.stderr)
    raise RuntimeError(f"all Overpass endpoints failed: {last_err}")


def normalize(name: str) -> tuple[str, str] | None:
    """OSM display name -> (street_norm, canon_type), matching the outage parser."""
    parsed = parse_street_line(name)  # reuses STREET_TYPE_RE + fold
    if not parsed:
        return None
    norm, stype, _ = parsed
    return norm, TYPE_CANON.get(stype, stype)


def display_name(stype: str, norm: str) -> str:
    # mirror publish.street_name's TYPE_DISPLAY for consistency; lightweight here
    disp = {"str": "Str", "bld": "Bld", "cal": "Calea", "spl": "Splaiul",
            "drm": "Drumul", "sos": "Sos", "intr": "Intr", "al": "Aleea",
            "pta": "Piata", "prel": "Prel", "": ""}.get(stype, stype.capitalize())
    return f"{disp} {norm.title()}".strip()


def main(out_path: str = "static/streets.json") -> None:
    polys = load_sector_polygons(Path("static/sectoare.geojson"))
    data = fetch(QUERY)
    # gather geometry points per (norm, type)
    pts: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        if el.get("tags", {}).get("highway") in SKIP_HIGHWAY:
            continue
        name = el.get("tags", {}).get("name")
        geom = el.get("geometry")
        if not name or not geom:
            continue
        key = normalize(name)
        if not key or not key[0]:
            continue
        bag = pts.setdefault(key, [])
        for g in geom:
            bag.append((g["lat"], g["lon"]))

    out = []
    seen = set()
    for (norm, stype), coords in pts.items():
        lat = sum(c[0] for c in coords) / len(coords)
        lon = sum(c[1] for c in coords) / len(coords)
        sector = sector_of_point(polys, lat, lon)
        ident = (norm, stype, sector)
        if ident in seen:
            continue
        seen.add(ident)
        out.append({
            "name": display_name(stype, norm), "type": stype, "norm": norm,
            "sector": sector, "lat": round(lat, 6), "lon": round(lon, 6),
        })

    out.sort(key=lambda s: (s["type"], s["norm"]))
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False) + "\n")
    in_sector = sum(1 for s in out if s["sector"])
    print(f"wrote {len(out)} streets to {out_path} "
          f"({in_sector} sector-assigned, {len(out) - in_sector} outside polygons)")
    # sanity: the acceptance street
    dmb = [s for s in out if s["norm"] == "dambovita"]
    print("dambovita entries:", dmb)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "static/streets.json")
