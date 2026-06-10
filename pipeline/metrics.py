"""Yearly metrics from reconstructed episodes.

Headline rule (locked in spec): a PT's "zile cu intreruperi de apa calda" in
year Y = distinct Bucharest-local calendar days touched by >=1 episode with
severity=oprire, service=ACC. Deficienta is excluded everywhere here.
Hero stat = median over the current official PT universe (page B map), zeros
included. Auto-aliases (pt_alias.status='auto') are applied before counting.
Street days = union of days of episodes that explicitly listed the street.

Usage: uv run python -m pipeline.metrics <db_path> <harta_html> <out_json>
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from pipeline.parse import normalize_pt

BUCHAREST = ZoneInfo("Europe/Bucharest")


def pt_universe(harta_html: str) -> set[str]:
    text = open(harta_html, encoding="utf-8", errors="replace").read()
    names = re.findall(r'"denumire"\s*:\s*"([^"]+)"', text)
    return {normalize_pt(n)[0] for n in names}


def episode_days(first: str, last: str):
    t0 = datetime.fromisoformat(first).astimezone(BUCHAREST)
    t1 = datetime.fromisoformat(last).astimezone(BUCHAREST)
    d = t0.date()
    while d <= t1.date():
        yield d
        d += timedelta(days=1)


def main(db_path: str, harta_html: str, out_json: str):
    universe = pt_universe(harta_html)
    db = sqlite3.connect(db_path)
    alias = dict(db.execute("SELECT alias_norm, pt_norm FROM pt_alias WHERE status='auto'"))

    eps = db.execute(
        """SELECT id, pt_norm, sector, cause_class, first_seen, last_seen, est_hours, gap_spanned
           FROM episode WHERE severity='oprire' AND service='ACC'"""
    ).fetchall()

    days = defaultdict(set)
    days_by_class = defaultdict(set)
    hours = defaultdict(float)
    episodes_per_year = defaultdict(int)
    class_per_year = defaultdict(lambda: defaultdict(int))
    gap_eps_per_year = defaultdict(int)
    pt_sector = {}

    for eid, pt, sector, cclass, first, last, est_h, gap in eps:
        pt = alias.get(pt, pt)
        y0 = datetime.fromisoformat(first).astimezone(BUCHAREST).year
        episodes_per_year[y0] += 1
        class_per_year[y0][cclass] += 1
        if gap:
            gap_eps_per_year[y0] += 1
        if sector:
            pt_sector[pt] = sector
        hours[(pt, y0)] += est_h or 0
        for d in episode_days(first, last):
            days[(pt, d.year)].add(d)
            days_by_class[(pt, d.year, cclass)].add(d)

    # street days: union of days of episodes that listed the street
    street_days = defaultdict(set)
    rows = db.execute(
        """SELECT es.street_type, es.street_norm, e.first_seen, e.last_seen
           FROM episode_street es JOIN episode e ON e.id = es.episode_id
           WHERE e.severity='oprire' AND e.service='ACC'"""
    )
    for stype, snorm, first, last in rows:
        for d in episode_days(first, last):
            street_days[((stype, snorm), d.year)].add(d)

    seen_pts = {pt for pt, _ in days}
    years = sorted({y for _, y in days})
    report = {"universe_size": len(universe), "pts_seen_in_outages": len(seen_pts),
              "aliases_applied": len(alias), "years": {}}

    for y in years:
        per_pt = {pt: len(days[(pt, y)]) for pt, yy in days if yy == y}
        full = [per_pt.get(pt, 0) for pt in universe]
        ranked = sorted(per_pt.items(), key=lambda kv: -kv[1])
        st_ranked = sorted(((k[0], len(v)) for k, v in street_days.items() if k[1] == y),
                           key=lambda kv: -kv[1])
        cls = class_per_year[y]
        report["years"][y] = {
            "median_days_all_pts": median(full),
            "mean_days_all_pts": round(sum(full) / len(full), 1),
            "pts_with_any_outage": len(per_pt),
            "share_of_universe_hit": round(100 * sum(1 for pt in universe if per_pt.get(pt, 0) > 0) / len(universe), 1),
            "episodes": episodes_per_year.get(y, 0),
            "episodes_by_class": dict(cls),
            "unclassified_share_pct": round(100 * cls.get("unclassified", 0) / max(1, episodes_per_year.get(y, 1)), 1),
            "gap_spanned_episodes": gap_eps_per_year.get(y, 0),
            "worst_10_pts": [
                {"pt": pt, "sector": pt_sector.get(pt), "days": d,
                 "days_avarie": len(days_by_class[(pt, y, "avarie")]),
                 "days_programat": len(days_by_class[(pt, y, "programat")]),
                 "est_day_equivalents": round(hours[(pt, y)] / 24, 1)}
                for pt, d in ranked[:10]
            ],
            "worst_10_streets": [
                {"street": f"{stype} {snorm}".strip(), "days": d}
                for (stype, snorm), d in st_ranked[:10]
            ],
        }

    json.dump(report, open(out_json, "w"), indent=2, ensure_ascii=False)

    print(f"PT universe (harta): {len(universe)} | seen in outages: {len(seen_pts)} "
          f"| auto-aliases applied: {len(alias)} | streets tracked: {len({k[0] for k in street_days})}")
    print(f"\n{'year':>5} {'median':>7} {'mean':>6} {'PTs hit':>8} {'%univ':>6} "
          f"{'episodes':>9} {'avarii':>7} {'progr':>6} {'unclass%':>9} {'gap-eps':>8}")
    for y in years:
        r = report["years"][y]
        c = r["episodes_by_class"]
        print(f"{y:>5} {r['median_days_all_pts']:>7} {r['mean_days_all_pts']:>6} "
              f"{r['pts_with_any_outage']:>8} {r['share_of_universe_hit']:>6} "
              f"{r['episodes']:>9} {c.get('avarie', 0):>7} {c.get('programat', 0):>6} "
              f"{r['unclassified_share_pct']:>9} {r['gap_spanned_episodes']:>8}")
    for y in years:
        if y == 2021:
            continue
        r = report["years"][y]
        print(f"\n{y} worst streets (days):")
        for w in r["worst_10_streets"][:5]:
            print(f"  {w['street']:<42} {w['days']:>4}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
