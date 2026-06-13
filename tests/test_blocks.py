"""Block tokenizer unit tests + a publish spot-check that the block->PT index
is populated and resolvable (powers the site's "find your block" finder)."""

import gzip
import json
from pathlib import Path

import pytest

from pipeline.publish import tokenize_blocks

DB = Path("db/termo.db")
HARTA = Path("data/harta.html")


def test_tokenize_basic():
    assert tokenize_blocks("bl. 23, 24, 25") == ["bl. 23", "bl. 24", "bl. 25"]


def test_tokenize_keeps_ranges_and_codes():
    assert tokenize_blocks("bl. 1-15, D30, 5 sc.F") == ["bl. 1-15", "bl. D30", "bl. 5 sc.F"]


def test_tokenize_marker_variants():
    assert tokenize_blocks("imobil 39A, 20") == ["imobil 39A", "imobil 20"]
    assert tokenize_blocks("nr. 7, 9") == ["nr. 7", "nr. 9"]


def test_tokenize_dedups_and_drops_junk():
    assert tokenize_blocks("bl. 5, 5, , -") == ["bl. 5"]
    assert tokenize_blocks("") == []
    assert tokenize_blocks(None) == []


def test_no_marker_defaults_to_bl():
    assert tokenize_blocks("12, 14") == ["bl. 12", "bl. 14"]


@pytest.mark.skipif(not DB.exists() or not HARTA.exists(),
                    reason="real db/harta not present")
def test_publish_blocks_populated_and_resolvable(tmp_path):
    from pipeline.publish import build
    out = tmp_path / "web"
    reg = tmp_path / "slugs.json"
    build(str(DB), str(reg), str(HARTA), "static", str(out))

    pt_slugs, streets = set(), {}
    with gzip.open(out / "pt" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        for line in f:
            pt_slugs.add(json.loads(line)["slug"])
    with gzip.open(out / "strazi" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            streets[o["slug"]] = o

    # a known multi-PT artery has block detail mapping to multiple PTs
    pan = streets["sos-pantelimon"]
    assert len(pan["blocks"]) > 10
    assert len({b["pt"] for b in pan["blocks"]}) > 1

    # every published block PT resolves to a real PT slug, across all streets
    for s in streets.values():
        for b in s.get("blocks", []):
            assert set(b) == {"label", "pt"}
            assert b["pt"] in pt_slugs
