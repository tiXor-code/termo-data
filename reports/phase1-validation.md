# Phase 1 validation report - backfill, episodes, metrics

Date: 2026-06-10. Scope: 2021-12-19 -> 2026-06-10 reconstruction from the
FlorinPopaCodes/termoficare-data archive (26,217 snapshots of the CMTEB outage
table) + gov2-ro/prometeu (registry/coords) + own scraper (tiXor-code/termo-data,
live since 2026-06-10).

## Headline results (oprire ACC; calendar days touched; universe = 947 PTs on the official map)

| year | median days/PT | mean | PTs hit | % of universe hit | episodes | avarie | programat |
|---|---|---|---|---|---|---|---|
| 2022 | 23 | 26.5 | 964 | 90.5% | 12,356 | 9,036 | 2,685 |
| 2023 | 28 | 30.5 | 935 | 92.3% | 13,213 | 8,584 | 3,784 |
| 2024 | 25 | 28.0 | 949 | 93.8% | 13,365 | 9,154 | 3,704 |
| 2025 | 22 | 27.3 | 905 | 91.8% | 11,325 | 7,881 | 3,114 |

2021 (Dec only) and 2026 (through Jun 10) are partial years. Worst PT every
year: "Modul Toporasi" (S5) - 237/190/210/179 days, dominated by Apr-Oct
planned works ("Revizie tehnica CTE Progresu"). Worst streets: Toporasi,
Pantelimon, Mihai Bravu, Iuliu Maniu, Theodor Pallady (180-242 days/yr - union
across the 22-29 PTs serving each artery; framing must be "days with an ACC
stoppage affecting at least one address on the street").

## Provenance verification

- **Git dates**: author == committer on all 26,217 commits, perfectly monotonic
  2021-12-19 -> today, median gap 60 min, 15 gaps > 24h (worst 251h, Aug 2023).
  The Jan-2026 re-push preserved original timestamps.
- **Wayback audit** (independent source): 144 archived copies of the outage
  page; 134 comparable against the nearest archive snapshot within 6h. Mean
  record-set Jaccard 0.951; >= 0.925 in every era except the archive's sparse
  first weeks (2021-H2: 0.768). Result: the re-pushed history matches what the
  Internet Archive independently captured. Detail: db/wayback_audit.json.

## Adversarial verification (4 independent agents, none using pipeline code)

- **Re-derivation**: full independent census of all 5,527 snapshots of 2024
  reproduced day counts exactly for 2/3 sampled PTs (sincai 49/49, modul termic
  h3 25/25) and within +4 for modul toporasi (210 vs 206) - the 4 days are the
  only 2024 calendar days with zero archive snapshots, bridged by a flagged
  gap-spanning episode. Verdict: trustworthy.
- **Parser**: 6/6 era samples exact (incl. winter ACC/INC decomposition);
  525-blob scan found 0.044% delta, all verified upstream duplicate listings
  correctly collapsed; ST tab proven equal to union of sector tabs. Verdict:
  sound, no systematic loss.
- **Metrics semantics**: caught a BLOCKER (fixed): the PT universe was read
  with raw JSON escapes, corrupting 196/951 names into unmatchable entries and
  deflating medians ~30% (17/23/20/17 -> the correct 23/28/25/22). Timezone
  handling, year boundaries, median-with-zeros mechanics all verified correct.
- **Street metrics**: ground-truth reparse of every 2024 snapshot puts the
  aggregate street-day overcount at 0.24% (worst single street +6 days). The
  high artery numbers are the legitimate union-across-PTs effect, not churn.

## Fixes applied after verification

1. JSON-decoded PT universe/registry (the blocker above).
2. est_hours midpoint credit capped at 3h per side so the Aug-2023 251h archive
   gap cannot smear ~125h artifacts into durations; episodes first seen right
   after a gap now carry gap_spanned=1.
3. Empty-state pages ("Nu exista inregistrari" banner, 42 of the 46 former
   "failures") recorded as `empty`, distinct from the 4 genuine truncated-page
   failures. Both are skipped for episode continuity (treating transient
   upstream glitches as real zeros would spuriously churn hundreds of episodes).
4. Earlier inspection fixes: `blocuri/imobile` regex alternation (junk street
   "/imobile"), digit-guarded fuzzy aliasing (school nr.128 != nr.189),
   mentenanta/curatat/spalare -> programat, lipsa tensiune -> avarie.

## Known limitations (carry into /metodologie)

- **2023 coverage caveat**: the 251h Aug-2023 archive gap interpolates ~9 days
  for 303 PTs. Covered-days-only median for 2023 is 25 (vs 28); other years
  move by 0. 2023 stays the worst year either way, but the margin is partly
  coverage artifact - publish both numbers.
- **Concurrent same-key outages** (~11% of snapshots have at least one):
  episode counts/durations mildly understate concurrency; the days-touched
  headline metric is unaffected.
- **No published start times**: all durations are bounded estimates; days are
  counted on the observed window only.
- **PMB reconciliation**: order of magnitude consistent (our avarie
  PT-episodes run ~1.6-2x PMB's network-avarii counts of 4,412/4,941/5,626 for
  2022-24, as expected since one network avarie hits multiple PTs), but
  year-over-year direction differs in 2023 (we dip, PMB rises). Units are not
  directly comparable; needs a paragraph, not a forced match.
- **Identity backlog**: 10 pending fuzzy aliases for human review; 99
  standalone entities (institutions etc.) rank but have no map coordinates;
  current-map universe is the median denominator (renamed historical PTs may
  briefly count as separate entities).

## Reproduce

```
git clone https://github.com/FlorinPopaCodes/termoficare-data archive
git clone https://github.com/gov2-ro/prometeu prometeu
uv run python -m pipeline.backfill archive db/termo.db
uv run python -m pipeline.identity db/termo.db prometeu data/harta.html
uv run python -m pipeline.metrics db/termo.db data/harta.html db/report.json
uv run python -m pipeline.wayback_audit db/termo.db archive db/wayback_audit.json
```
