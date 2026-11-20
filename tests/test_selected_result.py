"""result_selected.json: результат по паре пользователя. Официальный result.json остаётся по ТЗ."""

import json
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from coverage.config import load_settings
from coverage.errors import ScenarioError
from coverage.pipeline import Inputs, run_pipeline
from coverage.render import build_map_data
from coverage.result import RESULT_KEYS, build_selected_result, parse_pair
from tests.conftest import OPEN_DATA, ROOT, SMALL
from tests.test_api import files_from

EXPECTED = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pr():
    return run_pipeline(Inputs.from_dir(OPEN_DATA), load_settings(ROOT / "config.yaml"))


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(data_dir=OPEN_DATA)) as c:
        yield c


# 1. страж: official result.json не меняется ------------------------------------------------------------------


def test_official_result_unchanged_after_selected_requests(client):
    assert client.get("/api/result/selected", params={"pair": "C08,C11"}).status_code == 200
    assert client.get("/api/download/result_selected.json", params={"pair": "C08,C11"}).status_code == 200
    assert (
        client.post("/api/scenario", json={"excluded": [{"id": "C09", "reason": "", "category": "other"}]}).status_code
        == 200
    )
    assert client.get("/api/result").json() == EXPECTED
    assert json.loads(client.get("/api/download/result.json").content) == EXPECTED
    # лишний параметр pair официальные эндпоинты игнорируют
    assert client.get("/api/result", params={"pair": "C08,C11"}).json() == EXPECTED
    assert json.loads(client.get("/api/download/result.json", params={"pair": "C08,C11"}).content) == EXPECTED
    assert client.get("/api/mapdata").json()["result"] == EXPECTED


# 2. build_selected_result ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pair", "best", "covered"),
    [
        (("C08", "C11"), ["C08", "C11"], 108),
        (("C08", "C14"), ["C08", "C14"], 108),
        (("C09", "C11"), ["C09", "C11"], 108),
        (("C14", "C09"), ["C09", "C14"], 111),  # порядок нормализуется
    ],
)
def test_build_selected_result(pr, pair, best, covered):
    r = build_selected_result(pr, pair)
    assert tuple(r) == RESULT_KEYS  # ровно 4 ключа в порядке result.json
    assert r["best_candidates"] == best and r["covered_weak_cells"] == covered
    assert r["worst_cells"] == EXPECTED["worst_cells"] and r["dropped_rows"] == EXPECTED["dropped_rows"]


def test_selected_equals_official_for_tz_pair(pr):
    assert build_selected_result(pr, ("C09", "C14")) == EXPECTED


# 3. ошибки ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pair", "text"),
    [
        ("C01,C09", "C01 недопустима: 0 м до B01 (ближе 500 м к БС)"),
        ("C09,C09", "две разные"),
        ("C99,C09", "C99 неизвестна"),
        ("C09", "нужны две площадки"),
        ("C09,C14,C08", "нужны две площадки"),
        ("", "нужны две площадки"),
    ],
)
def test_bad_pairs_raise_and_give_422(pr, client, pair, text):
    with pytest.raises(ScenarioError, match=text.replace("(", r"\(").replace(")", r"\)")):
        build_selected_result(pr, parse_pair(pair))
    for path in ("/api/result/selected", "/api/download/result_selected.json"):
        r = client.get(path, params={"pair": pair})
        assert r.status_code == 422 and text in r.json()["detail"]


def test_pair_param_is_required(client):
    assert client.get("/api/result/selected").status_code == 422


# API: содержимое и вложение ----------------------------------------------------------------------------------------


def test_api_selected_and_attachment(client):
    body = client.get("/api/result/selected", params={"pair": "C14,C08"}).json()
    assert body["best_candidates"] == ["C08", "C14"] and body["covered_weak_cells"] == 108
    r = client.get("/api/download/result_selected.json", params={"pair": "C08,C11"})
    assert 'filename="result_selected.json"' in r.headers["content-disposition"]
    assert json.loads(r.content)["covered_weak_cells"] == 108 and tuple(json.loads(r.content)) == RESULT_KEYS


# 5. загруженный набор (analysis_id) -------------------------------------------------------------------------------------


def test_selected_for_uploaded_dataset(client):
    up = client.post("/api/analyze", files=files_from(SMALL)).json()
    key, official = up["analysis_id"], up["result"]
    pair = ",".join(official["best_candidates"])
    r = client.get("/api/result/selected", params={"pair": pair, "analysis_id": key})
    assert r.status_code == 200 and r.json() == official  # пара по ТЗ даёт тот же результат
    other = client.get("/api/result/selected", params={"pair": "C2,C3", "analysis_id": key})
    assert other.status_code == 200 and other.json()["best_candidates"] == ["C2", "C3"]
    assert other.json()["dropped_rows"] == official["dropped_rows"]
    assert client.get("/api/result/selected", params={"pair": pair, "analysis_id": "nope"}).status_code == 404


# 4. CLI -------------------------------------------------------------------------------------------------------------------


def run_cli(out, *args):
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    import os

    return subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--out", str(out), "--no-map", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **env},
    )


def test_cli_pair_writes_both_files(tmp_path):
    r = run_cli(tmp_path, "--pair", "C08,C11")
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == EXPECTED
    sel = json.loads((tmp_path / "result_selected.json").read_text(encoding="utf-8"))
    assert sel["best_candidates"] == ["C08", "C11"] and sel["covered_weak_cells"] == 108
    assert (
        "Selected pair: C08 + C11 -> 108 (optimum C09 + C14 -> 111, delta -3); written result_selected.json" in r.stdout
    )


def test_cli_without_pair_does_not_write_selected(tmp_path):
    assert run_cli(tmp_path).returncode == 0
    assert not (tmp_path / "result_selected.json").exists()


@pytest.mark.parametrize("pair", ["C01,C09", "C09,C09", "C99,C09", "C09"])
def test_cli_bad_pair_exit_2_but_result_written(tmp_path, pair):
    r = run_cli(tmp_path, "--pair", pair)
    assert r.returncode == 2 and "--pair" in r.stderr
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == EXPECTED
    assert not (tmp_path / "result_selected.json").exists()


def test_cli_pair_is_independent_of_constraints(tmp_path):
    r = run_cli(tmp_path, "--exclude", "C09", "--pair", "C09,C14")
    assert r.returncode == 0, r.stderr
    sel = json.loads((tmp_path / "result_selected.json").read_text(encoding="utf-8"))
    assert sel["best_candidates"] == ["C09", "C14"] and sel["covered_weak_cells"] == 111
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == EXPECTED


# паритет JS и Python ------------------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
def test_js_selected_result_matches_python(pr, tmp_path):
    data = tmp_path / "data.json"
    data.write_text(json.dumps(build_map_data(pr)), encoding="utf-8")
    runner = tmp_path / "run.cjs"
    runner.write_text(
        'const fs=require("fs"); const S=require(process.argv[2]); const d=JSON.parse(fs.readFileSync(process.argv[3],"utf8"));'
        'const pairs=[["C08","C11"],["C14","C09"],["C08","C14"]];'
        "process.stdout.write(JSON.stringify(pairs.map((p)=>S.buildSelectedResult(d,p))));",
        encoding="utf-8",
    )
    out = subprocess.run(
        [shutil.which("node"), str(runner), str(ROOT / "web" / "static" / "scenario.js"), str(data)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    for got, pair in zip(json.loads(out.stdout), [("C08", "C11"), ("C14", "C09"), ("C08", "C14")]):
        assert got == build_selected_result(pr, pair)
