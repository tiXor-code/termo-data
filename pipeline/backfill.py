"""Backfill: stream snapshot history from two sources into episodes + metrics.

Usage: uv run python -m pipeline.backfill <archive_path> <own_repo_path> <db_path>

Dual-source ingest: the FlorinPopaCodes archive supplies everything strictly
before CUTOVER_UTC; this repo's own scraper history (data/functionare.html)
supplies everything at or after it. One EpisodeMachine runs across the seam
(prev_hash carries over, so an unchanged page at the boundary doesn't churn
episodes). Timestamps are mixed-offset ISO strings (+02:00/+03:00/Z): always
compared as aware datetimes, never as strings.

Single pass over `git cat-file --batch` blob stream; parsing fans out to a
multiprocessing pool (order-preserving imap), the episode state machine and
SQLite writes stay in the parent process. Nightly runs are full rebuilds
(rm -f the db first) - idempotent, no incremental-state bugs.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.parse import EmptyState, ParseFailure, content_hash, parse_page

BUCHAREST = ZoneInfo("Europe/Bucharest")
GAP_HOURS = 6  # source silence above this marks open episodes gap_spanned
FILE_ARCHIVE = "data/termoficare.html"   # FlorinPopaCodes/termoficare-data
FILE_OWN = "data/functionare.html"       # this repo's scraper output
# == meta.json sources_cutover_utc; first own snapshot is 2026-06-10T19:34:39Z
CUTOVER_UTC = datetime(2026, 6, 10, 19, 34, tzinfo=timezone.utc)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
  id INTEGER PRIMARY KEY, sha TEXT, observed_utc TEXT, content_hash TEXT,
  n_records INT, parse_status TEXT, changed INT
);
CREATE TABLE IF NOT EXISTS episode (
  id INTEGER PRIMARY KEY, pt_norm TEXT, sector INT, service TEXT, severity TEXT,
  cause_class TEXT, cause_raw TEXT, remediere_last TEXT, blocks_count INT,
  first_seen TEXT, last_seen TEXT, started_after TEXT, ended_before TEXT,
  gap_spanned INT, est_hours REAL
);
CREATE TABLE IF NOT EXISTS street_pt (
  street_norm TEXT, street_type TEXT, pt_norm TEXT, times_seen INT,
  PRIMARY KEY (street_norm, street_type, pt_norm)
);
CREATE TABLE IF NOT EXISTS episode_street (
  episode_id INT, street_norm TEXT, street_type TEXT,
  PRIMARY KEY (episode_id, street_norm, street_type)
);
CREATE TABLE IF NOT EXISTS parse_failure (sha TEXT, observed_utc TEXT, error TEXT);
"""


def _worker(args: tuple[str, str, bytes]):
    sha, ts, blob = args
    try:
        recs = parse_page(blob)
    except EmptyState as e:
        return sha, ts, None, f"empty: {e}"
    except ParseFailure as e:
        return sha, ts, None, f"failed: {e}"
    rows = [(r.sector, r.pt_norm, r.severity, r.service, r.cause_class,
             r.cause_raw, r.remediere_raw, r.blocks_count,
             tuple((s[0], s[1]) for s in r.streets)) for r in recs]
    return sha, ts, rows, content_hash(recs)


def iter_blobs(repo: Path, file_path: str):
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--first-parent", "--reverse",
         "--format=%H|%aI", "--", file_path],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    # finally: iter_dual breaks out of this generator at the cutover on every
    # nightly run, which would otherwise orphan the cat-file child until exit
    try:
        for line in log:
            sha, ts = line.split("|")
            proc.stdin.write(f"{sha}:{file_path}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().strip()
            if header.endswith("missing"):
                continue
            size = int(header.split()[-1])
            blob = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline
            yield sha, ts, blob
    finally:
        proc.stdin.close()
        proc.wait()


def iter_dual(archive: Path, own: Path):
    """Archive blobs strictly before CUTOVER_UTC, then own-repo blobs at or
    after it. The archive keeps scraping past the cutover - everything >= T is
    dropped there; pre-cutover own commits (none expected) are skipped. The
    seam-monotonicity assert also guards against non-monotonic author dates
    inside the own history (rebases, force-pushes). Keepalive commits touching
    only data/meta.json never reach the log (path filter)."""
    last = None
    for sha, ts, blob in iter_blobs(archive, FILE_ARCHIVE):
        if datetime.fromisoformat(ts) >= CUTOVER_UTC:
            break
        last = datetime.fromisoformat(ts)
        yield sha, ts, blob
    for sha, ts, blob in iter_blobs(own, FILE_OWN):
        t = datetime.fromisoformat(ts)
        if t < CUTOVER_UTC:
            continue
        assert last is None or t >= last, f"seam not monotonic: {t} < {last}"
        last = t
        yield sha, ts, blob


class EpisodeMachine:
    def __init__(self):
        self.open: dict[tuple, dict] = {}
        self.closed: list[dict] = []
        self.prev_t: datetime | None = None

    def step(self, t: datetime, present: dict[tuple, dict]):
        gap = self.prev_t and (t - self.prev_t) > timedelta(hours=GAP_HOURS)
        if gap:
            for ep in self.open.values():
                ep["gap_spanned"] = 1
        for key, ep in list(self.open.items()):
            info = present.get(key)
            if info is None:
                ep["ended_before"] = t.isoformat()
                self.closed.append(ep)
                del self.open[key]
            elif info["cause_class"] != ep["cause_class"]:
                ep["ended_before"] = t.isoformat()
                self.closed.append(ep)
                self.open[key] = self._new(key, info, t)
            else:
                ep["last_seen"] = t.isoformat()
                ep["remediere_last"] = info["remediere"]
                ep["blocks_count"] = max(ep["blocks_count"] or 0, info["blocks"] or 0) or None
                ep["streets"].update(info["streets"])
        for key, info in present.items():
            if key not in self.open:
                self.open[key] = self._new(key, info, t)
        self.prev_t = t

    def extend_unchanged(self, t: datetime):
        for ep in self.open.values():
            ep["last_seen"] = t.isoformat()
        self.prev_t = t

    def _new(self, key: tuple, info: dict, t: datetime) -> dict:
        pt, service, severity = key
        # An episode first seen right after archive silence may have started
        # anywhere inside the gap: flag it like episodes that spanned the gap.
        opened_after_gap = int(
            self.prev_t is not None and (t - self.prev_t) > timedelta(hours=GAP_HOURS))
        return dict(pt_norm=pt, sector=info["sector"], service=service, severity=severity,
                    cause_class=info["cause_class"], cause_raw=info["cause_raw"],
                    remediere_last=info["remediere"], blocks_count=info["blocks"],
                    first_seen=t.isoformat(), last_seen=t.isoformat(),
                    started_after=self.prev_t.isoformat() if self.prev_t else None,
                    ended_before=None, gap_spanned=opened_after_gap,
                    streets=set(info["streets"]))

    def finish(self):
        for ep in self.open.values():
            self.closed.append(ep)
        self.open = {}


def est_hours(ep: dict) -> float:
    """Midpoint estimate, with pre/post credit capped at GAP_HOURS/2 so archive
    silence (e.g. the 251h Aug-2023 gap) cannot smear into episode durations."""
    first = datetime.fromisoformat(ep["first_seen"])
    last = datetime.fromisoformat(ep["last_seen"])
    core = (last - first).total_seconds() / 3600
    pre = post = 0.0
    if ep["started_after"]:
        pre = min((first - datetime.fromisoformat(ep["started_after"])).total_seconds() / 7200,
                  GAP_HOURS / 2)
    if ep["ended_before"]:
        post = min((datetime.fromisoformat(ep["ended_before"]) - last).total_seconds() / 7200,
                   GAP_HOURS / 2)
    return round(core + pre + post, 2)


def main(archive: str, own_repo: str, db_path: str):
    # db/ is gitignored, so a fresh checkout (nightly runner) has no parent dir
    # and sqlite3.connect would fail with "unable to open database file".
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    machine = EpisodeMachine()
    streets: dict[tuple, int] = {}
    prev_hash = None
    n = n_failed = 0

    with Pool(8) as pool:
        for sha, ts, rows, h in pool.imap(_worker, iter_dual(Path(archive), Path(own_repo)), chunksize=32):
            n += 1
            t = datetime.fromisoformat(ts)
            if rows is None:
                status = "empty" if h.startswith("empty:") else "failed"
                if status == "failed":
                    n_failed += 1
                db.execute("INSERT INTO parse_failure VALUES (?,?,?)", (sha, ts, h))
                db.execute("INSERT INTO snapshot (sha,observed_utc,content_hash,n_records,parse_status,changed) VALUES (?,?,?,?,?,0)",
                           (sha, ts, None, 0, status))
                continue
            changed = h != prev_hash
            db.execute("INSERT INTO snapshot (sha,observed_utc,content_hash,n_records,parse_status,changed) VALUES (?,?,?,?,?,?)",
                       (sha, ts, h, len(rows), "ok", int(changed)))
            if not changed:
                machine.extend_unchanged(t)
                continue
            prev_hash = h
            present: dict[tuple, dict] = {}
            for sector, pt, sev, svc, cclass, craw, rem, blocks, st in rows:
                key = (pt, svc, sev)
                cur = present.get(key)
                # concurrent same-key rows: keep avarie over programat, note both later
                if cur is None or (cclass == "avarie" and cur["cause_class"] != "avarie"):
                    streets_prev = cur["streets"] if cur else set()
                    present[key] = dict(sector=sector, cause_class=cclass, cause_raw=craw,
                                        remediere=rem, blocks=blocks,
                                        streets=streets_prev | set(st))
                else:
                    cur["streets"].update(st)
                for snorm, stype in st:
                    streets[(snorm, stype, pt)] = streets.get((snorm, stype, pt), 0) + 1
            machine.step(t, present)
            if n % 2000 == 0:
                print(f"  {n} snapshots, {len(machine.closed)} episodes closed", flush=True)

    machine.finish()
    cur = db.cursor()
    street_rows = []
    for ep in machine.closed:
        ep["est_hours"] = est_hours(ep)
        cur.execute(
            """INSERT INTO episode (pt_norm,sector,service,severity,cause_class,cause_raw,
               remediere_last,blocks_count,first_seen,last_seen,started_after,ended_before,
               gap_spanned,est_hours) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ep["pt_norm"], ep["sector"], ep["service"], ep["severity"], ep["cause_class"],
             ep["cause_raw"], ep["remediere_last"], ep["blocks_count"], ep["first_seen"],
             ep["last_seen"], ep["started_after"], ep["ended_before"], ep["gap_spanned"],
             ep["est_hours"]),
        )
        eid = cur.lastrowid
        street_rows += [(eid, s[0], s[1]) for s in ep["streets"]]
    db.executemany("INSERT OR IGNORE INTO episode_street VALUES (?,?,?)", street_rows)
    db.executemany("INSERT OR REPLACE INTO street_pt VALUES (?,?,?,?)",
                   [(k[0], k[1], k[2], v) for k, v in streets.items()])
    db.commit()
    print(f"done: {n} snapshots ({n_failed} parse failures), "
          f"{len(machine.closed)} episodes, {len(streets)} street-PT links")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
