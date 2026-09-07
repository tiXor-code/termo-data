#!/usr/bin/env python3
"""Did this scrape change the LIVE outage state, or only the raw bytes?

The site is rebuilt nightly, so a newly announced outage - or a revised
restore estimate - could sit invisible for up to 24h. A visitor said exactly
that on 2026-08-31: "Nu este actualizat la data de 31.08.2026". The scrape
itself runs far more often, so the data is already there; only the publish
lags.

Publishing on every byte change would be wasteful and, on Vercel Hobby (one
concurrent build, ~6 min each), self-queueing. So publish only when the state
a reader would actually see has changed.

"Live state" = the canonical hash over page A's parsed records
(pipeline.parse.content_hash). That hash is built from sorted key tuples, so:

  - a revised `remediere_raw` (the estimated restore time) DOES trigger, which
    matters because that is the number people are asking for;
  - reordered rows, whitespace and markup churn do NOT;
  - the affected-street list is NOT in the key tuple, so a change only to which
    streets a PT lists will not trigger a publish. Accepted: the PT identity,
    cause, restore estimate and block count are what the live band renders.

Page A only ever lists CURRENTLY ACTIVE outages - records vanish when resolved
- so a change to it is by definition a change to the live state.

Compares the working tree against HEAD, so it must run BEFORE the commit step.

Exits 0 always. Prints `changed=true|false`, and writes the same to
$GITHUB_OUTPUT when present. Any failure is reported as changed=true: a broken
detector should degrade to publishing too often, never to going silently stale.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAGE_A = REPO / "data" / "functionare.html"

sys.path.insert(0, str(REPO))


def emit(changed: bool, reason: str, degraded: bool = False) -> None:
    value = "true" if changed else "false"
    print(f"changed={value}  ({reason})")
    # Failing open is safe but SILENT: a permanently broken detector just
    # publishes on every commit and nobody notices. A missing lxml did exactly
    # that on the first real run. Surface it as a run annotation instead.
    if degraded:
        print(f"::warning title=Live-state detector degraded::{reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={value}\n")
    sys.exit(0)


def live_hash(html: bytes) -> str:
    """Canonical hash of the live records, with "no outages" as a real state.

    `EmptyState` is NOT an error: parse.py raises it for CMTEB's routine
    "nu exista inregistrari" banner, and backfill.py already handles it
    separately from ParseFailure. Folding it into the generic failure path
    would make every scrape look changed for as long as the city has no active
    outages - publishing constantly at exactly the times there is nothing to
    publish. Zero records is a legitimate hash, so two empty scrapes compare
    equal while empty->outage and outage->empty both still trigger.
    """
    from pipeline.parse import EmptyState, content_hash, parse_page

    try:
        return content_hash(parse_page(html))
    except EmptyState:
        return content_hash([])


def main() -> None:
    try:
        current = PAGE_A.read_bytes()
    except OSError as exc:
        emit(True, f"cannot read working-tree page A ({exc}) - failing open", degraded=True)

    try:
        previous = subprocess.run(
            ["git", "show", f"HEAD:data/{PAGE_A.name}"],
            cwd=REPO,
            capture_output=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        emit(True, "no previous page A at HEAD - first run, failing open", degraded=True)

    try:
        before = live_hash(previous)
        after = live_hash(current)
    except Exception as exc:  # noqa: BLE001 - a real ParseFailure must not go silent
        emit(True, f"parse failed ({type(exc).__name__}: {exc}) - failing open", degraded=True)

    emit(before != after, f"{before[:12]} -> {after[:12]}")


if __name__ == "__main__":
    # This step runs BEFORE "Commit if changed". An uncaught exception here
    # would fail the step and skip the commit, losing the scraped snapshot
    # entirely - far worse than a missed publish. Nothing escapes.
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        emit(True, f"detector crashed ({type(exc).__name__}: {exc}) - failing open", degraded=True)
