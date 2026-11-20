import numpy as np

from coverage.cleaning import REASONS, clean_measurements
from coverage.io import read_measurements
from tests.conftest import csv_bytes

H = "lat,lon,rsrp_dbm,timestamp"
OK_TS = "2026-11-16T13:58:30+06:00"


def run(rows, settings):
    frame, malformed = read_measurements(csv_bytes(H, rows))
    return clean_measurements(frame, malformed, settings)


def row(rsrp="-95", lat="42.85", lon="74.58", ts=OK_TS):
    return f"{lat},{lon},{rsrp},{ts}"


def check_invariants(rep):
    assert rep.dropped == rep.total - rep.kept
    assert sum(rep.by_reason.values()) == rep.dropped
    assert set(rep.by_reason) == set(REASONS)


def test_rsrp_bounds_inclusive(settings):
    clean, rep = run([row("-140"), row("-44"), row("-140.1"), row("-43.9")], settings)
    assert sorted(clean["rsrp_dbm"].tolist()) == [-140.0, -44.0]
    assert rep.by_reason["rsrp_out_of_range"] == 2
    check_invariants(rep)


def test_out_of_bounds_and_swapped(settings):
    rows = [row(), row(lat="0", lon="0"), row(lat="74.58", lon="42.85"), row(lat="42.84", lon="74.56")]
    _clean, rep = run(rows, settings)
    assert rep.by_reason["out_of_bounds"] == 2
    assert rep.kept == 2  # граница lat_min/lon_min включительна
    check_invariants(rep)


def test_exact_duplicate_and_numeric_forms(settings):
    _clean, rep = run([row("-95"), row("-95"), row("-95.0"), row("-95.00", lat=" 42.85")], settings)
    assert rep.kept == 1
    assert rep.by_reason["duplicates"] == 3
    check_invariants(rep)


def test_duplicate_same_instant_other_offset(settings):
    _clean, rep = run([row(ts="2026-11-16T13:58:30+06:00"), row(ts="2026-11-16T07:58:30Z")], settings)
    assert rep.by_reason["duplicates"] == 1


def test_different_timestamp_not_duplicate(settings):
    _clean, rep = run([row(ts="2026-11-16T13:58:30+06:00"), row(ts="2026-11-16T13:58:31+06:00")], settings)
    assert rep.kept == 2


def test_unparseable_values(settings):
    rows = [row(""), row("abc"), row("-95 dBm"), row(lat="NaN"), row(lon="Infinity"), row(lat="")]
    _clean, rep = run(rows, settings)
    assert rep.kept == 0
    assert rep.by_reason["unparseable"] == 6
    check_invariants(rep)


def test_empty_and_malformed_lines(settings):
    _clean, rep = run([row(), "", "1,2,3", row("-90") + ",extra", ",,,"], settings)
    assert rep.total == 5
    assert rep.kept == 1
    assert rep.by_reason["malformed"] == 4
    check_invariants(rep)


def test_bad_timestamp_dropped_by_default(settings):
    _clean, rep = run([row(ts="bad-time"), row(ts=""), row(ts="2026-13-45T10:00:00"), row()], settings)
    assert rep.by_reason["bad_timestamp"] == 3
    assert rep.kept == 1


def test_bad_timestamp_kept_when_configured(settings):
    from coverage.config import apply_overrides

    s = apply_overrides(settings, {"cleaning.require_valid_timestamp": False})
    clean, rep = run([row(ts="bad-time"), row()], s)
    assert rep.kept == 2
    assert clean["ts_utc"].isna().sum() == 1


def test_iso_variants_accepted(settings):
    rows = [
        row(ts="2026-11-16T13:58:30"),
        row(ts="2026-11-16 13:58:31"),
        row(ts="2026-11-16T13:58:32Z"),
        row(ts="2026-11-16T13:58:33.123+06:00"),
        row(ts="2026-11-16T13:58:34+0600"),
    ]
    _clean, rep = run(rows, settings)
    assert rep.kept == 5


def test_naive_timestamp_is_local_bishkek(settings):
    clean, _ = run([row(ts="2026-11-16T13:00:00")], settings)
    assert clean["ts_utc"].iloc[0].hour == 7  # UTC+6


def test_whitespace_is_stripped(settings):
    clean, rep = run([" 42.85 , 74.58 , -95 , 2026-11-16T13:58:30+06:00 "], settings)
    assert rep.kept == 1
    assert np.isclose(clean["rsrp_dbm"].iloc[0], -95)


def test_open_dataset_counts(settings):
    from tests.conftest import OPEN_DATA

    frame, malformed = read_measurements(OPEN_DATA / "measurements.csv")
    s = settings
    _clean, rep = clean_measurements(frame, malformed, s)
    assert rep.total == 5000
    assert rep.dropped == 200
    check_invariants(rep)
