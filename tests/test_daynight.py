"""Легенда «день/ночь»: пороги шкалы Δ и подписи окна идут из конфига и данных, а не из JS."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from coverage.config import load_settings, settings_from_dict
from coverage.errors import ConfigError
from coverage.pipeline import Inputs, run_pipeline
from coverage.render import render_map_html
from tests.conftest import OPEN_DATA, ROOT

EXPECTED = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(data_dir=OPEN_DATA)) as c:
        yield c


def test_mapdata_meta_has_bins_and_window(client):
    meta = client.get("/api/mapdata").json()["meta"]
    assert meta["daynight_delta_bins"] == [-3, 0, 3]
    assert meta["daynight_window"] == "[08:00, 20:00) Asia/Bishkek"


def test_delta_items_have_day_and_night_medians(client):
    delta = client.get("/api/mapdata").json()["daynight"]["delta"]
    assert delta
    for c in delta:
        assert {"id", "d", "m_day", "m_night", "n_day", "n_night", "b"} <= set(c)
        assert abs(c["d"] - (c["m_night"] - c["m_day"])) < 1e-9
        assert c["n_day"] >= 5 and c["n_night"] >= 5  # Δ только для клеток, учитываемых и днём, и ночью


def test_standalone_map_html_embeds_bins_and_layers_script():
    pr = run_pipeline(Inputs.from_dir(OPEN_DATA), load_settings(ROOT / "config.yaml"))
    html = render_map_html(pr)
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["meta"]["daynight_delta_bins"] == [-3, 0, 3] and data["meta"]["daynight_window"]
    assert "CoverageLayers" in html and "deltaLegendItems" in html  # layers.js встроен, без внешних файлов


def test_index_loads_layers_script_before_app(client):
    html = client.get("/").text
    assert html.index("/static/layers.js") < html.index("/static/app.js")
    assert client.get("/static/layers.js").status_code == 200


def test_result_json_unchanged(client):
    assert client.get("/api/result").json() == EXPECTED


def test_custom_bins_reach_meta(tmp_path):
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8").replace("[-3, 0, 3]", "[-5, 0, 2.5]")
    path = tmp_path / "config.yaml"
    path.write_text(cfg, encoding="utf-8")
    with TestClient(create_app(data_dir=OPEN_DATA, config_path=path)) as c:
        assert c.get("/api/mapdata").json()["meta"]["daynight_delta_bins"] == [-5, 0, 2.5]


@pytest.mark.parametrize("bins", [[-3, 0], [3, 0, -3], [-3, 0, 0], ["a", 0, 3], [-3, 0, 3, 6]])
def test_bad_bins_are_rejected(bins):
    with pytest.raises(ConfigError, match="delta_bins_db"):
        settings_from_dict({"daynight": {"delta_bins_db": bins}})


def test_js_legend_unit_tests_with_node():
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node не установлен: проверки легенды описаны в README, «Проверка UI»")
    r = subprocess.run([node, "--test", str(ROOT / "tests" / "js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_delta_insufficient_complements_delta(client):
    d = client.get("/api/mapdata").json()
    dn, counted = d["daynight"], d["stats"]["cells_counted"]
    assert dn["delta_insufficient"]
    assert len(dn["delta"]) + len(dn["delta_insufficient"]) == counted
    assert not {c["id"] for c in dn["delta"]} & {c["id"] for c in dn["delta_insufficient"]}
    min_n = d["meta"]["min_samples"]
    by_id = {c["id"]: c for c in d["cells"]}
    for c in dn["delta_insufficient"]:
        assert {"id", "b", "n_day", "n_night"} <= set(c)
        assert by_id[c["id"]]["counted"] and (c["n_day"] < min_n or c["n_night"] < min_n)
    assert len(dn["delta"]) == dn["common_cells"]
