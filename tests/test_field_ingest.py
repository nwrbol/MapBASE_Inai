"""Проверка полевых замеров теми же правилами, что и CSV; пересчёт с полевыми; неизменность result.json."""

import json

import pandas as pd
import pytest

from coverage.config import load_settings
from coverage.field_ingest import (
    cell_feedback,
    cell_lookup,
    existing_frame,
    field_diff,
    field_points,
    rejected_by_reason,
    run_pipeline_with_field,
    validate_field_rows,
)
from coverage.pipeline import Inputs, run_pipeline
from coverage.result import dumps
from tests.conftest import OPEN_DATA, ROOT

TS = "2026-11-20T22:15:00+06:00"


@pytest.fixture(scope="module")
def settings():
    return load_settings(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def pr(settings):
    return run_pipeline(Inputs.from_dir(OPEN_DATA), settings)


@pytest.fixture(scope="module")
def cell3(pr):
    """Клетка из открытого набора с n=3 (не учитывается)."""
    return pr.grid[pr.grid["n"] == 3].iloc[0]


def item(lat, lon, rsrp=-110, ts=TS, **kw):
    return {"lat": lat, "lon": lon, "rsrp_dbm": rsrp, "timestamp": ts, "source": "manual"} | kw


def validate(rows, settings, pr, field_df=None):
    return validate_field_rows(rows, settings, existing_frame(pr.clean, field_df))


def one(rows, settings, pr, field_df=None):
    acc, rej = validate(rows, settings, pr, field_df)
    return acc, rej


# --- 2. проверка --------------------------------------------------------------------------------


def test_valid_row_is_accepted_and_normalized(settings, pr, cell3):
    acc, rej = one([item(cell3.center_lat, cell3.center_lon, "-112", "2026-11-20T22:15:00")], settings, pr)
    assert rej == [] and len(acc) == 1
    assert acc[0].timestamp == "2026-11-20T22:15:00+06:00"  # без пояса -> Asia/Bishkek, смещение +06:00
    assert acc[0].rsrp_dbm == -112.0 and acc[0].source == "manual" and acc[0].warning is None


def test_timestamp_with_other_offset_is_converted_to_bishkek(settings, pr, cell3):
    acc, _ = one([item(cell3.center_lat, cell3.center_lon, ts="2026-11-20T16:15:00Z")], settings, pr)
    assert acc[0].timestamp == "2026-11-20T22:15:00+06:00"


@pytest.mark.parametrize(
    ("kw", "reason", "text"),
    [
        ({"lat": "abc"}, "unparseable", "не являются числом"),
        ({"rsrp_dbm": "-95 dBm"}, "unparseable", "не являются числом"),
        ({"ts": "вчера"}, "bad_timestamp", "ISO 8601"),
        ({"rsrp": -30}, "rsrp_out_of_range", "RSRP −30 вне −140…−44"),
        ({"rsrp": -140.1}, "rsrp_out_of_range", "вне"),
        ({"rsrp": -43.9}, "rsrp_out_of_range", "вне"),
        ({"lat": 0, "lon": 0}, "out_of_bounds", "Точка вне района (0.0000, 0.0000)"),
        ({"lat": 42.9010, "lon": 74.58}, "out_of_bounds", "Точка вне района (42.9010, 74.5800)"),
    ],
)
def test_each_rejection_reason(settings, pr, cell3, kw, reason, text):
    row = item(kw.get("lat", cell3.center_lat), kw.get("lon", cell3.center_lon), kw.get("rsrp", -110), kw.get("ts", TS))
    if "rsrp_dbm" in kw:
        row["rsrp_dbm"] = kw["rsrp_dbm"]
    acc, rej = one([row], settings, pr)
    assert acc == [] and len(rej) == 1 and rej[0].reason == reason and text in rej[0].message


@pytest.mark.parametrize("missing", ["lat", "lon", "rsrp_dbm", "timestamp"])
def test_missing_field(settings, pr, cell3, missing):
    row = item(cell3.center_lat, cell3.center_lon)
    row[missing] = "  " if missing == "timestamp" else None
    acc, rej = one([row], settings, pr)
    assert acc == [] and rej[0].reason == "missing_field" and missing in rej[0].message


@pytest.mark.parametrize("rsrp", [-140, -44, "-140.0", -44.0])
def test_rsrp_boundaries_are_accepted(settings, pr, cell3, rsrp):
    acc, rej = one([item(cell3.center_lat, cell3.center_lon, rsrp)], settings, pr)
    assert len(acc) == 1 and rej == []


def test_point_exactly_on_lat_max_is_accepted(settings, pr):
    b = settings.bounds
    acc, rej = one([item(b.lat_max, 74.58), item(b.lat_min, b.lon_min, ts="2026-11-20T22:16:00+06:00")], settings, pr)
    assert len(acc) == 2 and rej == []  # границы включительно


def test_duplicate_of_source_measurement(settings, pr):
    first = pr.clean.iloc[0]
    acc, rej = one([item(first.lat, first.lon, first.rsrp_dbm, first.timestamp)], settings, pr)
    assert acc == [] and rej[0].reason == "duplicates" and "уже есть" in rej[0].message


def test_duplicate_of_stored_field_measurement(settings, pr, cell3):
    acc1, _ = one([item(cell3.center_lat, cell3.center_lon)], settings, pr)
    stored = pd.DataFrame([a.to_store() | {"id": "x"} for a in acc1])
    # то же время в другой записи (Z вместо +06:00) тоже дубль
    acc2, rej2 = one([item(cell3.center_lat, cell3.center_lon, ts="2026-11-20T16:15:00Z")], settings, pr, stored)
    assert acc2 == [] and rej2[0].reason == "duplicates"


def test_duplicate_inside_batch(settings, pr, cell3):
    rows = [item(cell3.center_lat, cell3.center_lon), item(cell3.center_lat, cell3.center_lon)]
    acc, rej = one(rows, settings, pr)
    assert [a.index for a in acc] == [0] and rej[0].index == 1 and "внутри" in rej[0].message
    assert rejected_by_reason(rej) == {"duplicates": 1}


def test_low_accuracy_warns_but_accepts(settings, pr, cell3):
    acc, rej = one(
        [
            item(cell3.center_lat, cell3.center_lon, source="gps", accuracy_m=80),
            item(cell3.center_lat, cell3.center_lon, rsrp=-111, source="gps", accuracy_m=50),
        ],
        settings,
        pr,
    )
    assert rej == [] and acc[0].warning == "low_accuracy" and acc[1].warning is None and acc[0].accuracy_m == 80.0


def test_bad_accuracy_rejected(settings, pr, cell3):
    _, rej = one([item(cell3.center_lat, cell3.center_lon, accuracy_m=-3)], settings, pr)
    assert rej[0].reason == "unparseable" and "точность" in rej[0].message


def test_device_label_is_trimmed(settings, pr, cell3):
    acc, _ = one([item(cell3.center_lat, cell3.center_lon, author_label=" " + "я" * 80)], settings, pr)
    assert len(acc[0].author_label) == settings.field_data.device_label_max


def test_good_rows_survive_a_bad_one(settings, pr, cell3):
    rows = [
        item(cell3.center_lat, cell3.center_lon, -100),
        item(0, 0),
        item(cell3.center_lat, cell3.center_lon, -101, "2026-11-20T22:16:00+06:00"),
    ]
    acc, rej = one(rows, settings, pr)
    assert [a.index for a in acc] == [0, 2] and [r.index for r in rej] == [1]


# --- 3. пересчёт ---------------------------------------------------------------------------------


def test_two_field_rows_make_cell_counted(settings, pr, cell3):
    assert cell3.n == 3 and not cell3.counted
    rows = [
        item(cell3.center_lat, cell3.center_lon, -112, "2026-11-20T22:15:00+06:00"),
        item(cell3.center_lat, cell3.center_lon, -108, "2026-11-20T22:16:00+06:00"),
    ]
    ex = existing_frame(pr.clean, None)
    acc, rej = validate_field_rows(rows, settings, ex)
    assert rej == []
    fb = cell_feedback(ex, acc, settings)
    assert all(
        f["cell_id"] == cell3.cell_id and f["n_after"] == 5 and f["counted_after"] and f["became_counted"] for f in fb
    )
    assert fb[0]["class_after"] in {"green", "yellow", "red"}

    field_df = pd.DataFrame([a.to_store("b") | {"id": str(i), "created_at": ""} for i, a in enumerate(acc)])
    withf = run_pipeline_with_field(pr, field_df)
    row = withf.grid[withf.grid["cell_id"] == cell3.cell_id].iloc[0]
    assert row.n == 5 and bool(row.counted)

    diff = field_diff(pr, withf)
    changed = {c["cell_id"]: c for c in diff["cells"]}
    assert changed[cell3.cell_id]["before"]["counted"] is False and changed[cell3.cell_id]["after"]["counted"] is True
    assert diff["summary"]["became_counted"] == 1 and diff["best_pair"]["before"] == ["C09", "C14"]

    # dropped_rows — метрика исходного файла — не меняется; исходный результат не тронут
    assert withf.cleaning.dropped == pr.cleaning.dropped == 200
    assert json.loads(dumps(pr.result)) == json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))

    info = cell_lookup(withf, cell3.center_lat, cell3.center_lon)
    assert info["n"] == 5 and info["counted"] and info["in_bounds"]
    assert cell_lookup(pr, cell3.center_lat, cell3.center_lon)["n"] == 3

    pts = field_points(field_df.assign(source="gps", accuracy_m=None, batch_id=None), settings)
    assert pts[0]["cell_id"] == cell3.cell_id and pts[0]["source"] == "gps"


def test_feedback_not_became_counted_if_still_below_threshold(settings, pr, cell3):
    ex = existing_frame(pr.clean, None)
    acc, _ = validate_field_rows([item(cell3.center_lat, cell3.center_lon)], settings, ex)
    fb = cell_feedback(ex, acc, settings)[0]
    assert fb["n_after"] == 4 and fb["counted_after"] is False and fb["became_counted"] is False


def test_empty_field_gives_same_grid_as_base(settings, pr):
    withf = run_pipeline_with_field(pr, pd.DataFrame())
    assert field_diff(pr, withf)["cells"] == [] and withf.result == pr.result


def test_official_result_unchanged_after_field_recompute(settings, pr, cell3):
    before = dumps(pr.result)
    rows = [item(cell3.center_lat, cell3.center_lon, -125, f"2026-11-20T22:{i:02d}:00+06:00") for i in range(10)]
    acc, _ = validate(rows, settings, pr)
    field_df = pd.DataFrame([a.to_store() | {"id": str(i)} for i, a in enumerate(acc)])
    withf = run_pipeline_with_field(pr, field_df)
    assert withf.result["covered_weak_cells"] is not None
    assert dumps(pr.result) == before
    assert json.loads(before) == json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))
