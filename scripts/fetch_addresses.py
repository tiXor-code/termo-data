"""One-time / occasional fetch of Bucharest house-number points -> static/addresses.json.gz.

CMTEB publishes no house numbers, and there is no authoritative address->PT map.
But a long street (e.g. Sos Colentina) is served by MANY puncte termice and the
house NUMBER decides which one covers a given stretch. This pulls OSM address
points (addr:housenumber + addr:street) so publish.py can resolve each number to
the nearest serving PT among the street's known serving PTs (a labeled estimate).

Streets are normalized with the SAME logic as the outage parser + the street
universe (scripts.fetch_streets.normalize), so an address joins to a published
street by fingerprint. The house number is folded but kept as a string ("64",
"64a", "64-66", "12 bis"). Coverage is partial (OSM-derived).

Output: addresses.json.gz = [{street_norm, type, number, sector, lat, lon}].
Data (c) OpenStreetMap contributors, ODbL - attribution in README / metodologie.

Usage:
  uv run python scripts/fetch_addresses.py --counts        # coverage gate, no write
  uv run python scripts/fetch_addresses.py [out_path]      # default static/addresses.json.gz
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import pipeline

from pipeline.parse import fold  # noqa: E402
from pipeline.publish import load_sector_polygons, sector_of_point, street_fp  # noqa: E402
from scripts.fetch_streets import fetch, normalize  # noqa: E402

QUERY = """
[out:json][timeout:300];
area["name"="București"]["admin_level"="4"]->.buc;
( nwr(area.buc)["addr:housenumber"]["addr:street"]; );
out center;
"""


def _coord(el: dict) -> tuple[float, float] | None:
    if el.get("type") == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        c = el.get("center")
        lat, lon = (c.get("lat"), c.get("lon")) if c else (None, None)
    if lat is None or lon is None:
        return None
    return (lat, lon)


def _rows(elements: list[dict], polys) -> tuple[list[dict], int]:
    """Returns (rows, unmatched) where unmatched = addresses whose street_norm
    did not normalize (junk / un-parseable street tag)."""
    rows: list[dict] = []
    unmatched = 0
    seen: set[tuple] = set()
    for el in elements:
        tags = el.get("tags", {})
        street = tags.get("addr:street")
        number = tags.get("addr:housenumber")
        coord = _coord(el)
        if not street or not number or coord is None:
            continue
        key = normalize(street)
        if not key or not key[0]:
            unmatched += 1
            continue
        norm, stype = key
        num = fold(number)
        if not num:
            continue
        lat, lon = coord
        sector = sector_of_point(polys, lat, lon)
        ident = (stype, norm, num, sector)
        if ident in seen:
            continue
        seen.add(ident)
        rows.append({
            "street_norm": norm, "type": stype, "number": num,
            "sector": sector, "lat": round(lat, 6), "lon": round(lon, 6),
        })
    return rows, unmatched


def main(argv: list[str]) -> None:
    counts_only = "--counts" in argv
    out_path = next((a for a in argv if not a.startswith("--")), "static/addresses.json.gz")

    polys = load_sector_polygons(Path("static/sectoare.geojson"))
    data = fetch(QUERY)
    elements = data.get("elements", [])
    rows, unmatched = _rows(elements, polys)

    by_sector: Counter = Counter(r["sector"] for r in rows)
    total = len(rows)
    print("OSM addr:housenumber+addr:street coverage (deduped rows):")
    for s in range(1, 7):
        print(f"  sector {s}: {by_sector.get(s, 0):>7,}")
    print(f"  no-sector: {by_sector.get(None, 0):>7,}")
    print(f"  TOTAL:     {total:>7,}")
    pct = (unmatched / (total + unmatched) * 100) if (total + unmatched) else 0
    print(f"  unmatched street tag: {unmatched:,} ({pct:.1f}% of address tags)")

    if counts_only:
        print("--counts: not writing the file.")
        return

    rows.sort(key=lambda r: (r["type"], r["street_norm"], r["number"]))
    payload = json.dumps(rows, ensure_ascii=False) + "\n"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if out_path.endswith(".gz"):
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            f.write(payload)
    else:
        Path(out_path).write_text(payload, encoding="utf-8")
    print(f"wrote {total} addresses to {out_path}")
    # sanity: the hero street (match by fingerprint - OSM tags Colentina as the
    # leaky "Soseaua Colentina", which publish.py joins via street_fp anyway).
    col = [r for r in rows if street_fp(r["type"], r["street_norm"]) == ("sos", "colentina")]
    print(f"sos colentina addresses: {len(col)} (sample: {sorted({r['number'] for r in col})[:8]})")


if __name__ == "__main__":
    main(sys.argv[1:])
