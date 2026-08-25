"""Validate checks against synthetic dbs and a minimal artifact tree."""

import json
import sqlite3

from pipeline import validate
from pipeline.backfill import SCHEMA as EPISODE_SCHEMA
from pipeline.identity import SCHEMA as IDENTITY_SCHEMA
from pipeline.publish import write_json, write_ndjson_gz


def _db(tmp_path):
    db = sqlite3.connect(tmp_path / "t.db")
    db.executescript(EPISODE_SCHEMA)
    db.executescript(IDENTITY_SCHEMA)
    return db


def _ep(db, pt="pt a", sev="oprire", svc="ACC", cls="avarie",
        first="2025-05-01T10:00:00+03:00", last="2025-05-02T10:00:00+03:00",
        sector=1):
    db.execute(
        """INSERT INTO episode (pt_norm, sector, service, severity, cause_class,
           cause_raw, remediere_last, blocks_count, first_seen, last_seen,
           started_after, ended_before, gap_spanned, est_hours)
           VALUES (?,?,?,?,?, 'c', NULL, NULL, ?,?, NULL, NULL, 0, 24.0)""",
        (pt, sector, svc, sev, cls, first, last))


def test_overlap_fails_and_exits_nonzero(tmp_path):
    db = _db(tmp_path)
    _ep(db, first="2025-05-01T10:00:00+03:00", last="2025-05-03T10:00:00+03:00")
    _ep(db, first="2025-05-02T10:00:00+03:00", last="2025-05-04T10:00:00+03:00")
    results = validate.check_episode_overlap(db)
    assert results[0][0] == validate.FAIL
    assert validate.exit_code(results) == 1


def test_no_overlap_passes(tmp_path):
    db = _db(tmp_path)
    _ep(db, first="2025-05-01T10:00:00+03:00", last="2025-05-02T10:00:00+03:00")
    _ep(db, first="2025-05-03T10:00:00+03:00", last="2025-05-04T10:00:00+03:00")
    # mixed-offset comparison must be aware, not lexicographic
    _ep(db, first="2026-06-10T19:40:00Z", last="2026-06-10T20:00:00Z")
    results = validate.check_episode_overlap(db)
    assert results[0][0] == validate.PASS
    assert validate.exit_code(results) == 0


def test_unclassified_25_pct_fails(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        _ep(db, cls="avarie", first=f"2025-03-0{i + 1}T10:00:00+02:00",
            last=f"2025-03-0{i + 1}T12:00:00+02:00")
    _ep(db, cls="unclassified", first="2025-03-04T10:00:00+02:00",
        last="2025-03-04T12:00:00+02:00")
    results = validate.check_unclassified_share(db, [2025])
    assert results[0][0] == validate.FAIL


def test_unclassified_12_pct_warns_exit_zero(tmp_path):
    db = _db(tmp_path)
    for i in range(22):
        _ep(db, cls="programat", first="2025-03-01T10:00:00+02:00",
            last="2025-03-01T12:00:00+02:00")
    for i in range(3):
        _ep(db, cls="unclassified", first="2025-03-02T10:00:00+02:00",
            last="2025-03-02T12:00:00+02:00")
    results = validate.check_unclassified_share(db, [2025])
    assert results[0][0] == validate.WARN
    assert validate.exit_code(results) == 0


# --- fixture year objects -------------------------------------------------
# Internally consistent by construction: every day count is exactly the union
# of the matching runs, so any override that breaks one relation trips exactly
# the check under test and nothing else.
_EP = {"start": "2025-05-01T10:00", "end": "2025-05-03T10:00", "ongoing": False,
       "uncertain": False, "cause_class": "avarie", "cause_raw": "c",
       "remediere_last": None}
_EP_DEFI = {"start": "2025-07-19T08:00", "end": "2025-07-20T08:00",
            "ongoing": False, "uncertain": False, "cause_class": "unclassified",
            "cause_raw": "presiune scazuta", "remediere_last": None}


def _pt_year(**over):
    """days=3 <- [121,3,avarie] (doy 121-123); days_deficienta=2 <- [200,2,deficienta]."""
    y = {"days": 3, "days_avarie": 3, "days_programat": 0, "days_deficienta": 2,
         "episodes_count": 1, "longest_days": 3, "est_hours": 48.0,
         "runs": [[121, 3, "avarie"], [200, 2, "deficienta"]],
         "episodes": [_EP],
         "episodes_count_deficienta": 1, "est_hours_deficienta": 24.0,
         "episodes_deficienta": [_EP_DEFI]}
    y.update(over)
    return y


def _st_year(**over):
    """days=3 <- [121,3,avarie]; days_deficienta=1 <- [300,1,deficienta]."""
    y = {"days": 3, "days_avarie": 3, "days_programat": 0, "days_deficienta": 1,
         "runs": [[121, 3, "avarie"], [300, 1, "deficienta"]]}
    y.update(over)
    return y


def _pt_record(**over):
    r = {"slug": "pt-a", "name": "A", "sector": 1, "lat": 44.4, "lon": 26.1,
         "on_map": True, "blocks_estimate": None, "streets": [], "nearest": [],
         "years": {"2025": _pt_year()}}
    r.update(over)
    return r


def _st_record(**over):
    r = {"slug": "str-b", "name": "Str B", "type": "str", "sectors": [1],
         "pts": ["pt-a"], "neighbors": [], "years": {"2025": _st_year()}}
    r.update(over)
    return r


def _minimal_web(tmp_path):
    web = tmp_path / "web"
    write_json(web / "meta.json", {
        "generated_at": "2026-01-01T00:00:00Z", "data_through": "2026-01-01",
        "years": [2025], "last_complete_year": 2025, "partial_years": [],
        "universe_size": 1,
        "coverage": {"2025": {"snapshots": 1, "missing_days": 0, "gap_hours_max": 0.5}},
        "sources_cutover_utc": "2026-06-10T19:34:00Z"})
    write_json(web / "city" / "summary.json", [
        {"year": 2025, "partial": False, "median_pt_days": 3, "mean_pt_days": 3.0,
         "p90_pt_days": 3, "pts_hit": 1, "share_universe_hit_pct": 100.0,
         "episodes": 1, "episodes_avarie": 1, "episodes_programat": 0,
         "monthly_pt_days": [0] * 12}])
    write_json(web / "city" / "distribution-2025.json", {
        "year": 2025, "percentiles": {"p10": 3, "p25": 3, "p50": 3, "p75": 3,
                                      "p90": 3, "p99": 3},
        "histogram": [[0, 1]]})
    write_json(web / "rankings" / "pt-2025.json", [
        {"slug": "pt-a", "name": "A", "sector": 1, "days": 3, "days_avarie": 3,
         "days_programat": 0, "days_deficienta": 0, "episodes": 1,
         "longest_days": 3, "est_day_eq": 3.0, "delta_prev": None}])
    write_json(web / "rankings" / "strazi-2025.json", [
        {"slug": "str-b", "name": "Str B", "sectors": [1], "pt_slugs": ["pt-a"],
         "days": 3, "days_avarie": 3, "days_programat": 0, "days_deficienta": 1,
         "episodes": 1, "longest_days": 3, "est_day_eq": 3.0, "delta_prev": None}])
    write_json(web / "rankings" / "sectoare-2025.json", [
        {"sector": s, "pts": 1 if s == 1 else 0, "median_days": 0,
         "mean_days": 0.0, "mean_days_avarie": 0.0, "mean_days_programat": 0.0,
         "episodes": 1 if s == 1 else 0} for s in range(1, 7)])
    write_json(web / "client" / "map" / "pt-2025.geojson", {
        "type": "FeatureCollection", "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [26.1, 44.4]},
             "properties": {"slug": "pt-a", "name": "A", "sector": 1, "days": 3,
                            "days_avarie": 3, "days_programat": 0, "episodes": 1}}]})
    write_json(web / "client" / "search-index.json", [
        {"t": "pt", "n": "A", "s": "pt-a", "sec": 1, "d": 3},
        {"t": "st", "n": "Str B", "s": "str-b", "sec": 1, "d": 3}])
    write_json(web / "og" / "stats.json", {"pt-a": ["pt", "A", 1, 3, 2025],
                                           "str-b": ["st", "Str B", 1, 3, 2025]})
    write_ndjson_gz(web / "pt" / "all.ndjson.gz", [_pt_record()])
    write_ndjson_gz(web / "strazi" / "all.ndjson.gz", [_st_record()])
    return web


def test_artifacts_complete_minimal_web(tmp_path):
    web = _minimal_web(tmp_path)
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert not [r for r in results if r[0] == validate.FAIL]
    # sectoare.geojson absent is tolerated as WARN, never FAIL
    assert any(r[1] == "sectoare_geojson" and r[0] == validate.WARN for r in results)


def test_missing_rankings_file_fails(tmp_path):
    web = _minimal_web(tmp_path)
    (web / "rankings" / "pt-2025.json").unlink()
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    fails = [r for r in results if r[0] == validate.FAIL]
    assert any("rankings/pt-2025.json" in r[2] for r in fails)
    assert validate.exit_code(results) == 1


def test_unsorted_rankings_fail(tmp_path):
    web = _minimal_web(tmp_path)
    write_json(web / "rankings" / "pt-2025.json", [
        {"slug": "pt-a", "name": "A", "sector": 1, "days": 1, "days_avarie": 1,
         "days_programat": 0, "days_deficienta": 0, "episodes": 1,
         "longest_days": 1, "est_day_eq": 1.0, "delta_prev": None},
        {"slug": "pt-a", "name": "A", "sector": 1, "days": 9, "days_avarie": 9,
         "days_programat": 0, "days_deficienta": 0, "episodes": 1,
         "longest_days": 9, "est_day_eq": 9.0, "delta_prev": None}])
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "rankings_integrity" and r[0] == validate.FAIL
               for r in results)


# --- deficienta contract (ARTIFACTS.md:88-92) -----------------------------

def test_run_day_set_unions_overlapping_classes():
    # A day can carry BOTH an avarie and a programat episode - ARTIFACTS.md says
    # the per-class day counts may sum above `days`. So the invariant must union
    # day-of-year numbers, never sum run lengths, or it fails on correct data.
    runs = [[10, 3, "avarie"], [12, 3, "programat"], [50, 1, "deficienta"]]
    assert validate._run_day_set(runs, validate.NON_DEFICIENTA) == {10, 11, 12, 13, 14}
    assert validate._run_day_set(runs, ("deficienta",)) == {50}


def test_runs_days_union_mismatch_fails(tmp_path):
    web = _minimal_web(tmp_path)
    # runs still cover only 3 days while `days` claims 4
    write_ndjson_gz(web / "pt" / "all.ndjson.gz",
                    [_pt_record(years={"2025": _pt_year(days=4)})])
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "runs_days_union" and r[0] == validate.FAIL for r in results)
    assert validate.exit_code(results) == 1


def test_street_runs_days_union_mismatch_fails(tmp_path):
    # The strazi half of the ARTIFACTS.md:91 guarantee. Vacuous before this work,
    # because street year objects carried no days_deficienta at all.
    web = _minimal_web(tmp_path)
    write_ndjson_gz(web / "strazi" / "all.ndjson.gz",
                    [_st_record(years={"2025": _st_year(days_deficienta=7)})])
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "runs_days_union" and r[0] == validate.FAIL for r in results)


def test_missing_year_key_fails(tmp_path):
    web = _minimal_web(tmp_path)
    y = _pt_year()
    del y["days_deficienta"]
    write_ndjson_gz(web / "pt" / "all.ndjson.gz", [_pt_record(years={"2025": y})])
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "ndjson_year_keys" and r[0] == validate.FAIL for r in results)


def test_deficienta_episode_count_mismatch_fails(tmp_path):
    web = _minimal_web(tmp_path)
    write_ndjson_gz(web / "pt" / "all.ndjson.gz",
                    [_pt_record(years={"2025": _pt_year(episodes_count_deficienta=4)})])
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "deficienta_reconciliation" and r[0] == validate.FAIL
               for r in results)


def test_ranking_row_missing_days_deficienta_fails(tmp_path):
    web = _minimal_web(tmp_path)
    rows = [{"slug": "pt-a", "name": "A", "sector": 1, "days": 3, "days_avarie": 3,
             "days_programat": 0, "episodes": 1, "longest_days": 3,
             "est_day_eq": 3.0, "delta_prev": None}]      # days_deficienta dropped
    write_json(web / "rankings" / "pt-2025.json", rows)
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "rankings_row_keys" and r[0] == validate.FAIL for r in results)


def test_ranking_key_check_survives_a_bad_first_row(tmp_path):
    # Guards the pre-existing `break` short-circuit: an unresolvable slug in row 0
    # must not hide a key regression in row 1.
    web = _minimal_web(tmp_path)
    write_json(web / "rankings" / "pt-2025.json", [
        {"slug": "pt-ghost", "name": "G", "sector": 1, "days": 9, "days_avarie": 9,
         "days_programat": 0, "days_deficienta": 0, "episodes": 1,
         "longest_days": 9, "est_day_eq": 9.0, "delta_prev": None},
        {"slug": "pt-a", "name": "A", "sector": 1, "days": 3, "days_avarie": 3,
         "days_programat": 0, "episodes": 1, "longest_days": 3,
         "est_day_eq": 3.0, "delta_prev": None}])          # days_deficienta dropped
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "rankings_row_keys" and r[0] == validate.FAIL for r in results)


def test_all_zero_deficienta_warns_but_does_not_block_release(tmp_path):
    # A quiet year is plausible upstream; blocking the nightly on it would let an
    # editorial change at CMTEB stop publication. WARN, not FAIL.
    web = _minimal_web(tmp_path)
    write_ndjson_gz(web / "pt" / "all.ndjson.gz", [_pt_record(years={"2025": _pt_year(
        days_deficienta=0, episodes_count_deficienta=0, est_hours_deficienta=0.0,
        episodes_deficienta=[], runs=[[121, 3, "avarie"]])})])
    results = validate.check_artifacts(web, {"pt-a", "str-b"})
    assert any(r[1] == "deficienta_non_vacuous" and r[0] == validate.WARN
               for r in results)
    assert validate.exit_code(results) == 0


def test_deficienta_episodes_published_without_touching_headline(tmp_path):
    """The additivity contract, proven end-to-end through publish.build().

    A deficienta episode must contribute to days_deficienta and the new
    episode fields, and to NOTHING else: not days, not episodes_count, not
    est_hours, not the episodes array.
    """
    from pipeline import publish
    db = _db(tmp_path)
    _ep(db, pt="pt a", sev="oprire",
        first="2025-05-01T10:00:00+03:00", last="2025-05-02T10:00:00+03:00")
    _ep(db, pt="pt a", sev="deficienta",
        first="2025-07-01T10:00:00+03:00", last="2025-07-03T10:00:00+03:00")
    # build() derives the year range from snapshot coverage, so the synthetic db
    # needs the two bookend snapshots that bracket 2025.
    for i, ts in enumerate(("2025-01-01T00:00:00+00:00", "2025-12-31T00:00:00+00:00")):
        db.execute("INSERT INTO snapshot (sha, observed_utc, content_hash, "
                   "n_records, parse_status, changed) VALUES (?,?,?,?,?,?)",
                   (f"sha{i}", ts, f"h{i}", 1, "ok", 1))
    db.commit()
    db.close()

    # Empty static/ skips the OSM street + address joins, which are irrelevant
    # here and turn a 0.0s test into a 45s one.
    empty_static = tmp_path / "static"
    empty_static.mkdir()
    out = tmp_path / "web"
    publish.build(str(tmp_path / "t.db"), str(tmp_path / "reg.json"),
                  "data/harta.html", str(empty_static), str(out))

    import gzip as _gz
    with _gz.open(out / "pt" / "all.ndjson.gz", "rt", encoding="utf-8") as f:
        rec = next(r for r in map(json.loads, f) if r["slug"] == "pt-pt-a")
    y = rec["years"]["2025"]

    # headline half - every one of these must be blind to the deficienta episode
    assert y["days"] == 2
    assert y["episodes_count"] == 1
    assert y["est_hours"] == 24.0          # the deficienta episode's hours are NOT here
    assert len(y["episodes"]) == 1
    assert y["episodes"][0]["cause_class"] == "avarie"

    # deficienta half
    assert y["days_deficienta"] == 3
    assert y["episodes_count_deficienta"] == 1
    assert len(y["episodes_deficienta"]) == 1
    assert y["est_hours_deficienta"] > 0

    # and the two never mix
    assert y["episodes_deficienta"][0] not in y["episodes"]
