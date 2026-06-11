"""Slug rules + registry stability/collision semantics."""

import json

import pytest

from pipeline.slugs import SlugRegistry, slugify


def test_slugify_diacritics():
    assert slugify("Șoseaua Olteniței") == "soseaua-oltenitei"


def test_slugify_punctuation():
    assert slugify("B-dul 1 Mai") == "b-dul-1-mai"
    assert slugify("  --a,, b!! ") == "a-b"
    assert slugify("...") == ""


def test_ensure_idempotent_and_file_stable(tmp_path):
    p = tmp_path / "slugs.json"
    reg = SlugRegistry.load(p)
    assert reg.ensure_pt("modul toporasi") == "pt-modul-toporasi"
    assert reg.ensure_street("sos", "pantelimon", [2, 3]) == "sos-pantelimon"
    assert reg.ensure_street("", "13 septembrie", [5]) == "13-septembrie"
    assert reg.save(p) is True
    first_bytes = p.read_bytes()

    reg2 = SlugRegistry.load(p)
    assert reg2.ensure_pt("modul toporasi") == "pt-modul-toporasi"
    assert reg2.ensure_street("sos", "pantelimon", [3]) == "sos-pantelimon"
    assert reg2.save(p) is False  # nothing new -> not dirty, file untouched
    assert p.read_bytes() == first_bytes


def test_street_collision_sector_then_numeric(tmp_path):
    reg = SlugRegistry()
    assert reg.ensure_street("str", "x y", [3]) == "str-x-y"
    # different identity, same base -> -sector-<min>
    assert reg.ensure_street("", "str x y", [2, 4]) == "str-x-y-sector-2"
    # third identity colliding when the -sector candidate is taken too -> -2
    assert reg.ensure_street("str", "x. y", [2]) == "str-x-y-2"


def test_pt_collision_numeric(tmp_path):
    reg = SlugRegistry()
    assert reg.ensure_pt("turn a") == "pt-turn-a"
    assert reg.ensure_pt("turn. a") == "pt-turn-a-2"
    assert reg.ensure_pt("turn, a") == "pt-turn-a-3"


def test_load_raises_on_duplicate_slug(tmp_path):
    p = tmp_path / "slugs.json"
    p.write_text(json.dumps({"version": 1, "pt": {"a": "pt-x", "b": "pt-x"},
                             "street": {}}))
    with pytest.raises(ValueError):
        SlugRegistry.load(p)


def test_registered_value_wins_over_slugify(tmp_path):
    """Committed slugs survive future slugify-rule changes."""
    p = tmp_path / "slugs.json"
    p.write_text(json.dumps({"version": 1, "pt": {"weird": "pt-old-style"},
                             "street": {"sos|x": "legacy-x"}}))
    reg = SlugRegistry.load(p)
    assert reg.ensure_pt("weird") == "pt-old-style"
    assert reg.ensure_street("sos", "x", [1]) == "legacy-x"
    assert reg.save(p) is False
