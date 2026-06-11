"""One-time fetch of Bucharest sector boundaries -> static/sectoare.geojson.

Sectors are OSM admin_level=9 relations inside Municipiul Bucuresti
(admin_level=4). Queried via Overpass `out geom`, way members stitched into
closed rings here (stdlib only - this is a provenance script, not a pipeline
dependency). Output contract (ARTIFACTS.md client/sectoare.geojson):
FeatureCollection of 6 features, properties exactly
{"sector": <int 1-6>, "name": "Sectorul N"}, coords rounded to 5 dp,
file under 300 KB (point-decimation if needed). Data (c) OpenStreetMap
contributors, ODbL - attribution lives in README / metodologie.

Usage: uv run python scripts/fetch_sectoare.py [out_path]
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

QUERY = """
[out:json][timeout:120];
area["name"="București"]["admin_level"="4"]->.buc;
relation(area.buc)["boundary"="administrative"]["admin_level"="9"];
out geom;
"""

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

SIZE_LIMIT = 300_000  # bytes
BBOX = (25.9, 44.3, 26.3, 44.6)  # lon_min, lat_min, lon_max, lat_max


def fetch(query: str) -> dict:
    last_err = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(
                url, data=query.encode(), method="POST",
                headers={"User-Agent": "termo-data/1.0 (sector boundaries, one-off)"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.load(resp)
        except Exception as e:  # try next mirror
            last_err = e
            print(f"WARN {url}: {e}", file=sys.stderr)
    raise RuntimeError(f"all Overpass endpoints failed: {last_err}")


def stitch_rings(segments: list[list[tuple]]) -> list[list[tuple]]:
    """Assemble unordered way segments into closed rings.

    OSM relations list outer/inner ways in arbitrary order and direction;
    shared nodes have bit-identical coords in `out geom`, so endpoint
    equality is exact.
    """
    segs = [list(s) for s in segments if len(s) >= 2]
    rings = []
    while segs:
        ring = segs.pop()
        while ring[0] != ring[-1]:
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring.extend(s[1:]); segs.pop(i); break
                if s[-1] == ring[-1]:
                    ring.extend(reversed(s[:-1])); segs.pop(i); break
                if s[-1] == ring[0]:
                    ring[:0] = s[:-1]; segs.pop(i); break
                if s[0] == ring[0]:
                    ring[:0] = reversed(s[1:]); segs.pop(i); break
            else:
                raise ValueError("open ring: no segment continues the chain")
        rings.append(ring)
    return rings


def signed_area(ring: list[tuple]) -> float:
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:])) / 2


def orient(ring: list[tuple], ccw: bool) -> list[tuple]:
    return ring if (signed_area(ring) > 0) == ccw else ring[::-1]


def point_in_ring(pt: tuple, ring: list[tuple]) -> bool:
    x, y = pt
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def decimate(ring: list[tuple], step: int) -> list[tuple]:
    if step <= 1:
        return ring
    pts = ring[:-1][::step]
    if len(pts) < 4:
        pts = ring[:-1]
    return pts + [pts[0]]


def relation_to_geometry(rel: dict, step: int) -> dict:
    by_role: dict[str, list] = {"outer": [], "inner": []}
    for m in rel.get("members", []):
        if m.get("type") == "way" and m.get("role") in by_role and "geometry" in m:
            by_role[m["role"]].append([(p["lon"], p["lat"]) for p in m["geometry"]])
    outers = [orient(r, ccw=True) for r in stitch_rings(by_role["outer"])]
    inners = [orient(r, ccw=False) for r in stitch_rings(by_role["inner"])]
    if not outers:
        raise ValueError(f"relation {rel.get('id')}: no outer ring")

    def finish(ring):
        pts = [(round(lon, 5), round(lat, 5)) for lon, lat in decimate(ring, step)]
        out = [pts[0]]
        for p in pts[1:]:
            if p != out[-1]:
                out.append(p)
        if out[0] != out[-1]:
            out.append(out[0])
        return [[lon, lat] for lon, lat in out]

    polys = []
    for outer in outers:
        holes = [i for i in inners if point_in_ring(i[0], outer)]
        polys.append([finish(outer)] + [finish(h) for h in holes])
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def build(data: dict, step: int) -> dict:
    feats = []
    for rel in data["elements"]:
        if rel.get("type") != "relation":
            continue
        name = rel.get("tags", {}).get("name", "")
        m = re.search(r"Sector(?:ul)?\s*(\d)", name)
        if not m:
            continue
        n = int(m.group(1))
        feats.append({"type": "Feature",
                      "properties": {"sector": n, "name": f"Sectorul {n}"},
                      "geometry": relation_to_geometry(rel, step)})
    feats.sort(key=lambda f: f["properties"]["sector"])
    return {"type": "FeatureCollection", "features": feats}


def validate(fc: dict) -> None:
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 6, f"expected 6 features, got {len(fc['features'])}"
    sectors = [f["properties"]["sector"] for f in fc["features"]]
    assert sorted(sectors) == [1, 2, 3, 4, 5, 6], f"sectors: {sectors}"
    for f in fc["features"]:
        n = f["properties"]["sector"]
        assert f["properties"] == {"sector": n, "name": f"Sectorul {n}"}
        g = f["geometry"]
        rings = g["coordinates"] if g["type"] == "Polygon" else \
            [r for poly in g["coordinates"] for r in poly]
        pts = [p for ring in rings for p in ring]
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        bbox = (min(lons), min(lats), max(lons), max(lats))
        assert BBOX[0] <= bbox[0] and bbox[2] <= BBOX[2], f"sector {n} lon bbox {bbox}"
        assert BBOX[1] <= bbox[1] and bbox[3] <= BBOX[3], f"sector {n} lat bbox {bbox}"
        for ring in rings:
            assert ring[0] == ring[-1] and len(ring) >= 4, f"sector {n}: bad ring"


def main() -> None:
    out_path = Path(sys.argv[1] if len(sys.argv) > 1 else "static/sectoare.geojson")
    data = fetch(QUERY)
    for step in (1, 2, 3, 4, 6, 8, 12, 16):
        fc = build(data, step)
        blob = json.dumps(fc, ensure_ascii=False, separators=(",", ":"))
        if len(blob.encode()) <= SIZE_LIMIT:
            break
    else:
        raise RuntimeError(f"could not get under {SIZE_LIMIT} bytes")
    validate(json.loads(blob))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(blob + "\n", encoding="utf-8")
    pts = sum(len(r) for f in fc["features"]
              for r in (f["geometry"]["coordinates"] if f["geometry"]["type"] == "Polygon"
                        else [r for p in f["geometry"]["coordinates"] for r in p]))
    print(f"OK {out_path}: 6 sectors, {pts} points, "
          f"{len(blob.encode())} bytes (decimation step {step})")


if __name__ == "__main__":
    main()
