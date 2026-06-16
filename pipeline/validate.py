"""Validate: invariant gate between the reconstructed db and the published web/.

Each check prints PASS/WARN/FAIL lines; any FAIL exits nonzero. The nightly
workflow runs this before the registry commit, release upload and deploy hook,
so a breached invariant leaves yesterday's data-latest bundle and site intact.

Sector rollups are cross-checked by re-deriving per-PT day counts from the db
(same alias filter + day expansion as publish) against the published sector
assignment in pt/all.ndjson.gz - an independent re-derivation, not a re-run of
publish. Unclassified-share thresholds are scoped to severity=oprire AND
service=ACC: the global share (~46%) is dominated by deficienta causes and
would falsely trip the gate.

Usage: uv run python -m pipeline.validate <db> <web_dir> [harta_html] [registry_json]
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from pipeline.metrics import BUCHAREST, episode_days, pt_universe
from pipeline.publish import pctl

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

ARTIFACT_KEYS_META = ("generated_at", "data_through", "years", "last_complete_year",
                      "partial_years", "universe_size", "coverage", "sources_cutover_utc")


def _alias(db) -> dict[str, str]:
    try:
        return dict(db.execute(
            "SELECT alias_norm, pt_norm FROM pt_alias WHERE status IN ('auto','approved')"))
    except sqlite3.OperationalError:
        return {}


def check_episode_overlap(db) -> list[tuple]:
    by_key: dict[tuple, list] = defaultdict(list)
    for pt, svc, sev, first, last in db.execute(
            "SELECT pt_norm, service, severity, first_seen, last_seen FROM episode"):
        by_key[(pt, svc, sev)].append(
            (datetime.fromisoformat(first), datetime.fromisoformat(last)))
    bad = 0
    for ivs in by_key.values():
        ivs.sort()
        max_end = None  # running max catches containment beyond adjacent pairs
        for f, l in ivs:
            if max_end is not None and f < max_end:
                bad += 1
            max_end = l if max_end is None else max(max_end, l)
    return [(FAIL if bad else PASS, "episode_overlap",
             f"{bad} overlapping same-key episode pairs")]


def check_episode_sanity(db) -> list[tuple]:
    n_rev = n_end = n_sec = 0
    for first, last, ended, sector in db.execute(
            "SELECT first_seen, last_seen, ended_before, sector FROM episode"):
        f, l = datetime.fromisoformat(first), datetime.fromisoformat(last)
        if f > l:
            n_rev += 1
        if ended is not None and datetime.fromisoformat(ended) < l:
            n_end += 1
        if sector is None or not 1 <= sector <= 6:
            n_sec += 1
    return [
        (FAIL if n_rev else PASS, "episode_sanity_order",
         f"{n_rev} episodes with first_seen > last_seen"),
        (FAIL if n_end else PASS, "episode_sanity_ended",
         f"{n_end} episodes with ended_before < last_seen"),
        (FAIL if n_sec else PASS, "episode_sanity_sector",
         f"{n_sec} episodes with sector outside 1..6 or null"),
    ]


def check_snapshot_accounting(db) -> list[tuple]:
    statuses = dict(db.execute(
        "SELECT parse_status, COUNT(*) FROM snapshot GROUP BY parse_status"))
    unknown = {s for s in statuses if s not in ("ok", "empty", "failed")}
    orphans = db.execute(
        """SELECT COUNT(*) FROM snapshot s WHERE parse_status != 'ok'
           AND NOT EXISTS (SELECT 1 FROM parse_failure f WHERE f.sha = s.sha)"""
    ).fetchone()[0]
    total = sum(statuses.values()) or 1
    share = statuses.get("failed", 0) / total
    return [
        (FAIL if unknown else PASS, "snapshot_status",
         f"unknown parse_status values: {sorted(unknown)}" if unknown
         else f"all {total} snapshots ok/empty/failed"),
        (FAIL if orphans else PASS, "snapshot_failures_logged",
         f"{orphans} non-ok snapshots without a parse_failure row"),
        (FAIL if share >= 0.005 else PASS, "snapshot_failed_share",
         f"failed share {100 * share:.2f}% (limit 0.5%)"),
    ]


def check_coords_coverage(db, harta_html: str) -> list[tuple]:
    universe = pt_universe(harta_html)
    alias = _alias(db)
    hit = universe & {alias.get(p, p) for (p,) in db.execute(
        "SELECT DISTINCT pt_norm FROM episode WHERE service='ACC'")}
    coords = {n for n, lat, lon in db.execute(
        "SELECT pt_norm, lat, lon FROM pt_registry")
        if lat is not None and lon is not None}
    if not hit:
        return [(FAIL, "coords_coverage", "no universe PTs with episodes")]
    share = sum(1 for p in hit if p in coords) / len(hit)
    return [(FAIL if share < 0.9 else PASS, "coords_coverage",
             f"{100 * share:.1f}% of {len(hit)} universe PTs with episodes have coords")]


def check_unclassified_share(db, years: list[int]) -> list[tuple]:
    per_year: dict[int, Counter] = defaultdict(Counter)
    for cls, first in db.execute(
            "SELECT cause_class, first_seen FROM episode "
            "WHERE service='ACC' AND severity='oprire'"):
        per_year[datetime.fromisoformat(first).astimezone(BUCHAREST).year][cls] += 1
    out = []
    for y in years:
        c = per_year.get(y)
        if not c:
            out.append((WARN, f"unclassified_share_{y}", "no oprire-ACC episodes"))
            continue
        share = 100 * c.get("unclassified", 0) / sum(c.values())
        lvl = FAIL if share >= 20 else WARN if share >= 10 else PASS
        out.append((lvl, f"unclassified_share_{y}",
                    f"{share:.1f}% unclassified of {sum(c.values())} oprire-ACC episodes"))
    return out


def check_sector_consistency(db, web: Path, harta_html: str,
                             registry_path: str) -> list[tuple]:
    reg_p = Path(registry_path)
    meta_p = web / "meta.json"
    if not reg_p.exists() or not meta_p.exists():
        return [(FAIL, "sector_rollup", "registry or meta.json missing")]
    pt_map = json.loads(reg_p.read_text(encoding="utf-8")).get("pt", {})
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    universe = pt_universe(harta_html)

    sector_by_slug: dict[str, int | None] = {}
    with gzip.open(web / "pt" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            sector_by_slug[o["slug"]] = o["sector"]

    alias = _alias(db)
    days: dict[tuple, set] = defaultdict(set)
    cls_days: dict[tuple, set] = defaultdict(set)
    sector_year_eps: Counter = Counter()
    for pt0, sector, cls, first, last in db.execute(
            """SELECT pt_norm, sector, cause_class, first_seen, last_seen
               FROM episode WHERE service='ACC' AND severity='oprire'"""):
        pt = alias.get(pt0, pt0)
        if sector:
            sector_year_eps[(sector, datetime.fromisoformat(first)
                             .astimezone(BUCHAREST).year)] += 1
        for d in episode_days(first, last):
            days[(pt, d.year)].add(d)
            cls_days[(pt, d.year, cls)].add(d)

    out = []
    unassigned = sum(1 for p in universe if sector_by_slug.get(pt_map.get(p)) is None)
    out.append((WARN if unassigned / len(universe) >= 0.05 else PASS,
                "sector_unassigned",
                f"{unassigned}/{len(universe)} universe PTs without a sector"))

    summary_eps = {row["year"]: row["episodes"] for row in
                   json.loads((web / "city" / "summary.json").read_text(encoding="utf-8"))}
    by_sector: dict[int, list[str]] = defaultdict(list)
    for p in sorted(universe):
        s = sector_by_slug.get(pt_map.get(p))
        if s is not None:
            by_sector[s].append(p)

    mismatches = []
    for y in meta["years"]:
        rows = json.loads((web / "rankings" / f"sectoare-{y}.json")
                          .read_text(encoding="utf-8"))
        if sum(r["episodes"] for r in rows) != summary_eps.get(y):
            mismatches.append(f"{y}: sector episode sum != citywide episodes")
        for r in rows:
            spts = by_sector.get(r["sector"], [])
            vals = sorted(len(days.get((p, y), ())) for p in spts)
            n = len(spts) or 1
            expect = {
                "pts": len(spts),
                "median_days": pctl(vals, 50),
                "mean_days": round(sum(vals) / n, 1),
                "mean_days_avarie": round(
                    sum(len(cls_days.get((p, y, "avarie"), ())) for p in spts) / n, 1),
                "mean_days_programat": round(
                    sum(len(cls_days.get((p, y, "programat"), ())) for p in spts) / n, 1),
                "episodes": sector_year_eps[(r["sector"], y)],
            }
            got = {k: r[k] for k in expect}
            if got != expect:
                mismatches.append(f"{y} sector {r['sector']}: {got} != {expect}")
    out.append((FAIL if mismatches else PASS, "sector_rollup",
                "; ".join(mismatches[:5]) if mismatches
                else f"rollups consistent across {len(meta['years'])} years"))
    return out


def check_artifacts(web: Path, registry_slugs: set[str] | None = None) -> list[tuple]:
    out = []

    def load(rel: str):
        p = web / rel
        if not p.exists():
            out.append((FAIL, "artifact_missing", rel))
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            out.append((FAIL, "artifact_unparseable", f"{rel}: {e}"))
            return None

    meta = load("meta.json")
    if meta is None:
        return out
    missing_meta = [k for k in ARTIFACT_KEYS_META if k not in meta]
    if missing_meta:
        out.append((FAIL, "meta_keys", f"missing: {missing_meta}"))
    years = meta.get("years", [])

    load("city/summary.json")
    pt_slugs: set[str] = set()
    st_slugs: set[str] = set()
    block_pts: set[str] = set()
    streets_with_blocks = 0
    inferred_pts: set[str] = set()
    inferred_count = 0
    for ns, rel in (("pt", "pt/all.ndjson.gz"), ("st", "strazi/all.ndjson.gz")):
        p = web / rel
        if not p.exists():
            out.append((FAIL, "artifact_missing", rel))
            continue
        bag = pt_slugs if ns == "pt" else st_slugs
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                for line in f:
                    o = json.loads(line)
                    bag.add(o["slug"])
                    if ns == "st" and o.get("blocks"):
                        streets_with_blocks += 1
                        block_pts.update(b["pt"] for b in o["blocks"])
                    if ns == "st" and o.get("inferred_pt"):
                        inferred_count += 1
                        inferred_pts.add(o["inferred_pt"])
        except (ValueError, KeyError, OSError) as e:
            out.append((FAIL, "artifact_unparseable", f"{rel}: {e}"))
    inter = pt_slugs & st_slugs
    out.append((FAIL if inter else PASS, "slug_namespaces",
                f"pt/street slug overlap: {sorted(inter)[:5]}" if inter
                else f"{len(pt_slugs)} pt + {len(st_slugs)} street slugs disjoint"))
    unresolved = block_pts - pt_slugs
    out.append((WARN if unresolved else PASS, "block_pt_resolvable",
                f"block PTs not in pt set: {sorted(unresolved)[:5]}" if unresolved
                else f"blocks on {streets_with_blocks} streets, all PTs resolvable"))
    inf_bad = inferred_pts - pt_slugs
    out.append((FAIL if inf_bad else PASS, "inferred_pt_resolvable",
                f"inferred PTs not in pt set: {sorted(inf_bad)[:5]}" if inf_bad
                else f"{inferred_count} OSM-only streets inferred, all PTs resolvable"))

    rank_bad = []
    for y in years:
        dist = load(f"city/distribution-{y}.json")
        if dist is not None:
            total = sum(c for _, c in dist.get("histogram", []))
            if total != meta.get("universe_size"):
                out.append((FAIL, "histogram_sum",
                            f"{y}: {total} != universe_size {meta.get('universe_size')}"))
        for kind, bag in (("pt", pt_slugs), ("strazi", st_slugs)):
            rows = load(f"rankings/{kind}-{y}.json")
            if rows is None:
                continue
            days_seq = [r["days"] for r in rows]
            if days_seq != sorted(days_seq, reverse=True):
                rank_bad.append(f"{kind}-{y} not sorted desc")
            for r in rows:
                if r["slug"] not in bag:
                    rank_bad.append(f"{kind}-{y}: {r['slug']} not in ndjson")
                    break
                if registry_slugs is not None and r["slug"] not in registry_slugs:
                    rank_bad.append(f"{kind}-{y}: {r['slug']} not in registry")
                    break
        sect = load(f"rankings/sectoare-{y}.json")
        if sect is not None and [r["sector"] for r in sect] != [1, 2, 3, 4, 5, 6]:
            out.append((FAIL, "sectoare_rows", f"{y}: expected sectors 1..6"))
        geo = load(f"client/map/pt-{y}.geojson")
        if geo is not None:
            feats = geo.get("features")
            if geo.get("type") != "FeatureCollection" or not isinstance(feats, list) or any(
                    f["geometry"]["type"] != "Point" or len(f["geometry"]["coordinates"]) != 2
                    for f in feats):
                out.append((FAIL, "map_geojson", f"pt-{y}.geojson malformed"))
    out.append((FAIL if rank_bad else PASS, "rankings_integrity",
                "; ".join(rank_bad[:5]) if rank_bad
                else f"rankings resolvable + sorted for {len(years)} years"))

    search = load("client/search-index.json")
    if search is not None:
        slugs = [e.get("s") for e in search]
        bad_shape = [e for e in search if set(e) != {"t", "n", "s", "sec", "d"}]
        if len(slugs) != len(set(slugs)):
            out.append((FAIL, "search_index", "duplicate slugs"))
        elif bad_shape:
            out.append((FAIL, "search_index", f"bad entry shape: {bad_shape[0]}"))
        else:
            out.append((PASS, "search_index", f"{len(slugs)} unique entries"))

    og = load("og/stats.json")
    if og is not None:
        missing = (pt_slugs | st_slugs) - set(og)
        out.append((FAIL if missing else PASS, "og_coverage",
                    f"{len(missing)} slugs missing from og/stats.json" if missing
                    else f"og/stats.json covers all {len(og)} slugs"))

    if not (web / "client" / "sectoare.geojson").exists():
        out.append((WARN, "sectoare_geojson", "client/sectoare.geojson absent (optional)"))
    return out


def check_registry_append_only(registry_path: str) -> list[tuple]:
    p = Path(registry_path)
    if not p.exists():
        return [(FAIL, "registry_append_only", "registry file missing")]
    res = subprocess.run(["git", "show", f"HEAD:{registry_path}"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        # Only an untracked path (or an unborn HEAD) counts as a first run.
        # Any other git failure (wrong cwd, absolute path, not a repo) must
        # not silently disable the gate.
        err = res.stderr.strip()
        first_run = ("does not exist in" in err or "exists on disk, but not in" in err
                     or "invalid object name 'HEAD'" in err)
        if first_run:
            return [(PASS, "registry_append_only", "no committed baseline (first run)")]
        return [(WARN, "registry_append_only", f"git show failed, gate skipped: {err}")]
    old = json.loads(res.stdout)
    new = json.loads(p.read_text(encoding="utf-8"))
    broken = []
    for ns in ("pt", "street"):
        for ident, slug in old.get(ns, {}).items():
            got = new.get(ns, {}).get(ident)
            if got != slug:
                broken.append(f"{ns}:{ident}: {slug} -> {got}")
    return [(FAIL if broken else PASS, "registry_append_only",
             "; ".join(broken[:5]) if broken else "all committed identities stable")]


def exit_code(results: list[tuple]) -> int:
    return 1 if any(lvl == FAIL for lvl, _, _ in results) else 0


def run(db_path: str, web_dir: str, harta_html: str = "data/harta.html",
        registry_path: str = "registry/slugs.json") -> int:
    db = sqlite3.connect(db_path)
    web = Path(web_dir)
    results: list[tuple] = []
    results += check_episode_overlap(db)
    results += check_episode_sanity(db)
    results += check_snapshot_accounting(db)
    results += check_coords_coverage(db, harta_html)

    meta_p = web / "meta.json"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        results += check_unclassified_share(
            db, sorted({meta["last_complete_year"], max(meta["years"])}))
        results += check_sector_consistency(db, web, harta_html, registry_path)
    else:
        results.append((FAIL, "meta_json", f"{meta_p} missing"))

    registry_slugs = None
    reg_p = Path(registry_path)
    if reg_p.exists():
        reg = json.loads(reg_p.read_text(encoding="utf-8"))
        registry_slugs = set(reg.get("pt", {}).values()) | set(reg.get("street", {}).values())
    results += check_artifacts(web, registry_slugs)
    results += check_registry_append_only(registry_path)

    for lvl, name, detail in results:
        print(f"{lvl} {name}: {detail}")
    code = exit_code(results)
    print(f"\nvalidate: {sum(1 for r in results if r[0] == FAIL)} FAIL, "
          f"{sum(1 for r in results if r[0] == WARN)} WARN, "
          f"{sum(1 for r in results if r[0] == PASS)} PASS")
    return code


if __name__ == "__main__":
    sys.exit(run(*sys.argv[1:]))
