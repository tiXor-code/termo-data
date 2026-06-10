#!/usr/bin/env python3
"""Snapshot CMTEB/Termoenergetica outage pages into data/.

Stdlib only so the GitHub Action needs no install step. Writes a page file
only when its content hash changes; meta.json carries day-granularity fetch
info so the first run of each day always produces a commit (keepalive against
GitHub's 60-day idle-cron disable).
"""

import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PAGES = {
    "functionare.html": "https://www.cmteb.ro/functionare_sistem_termoficare.php",
    "harta.html": "https://www.cmteb.ro/harta_stare_sistem_termoficare_bucuresti.php",
}
# Error pages are tiny; real pages are ~76KB (table) and ~154KB (map).
MIN_BODY_BYTES = 10_000
UA = "termo-data scraper (+https://github.com/tiXor-code/termo-data)"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
META_PATH = DATA_DIR / "meta.json"


def fetch(url: str, attempts: int = 3) -> bytes:
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                body = resp.read()
            if len(body) < MIN_BODY_BYTES:
                raise RuntimeError(f"body too small ({len(body)} bytes)")
            return body
        except Exception as err:  # noqa: BLE001 - retry on anything, fail loudly after
            last_err = err
            time.sleep(5 * (i + 1))
    sys.exit(f"FETCH FAILED {url}: {last_err}")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text())

    now = datetime.now(timezone.utc)
    changed = []
    for filename, url in PAGES.items():
        body = fetch(url)
        digest = hashlib.sha256(body).hexdigest()
        entry = meta.setdefault(filename, {})
        if entry.get("sha256") != digest:
            (DATA_DIR / filename).write_bytes(body)
            entry["sha256"] = digest
            entry["last_change_utc"] = now.isoformat(timespec="seconds")
            entry["bytes"] = len(body)
            changed.append(filename)

    # Day granularity on purpose: full timestamps would commit every run.
    meta["fetch_date_utc"] = now.date().isoformat()
    META_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(f"changed: {changed or 'nothing'}")


if __name__ == "__main__":
    main()
