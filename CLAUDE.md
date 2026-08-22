# termo-data

The data pipeline behind [faraapacalda.ro](https://faraapacalda.ro). Scrapes CMTEB /
Termoenergetica outage announcements, rebuilds a SQLite database from archived
snapshots, and publishes a slug-keyed artifact bundle to the GitHub release
`data-latest`. The website (`tiXor-code/termo-site`) consumes that bundle at build
time and nothing else.

`ARTIFACTS.md` is the binding contract between the two repos.

## Build and test

```bash
uv sync                  # installs the dev group, which provides pytest
uv run pytest            # 51 tests; 15 SKIP without db/termo.db - that is expected
```

The 15 skipped tests require a locally built `db/termo.db`, which is a gitignored
derivative that only the nightly builds. **A run reporting 51 passed instead of
36 passed / 15 skipped means a stale local database is present and those tests are
asserting frozen constants against out-of-date data.** Skipping is the correct state.

Pipeline stages, in order (see `.github/workflows/nightly.yml` for the real invocation):

```
pipeline.backfill -> pipeline.identity -> pipeline.publish -> pipeline.validate
```

`pipeline.validate` is the invariant gate. It runs before the registry commit, the
release upload and the deploy hook, so a breached invariant leaves the previous
bundle and the live site intact.

## The headline metric

A day counts toward the headline `days` only if it is a Bucharest-local calendar day
touched by an episode with `severity=oprire` AND `service=ACC`.

CMTEB publishes a third state, `Deficiență` (pressure or temperature below spec).
Those days are counted in the separate `days_deficienta` counter and **must never be
added into `days`**. Consumers painting or summing headline days must exclude
`deficienta` runs. Heating (INC) is excluded from v1 artifacts entirely.

## Never

- **Never run `gh release upload data-latest` from a local clone.** Only the nightly,
  running from a fresh CI checkout, may write that release. Uploading a locally built
  bundle regressed published data by three days on 2026-06-13.
- **Never edit or commit `registry/slugs.json` by hand.** It is append-only: a slug
  never changes or gets reassigned, because slugs are public URLs. The nightly commits
  registry growth with a rebase-and-retry loop because it races the 15-minute scraper.
- **Never send HTTP requests to cmteb.ro from a development machine.** Burst-probing it
  got a home IP banned. The committed `data/functionare.html` and `data/harta.html`,
  plus their git history, are the local source of truth.
- **Never assume this clone is current.** `main` receives roughly 21-28 automated
  `snapshot` commits per day. Run `git fetch` and check
  `git rev-list --left-right --count origin/main...HEAD` before acting.

## Instead

To get freshly published data, dispatch the nightly and let CI do it:

```bash
gh workflow run nightly.yml -R tiXor-code/termo-data   # ~3 minutes
```

`db/`, `web/`, `bundle.tar.gz`, `archive/` and `prometeu/` are gitignored derivatives
and are intentionally absent from this working copy. Their absence is what makes an
accidental local publish impossible. Do not recreate them here; build in a temporary
directory if you need them.

## Artifact changes

Additive only within contract v1. A new field lands in `ARTIFACTS.md` in the same
commit that emits it, and gets a `pipeline/validate.py` assertion in that commit too.
No existing field may change value: the nightly full-rebuilds all years at once, so
there is no migration and no rollback but a code revert plus another nightly.
