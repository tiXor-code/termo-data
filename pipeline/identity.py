"""PT identity: registry from prometeu history + current harta, alias resolution.

Walks every version of prometeu's CSV (Aug 2023 -> today) plus the current
harta.html to build the union registry of PT names ever published with
coordinates, then resolves page-A pt_norms that miss exact matches via fuzzy
matching. >=0.95 auto-aliases, 0.85-0.95 lands in a pending review queue,
below stays a standalone entity (institutions are legitimately not on the map).

Usage: uv run python -m pipeline.identity <db_path> <prometeu_repo> <harta_html>
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import subprocess
import sys

from rapidfuzz import fuzz, process

from pipeline.parse import normalize_pt

CSV_PATH = "data/cmteb/status-sistem-termoficare-bucuresti.csv"
AUTO_T = 90.0
PENDING_T = 85.0


def digits_of(name: str) -> list[str]:
    return sorted(re.findall(r"\d+", name))

SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_registry (
  pt_norm TEXT PRIMARY KEY, display_name TEXT, lat REAL, lon REAL,
  first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS pt_alias (
  alias_norm TEXT PRIMARY KEY, pt_norm TEXT, score REAL, status TEXT
);
"""


def walk_registry(repo: str) -> dict[str, dict]:
    shas = subprocess.run(
        ["git", "-C", repo, "log", "--first-parent", "--reverse", "--format=%H|%aI", "--", CSV_PATH],
        capture_output=True, text=True, check=True).stdout.splitlines()
    registry: dict[str, dict] = {}
    proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for line in shas:
        sha, ts = line.split("|")
        proc.stdin.write(f"{sha}:{CSV_PATH}\n".encode())
        proc.stdin.flush()
        header = proc.stdout.readline().decode().strip()
        if header.endswith("missing"):
            continue
        size = int(header.split()[-1])
        blob = proc.stdout.read(size)
        proc.stdout.read(1)
        try:
            rows = list(csv.DictReader(io.StringIO(blob.decode("utf-8", "replace"))))
        except csv.Error:
            continue
        for r in rows:
            name = (r.get("denumire") or "").strip()
            if not name:
                continue
            norm, _, _ = normalize_pt(name)
            entry = registry.setdefault(norm, dict(display=name, lat=None, lon=None,
                                                   first_seen=ts, last_seen=ts))
            entry["last_seen"] = ts
            try:
                entry["lat"], entry["lon"] = float(r["Lat"]), float(r["Long"])
            except (KeyError, TypeError, ValueError):
                pass
    proc.stdin.close()
    proc.wait()
    return registry


def add_harta(registry: dict[str, dict], harta_html: str):
    from pipeline.parse import harta_points
    text = open(harta_html, encoding="utf-8", errors="replace").read()
    for name, lat, lon in harta_points(text):
        norm, _, _ = normalize_pt(name)
        entry = registry.setdefault(norm, dict(display=name, lat=None, lon=None,
                                               first_seen="harta", last_seen="harta"))
        if lat is not None and lon is not None:
            entry["lat"], entry["lon"] = lat, lon


def main(db_path: str, prometeu_repo: str, harta_html: str):
    registry = walk_registry(prometeu_repo)
    add_harta(registry, harta_html)
    print(f"registry: {len(registry)} distinct pt_norms with coords/history")

    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT OR REPLACE INTO pt_registry VALUES (?,?,?,?,?,?)",
        [(n, e["display"], e["lat"], e["lon"], e["first_seen"], e["last_seen"])
         for n, e in registry.items()])

    page_a_pts = [r[0] for r in db.execute("SELECT DISTINCT pt_norm FROM episode")]
    unmatched = [p for p in page_a_pts if p not in registry]
    print(f"page A pt_norms: {len(page_a_pts)}; exact-matched: {len(page_a_pts)-len(unmatched)}; "
          f"unmatched: {len(unmatched)}")

    choices = list(registry.keys())
    auto, pending, standalone = [], [], []
    for p in unmatched:
        # Digits are identity (school no., PT no.): candidates with different
        # numbers are different entities no matter how similar the strings.
        ok = [c for c in choices if digits_of(c) == digits_of(p)]
        best = process.extractOne(p, ok, scorer=fuzz.token_sort_ratio) if ok else None
        if best and best[1] >= AUTO_T:
            auto.append((p, best[0], best[1]))
        elif best and best[1] >= PENDING_T:
            pending.append((p, best[0], best[1]))
        else:
            standalone.append(p)

    db.executemany("INSERT OR REPLACE INTO pt_alias VALUES (?,?,?,'auto')",
                   [(a, t, s) for a, t, s in auto])
    db.executemany("INSERT OR REPLACE INTO pt_alias VALUES (?,?,?,'pending')",
                   [(a, t, s) for a, t, s in pending])
    db.commit()

    print(f"auto-aliased (>= {AUTO_T}): {len(auto)}; pending review "
          f"({PENDING_T}-{AUTO_T}): {len(pending)}; standalone: {len(standalone)}")
    print("\nSample auto aliases:")
    for a, t, s in auto[:10]:
        print(f"  {a!r:40} -> {t!r:40} ({s:.0f})")
    print("\nSample pending:")
    for a, t, s in pending[:10]:
        print(f"  {a!r:40} -> {t!r:40} ({s:.0f})")
    print("\nSample standalone:")
    for p in standalone[:12]:
        print(f"  {p!r}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
