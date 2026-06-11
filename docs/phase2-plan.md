# Phase 2 implementation plan — termo-data (aggregates + contract)

Verified against the live repo state: db/termo.db has 160,442 episodes / 26,217 snapshots (2021-12-19T21:17+02:00 → 2026-06-10T19:59Z, mixed `+02:00/+03:00` and `Z` ISO offsets), pt_registry 976 rows (all with coords), pt_alias 6 auto + 10 pending, 1,070 distinct episode pt_norms (so ~90 standalone after aliasing), street_pt has 2,978 (street,type,pt) links across 1,244 streets with street_type values `str|bld|cal|spl|drm|""` (593 links have empty type). Unclassified is 46% over ALL episodes but only 3-6%/yr within the headline scope (oprire+ACC) — thresholds must be scoped. Own-repo snapshot file is `data/functionare.html` (archive uses `data/termoficare.html`); first own commit `5de8e8d snapshot 2026-06-10T19:34:39Z`. `registry/`, `static/`, `tests/`, `pipeline/publish.py`, `pipeline/validate.py`, nightly.yml do not exist yet.

Ordered tasks:

---

## Task 1 — `pipeline/slugs.py` + `registry/slugs.json`

Committed registry format (identity → slug, two namespaces, append-only):

```json
{"version": 1,
 "pt":     {"modul toporasi": "pt-modul-toporasi"},
 "street": {"sos|pantelimon": "sos-pantelimon", "|13 septembrie": "13-septembrie"}}
```

Street identity key = `f"{street_type}|{street_norm}"` (matches db PK on episode_street/street_pt; sector is NOT part of identity — streets span sectors, per the strazi artifact shape).

```python
def slugify(text: str) -> str
    # NFKD -> drop combining marks -> lower -> re.sub(r"[^a-z0-9]+","-") -> strip("-")
def pt_slug_base(pt_norm: str) -> str          # "pt-" + slugify(pt_norm)
def street_slug_base(stype: str, snorm: str)   # slugify(f"{stype} {snorm}")  ("" type -> no prefix)

class SlugRegistry:
    @classmethod
    def load(cls, path: Path) -> "SlugRegistry"   # missing file -> empty v1; asserts no slug duplicated across BOTH namespaces
    def ensure_pt(self, pt_norm: str) -> str
    def ensure_street(self, stype: str, snorm: str, sectors: list[int]) -> str
    def save(self, path: Path) -> bool            # writes only if dirty; sorted keys, indent=1, ascii; returns changed
```

- `ensure_*`: if identity already registered, return stored slug untouched (never recompute — this is what makes old slugs survive future slugify-rule changes). Otherwise compute base; on collision with an existing slug owned by a *different* identity: streets try `f"{base}-sector-{min(sectors)}"` (per ARTIFACTS), then `-2`, `-3`...; PTs go straight to `-2`, `-3`.
- Determinism requirement documented in the docstring: callers must register identities in sorted order (publish.py does `for k in sorted(...)`), so first-registration tie-breaks are stable.
- Mutating/deleting an existing entry is impossible by API; `load` raises if the same slug maps from two identities (corruption guard).

Gotcha: empty street_type yields slugs like `13-septembrie` with `"type": ""` in strazi ndjson — the contract allows it (site must tolerate); guaranteed never to collide with `pt-*` namespace because observed types are str/bld/cal/spl/drm/"" — but validate.py still checks pt∩street slug sets = ∅.

---

## Task 2 — `pipeline/backfill.py` dual-source ingest

Cutover constant: `CUTOVER_UTC = datetime(2026, 6, 10, 19, 34, tzinfo=timezone.utc)` (== ARTIFACTS `sources_cutover_utc`).

```python
FILE_ARCHIVE = "data/termoficare.html"   # FlorinPopaCodes repo
FILE_OWN     = "data/functionare.html"   # this repo

def iter_blobs(repo: Path, file_path: str)            # existing iter_blobs, parametrized file path
def iter_dual(archive: Path, own: Path):
    last = None
    for sha, ts, blob in iter_blobs(archive, FILE_ARCHIVE):
        if datetime.fromisoformat(ts) >= CUTOVER_UTC: break        # archive keeps scraping; drop everything >= T
        last = datetime.fromisoformat(ts); yield sha, ts, blob
    for sha, ts, blob in iter_blobs(own, FILE_OWN):
        t = datetime.fromisoformat(ts)
        if t < CUTOVER_UTC: continue                                # pre-cutover own commits (none expected)
        assert last is None or t >= last, f"seam not monotonic: {t} < {last}"
        last = t; yield sha, ts, blob

def main(archive: str, own_repo: str, db_path: str)    # CLI: uv run python -m pipeline.backfill archive . db/termo.db
```

- One `EpisodeMachine` across the seam; `prev_hash` carries over so an unchanged page at the boundary doesn't churn episodes; seam gap is ~minutes so no spurious gap_spanned.
- `git log ... -- data/functionare.html` automatically skips keepalive commits (they only touch `data/meta.json`).
- Timestamps: archive `%aI` is `+02:00/+03:00`, own is `Z`/`+00:00`; compare as aware datetimes only (`datetime.fromisoformat` handles both on 3.14). Never compare strings.
- Nightly is a full rebuild: `rm -f db/termo.db` first — idempotent, no incremental-state bugs; full pass is well under the 15-min budget today.
- Own checkout must be `fetch-depth: 0` in CI or `git log` sees one commit.

---

## Task 3 — `static/sectoare.geojson` (one-time asset + fetch script)

- **Primary source**: OSM Overpass — Bucharest sectors are `admin_level=9` relations inside `Municipiul București` (`admin_level=4`):
  ```
  [out:json][timeout:120];
  area["name"="București"]["admin_level"="4"]->.buc;
  relation(area.buc)["boundary"="administrative"]["admin_level"="9"];
  out geom;
  ```
  One-off `scripts/fetch_sectoare.py` (dev-only dep `osm2geojson`, NOT added to project deps — run manually, commit output). Post-process: keep `{"sector": <1-6 parsed from name>, "name": "Sectorul N"}` properties, round coords to 5 dp, target <200 KB (if larger, `npx mapshaper -simplify 15% keep-shapes`). Commit to `static/sectoare.geojson`. ODbL attribution line goes in README now and /metodologie in Phase 3.
- **Secondary**: geo-spatial.org "limite administrative" shapefiles → `ogr2ogr -f GeoJSON`.
- **Fallback (must work)**: if the file is absent, `publish.py` logs a WARN, skips copying `client/sectoare.geojson`, and sector assignment for zero-episode universe PTs falls back to episode-derived only (see Task 4); `validate.py` treats absence as WARN, not FAIL. Site contract: tolerate missing file (no sector overlay / silhouette).

Bonus use: with the polygons present, publish assigns sectors to universe PTs that never had an outage (point-in-polygon), which makes the sectoare universe denominators honest.

---

## Task 4 — `pipeline/publish.py` (the big one)

CLI: `uv run python -m pipeline.publish db/termo.db registry/slugs.json data/harta.html static web`

```python
def build(db_path: str, registry_path: str, harta_html: str,
          static_dir: str, out_dir: str, now: datetime | None = None) -> dict  # returns counters for logging
```

### Load phase (everything in RAM; ~86k ACC episodes, trivially fits)

1. `alias = dict(db.execute("SELECT alias_norm, pt_norm FROM pt_alias WHERE status IN ('auto','approved')"))`; `canon(pt) = alias.get(pt, pt)`.
2. Universe: `pt_universe(harta_html)` from pipeline.metrics (947 norms, JSON-decoded — the past blocker). Registry rows: `SELECT pt_norm, display_name, lat, lon FROM pt_registry` (976, all coords today; still guard None).
3. Episodes (ACC only — INC excluded from v1):
   ```sql
   SELECT id, pt_norm, sector, severity, cause_class, cause_raw, remediere_last,
          blocks_count, first_seen, last_seen, started_after, ended_before,
          gap_spanned, est_hours
   FROM episode WHERE service='ACC'
   ```
4. `episode_street` join (ACC only, both severities) and full `street_pt`.

### Core aggregation

Day expansion (reuse/move `episode_days` from metrics.py): convert `first_seen`/`last_seen` → `astimezone(BUCHAREST)` → iterate **dates** (never +24h steps; DST-safe).

Per PT (canonical norm), per year, build:
- `days_union: set[date]` — oprire episodes only (headline)
- `days_by_class[cls]: set[date]` — cls = episode.cause_class for oprire; plus pseudo-class `"deficienta"` = day set of severity='deficienta' episodes. So run/strip classes are `avarie | programat | unclassified | deficienta`; `days` = union of the three oprire classes only; `days_deficienta` separate (a day can be in several classes; documented in ARTIFACTS).
- `episodes[]` (oprire only, attributed to every year they touch, fields clipped for display: `start`/`end` = local `YYYY-MM-DDTHH:MM` minute precision from first_seen/last_seen, `ongoing = ended_before is None`, `uncertain = bool(gap_spanned)`, `cause_class`, `cause_raw`, `remediere_last`).
- `est_hours` per year: split each episode's est_hours across years **proportional to its calendar-day count per year** (year-spanning revisions would otherwise dump 4,000h into the start year). `episodes_count` per year = episodes touching the year. City-level `episodes`/`episodes_avarie`/`episodes_programat` counts stay **start-year attributed** (must reproduce phase1: 2025 = 11,325 / 7,881 / 3,114 — regression-tested).
- `blocks_estimate` = max(blocks_count) over all its episodes, else null.

```python
def runs_from_days(doys: set[int], cls: str) -> list[list]   # sorted, compress consecutive doys -> [doy_start(1-based), length, cls]
def year_doys(days: set[date], year: int) -> set[int]        # d.timetuple().tm_yday for d.year == year
```
`runs` per (pt, year) = concatenation over the 4 classes of `runs_from_days(year_doys(days_by_class[cls]))`, sorted by doy. Day-set compression (not raw episode clipping) automatically merges overlapping same-class episodes and splits year-spanning ones at Dec 31/Jan 1. `longest_days` = max run length over `runs_from_days(year_doys(days_union))` (class-merged union).

**delta_prev**: complete year Y → `days(Y) - days(Y-1)`, null if Y-1 is partial (so all 2022 rows null) or PT had 0 days in Y-1. Partial year Y (2026) → YTD-vs-YTD: clip BOTH years' day sets to `(month, day) <= (data_through.month, data_through.day)` — month/day cutoff, not doy, so leap years align.

**nearest-5**: brute-force haversine over all registry PTs with coords (976² ≈ 950k pairs, <1 s):
```python
def haversine_km(a: tuple[float,float], b: tuple[float,float]) -> float
def nearest_map(coords: dict[str, tuple]) -> dict[str, list[str]]   # 5 nearest slugs, excluding self
```
Standalone PTs (episode pt_norms not in registry after aliasing, ~90 — institutions): `lat/lon = null`, `on_map = false`, `nearest = []`, sector from episodes, name = title-cased pt_norm (no diacritics available). They appear in rankings and pt/all but are NOT in the universe denominators.

**Sector assignment** for universe PTs (needed for sectoare rollup denominators): precedence (1) majority `episode.sector` for that canonical pt_norm (page-A ground truth), (2) point-in-polygon against `static/sectoare.geojson` (hand-rolled ray casting, ~20 lines, no deps), (3) None → excluded from sector rollups, count reported.

**Streets**: entity = (street_type, street_norm) from episode_street. `sectors` = sorted distinct episode.sector over its episodes. `pts` = sorted canonical pt slugs from street_pt. Day sets/runs/classes identical algorithm via the episode join (days = union over episodes that listed the street). `neighbors` = up to 8 streets sharing ≥1 PT, ranked by (#shared PTs desc, days in last_complete_year desc), self excluded. Display name = `f"{TYPE_DISPLAY.get(stype, stype).capitalize()} {snorm.title()}".strip()` with `TYPE_DISPLAY = {"str":"Str","bld":"Bld","cal":"Calea","spl":"Splaiul","drm":"Drumul","sos":"Sos", ...}` — **street display names are ASCII-only** (norms were diacritic-folded at parse; the ARTIFACTS example "Sos Pantelimon" is ASCII, consistent). PT display names keep diacritics via pt_registry.display_name.

**meta.json**: `data_through` = local date of MAX(observed_utc); `last_complete_year` = `data_through.year - 1` if data covers Dec 31 of it (it does) — i.e. 2025 until 2026-12-31; `partial_years = [2021, data_through.year]`; coverage per year from snapshot table: `snapshots` = COUNT(parse_status='ok'), `missing_days` = calendar days inside `[max(Jan1, first_snapshot_day), min(Dec31, data_through)]` with zero snapshots, `gap_hours_max` = max consecutive-snapshot gap within the year, 1 dp. `sources_cutover_utc` = the Task-2 constant.

**city/summary.json**: per year over **universe with zeros** (947): `median_pt_days` (int), `mean_pt_days` (1 dp), `p90_pt_days`, `pts_hit` = universe PTs with days>0 (2025 must equal 905), `share_universe_hit_pct` (91.8), episodes triplet (start-year attributed), `monthly_pt_days` = 12 ints, count of distinct (pt, day) in month m+1 across ALL pts incl. standalone, future months of a partial year = 0, `partial` flag.

**city/distribution-{year}.json**: percentiles p10/p25/p50/p75/p90/p99 over universe-with-zeros (nearest-rank); histogram = every 5-day bucket from 0 through `ceil(max/5)*5`, `[bucket_start, pt_count]`, zero-count buckets included, Σcounts == 947.

**rankings/**: pt-{year} rows only for PTs (universe + standalone) with ≥1 oprire day, sorted days desc then slug asc (stable). strazi-{year} same minus `sector`, plus `sectors`, `pt_slugs`. sectoare-{year}: 6 rows; `pts` = universe PTs assigned to the sector; median/mean/mean_avarie/mean_programat over those PTs **with zeros**; `episodes` = oprire-ACC episodes with that episode.sector and start-year = year.

**pt/all.ndjson.gz** / **strazi/all.ndjson.gz**: one line per entity ever seen (PT: universe ∪ standalone), shapes exactly per ARTIFACTS; `gzip.GzipFile(fileobj=..., mtime=0)` + `json.dumps(..., ensure_ascii=False, separators=(",",":"))` + sorted iteration for byte-reproducible output.

**client/search-index.json**: PT entries (n = display_name with diacritics, sec = sector|null) + street entries (n = ASCII display, sec = the sole sector if len(sectors)==1 else null); `d` = days in last_complete_year (0 if none).

**client/map/pt-{year}.geojson**: all `on_map` PTs incl. zero-day ones (`days:0, episodes:0` — lets the map show green), coords rounded 5 dp.

**og/stats.json**: every pt + street slug → `[type, name, sector|null, days_in_last_complete_year, last_complete_year]`.

Output handling: refuse to wipe `out_dir` unless it's empty or contains `meta.json` (rm-rf guard); register slugs in sorted identity order; `registry.save()` at the end; print summary counters (entities, files, bytes).

---

## Task 5 — `pipeline/validate.py`

CLI: `uv run python -m pipeline.validate db/termo.db web data/harta.html` → prints `PASS/WARN/FAIL <name>: detail` per check, `sys.exit(1)` if any FAIL.

1. **episode_overlap**: self-join per (pt_norm, service, severity) on interval overlap of [first_seen, last_seen] (create temp index first; verified 0 today). FAIL on any.
2. **episode_sanity**: first_seen <= last_seen; ended_before null or >= last_seen; sector in 1..6 or null (currently 0 nulls — FAIL if any null).
3. **snapshot_accounting**: every snapshot parse_status ∈ {ok, empty, failed}; every non-ok snapshot has a parse_failure row; failed share < 0.5% FAIL.
4. **coords_coverage**: ≥90% of universe PTs that have ≥1 episode (post-alias) resolve to registry coords. FAIL below.
5. **unclassified_share**: among **severity='oprire' AND service='ACC'** episodes (scope matters — global share is 46% because deficienta causes are unclassifiable), per last complete year and current year: ≥10% WARN, ≥20% FAIL. (Today: 3-6%.)
6. **sector_sums**: per year, Σ sectoare episodes == citywide episodes; recompute Σ per-sector (pt, day) totals == citywide total over assigned PTs; unassigned-sector universe PTs < 5% WARN.
7. **artifact_integrity** (reads web/): every file in ARTIFACTS exists for every year in meta.years (sectoare.geojson allowed missing → WARN); all JSON parses; every rankings slug exists in registry and in the matching ndjson; search-index slugs unique; pt-slug set ∩ street-slug set == ∅; og/stats covers all slugs; rankings sorted desc; histogram Σ == universe_size.
8. **registry_append_only**: diff `registry/slugs.json` against `git show HEAD:registry/slugs.json` — every pre-existing identity maps to the same slug, no slug re-used by a new identity. Skip silently if file untracked (first run).

---

## Task 6 — `.github/workflows/nightly.yml`

```yaml
name: Nightly publish
on: { schedule: [{cron: "10 3 * * *"}], workflow_dispatch: {} }
permissions: { contents: write }
concurrency: { group: nightly, cancel-in-progress: false }
jobs:
  publish:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v5
        with: { fetch-depth: 0 }                      # own snapshot history is required
      - run: git clone --single-branch https://github.com/FlorinPopaCodes/termoficare-data archive
      - run: git clone --single-branch https://github.com/gov2-ro/prometeu prometeu
      - uses: astral-sh/setup-uv@v5
      - run: rm -f db/termo.db && uv run python -m pipeline.backfill archive . db/termo.db
      - run: uv run python -m pipeline.identity db/termo.db prometeu data/harta.html
      - run: uv run python -m pipeline.publish db/termo.db registry/slugs.json data/harta.html static web
      - run: uv run python -m pipeline.validate db/termo.db web data/harta.html   # nonzero -> job fails, stale bundle stays live
      - name: Commit slug registry growth
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add registry/slugs.json
          git diff --cached --quiet || { git commit -m "registry: new slugs $(date -u +%F)";
            for i in 1 2 3; do git pull --rebase origin main && git push && break || sleep 20; done; }
      - run: tar -czf bundle.tar.gz -C web .
      - name: Upload rolling release
        env: { GH_TOKEN: "${{ github.token }}" }
        run: |
          gh release view data-latest >/dev/null 2>&1 || gh release create data-latest --title "data-latest" --notes "rolling nightly bundle"
          gh release upload data-latest bundle.tar.gz --clobber
      - name: Fire deploy hook
        if: ${{ vars.HAS_DEPLOY_HOOK == 'true' || secrets.VERCEL_DEPLOY_HOOK != '' }}
        env: { HOOK: "${{ secrets.VERCEL_DEPLOY_HOOK }}" }
        run: '[ -z "$HOOK" ] && echo "no hook set, skipping" || curl -fsS -X POST "$HOOK"'
```

Ordering is load-bearing: validate before registry commit, release upload, and deploy hook — a failed invariant leaves yesterday's `data-latest` and yesterday's site untouched. Note: `if:` cannot read secrets directly in all contexts; the in-script `[ -z "$HOOK" ]` guard is the real gate (drop the `if:` line if it fights you). Registry push races the 15-min scraper (different concurrency groups) — hence the rebase-retry loop, same pattern scrape.yml already uses.

---

## Task 7 — `tests/` (pytest; `uv add --dev pytest`)

- `tests/test_slugs.py`
  - slugify: "Șoseaua Olteniței" → "soseaua-oltenitei"; "B-dul 1 Mai" → "b-dul-1-mai"; punctuation runs collapse; no leading/trailing hyphens.
  - ensure_pt/ensure_street idempotent: second call returns same slug; save() not dirty; file bytes identical across reruns (stability).
  - street collision: two identities slugging to same base → second gets `-sector-<n>`; third (no distinct sector) gets `-2`.
  - load() raises on duplicate slug across identities; pre-existing entries survive a changed slugify rule (registered value wins).
- `tests/test_runs.py`
  - year-spanning episode (2025-12-28 → 2026-01-03): 2025 run `[362, 4, cls]`, 2026 run `[1, 3, cls]` (2025 not leap; assert via date arithmetic, not hardcoded doy guesses for leap years).
  - two overlapping same-class episodes → one merged run; gap between episodes → two runs.
  - DST boundary day (last Sunday of March) counted once; UTC `Z` timestamp at 22:30 UTC lands on the NEXT Bucharest day.
  - longest_days = max union-run length when avarie and programat runs interleave.
- `tests/test_dual_source.py` — build two tiny real git repos in `tmp_path` (`git init`; commits with `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` env): archive commits at T-2h, T-1h, T+1h; own at T+30s, T+2h. Assert `iter_dual` yields exactly [T-2h, T-1h, T+30s, T+2h] in order; post-cutover archive commit dropped; monotonic-seam assertion raises when own history starts before the last kept archive commit; keepalive commit touching only meta.json is invisible.
- `tests/test_publish_shapes.py` — `@pytest.mark.skipif(not Path("db/termo.db").exists())`; run `build()` into tmpdir against the real db and assert against frozen phase-1 truth: summary 2025 row `median_pt_days == 22, pts_hit == 905, episodes == 11325, episodes_avarie == 7881, episodes_programat == 3114`; meta universe_size == 947; rankings/pt-2025 sorted desc, top row slug `pt-modul-toporasi` with days 179; every ARTIFACTS key present per file; histogram Σ == 947; ndjson.gz lines all parse, every line has slug/name/years; delta_prev is null for all 2022 rows; 2026 rows carry partial YTD deltas; search-index entries match `{t,n,s,sec,d}`; pt/street slug sets disjoint.
- `tests/test_validate.py` — synthetic tiny db: inject overlapping episode pair → exit code 1; 25% unclassified oprire-ACC year → FAIL; 12% → WARN, exit 0; web dir missing a rankings file → FAIL.

---

## Gotchas (binding for implementers)

1. **Timestamps are mixed-offset ISO strings** (`+02:00`, `+03:00`, `Z`). Always `datetime.fromisoformat(...).astimezone(BUCHAREST)` before taking `.date()`; never sort/compare the raw strings across sources, never step days by `+timedelta(hours=24)`.
2. **Partial years**: 2021 (Dec 19+ only) and the current year. They never set `last_complete_year`, get `partial: true`, never serve as a delta baseline, and partial-year delta_prev is month/day-aligned YTD (leap-safe).
3. **Standalone PTs** (~90 post-alias): in rankings/pt-all/og/search, NOT in universe denominators, no coords, `on_map:false`, `nearest:[]`.
4. **deficienta is severity, not cause_class**: `days_deficienta` = days of `severity='deficienta' AND service='ACC'` episodes; never enters `days`; appears as pseudo-class `"deficienta"` in runs.
5. **Unclassified thresholds are scoped to oprire-ACC** — the global 46% figure is dominated by deficienta and would falsely fail the gate.
6. **Slug stability**: identity lookup precedes slug computation; the committed registry is law; nightly must commit registry growth after validate; validate diffs against `git HEAD` for append-only.
7. **Empty street_type** (593 links): slug has no type prefix, `"type": ""` in ndjson — keep, don't "fix".
8. **days_avarie + days_programat (+ unclassified) may exceed days** — union semantics, per contract; don't "normalize".
9. **est_hours**: split across years proportional to per-year day counts; city episode counts stay start-year attributed to reproduce the phase-1 validated numbers.
10. **Determinism**: sorted iteration everywhere, `gzip mtime=0`, compact separators, 5 dp coords, 1 dp means/shares — reruns over the same db must be byte-identical (test asserts it for the registry; cheap to assert for one artifact too).
11. **Nightly clones need blobs + full history** — no `--filter=blob:none`, no shallow checkout of self.
12. **sectoare.geojson is optional at runtime**: publish warns and skips, validate WARNs, sector assignment degrades to episode-derived; the site must already tolerate the missing file.

Files touched: `pipeline/slugs.py` (new), `pipeline/publish.py` (new), `pipeline/validate.py` (new), `pipeline/backfill.py` (modified: parametrized iter_blobs + iter_dual + 3-arg main), `scripts/fetch_sectoare.py` (new, dev-only), `static/sectoare.geojson` (new asset), `registry/slugs.json` (generated, committed), `.github/workflows/nightly.yml` (new), `tests/test_slugs.py`, `tests/test_runs.py`, `tests/test_dual_source.py`, `tests/test_publish_shapes.py`, `tests/test_validate.py` (all new), `pyproject.toml` (add dev dep pytest). Phase-2 checkpoint: run backfill+identity+publish+validate locally, `tar -tzf` the bundle, and hand `web/` to Teodor for inspection per the spec.