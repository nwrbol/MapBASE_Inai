import json
import os
import subprocess
import sys

from coverage.config import load_settings
from coverage.pipeline import Inputs, run_pipeline
from coverage.render import render_map_html
from coverage.result import RESULT_KEYS, dumps
from tests.conftest import OPEN_DATA, ROOT, SMALL

# Дочерний python пишет stdout/stderr в UTF-8 независимо от кодировки консоли (на Windows это cp1251).
UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def test_small_fixture_end_to_end(settings):
    pr = run_pipeline(Inputs.from_dir(SMALL), settings)
    expected = json.loads((SMALL / "expected.json").read_text(encoding="utf-8"))
    assert pr.result == expected
    assert pr.report["selection"]["ties"] == [["C2", "C3"], ["C2", "C4"], ["C3", "C4"]]
    assert pr.report["cleaning"]["by_reason"] == {
        "malformed": 1, "unparseable": 1, "bad_timestamp": 1,
        "rsrp_out_of_range": 1, "out_of_bounds": 1, "duplicates": 1,
    }  # fmt: skip


def test_open_dataset_matches_published_expected(settings):
    pr = run_pipeline(Inputs.from_dir(OPEN_DATA), settings)
    expected = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))
    assert pr.result == expected


def test_result_has_exactly_four_keys(settings):
    pr = run_pipeline(Inputs.from_dir(SMALL), settings)
    assert tuple(pr.result) == RESULT_KEYS


def test_deterministic(settings):
    a = dumps(run_pipeline(Inputs.from_dir(OPEN_DATA), settings).result)
    b = dumps(run_pipeline(Inputs.from_dir(OPEN_DATA), load_settings()).result)
    assert a == b


def test_bounds_from_config_when_file_missing(settings, tmp_path):
    for name in ("measurements.csv", "towers.csv", "candidates.csv"):
        (tmp_path / name).write_bytes((OPEN_DATA / name).read_bytes())
    pr = run_pipeline(Inputs.from_dir(tmp_path), settings)
    assert pr.bounds_source == "config.yaml"
    assert pr.result["dropped_rows"] == 200


def test_map_html_is_self_contained(settings):
    html = render_map_html(run_pipeline(Inputs.from_dir(SMALL), settings))
    assert "L.map" in html  # leaflet + app.js встроены
    # библиотеки встроены: никаких <script src=...> и <link href=...> на CDN (тайлы карты не в счёт)
    assert '<script src="http' not in html and '<link rel="stylesheet" href="http' not in html
    assert "unpkg.com" not in html and "cdnjs" not in html and "jsdelivr" not in html
    assert '<script id="data" type="application/json">' in html


def test_cli_writes_outputs(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--data-dir", str(SMALL), "--out", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", env=UTF8_ENV, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert "dropped: 6" in proc.stdout
    for name in ("result.json", "map.html", "report.json"):
        assert (tmp_path / name).exists()


def test_cli_bad_input_exit_code_2(tmp_path):
    (tmp_path / "measurements.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "towers.csv").write_text("id,lat,lon\n", encoding="utf-8")
    (tmp_path / "candidates.csv").write_text("id,lat,lon\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--data-dir", str(tmp_path), "--out", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", env=UTF8_ENV, check=False,
    )  # fmt: skip
    assert proc.returncode == 2
    assert "нет обязательных столбцов" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_missing_file_exit_code_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--data-dir", str(tmp_path / "nope"), "--out", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", env=UTF8_ENV, check=False,
    )  # fmt: skip
    assert proc.returncode == 2
    assert "не найден" in proc.stderr
