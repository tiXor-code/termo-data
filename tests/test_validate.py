"""Validate checks against synthetic dbs and a minimal artifact tree."""

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
    write_json(web / "rankings" / "strazi-2025.json", [])
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
    write_ndjson_gz(web / "pt" / "all.ndjson.gz", [
        {"slug": "pt-a", "name": "A", "sector": 1, "lat": 44.4, "lon": 26.1,
         "on_map": True, "blocks_estimate": None, "streets": [], "nearest": [],
         "years": {"2025": {"days": 3}}}])
    write_ndjson_gz(web / "strazi" / "all.ndjson.gz", [
        {"slug": "str-b", "name": "Str B", "type": "str", "sectors": [1],
         "pts": ["pt-a"], "neighbors": [], "years": {"2025": {"days": 3}}}])
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
