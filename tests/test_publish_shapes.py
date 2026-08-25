"""Publish schema spot-checks against the REAL db (frozen phase-1 truth).

Skipped when db/termo.db has not been built. The 2025 numbers are locked by
the phase-1 validation report and must survive the dual-source rebuild
(the cutover only touches the June-2026 tail).
"""

import gzip
import json
from pathlib import Path

import pytest

from pipeline import validate

DB = Path("db/termo.db")
HARTA = Path("data/harta.html")

pytestmark = pytest.mark.skipif(not DB.exists() or not HARTA.exists(),
                                reason="real db/harta not present")

# Imported rather than redeclared: pipeline/validate.py enforces these same key
# sets on CI (where this file is skipped for lack of a db), so two copies would
# be free to drift apart silently.
PT_KEYS = set(validate.ARTIFACT_KEYS_PT_RANK)
ST_KEYS = set(validate.ARTIFACT_KEYS_ST_RANK)


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    from pipeline.publish import build
    out = tmp_path_factory.mktemp("web")
    reg = tmp_path_factory.mktemp("reg") / "slugs.json"
    build(str(DB), str(reg), str(HARTA), "static", str(out))
    return out


def _load(web, rel):
    return json.loads((web / rel).read_text(encoding="utf-8"))


def _ndjson(web, rel):
    with gzip.open(web / rel, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_meta(web):
    meta = _load(web, "meta.json")
    assert meta["universe_size"] == 947
    assert meta["years"][0] == 2021
    assert meta["last_complete_year"] == max(
        y for y in meta["years"] if y not in meta["partial_years"])
    assert 2021 in meta["partial_years"]
    assert meta["sources_cutover_utc"] == "2026-06-10T19:34:00Z"
    for y in meta["years"]:
        cov = meta["coverage"][str(y)]
        assert set(cov) == {"snapshots", "missing_days", "gap_hours_max"}


def test_summary_2025_frozen_truth(web):
    row = next(r for r in _load(web, "city/summary.json") if r["year"] == 2025)
    assert row["partial"] is False
    assert row["median_pt_days"] == 22
    assert row["mean_pt_days"] == 27.3
    assert row["pts_hit"] == 905
    assert row["share_universe_hit_pct"] == 91.8
    assert row["episodes"] == 11325
    assert row["episodes_avarie"] == 7881
    assert row["episodes_programat"] == 3114
    assert len(row["monthly_pt_days"]) == 12


def test_rankings_pt_2025(web):
    rows = _load(web, "rankings/pt-2025.json")
    days = [r["days"] for r in rows]
    assert days == sorted(days, reverse=True)
    top = rows[0]
    assert top["slug"] == "pt-modul-toporasi"
    assert top["days"] == 179
    assert top["sector"] == 5
    assert top["days_avarie"] == 1
    assert top["days_programat"] == 176
    assert top["est_day_eq"] == 176.1
    for r in rows:
        assert set(r) == PT_KEYS


def test_delta_prev_semantics(web):
    meta = _load(web, "meta.json")
    rows_2022 = _load(web, "rankings/pt-2022.json")
    assert all(r["delta_prev"] is None for r in rows_2022)  # 2021 is partial
    cur = max(meta["years"])
    if cur in meta["partial_years"]:
        rows_cur = _load(web, f"rankings/pt-{cur}.json")
        assert any(r["delta_prev"] is not None for r in rows_cur)  # YTD deltas


def test_distribution_2025(web):
    dist = _load(web, "city/distribution-2025.json")
    assert set(dist["percentiles"]) == {"p10", "p25", "p50", "p75", "p90", "p99"}
    assert dist["percentiles"]["p50"] == 22
    assert sum(c for _, c in dist["histogram"]) == 947
    starts = [b for b, _ in dist["histogram"]]
    assert starts == list(range(0, starts[-1] + 1, 5))


def test_pt_ndjson(web):
    lines = _ndjson(web, "pt/all.ndjson.gz")
    assert len(lines) >= 1050
    slugs = [l["slug"] for l in lines]
    assert len(slugs) == len(set(slugs))
    for l in lines:
        assert {"slug", "name", "sector", "lat", "lon", "on_map",
                "blocks_estimate", "streets", "nearest", "years"} <= set(l)
        if l["on_map"]:
            assert l["lat"] is not None and l["lon"] is not None
        else:
            assert l["nearest"] == []
    toporasi = next(l for l in lines if l["slug"] == "pt-modul-toporasi")
    y2025 = toporasi["years"]["2025"]
    assert y2025["days"] == 179
    assert y2025["runs"] and all(len(r) == 3 for r in y2025["runs"])
    assert y2025["episodes"][0]["start"] < y2025["episodes"][-1]["start"] or \
        len(y2025["episodes"]) == 1


def test_strazi_ndjson(web):
    lines = _ndjson(web, "strazi/all.ndjson.gz")
    assert len(lines) >= 1100
    for l in lines:
        assert {"slug", "name", "type", "sectors", "pts", "neighbors", "years"} <= set(l)
        assert len(l["neighbors"]) <= 8


def test_slug_namespaces_disjoint(web):
    pt = {l["slug"] for l in _ndjson(web, "pt/all.ndjson.gz")}
    st = {l["slug"] for l in _ndjson(web, "strazi/all.ndjson.gz")}
    assert not (pt & st)


def test_rankings_strazi_shape(web):
    rows = _load(web, "rankings/strazi-2025.json")
    assert rows
    for r in rows:
        assert set(r) == ST_KEYS
    days = [r["days"] for r in rows]
    assert days == sorted(days, reverse=True)


def test_search_index(web):
    entries = _load(web, "client/search-index.json")
    assert len(entries) >= 2100
    slugs = [e["s"] for e in entries]
    assert len(slugs) == len(set(slugs))
    for e in entries:
        assert set(e) == {"t", "n", "s", "sec", "d"}
        assert e["t"] in ("pt", "st")


def test_map_geojson(web):
    geo = _load(web, "client/map/pt-2025.geojson")
    assert geo["type"] == "FeatureCollection"
    assert len(geo["features"]) >= 900
    for f in geo["features"][:50]:
        assert f["geometry"]["type"] == "Point"
        assert len(f["geometry"]["coordinates"]) == 2
        assert {"slug", "name", "sector", "days", "days_avarie",
                "days_programat", "episodes"} == set(f["properties"])


def test_og_stats(web):
    og = _load(web, "og/stats.json")
    pt = {l["slug"] for l in _ndjson(web, "pt/all.ndjson.gz")}
    st = {l["slug"] for l in _ndjson(web, "strazi/all.ndjson.gz")}
    assert set(og) == pt | st
    for v in og.values():
        assert len(v) == 5 and v[0] in ("pt", "st")
