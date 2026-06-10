"""Yearly metrics from reconstructed episodes.

Headline rule (locked in spec): a PT's "zile cu intreruperi de apa calda" in
year Y = distinct Bucharest-local calendar days touched by >=1 episode with
severity=oprire, service=ACC. Deficienta is excluded everywhere here.
Hero stat = median over the full PT universe (page B registry), zeros included.

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


def main(db_path: str, harta_html: str, out_json: str):
    universe = pt_universe(harta_html)
    db = sqlite3.connect(db_path)
    eps = db.execute(
        """SELECT pt_norm, sector, cause_class, first_seen, last_seen, est_hours, gap_spanned
           FROM episode WHERE severity='oprire' AND service='ACC'"""
    ).fetchall()

    days = defaultdict(set)            # (pt, year) -> {date}
    days_by_class = defaultdict(set)   # (pt, year, cause_class) -> {date}
    hours = defaultdict(float)         # (pt, year) -> est hours
    episodes_per_year = defaultdict(int)
    avarie_eps_per_year = defaultdict(int)
    pt_sector = {}

    for pt, sector, cclass, first, last, est_h, gap in eps:
        t0 = datetime.fromisoformat(first).astimezone(BUCHAREST)
        t1 = datetime.fromisoformat(last).astimezone(BUCHAREST)
        episodes_per_year[t0.year] += 1
        if cclass == "avarie":
            avarie_eps_per_year[t0.year] += 1
        if sector:
            pt_sector[pt] = sector
        hours[(pt, t0.year)] += est_h or 0
        d = t0.date()
        while d <= t1.date():
            days[(pt, d.year)].add(d)
            days_by_class[(pt, d.year, cclass)].add(d)
            d += timedelta(days=1)

    seen_pts = {pt for pt, _ in days}
    outside = seen_pts - universe
    years = sorted({y for _, y in days})

    report = {"universe_size": len(universe), "pts_seen_in_outages": len(seen_pts),
              "pts_outside_universe": len(outside),
              "outside_sample": sorted(outside)[:15], "years": {}}

    for y in years:
        per_pt = {pt: len(days[(pt, y)]) for pt, yy in days if yy == y}
        full = [per_pt.get(pt, 0) for pt in universe]
        ranked = sorted(per_pt.items(), key=lambda kv: -kv[1])
        report["years"][y] = {
            "median_days_all_pts": median(full),
            "mean_days_all_pts": round(sum(full) / len(full), 1),
            "pts_with_any_outage": len(per_pt),
            "share_of_universe_hit": round(100 * sum(1 for pt in universe if per_pt.get(pt, 0) > 0) / len(universe), 1),
            "episodes": episodes_per_year.get(y, 0),
            "avarie_episodes": avarie_eps_per_year.get(y, 0),
            "worst_10": [
                {"pt": pt, "sector": pt_sector.get(pt), "days": d,
                 "days_avarie": len(days_by_class[(pt, y, "avarie")]),
                 "days_programat": len(days_by_class[(pt, y, "programat")]),
                 "est_day_equivalents": round(hours[(pt, y)] / 24, 1)}
                for pt, d in ranked[:10]
            ],
        }

    json.dump(report, open(out_json, "w"), indent=2, ensure_ascii=False)

    print(f"PT universe (harta): {len(universe)} | seen in outages: {len(seen_pts)} "
          f"| outside universe: {len(outside)}")
    print(f"\n{'year':>5} {'median':>7} {'mean':>6} {'PTs hit':>8} {'%univ':>6} "
          f"{'episodes':>9} {'avarii':>7}")
    for y in years:
        r = report["years"][y]
        print(f"{y:>5} {r['median_days_all_pts']:>7} {r['mean_days_all_pts']:>6} "
              f"{r['pts_with_any_outage']:>8} {r['share_of_universe_hit']:>6} "
              f"{r['episodes']:>9} {r['avarie_episodes']:>7}")
    for y in years:
        if y in (2021,):
            continue
        print(f"\nWorst 10 in {y} (days / avarie / programat / est day-equiv):")
        for w in report["years"][y]["worst_10"]:
            print(f"  S{w['sector'] or '?'} {w['pt']:<30} {w['days']:>4} "
                  f"{w['days_avarie']:>4} {w['days_programat']:>4} {w['est_day_equivalents']:>7}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
