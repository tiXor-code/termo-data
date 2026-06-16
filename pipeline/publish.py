"""Publish: emit the full artifact contract (ARTIFACTS.md) into web/.

Every output key is a slug from the append-only registry (registry/slugs.json,
committed; new identities are appended in sorted order, existing slugs never
recomputed). All day counts are Bucharest-local calendar days. Headline days =
days touched by >=1 episode with severity=oprire, service=ACC; deficienta-ACC
days are the separate secondary counter (pseudo-class "deficienta" in runs,
never part of `days`); INC is excluded from v1. days_avarie + days_programat
(+ unclassified) may exceed `days` - union semantics, per contract.

Timestamps in the db are mixed-offset ISO strings (+02:00/+03:00/Z): always
parsed to aware datetimes and converted to Bucharest before .date(); day
expansion iterates dates, never +24h steps (DST-safe). est_hours of
year-spanning episodes is split across years proportional to per-year day
counts; city episode counts stay start-year attributed (reproduces the
phase-1 validated numbers).

static/sectoare.geojson is optional at runtime: absent -> WARN, the bundle
ships without client/sectoare.geojson and sector assignment for zero-episode
universe PTs degrades to episode-derived only.

Usage: uv run python -m pipeline.publish <db> <registry_json> <harta_html> <static_dir> <out_dir>
"""

from __future__ import annotations

import gzip
import json
import math
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pipeline.backfill import CUTOVER_UTC
from pipeline.metrics import BUCHAREST, episode_days, pt_universe
from pipeline.parse import parse_remediere
from pipeline.slugs import SlugRegistry

CLASSES = ("avarie", "programat", "unclassified", "deficienta")
TYPE_DISPLAY = {"str": "Str", "bld": "Bld", "cal": "Calea", "spl": "Splaiul",
                "drm": "Drumul", "sos": "Sos", "intr": "Intr", "al": "Aleea",
                "pta": "Piata", "prel": "Prel", "": ""}


def local(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(BUCHAREST)


def fmt_minute(ts: str) -> str:
    return local(ts).strftime("%Y-%m-%dT%H:%M")


def year_doys(days: set[date], year: int) -> set[int]:
    return {d.timetuple().tm_yday for d in days if d.year == year}


def runs_from_days(doys: set[int], cls: str) -> list[list]:
    """Compress a day-of-year set into [start_doy(1-based), length, cls] runs.
    Day-set compression (not raw episode clipping) merges overlapping
    same-class episodes and splits year-spanning ones at Dec 31/Jan 1."""
    runs: list[list] = []
    start = prev = None
    for doy in sorted(doys):
        if start is None:
            start = prev = doy
        elif doy == prev + 1:
            prev = doy
        else:
            runs.append([start, prev - start + 1, cls])
            start = prev = doy
    if start is not None:
        runs.append([start, prev - start + 1, cls])
    return runs


def pctl(sorted_vals: list[int], p: float) -> int:
    """Nearest-rank percentile over an ascending list."""
    if not sorted_vals:
        return 0
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def nearest_map(coords: dict[str, tuple[float, float]]) -> dict[str, list[str]]:
    """5 nearest slugs per slug, self excluded. Brute force (<1100 points)."""
    slugs = sorted(coords)
    out = {}
    for s in slugs:
        ranked = sorted((haversine_km(coords[s], coords[o]), o)
                        for o in slugs if o != s)
        out[s] = [o for _, o in ranked[:5]]
    return out


def load_sector_polygons(path: Path) -> list[tuple[int, list]]:
    """[(sector, [polygon, ...])] where polygon = [outer_ring, hole, ...] and
    each ring is [[lon, lat], ...]. Accepts Polygon and MultiPolygon."""
    gj = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for feat in gj.get("features", []):
        sector = feat.get("properties", {}).get("sector")
        if not isinstance(sector, int):
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Polygon":
            out.append((sector, [geom["coordinates"]]))
        elif geom.get("type") == "MultiPolygon":
            out.append((sector, geom["coordinates"]))
    return out


def _ring_contains(ring: list, lon: float, lat: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def sector_of_point(polys: list[tuple[int, list]], lat: float, lon: float) -> int | None:
    for sector, polygons in polys:
        for polygon in polygons:
            if _ring_contains(polygon[0], lon, lat) and not any(
                    _ring_contains(hole, lon, lat) for hole in polygon[1:]):
                return sector
    return None


def street_name(stype: str, snorm: str) -> str:
    """ASCII-only by construction: norms were diacritic-folded at parse."""
    disp = TYPE_DISPLAY.get(stype, stype.capitalize())
    return f"{disp} {snorm.title()}".strip()


_BLOCK_MARKER_RE = re.compile(r"^(bl\.?|imobile?|nr\.?)\s*", re.I)


def tokenize_blocks(blocks_raw: str) -> list[str]:
    """'bl. 23, 24, D30' -> ['bl. 23', 'bl. 24', 'bl. D30']. Ranges ('1-15') and
    prefixed codes ('D30') stay single tokens; the leading marker is normalized
    and reapplied per token. Best-effort: junk tokens dropped, order preserved."""
    s = (blocks_raw or "").strip()
    if not s:
        return []
    marker = "bl."
    m = _BLOCK_MARKER_RE.match(s)
    if m:
        mk = m.group(1).lower()
        marker = "imobil" if mk.startswith("imobil") else "nr." if mk.startswith("nr") else "bl."
        s = s[m.end():]
    out, seen = [], set()
    for tok in re.split(r"[;,]", s):  # CMTEB separates blocks with both , and ;
        tok = tok.strip().strip(".").strip()
        if not tok or not re.search(r"[0-9A-Za-z]", tok):
            continue
        label = f"{marker} {tok}"
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _block_natkey(label: str):
    return [int(t) if t.isdigit() else t for t in re.findall(r"\d+|\D+", label.lower())]


def _rem_iso(raw: str | None) -> str | None:
    iso = parse_remediere(raw or "")
    return iso[:16] if iso else None


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8")


def write_ndjson_gz(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
            for row in rows:
                gz.write(json.dumps(row, ensure_ascii=False,
                                    separators=(",", ":")).encode() + b"\n")


def build(db_path: str, registry_path: str, harta_html: str,
          static_dir: str, out_dir: str, now: datetime | None = None) -> dict:
    db = sqlite3.connect(db_path)
    universe = pt_universe(harta_html)
    alias = dict(db.execute(
        "SELECT alias_norm, pt_norm FROM pt_alias WHERE status IN ('auto','approved')"))
    canon = lambda p: alias.get(p, p)
    reg_rows = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
        "SELECT pt_norm, display_name, lat, lon FROM pt_registry")}

    # --- snapshot coverage / meta -------------------------------------------
    snaps = sorted((datetime.fromisoformat(ts), status) for ts, status in
                   db.execute("SELECT observed_utc, parse_status FROM snapshot"))
    first_local = snaps[0][0].astimezone(BUCHAREST).date()
    data_through = snaps[-1][0].astimezone(BUCHAREST).date()
    years = list(range(first_local.year, data_through.year + 1))
    partial = set()
    if first_local != date(first_local.year, 1, 1):
        partial.add(first_local.year)
    if data_through != date(data_through.year, 12, 31):
        partial.add(data_through.year)
    complete = [y for y in years if y not in partial]
    lcy = max(complete) if complete else years[-1]

    snap_dates: dict[int, set[date]] = defaultdict(set)
    ok_counts: Counter = Counter()
    gap_max: dict[int, float] = defaultdict(float)
    for t, status in snaps:
        d = t.astimezone(BUCHAREST).date()
        snap_dates[d.year].add(d)
        if status == "ok":
            ok_counts[d.year] += 1
    for (a, _), (b, _) in zip(snaps, snaps[1:]):
        y = b.astimezone(BUCHAREST).year
        gap_max[y] = max(gap_max[y], (b - a).total_seconds() / 3600)
    coverage = {}
    for y in years:
        d = max(date(y, 1, 1), first_local)
        end = min(date(y, 12, 31), data_through)
        missing = 0
        while d <= end:
            if d not in snap_dates[y]:
                missing += 1
            d += timedelta(days=1)
        coverage[str(y)] = {"snapshots": ok_counts[y], "missing_days": missing,
                            "gap_hours_max": round(gap_max[y], 1)}

    # --- episode aggregation (ACC only) -------------------------------------
    pt_days: dict[tuple, set[date]] = defaultdict(set)        # (pt, y) oprire union
    pt_cls: dict[tuple, set[date]] = defaultdict(set)         # (pt, y, cls)
    pt_eps: dict[tuple, list[dict]] = defaultdict(list)       # (pt, y) display episodes
    pt_epn: Counter = Counter()                               # (pt, y) oprire eps touching
    pt_hours: dict[tuple, float] = defaultdict(float)         # (pt, y) split est_hours
    pt_blocks: dict[str, int] = {}
    sector_votes: dict[str, Counter] = defaultdict(Counter)
    city_eps: Counter = Counter()                             # start-year attributed
    city_cls: dict[int, Counter] = defaultdict(Counter)
    sector_year_eps: Counter = Counter()                      # (sector, start-year)
    ep_info: dict[int, tuple] = {}                            # id -> per-episode cache

    for (eid, pt0, sector, sev, cls, craw, rem, blocks, first, last,
         ended, gap, est_h) in db.execute(
            """SELECT id, pt_norm, sector, severity, cause_class, cause_raw,
                      remediere_last, blocks_count, first_seen, last_seen,
                      ended_before, gap_spanned, est_hours
               FROM episode WHERE service='ACC' ORDER BY id"""):
        pt = canon(pt0)
        days = list(episode_days(first, last))
        ycount = Counter(d.year for d in days)
        est_h = est_h or 0.0
        ep_info[eid] = (sev, cls, days, ycount, est_h, sector)
        if sector:
            sector_votes[pt][sector] += 1
        if blocks:
            pt_blocks[pt] = max(pt_blocks.get(pt, 0), blocks)
        if sev == "deficienta":
            for d in days:
                pt_cls[(pt, d.year, "deficienta")].add(d)
            continue
        y0 = local(first).year
        city_eps[y0] += 1
        city_cls[y0][cls] += 1
        if sector:
            sector_year_eps[(sector, y0)] += 1
        for d in days:
            pt_days[(pt, d.year)].add(d)
            pt_cls[(pt, d.year, cls)].add(d)
        epd = {"start": fmt_minute(first), "end": fmt_minute(last),
               "ongoing": ended is None, "uncertain": bool(gap),
               "cause_class": cls, "cause_raw": craw,
               "remediere_last": _rem_iso(rem)}
        for y, c in ycount.items():
            pt_eps[(pt, y)].append(epd)
            pt_epn[(pt, y)] += 1
            pt_hours[(pt, y)] += est_h * (c / len(days))

    # --- street aggregation (via the episode join) ---------------------------
    st_days: dict[tuple, set[date]] = defaultdict(set)
    st_cls: dict[tuple, set[date]] = defaultdict(set)
    st_epn: Counter = Counter()
    st_hours: dict[tuple, float] = defaultdict(float)
    st_sectors: dict[tuple, set[int]] = defaultdict(set)
    street_keys: set[tuple] = set()
    for eid, stype, snorm in db.execute(
            """SELECT es.episode_id, es.street_type, es.street_norm
               FROM episode_street es JOIN episode e ON e.id = es.episode_id
               WHERE e.service='ACC' ORDER BY es.episode_id"""):
        sev, cls, days, ycount, est_h, sector = ep_info[eid]
        key = (stype, snorm)
        street_keys.add(key)
        if sector:
            st_sectors[key].add(sector)
        if sev == "deficienta":
            for d in days:
                st_cls[(key, d.year, "deficienta")].add(d)
            continue
        for d in days:
            st_days[(key, d.year)].add(d)
            st_cls[(key, d.year, cls)].add(d)
        for y, c in ycount.items():
            st_epn[(key, y)] += 1
            st_hours[(key, y)] += est_h * (c / len(days))

    # --- OSM street universe: every named Bucharest street, so ANY street is
    # searchable even when CMTEB never named it (publish infers a serving PT
    # for those below). OSM names normalize to the same (norm,type) as the
    # parser, so a street CMTEB also named merges to one slug, not a duplicate.
    osm_coord: dict[tuple, tuple[float, float]] = {}
    osm_path = Path(static_dir) / "streets.json"
    if osm_path.exists():
        for s in json.loads(osm_path.read_text(encoding="utf-8")):
            key = (s["type"], s["norm"])
            osm_coord[key] = (s["lat"], s["lon"])
            if s.get("sector"):
                st_sectors[key].add(s["sector"])
    else:
        print("WARN: static/streets.json absent - only CMTEB-named streets searchable")
    osm_keys = set(osm_coord)

    # --- entities + slugs (PTs first: pt-* namespace has priority) ----------
    acc_pts = {canon(p) for (p,) in db.execute(
        "SELECT DISTINCT pt_norm FROM episode WHERE service='ACC'")}
    pts_all = sorted(universe | acc_pts)
    streets_all = sorted(street_keys | osm_keys)

    registry = SlugRegistry.load(registry_path)
    pt_slug = {pt: registry.ensure_pt(pt) for pt in pts_all}
    st_slug = {k: registry.ensure_street(k[0], k[1], sorted(st_sectors.get(k, ())))
               for k in streets_all}
    registry_changed = registry.save(registry_path)

    # street_pt links restricted to entities that exist in this build
    st_pts: dict[tuple, set[str]] = defaultdict(set)
    pt_sts: dict[str, set[tuple]] = defaultdict(set)
    pts_set = set(pts_all)
    for snorm, stype, pt0 in db.execute(
            "SELECT street_norm, street_type, pt_norm FROM street_pt"):
        key, pt = (stype, snorm), canon(pt0)
        if key in street_keys and pt in pts_set:
            st_pts[key].add(pt)
            pt_sts[pt].add(key)

    # block -> serving-PT index (powers the site's "find your block" finder).
    # Per street, per block label, count which PT listed it; resolve to the most
    # frequent PT (ties -> smallest slug, for deterministic output).
    st_block_pt: dict[tuple, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for stype, snorm, pt0, blk in db.execute(
            """SELECT es.street_type, es.street_norm, e.pt_norm, es.blocks_raw
               FROM episode_street es JOIN episode e ON e.id = es.episode_id
               WHERE e.service='ACC' AND es.blocks_raw IS NOT NULL AND es.blocks_raw <> ''"""):
        key, pt = (stype, snorm), canon(pt0)
        if key not in street_keys or pt not in pts_set:
            continue
        for label in tokenize_blocks(blk):
            st_block_pt[key][label][pt] += 1

    # --- PT metadata ----------------------------------------------------------
    static = Path(static_dir)
    sect_geo = static / "sectoare.geojson"
    polys = load_sector_polygons(sect_geo) if sect_geo.exists() else None
    if polys is None:
        print("WARN: static/sectoare.geojson absent - skipping client/sectoare.geojson, "
              "sector assignment is episode-derived only")

    pt_name: dict[str, str] = {}
    pt_coord: dict[str, tuple[float, float]] = {}
    pt_sector: dict[str, int] = {}
    for pt in pts_all:
        r = reg_rows.get(pt)
        pt_name[pt] = r[0] if r and r[0] else pt.title()
        if r and r[1] is not None and r[2] is not None:
            pt_coord[pt] = (r[1], r[2])
        votes = sector_votes.get(pt)
        if votes:
            pt_sector[pt] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        elif polys and pt in pt_coord:
            s = sector_of_point(polys, *pt_coord[pt])
            if s:
                pt_sector[pt] = s
    nearest = nearest_map({pt_slug[pt]: c for pt, c in pt_coord.items()})

    # --- anchored inference: for a street CMTEB never named, guess its serving
    # PT as the one whose ACTUALLY-NAMED streets (or own location) are closest.
    # Anchoring on named streets beats raw nearest-PT-point (district-heating
    # topology is not nearest-neighbor). Result is shown as an ESTIMATE on-site.
    CELL = 0.012  # ~1.3 km grid cell
    anchor_grid: dict[tuple, list[tuple]] = defaultdict(list)
    for pt in pts_all:
        pts_anchors = [osm_coord[k] for k in pt_sts.get(pt, ()) if k in osm_coord]
        if pt in pt_coord:
            pts_anchors.append(pt_coord[pt])
        for (alat, alon) in pts_anchors:
            anchor_grid[(int(alat / CELL), int(alon / CELL))].append((alat, alon, pt))

    def nearest_anchor(lat: float, lon: float):
        best, found_r = None, None
        ci, cj = int(lat / CELL), int(lon / CELL)
        r = 0
        while r <= 60:
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    for (alat, alon, pt) in anchor_grid.get((ci + di, cj + dj), ()):
                        d = haversine_km((lat, lon), (alat, alon))
                        if best is None or d < best[1]:
                            best, found_r = (pt, d), found_r if found_r is not None else r
            if found_r is not None and r >= found_r + 1:
                break
            r += 1
        return best

    inferred: dict[tuple, tuple] = {}  # street key -> (pt_norm, km)
    for k in osm_keys - street_keys:
        res = nearest_anchor(*osm_coord[k])
        if res:
            inferred[k] = res

    st_days_lcy = {k: len(st_days.get((k, lcy), ())) for k in streets_all}
    neighbors: dict[tuple, list[str]] = {}
    for k in streets_all:
        shared: Counter = Counter()
        for pt in st_pts.get(k, ()):
            for o in pt_sts.get(pt, ()):
                if o != k:
                    shared[o] += 1
        ranked = sorted(shared.items(),
                        key=lambda kv: (-kv[1], -st_days_lcy.get(kv[0], 0), st_slug[kv[0]]))
        neighbors[k] = [st_slug[o] for o, _ in ranked[:8]]

    def _delta(days_map: dict, ent, y: int) -> int | None:
        """Complete year: days(Y) - days(Y-1). Partial year: YTD-vs-YTD via a
        (month, day) cutoff - not doy, so leap years align. Null when the
        prior year is partial/absent or the entity had no prior(-YTD) days."""
        if (y - 1) not in years or (y - 1) in partial:
            return None
        cur = days_map.get((ent, y), set())
        prev = days_map.get((ent, y - 1), set())
        if y in partial:
            cut = (data_through.month, data_through.day)
            cur = {d for d in cur if (d.month, d.day) <= cut}
            prev = {d for d in prev if (d.month, d.day) <= cut}
        if not prev:
            return None
        return len(cur) - len(prev)

    def _runs(cls_map: dict, ent, y: int) -> list[list]:
        runs: list[list] = []
        for cls in CLASSES:
            runs += runs_from_days(year_doys(cls_map.get((ent, y, cls), set()), y), cls)
        runs.sort(key=lambda r: (r[0], r[2]))
        return runs

    def _longest(days_map: dict, ent, y: int) -> int:
        rs = runs_from_days(year_doys(days_map.get((ent, y), set()), y), "u")
        return max((r[1] for r in rs), default=0)

    # --- output ---------------------------------------------------------------
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()) and not (out / "meta.json").exists():
        raise SystemExit(f"refusing to wipe {out}: non-empty and no meta.json")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    write_json(out / "meta.json", {
        "generated_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_through": data_through.isoformat(),
        "years": years,
        "last_complete_year": lcy,
        "partial_years": sorted(partial),
        "universe_size": len(universe),
        "coverage": coverage,
        "sources_cutover_utc": CUTOVER_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    universe_sorted = sorted(universe)
    summary = []
    for y in years:
        full = [len(pt_days.get((pt, y), ())) for pt in universe_sorted]
        sf = sorted(full)
        monthly = [0] * 12
        for (pt, yy), ds in pt_days.items():
            if yy == y:
                for d in ds:
                    monthly[d.month - 1] += 1
        summary.append({
            "year": y, "partial": y in partial,
            "median_pt_days": pctl(sf, 50),
            "mean_pt_days": round(sum(full) / len(full), 1),
            "p90_pt_days": pctl(sf, 90),
            "pts_hit": sum(1 for pt in pts_all if pt_days.get((pt, y))),
            "share_universe_hit_pct": round(100 * sum(1 for v in full if v) / len(full), 1),
            "episodes": city_eps[y],
            "episodes_avarie": city_cls[y]["avarie"],
            "episodes_programat": city_cls[y]["programat"],
            "monthly_pt_days": monthly,
        })
        hist = [[5 * i, 0] for i in range(sf[-1] // 5 + 1)]
        for v in full:
            hist[v // 5][1] += 1
        write_json(out / "city" / f"distribution-{y}.json", {
            "year": y,
            "percentiles": {f"p{p}": pctl(sf, p) for p in (10, 25, 50, 75, 90, 99)},
            "histogram": hist,
        })
    write_json(out / "city" / "summary.json", summary)

    for y in years:
        rows = []
        for pt in pts_all:
            ds = pt_days.get((pt, y))
            if not ds:
                continue
            rows.append({
                "slug": pt_slug[pt], "name": pt_name[pt], "sector": pt_sector.get(pt),
                "days": len(ds),
                "days_avarie": len(pt_cls.get((pt, y, "avarie"), ())),
                "days_programat": len(pt_cls.get((pt, y, "programat"), ())),
                "days_deficienta": len(pt_cls.get((pt, y, "deficienta"), ())),
                "episodes": pt_epn[(pt, y)],
                "longest_days": _longest(pt_days, pt, y),
                "est_day_eq": round(pt_hours[(pt, y)] / 24, 1),
                "delta_prev": _delta(pt_days, pt, y),
            })
        rows.sort(key=lambda r: (-r["days"], r["slug"]))
        write_json(out / "rankings" / f"pt-{y}.json", rows)

        srows = []
        for k in streets_all:
            ds = st_days.get((k, y))
            if not ds:
                continue
            srows.append({
                "slug": st_slug[k], "name": street_name(*k),
                "sectors": sorted(st_sectors.get(k, ())),
                "pt_slugs": sorted(pt_slug[p] for p in st_pts.get(k, ())),
                "days": len(ds),
                "days_avarie": len(st_cls.get((k, y, "avarie"), ())),
                "days_programat": len(st_cls.get((k, y, "programat"), ())),
                "days_deficienta": len(st_cls.get((k, y, "deficienta"), ())),
                "episodes": st_epn[(k, y)],
                "longest_days": _longest(st_days, k, y),
                "est_day_eq": round(st_hours[(k, y)] / 24, 1),
                "delta_prev": _delta(st_days, k, y),
            })
        srows.sort(key=lambda r: (-r["days"], r["slug"]))
        write_json(out / "rankings" / f"strazi-{y}.json", srows)

        secrows = []
        for s in range(1, 7):
            spts = [pt for pt in universe_sorted if pt_sector.get(pt) == s]
            vals = sorted(len(pt_days.get((pt, y), ())) for pt in spts)
            n = len(spts) or 1
            secrows.append({
                "sector": s, "pts": len(spts),
                "median_days": pctl(vals, 50),
                "mean_days": round(sum(vals) / n, 1),
                "mean_days_avarie": round(
                    sum(len(pt_cls.get((pt, y, "avarie"), ())) for pt in spts) / n, 1),
                "mean_days_programat": round(
                    sum(len(pt_cls.get((pt, y, "programat"), ())) for pt in spts) / n, 1),
                "episodes": sector_year_eps[(s, y)],
            })
        write_json(out / "rankings" / f"sectoare-{y}.json", secrows)

        feats = []
        for pt in sorted(pt_coord, key=lambda p: pt_slug[p]):
            lat, lon = pt_coord[pt]
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point",
                             "coordinates": [round(lon, 5), round(lat, 5)]},
                "properties": {
                    "slug": pt_slug[pt], "name": pt_name[pt],
                    "sector": pt_sector.get(pt),
                    "days": len(pt_days.get((pt, y), ())),
                    "days_avarie": len(pt_cls.get((pt, y, "avarie"), ())),
                    "days_programat": len(pt_cls.get((pt, y, "programat"), ())),
                    "episodes": pt_epn[(pt, y)],
                }})
        write_json(out / "client" / "map" / f"pt-{y}.geojson",
                   {"type": "FeatureCollection", "features": feats})

    pt_lines = []
    for pt in sorted(pts_all, key=lambda p: pt_slug[p]):
        years_obj = {}
        for y in years:
            union = pt_days.get((pt, y), set())
            defi = pt_cls.get((pt, y, "deficienta"), set())
            if not union and not defi:
                continue
            eps = sorted(pt_eps.get((pt, y), ()),
                         key=lambda e: (e["start"], e["end"], e["cause_class"],
                                        e["cause_raw"] or ""))
            years_obj[str(y)] = {
                "days": len(union),
                "days_avarie": len(pt_cls.get((pt, y, "avarie"), ())),
                "days_programat": len(pt_cls.get((pt, y, "programat"), ())),
                "days_deficienta": len(defi),
                "episodes_count": pt_epn[(pt, y)],
                "longest_days": _longest(pt_days, pt, y),
                "est_hours": round(pt_hours[(pt, y)], 1),
                "runs": _runs(pt_cls, pt, y),
                "episodes": eps,
            }
        c = pt_coord.get(pt)
        pt_lines.append({
            "slug": pt_slug[pt], "name": pt_name[pt], "sector": pt_sector.get(pt),
            "lat": round(c[0], 5) if c else None,
            "lon": round(c[1], 5) if c else None,
            "on_map": c is not None,
            "blocks_estimate": pt_blocks.get(pt),
            "streets": [{"slug": st_slug[k], "name": street_name(*k)}
                        for k in sorted(pt_sts.get(pt, ()), key=lambda k: st_slug[k])],
            "nearest": nearest.get(pt_slug[pt], []),
            "years": years_obj,
        })
    write_ndjson_gz(out / "pt" / "all.ndjson.gz", pt_lines)

    st_lines = []
    for k in sorted(streets_all, key=lambda k: st_slug[k]):
        years_obj = {}
        for y in years:
            union = st_days.get((k, y), set())
            defi = st_cls.get((k, y, "deficienta"), set())
            if not union and not defi:
                continue
            years_obj[str(y)] = {
                "days": len(union),
                "days_avarie": len(st_cls.get((k, y, "avarie"), ())),
                "days_programat": len(st_cls.get((k, y, "programat"), ())),
                "runs": _runs(st_cls, k, y),
            }
        blocks = []
        for label in sorted(st_block_pt.get(k, {}), key=_block_natkey):
            cnt = st_block_pt[k][label]
            best = min(cnt, key=lambda p: (-cnt[p], pt_slug[p]))
            blocks.append({"label": label, "pt": pt_slug[best]})
        # OSM-only street (CMTEB never named it) -> attach the inferred PT.
        inf = inferred.get(k) if not years_obj else None
        st_lines.append({
            "slug": st_slug[k], "name": street_name(*k), "type": k[0],
            "sectors": sorted(st_sectors.get(k, ())),
            "pts": sorted(pt_slug[p] for p in st_pts.get(k, ())),
            "blocks": blocks,
            "neighbors": neighbors.get(k, []),
            "inferred_pt": pt_slug[inf[0]] if inf else None,
            "inferred_km": round(inf[1], 2) if inf else None,
            "years": years_obj,
        })
    write_ndjson_gz(out / "strazi" / "all.ndjson.gz", st_lines)

    search = []
    og = {}
    for pt in sorted(pts_all, key=lambda p: pt_slug[p]):
        d = len(pt_days.get((pt, lcy), ()))
        search.append({"t": "pt", "n": pt_name[pt], "s": pt_slug[pt],
                       "sec": pt_sector.get(pt), "d": d})
        og[pt_slug[pt]] = ["pt", pt_name[pt], pt_sector.get(pt), d, lcy]
    for k in sorted(streets_all, key=lambda k: st_slug[k]):
        secs = sorted(st_sectors.get(k, ()))
        sec = secs[0] if len(secs) == 1 else None
        # OSM-only streets show their inferred PT's days (estimate), not 0.
        d = st_days_lcy[k]
        if not d and k in inferred:
            d = len(pt_days.get((inferred[k][0], lcy), ()))
        search.append({"t": "st", "n": street_name(*k), "s": st_slug[k],
                       "sec": sec, "d": d})
        og[st_slug[k]] = ["st", street_name(*k), sec, d, lcy]
    write_json(out / "client" / "search-index.json", search)
    write_json(out / "og" / "stats.json", dict(sorted(og.items())))

    if sect_geo.exists():
        (out / "client").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sect_geo, out / "client" / "sectoare.geojson")

    files = [p for p in out.rglob("*") if p.is_file()]
    counters = {
        "pts": len(pts_all), "standalone_pts": len(acc_pts - set(reg_rows)),
        "streets": len(streets_all), "years": len(years),
        "registry_changed": registry_changed,
        "sector_unassigned_universe": sum(1 for pt in universe if pt not in pt_sector),
        "files": len(files), "bytes": sum(p.stat().st_size for p in files),
    }
    return counters


def main(db_path: str, registry_path: str, harta_html: str,
         static_dir: str, out_dir: str):
    counters = build(db_path, registry_path, harta_html, static_dir, out_dir)
    print("publish done: " + ", ".join(f"{k}={v}" for k, v in counters.items()))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
