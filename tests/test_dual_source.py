"""Dual-source seam: cutover filtering, ordering, keepalive invisibility.

Builds two tiny real git repos so iter_dual exercises the same `git log` +
`cat-file --batch` path as production.
"""

import os
import subprocess
from datetime import datetime, timedelta

import pytest

from pipeline.backfill import CUTOVER_UTC, FILE_ARCHIVE, FILE_OWN, iter_dual

T = CUTOVER_UTC


def _git(repo, *args, ts=None):
    env = dict(os.environ)
    if ts is not None:
        env["GIT_AUTHOR_DATE"] = ts.isoformat()
        env["GIT_COMMITTER_DATE"] = ts.isoformat()
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, env=env)


def _repo(path, commits):
    """commits: list of (datetime, {relpath: content})."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    for ts, files in commits:
        for rel, content in files.items():
            p = path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "snapshot", ts=ts)
    return path


def test_order_cutover_and_keepalive(tmp_path):
    archive = _repo(tmp_path / "archive", [
        (T - timedelta(hours=2), {FILE_ARCHIVE: "a1"}),
        (T - timedelta(hours=1), {FILE_ARCHIVE: "a2"}),
        (T + timedelta(hours=1), {FILE_ARCHIVE: "a3"}),  # archive keeps scraping
    ])
    own = _repo(tmp_path / "own", [
        (T + timedelta(seconds=30), {FILE_OWN: "o1"}),
        (T + timedelta(minutes=90), {"data/meta.json": "keepalive"}),
        (T + timedelta(hours=2), {FILE_OWN: "o2"}),
    ])
    got = list(iter_dual(archive, own))
    assert [b.decode() for _, _, b in got] == ["a1", "a2", "o1", "o2"]
    times = [datetime.fromisoformat(ts) for _, ts, _ in got]
    assert times == [T - timedelta(hours=2), T - timedelta(hours=1),
                     T + timedelta(seconds=30), T + timedelta(hours=2)]


def test_pre_cutover_own_commits_skipped(tmp_path):
    archive = _repo(tmp_path / "archive", [
        (T - timedelta(hours=1), {FILE_ARCHIVE: "a1"}),
    ])
    own = _repo(tmp_path / "own", [
        (T - timedelta(minutes=10), {FILE_OWN: "early"}),  # none expected, dropped
        (T + timedelta(minutes=10), {FILE_OWN: "o1"}),
    ])
    got = [b.decode() for _, _, b in iter_dual(archive, own)]
    assert got == ["a1", "o1"]


def test_seam_assert_raises_on_non_monotonic_history(tmp_path):
    """Author dates can go backwards in commit order (rebases, force-pushes);
    the seam guard must refuse to feed the EpisodeMachine out of order."""
    archive = _repo(tmp_path / "archive", [
        (T - timedelta(hours=1), {FILE_ARCHIVE: "a1"}),
    ])
    own = _repo(tmp_path / "own", [
        (T + timedelta(hours=2), {FILE_OWN: "o-late"}),
        (T + timedelta(seconds=30), {FILE_OWN: "o-early"}),
    ])
    with pytest.raises(AssertionError, match="seam not monotonic"):
        list(iter_dual(archive, own))
