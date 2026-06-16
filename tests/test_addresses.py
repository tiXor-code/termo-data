"""OSM house-number addresses -> nearest-serving-PT resolution.

Unit-tests street/number normalization and the nearest-serving-PT picker, plus a
real gated spot-check that Sos Colentina's numbers resolve to DIFFERENT serving
PTs (the whole point: a long street is served by many PTs and the house number
decides which one covers a given stretch).
"""

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.parse import fold  # noqa: E402
from pipeline.publish import min_anchor_km, nearest_serving_index  # noqa: E402
from scripts.fetch_streets import normalize  # noqa: E402

DB = Path("db/termo.db")
HARTA = Path("data/harta.html")
ADDR = Path("static/addresses.json.gz")


def test_street_normalization_matches_parser():
    # OSM addr:street normalizes to the same (norm, canon_type) the street
    # universe + outage parser produce, so an address joins its street by fp.
    assert normalize("Soseaua Colentina") == ("colentina", "sos")
    assert normalize("Strada Pajurei") == ("pajurei", "str")
    assert normalize("Bulevardul Iuliu Maniu") == ("iuliu maniu", "bld")


def test_house_number_folding():
    assert fold("64A") == "64a"
    assert fold("12 Bis") == "12 bis"
    assert fold("64-66") == "64-66"


def test_min_anchor_km():
    assert min_anchor_km(44.0, 26.0, []) is None
    # the nearer of two anchors wins
    d = min_anchor_km(44.001, 26.001, [(44.5, 26.5), (44.0, 26.0)])
    assert d is not None and d < 0.2


def test_nearest_serving_index_picks_closest():
    # Two serving PTs anchored at A (index 0) and B (index 1); the house number's
    # point decides which one covers it.
    a, b = [(44.40, 26.10)], [(44.46, 26.13)]
    assert nearest_serving_index(44.401, 26.101, [a, b])[0] == 0
    assert nearest_serving_index(44.459, 26.129, [a, b])[0] == 1
    # no usable anchors -> fall back to index 0, km None
    assert nearest_serving_index(44.4, 26.1, [[], []]) == (0, None)


def test_tie_resolves_to_lower_index():
    # equal distance -> lower index (callers pass pts in slug order, so the
    # smaller slug wins), matching the block-index tie rule.
    p = (44.40, 26.10)
    assert nearest_serving_index(p[0], p[1], [[p], [p]])[0] == 0


@pytest.mark.skipif(not (DB.exists() and HARTA.exists() and ADDR.exists()),
                    reason="real db/harta/addresses not present")
def test_publish_resolves_addresses_for_colentina(tmp_path):
    from pipeline.publish import build
    out = tmp_path / "web"
    reg = tmp_path / "slugs.json"
    reg.write_text(Path("registry/slugs.json").read_text())
    build(str(DB), str(reg), str(HARTA), "static", str(out))

    streets = {}
    with gzip.open(out / "strazi" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            streets[o["slug"]] = o

    col = streets["sos-colentina"]
    addr = col.get("addr", {})
    assert len(addr) > 50          # Colentina is densely addressed in OSM
    assert len(col["pts"]) > 3     # served by many PTs (the whole point)
    chosen = set()
    kms = []
    for num, (idx, km) in addr.items():
        assert idx == -1 or 0 <= idx < len(col["pts"])
        if km is not None:
            assert km < 5.0        # sane upper bound (long street, sparse anchors)
            kms.append(km)
        chosen.add(idx)
    assert len(chosen) >= 2        # different numbers -> different serving PTs
    kms.sort()
    assert kms[len(kms) // 2] < 1.5  # the typical address resolves to a near PT

    # global invariant: every addr pt_index across all streets is valid, and a
    # -1 sentinel only appears where the street has an inferred_pt.
    for o in streets.values():
        for num, (idx, km) in o.get("addr", {}).items():
            if idx == -1:
                assert o["inferred_pt"] is not None
            else:
                assert 0 <= idx < len(o["pts"])
