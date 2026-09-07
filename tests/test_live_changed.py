"""The publish trigger must not fire while the city simply has no outages.

Regression for a bug caught in review on PR #4: `live_hash` wrapped parsing in a
broad `except Exception`, which swallowed `EmptyState`. `EmptyState` is not a
failure - parse.py raises it for CMTEB's routine "nu exista inregistrari"
banner - so the detector fell into its fail-open branch and reported
`changed=true` on EVERY scrape for as long as the outage list stayed empty,
publishing constantly at exactly the times there was nothing to publish.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "live_changed", REPO / "scripts" / "live_changed.py"
)
live_changed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live_changed)
live_hash = live_changed.live_hash

EMPTY = (
    b"<html><body><div id='ST'>"
    b"Nu exista inregistrari pentru criteriile selectate."
    b"</div></body></html>"
)


def _real_page() -> bytes:
    return (REPO / "data" / "functionare.html").read_bytes()


def test_two_empty_snapshots_hash_equal():
    """Nothing happening, twice, is not a change."""
    assert live_hash(EMPTY) == live_hash(EMPTY)


def test_empty_is_not_conflated_with_a_real_page():
    """empty -> outage must still publish."""
    assert live_hash(EMPTY) != live_hash(_real_page())


def test_identical_real_pages_hash_equal():
    page = _real_page()
    assert live_hash(page) == live_hash(page)


def test_markup_churn_does_not_change_the_hash():
    """Whitespace and attribute noise must not trigger a rebuild."""
    page = _real_page()
    noisy = page.replace(b"<tr", b"<tr ").replace(b"\n", b"\n  ")
    assert live_hash(noisy) == live_hash(page)
