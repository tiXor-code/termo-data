"""Day expansion + run compression: year spans, DST, leap years, merges."""

from datetime import date

from pipeline.metrics import episode_days
from pipeline.publish import runs_from_days, year_doys


def _days(first, last):
    return set(episode_days(first, last))


def test_year_spanning_episode_splits_at_dec31():
    days = _days("2025-12-28T10:00:00+02:00", "2026-01-03T09:00:00+02:00")
    doy_start = date(2025, 12, 28).timetuple().tm_yday  # 2025 is not a leap year
    assert runs_from_days(year_doys(days, 2025), "programat") == [
        [doy_start, 4, "programat"]]
    assert runs_from_days(year_doys(days, 2026), "programat") == [[1, 3, "programat"]]


def test_leap_year_doy_arithmetic():
    days = _days("2024-02-28T08:00:00+02:00", "2024-03-01T08:00:00+02:00")
    doy_start = date(2024, 2, 28).timetuple().tm_yday
    assert runs_from_days(year_doys(days, 2024), "avarie") == [[doy_start, 3, "avarie"]]


def test_overlapping_same_class_episodes_merge():
    days = (_days("2025-01-01T06:00:00+02:00", "2025-01-05T06:00:00+02:00")
            | _days("2025-01-03T06:00:00+02:00", "2025-01-08T06:00:00+02:00"))
    assert runs_from_days(year_doys(days, 2025), "avarie") == [[1, 8, "avarie"]]


def test_gap_between_episodes_yields_two_runs():
    days = (_days("2025-01-01T06:00:00+02:00", "2025-01-02T06:00:00+02:00")
            | _days("2025-01-05T06:00:00+02:00", "2025-01-06T06:00:00+02:00"))
    assert runs_from_days(year_doys(days, 2025), "avarie") == [
        [1, 2, "avarie"], [5, 2, "avarie"]]


def test_dst_boundary_day_counted_once():
    # last Sunday of March 2025: 02:59 EET jumps to 04:00 EEST
    days = _days("2025-03-30T00:30:00+02:00", "2025-03-30T23:30:00+03:00")
    assert days == {date(2025, 3, 30)}


def test_utc_timestamp_lands_on_next_bucharest_day():
    days = _days("2025-06-01T22:30:00Z", "2025-06-01T23:00:00Z")
    assert days == {date(2025, 6, 2)}


def test_longest_is_max_union_run_when_classes_interleave():
    avarie = {date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)}
    programat = {date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5)}
    union = avarie | programat
    union_runs = runs_from_days(year_doys(union, 2025), "u")
    assert max(r[1] for r in union_runs) == 5
    per_class = (runs_from_days(year_doys(avarie, 2025), "avarie")
                 + runs_from_days(year_doys(programat, 2025), "programat"))
    assert max(r[1] for r in per_class) == 3
