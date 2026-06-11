# Artifact contract (v1)

The single contract between the data pipeline (this repo) and termo-site.
`pipeline/publish.py` emits exactly these files into `web/`; the site consumes
nothing else. All keys are slugs from the append-only registry
(`registry/slugs.json` - committed, never reassigned). All "days" are
Bucharest-local calendar days. Headline day counts = days touched by >=1
episode with severity `oprire`, service `ACC`. `deficienta` ACC days are the
separate secondary counter. Heating (INC) is excluded from v1 artifacts.

Slug rules: NFKD diacritic fold, lowercase, ASCII, non-alnum -> hyphen,
collapse hyphens. PT slug = `pt-<norm>`; street slug = `<type>-<norm>` plus
`-sector-<n>` suffix only on collision. Once in registry/slugs.json, a slug
never changes or gets reassigned.

Years: `meta.json.years` ascending; `last_complete_year` = latest year fully
covered (2025 until 2026-12-31). Partial years carry `partial: true` and
`data_through`.

## Files

### meta.json
```json
{"generated_at": "2026-06-11T03:10:00Z", "data_through": "2026-06-11",
 "years": [2021, 2022, 2023, 2024, 2025, 2026], "last_complete_year": 2025,
 "partial_years": [2021, 2026], "universe_size": 947,
 "coverage": {"2023": {"snapshots": 5622, "missing_days": 11, "gap_hours_max": 251.0}, "...": {}},
 "sources_cutover_utc": "2026-06-10T19:34:00Z"}
```

### city/summary.json
Array, one object per year:
```json
{"year": 2025, "partial": false, "median_pt_days": 22, "mean_pt_days": 27.3,
 "p90_pt_days": 61, "pts_hit": 905, "share_universe_hit_pct": 91.8,
 "episodes": 11325, "episodes_avarie": 7881, "episodes_programat": 3114,
 "monthly_pt_days": [3120, 2410, ...]}
```
`monthly_pt_days[m]` = count of distinct (PT, day) pairs in month m+1.

### city/distribution-{year}.json
```json
{"year": 2025, "percentiles": {"p10": 2, "p25": 8, "p50": 22, "p75": 38, "p90": 61, "p99": 102},
 "histogram": [[0, 42], [5, 118], [10, 96], "..."]}
```
Histogram buckets of 5 days, `[bucket_start, pt_count]`, zeros included.

### rankings/pt-{year}.json
Array sorted by `days` desc, one row per PT with >= 1 oprire-ACC day that year:
```json
{"slug": "pt-modul-toporasi", "name": "Modul Toporasi", "sector": 5, "days": 179,
 "days_avarie": 1, "days_programat": 176, "days_deficienta": 4,
 "episodes": 5, "longest_days": 142, "est_day_eq": 176.1, "delta_prev": -31}
```
`delta_prev` null when prior year partial or PT absent. `days_avarie` +
`days_programat` + unclassified-days may exceed `days` (a day can carry both
classes); `days` is the union.

### rankings/strazi-{year}.json
Same shape minus `sector` (streets can span sectors), plus `"sectors": [4],
"pt_slugs": ["pt-x", "..."]`. Sorted by days desc.

### rankings/sectoare-{year}.json
6 rows: `{"sector": 1, "pts": 142, "median_days": 19, "mean_days": 24.2,
"mean_days_avarie": 13.1, "mean_days_programat": 9.4, "episodes": 1820}`
(means over the sector's universe PTs, zeros included).

### pt/all.ndjson.gz
One JSON object per line per PT ever seen (universe + standalone entities):
```json
{"slug": "pt-modul-toporasi", "name": "Modul Toporasi", "sector": 5,
 "lat": 44.4, "lon": 26.05, "on_map": true, "blocks_estimate": 12,
 "streets": [{"slug": "str-toporasi", "name": "Str Toporasi"}],
 "nearest": ["pt-a", "pt-b", "pt-c", "pt-d", "pt-e"],
 "years": {"2025": {"days": 179, "days_avarie": 1, "days_programat": 176,
   "days_deficienta": 4, "episodes_count": 5, "longest_days": 142,
   "est_hours": 4226.4,
   "runs": [[114, 142, "programat"], [274, 12, "programat"]],
   "episodes": [{"start": "2025-04-24T07:00", "end": "2025-10-16T23:00",
     "ongoing": false, "uncertain": true, "cause_class": "programat",
     "cause_raw": "Revizie tehnica CTE Progresu",
     "remediere_last": "2025-10-15T23:00"}]}}}
```
`runs` = [start_day_of_year (1-based, local), length_days, cause_class],
episodes clipped to the year; ongoing = ended_before null; uncertain =
gap_spanned. Timestamps local Bucharest, minute precision.

### strazi/all.ndjson.gz
```json
{"slug": "sos-pantelimon", "name": "Sos Pantelimon", "type": "sos",
 "sectors": [2, 3], "pts": ["pt-7-pantelimon", "..."],
 "neighbors": ["str-x", "str-y"],
 "years": {"2025": {"days": 181, "days_avarie": 121, "days_programat": 74,
   "runs": [[10, 3, "avarie"], "..."]}}}
```
Street days = union of days of episodes that listed the street.
`neighbors` = up to 8 streets sharing a PT.

### client/search-index.json
`[{"t": "pt"|"st", "n": "Soseaua Oltenitei", "s": "sos-oltenitei", "sec": 4, "d": 23}]`
`d` = days in last_complete_year. Diacritics preserved in `n` for display;
search normalizes client-side.

### client/map/pt-{year}.geojson
FeatureCollection of Points; properties `{slug, name, sector, days,
days_avarie, days_programat, episodes}`. Only PTs with coords (`on_map`).

### client/sectoare.geojson
Static sector boundary polygons (OSM-derived, ODbL attribution in
/metodologie). Checked into the repo under `static/`, copied into the bundle.

### og/stats.json
`{"<slug>": ["pt"|"st", "<name>", <sector|null>, <days_last_complete>, <year>]}`

## Packaging + flow

Nightly GitHub Action (03:10 UTC):
1. checkout this repo (full history - own scraper snapshots live here),
   clone archive + prometeu repos
2. backfill (archive before `sources_cutover_utc`, own data/ after) ->
   identity -> publish into `web/`
3. validate invariants; fail loudly on breach
4. `tar -czf bundle.tar.gz -C web .`; upload as rolling release asset
   `data-latest` (`gh release upload --clobber`)
5. POST $VERCEL_DEPLOY_HOOK (repo secret; step skipped if unset)

Site build downloads
`https://github.com/tiXor-code/termo-data/releases/download/data-latest/bundle.tar.gz`
(override with `DATA_BUNDLE_PATH` env for local dev), extracts to `.data/`,
serves `client/*` files from `public/data/` with content hashing.
