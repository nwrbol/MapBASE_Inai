"""CLI --include-field: отдельные файлы, result.json побайтово тот же."""

import json
import os
import subprocess
import sys

import pytest

from coverage.config import load_settings
from coverage.field_ingest import existing_frame, validate_field_rows
from coverage.field_store import FieldStore
from coverage.pipeline import Inputs, run_pipeline
from tests.conftest import OPEN_DATA, ROOT

ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(*args, out):
    return subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--out", str(out), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
        check=False,
    )


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    settings = load_settings(ROOT / "config.yaml")
    pr = run_pipeline(Inputs.from_dir(OPEN_DATA), settings)
    cell = pr.grid[pr.grid["n"] == 3].iloc[0]
    rows = [
        {
            "lat": cell.center_lat,
            "lon": cell.center_lon,
            "rsrp_dbm": -112,
            "timestamp": "2026-11-20T22:15:00",
            "source": "gps",
        },
        {
            "lat": cell.center_lat,
            "lon": cell.center_lon,
            "rsrp_dbm": -108,
            "timestamp": "2026-11-20T22:16:00",
            "source": "map_pin",
        },
    ]
    accepted, _ = validate_field_rows(rows, settings, existing_frame(pr.clean, None))
    path = tmp_path_factory.mktemp("clidb") / "field.db"
    store = FieldStore(path)
    store.add_many([a.to_store() for a in accepted])
    store.close()
    return path, cell.cell_id


def test_include_field_writes_extra_files_and_keeps_result(tmp_path, db):
    path, cell_id = db
    base, withf = tmp_path / "base", tmp_path / "field"
    assert run(out=base).returncode == 0
    proc = run("--include-field", str(path), out=withf)
    assert proc.returncode == 0, proc.stderr
    assert (
        "Field: 2 measurements included" in proc.stdout and cell_id in proc.stdout and "became counted 1" in proc.stdout
    )
    assert (withf / "result.json").read_bytes() == (base / "result.json").read_bytes()  # официальный результат тот же
    assert not (base / "field_report.json").exists()  # без флага базу не читаем
    report = json.loads((withf / "field_report.json").read_text(encoding="utf-8"))
    assert report["field_count"] == 2 and report["official_result"] == json.loads(
        (OPEN_DATA / "expected.json").read_text(encoding="utf-8")
    )
    assert report["field_diff"]["summary"]["became_counted"] == 1
    html = (withf / "map_with_field.html").read_text(encoding="utf-8")
    assert "field_points" in html and (withf / "map.html").exists()


def test_missing_db_is_exit_code_2(tmp_path):
    proc = run("--include-field", str(tmp_path / "nope.db"), out=tmp_path)
    assert proc.returncode == 2 and "не найден" in proc.stderr and "Traceback" not in proc.stderr
