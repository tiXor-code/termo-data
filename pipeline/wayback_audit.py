"""Wayback provenance audit: archived CMTEB pages vs the git archive.

For every Wayback snapshot of functionare_sistem_termoficare.php, parse it
with the production parser and compare its record set against the nearest
archive snapshot (re-parsed from the git blob). High agreement validates the
re-pushed 2021-2025 git history against an independent source.

Usage: uv run python -m pipeline.wayback_audit <db_path> <archive_repo> <out_json>
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

from pipeline.parse import ParseFailure, parse_page

CDX = ("http://web.archive.org/cdx/search/cdx?url={u}&output=json"
       "&fl=timestamp,statuscode,digest&filter=statuscode:200&collapse=digest")
PAGE_VARIANTS = ("cmteb.ro/functionare_sistem_termoficare.php",
                 "www.cmteb.ro/functionare_sistem_termoficare.php")
SNAP_URL = "https://web.archive.org/web/{ts}id_/https://www.cmteb.ro/functionare_sistem_termoficare.php"
MAX_DELTA_H = 6
FILE_PATH = "data/termoficare.html"


def http_get(url: str, attempts: int = 4) -> bytes:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "termo-data audit"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(8 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def record_keys(html: bytes) -> set:
    return {(r.sector, r.pt_norm, r.severity, r.service, r.cause_raw, r.remediere_raw)
            for r in parse_page(html)}


def main(db_path: str, archive: str, out_json: str):
    stamps: set[str] = set()
    for variant in PAGE_VARIANTS:
        try:
            rows = json.loads(http_get(CDX.format(u=variant)).decode())
        except RuntimeError as e:
            print(f"CDX failed for {variant}: {e}")
            continue
        stamps.update(r[0] for r in rows[1:])
    stamps = sorted(stamps)
    print(f"wayback snapshots to audit: {len(stamps)}")

    db = sqlite3.connect(db_path)
    snaps = db.execute(
        "SELECT sha, observed_utc FROM snapshot WHERE parse_status='ok' ORDER BY observed_utc"
    ).fetchall()
    snap_times = [(datetime.fromisoformat(t).timestamp(), sha) for sha, t in snaps]

    results = []
    for i, ts in enumerate(stamps, 1):
        wb_dt = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        row = {"wayback_ts": ts, "status": None, "delta_min": None,
               "jaccard": None, "equal": None}
        try:
            wb_keys = record_keys(http_get(SNAP_URL.format(ts=ts)))
        except (RuntimeError, ParseFailure) as e:
            row["status"] = f"wayback_unusable: {type(e).__name__}"
            results.append(row)
            continue
        target = wb_dt.timestamp()
        nearest = min(snap_times, key=lambda p: abs(p[0] - target), default=None)
        if nearest is None or abs(nearest[0] - target) > MAX_DELTA_H * 3600:
            row["status"] = "no_archive_within_6h"
            row["wayback_records"] = len(wb_keys)
            results.append(row)
            continue
        blob = subprocess.run(
            ["git", "-C", archive, "cat-file", "blob", f"{nearest[1]}:{FILE_PATH}"],
            capture_output=True, check=True).stdout
        try:
            ar_keys = record_keys(blob)
        except ParseFailure:
            row["status"] = "archive_parse_failed"
            results.append(row)
            continue
        union = wb_keys | ar_keys
        row.update(status="compared",
                   delta_min=round(abs(nearest[0] - target) / 60),
                   jaccard=round(len(wb_keys & ar_keys) / len(union), 3) if union else 1.0,
                   equal=wb_keys == ar_keys,
                   wayback_records=len(wb_keys), archive_records=len(ar_keys))
        results.append(row)
        if i % 20 == 0:
            done = [r for r in results if r["status"] == "compared"]
            print(f"  {i}/{len(stamps)} fetched, {len(done)} compared", flush=True)
        time.sleep(1.5)

    compared = [r for r in results if r["status"] == "compared"]
    by_era: dict[str, list] = {}
    for r in compared:
        era = f"{r['wayback_ts'][:4]}-H{1 if int(r['wayback_ts'][4:6]) <= 6 else 2}"
        by_era.setdefault(era, []).append(r["jaccard"])
    summary = {
        "total_wayback": len(stamps),
        "compared": len(compared),
        "unusable": sum(1 for r in results if str(r["status"]).startswith("wayback_unusable")),
        "no_archive_match": sum(1 for r in results if r["status"] == "no_archive_within_6h"),
        "mean_jaccard": round(sum(r["jaccard"] for r in compared) / len(compared), 3) if compared else None,
        "share_jaccard_ge_90": round(100 * sum(1 for r in compared if r["jaccard"] >= 0.9) / len(compared), 1) if compared else None,
        "per_era_mean_jaccard": {e: round(sum(v) / len(v), 3) for e, v in sorted(by_era.items())},
    }
    json.dump({"summary": summary, "results": results}, open(out_json, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
