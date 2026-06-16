"""OSM street universe + anchored PT inference.

Unit-tests the normalization match (OSM full-word types -> CMTEB abbreviations)
and the publish spot-check that an OSM-only street resolves to a serving PT -
including the acceptance case Str Dambovita -> Ct Desisului.
"""

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fetch_streets import normalize  # noqa: E402

DB = Path("db/termo.db")
HARTA = Path("data/harta.html")
STREETS = Path("static/streets.json")


def test_osm_type_canon_matches_cmteb():
    # OSM full words must normalize to CMTEB's abbreviated (norm, type) so a
    # street CMTEB also named merges to one slug instead of duplicating.
    assert normalize("Strada Cetatea de Baltă") == ("cetatea de balta", "str")
    assert normalize("Splaiul Independenței") == ("independentei", "spl")
    assert normalize("Bulevardul Iuliu Maniu") == ("iuliu maniu", "bld")
    assert normalize("Calea Văcărești") == ("vacaresti", "cal")
    assert normalize("Drumul Taberei") == ("taberei", "drm")


@pytest.mark.skipif(not (DB.exists() and HARTA.exists() and STREETS.exists()),
                    reason="real db/harta/streets not present")
def test_publish_infers_pt_for_osm_only_streets(tmp_path):
    from pipeline.publish import build
    out = tmp_path / "web"
    reg = tmp_path / "slugs.json"
    # copy the committed registry so slugs stay stable
    reg.write_text(Path("registry/slugs.json").read_text())
    build(str(DB), str(reg), str(HARTA), "static", str(out))

    pt_slugs, streets = set(), {}
    with gzip.open(out / "pt" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        for line in f:
            pt_slugs.add(json.loads(line)["slug"])
    with gzip.open(out / "strazi" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            streets[o["slug"]] = o

    # acceptance case: Str Dambovita has no outage data but resolves to Ct Desisului
    dmb = streets["str-dambovita"]
    assert dmb["years"] == {}
    assert dmb["inferred_pt"] == "pt-desisului"
    assert dmb["inferred_km"] is not None and dmb["inferred_km"] < 1.0

    # every inferred PT resolves; OSM-only streets all carry an inference (or null)
    osm_only = [s for s in streets.values() if not s["years"]]
    assert len(osm_only) > 1000  # the OSM universe was merged in
    for s in osm_only:
        if s["inferred_pt"] is not None:
            assert s["inferred_pt"] in pt_slugs
            assert s["pts"] == [] and s["blocks"] == []

    # real outage streets are untouched (no inference)
    pan = streets["sos-pantelimon"]
    assert pan["years"] and pan["inferred_pt"] is None
