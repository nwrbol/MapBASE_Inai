"""Тест-страж: во внутренних формулировках («ТЗ», «жюри», «автопроверка») интерфейса и /docs не должно быть следов."""

import os
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.conftest import OPEN_DATA, ROOT

FORBIDDEN = re.compile(r"жюри|автопровер|по ТЗ|ТЗ 7|INAI 2026", re.IGNORECASE)


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(data_dir=OPEN_DATA)) as c:
        yield c


def test_pages_have_no_internal_wording(client):
    for path in ("/", "/map", "/field"):
        html = client.get(path).text
        m = FORBIDDEN.search(html)
        assert not m, (path, m.group(0))


def test_static_sources_have_no_internal_wording():
    static, templates = ROOT / "web" / "static", ROOT / "web" / "templates"
    files = [*static.glob("*.js"), *static.glob("*.html"), *static.glob("*.css"), *templates.glob("*.j2")]
    assert files
    for f in files:
        m = FORBIDDEN.search(f.read_text(encoding="utf-8"))  # включая комментарии: их видно в исходнике страницы
        assert not m, (f.name, m.group(0))


def test_openapi_has_no_internal_wording(client):
    spec = client.get("/openapi.json").text
    m = FORBIDDEN.search(spec)
    assert not m, m.group(0)
    assert client.get("/openapi.json").json()["info"]["title"] == "Карта покрытия"


def test_user_visible_errors_have_no_internal_wording(client):
    for path, params in (
        ("/api/result/selected", {"pair": "C01,C09"}),
        ("/api/result/selected", {"pair": "C99,C09"}),
        ("/api/result/selected", {"pair": "C09"}),
    ):
        text = client.get(path, params=params).text
        assert not FORBIDDEN.search(text), text
    assert (
        "недопустима: 0 м до B01 (ближе 500 м к БС)"
        in client.get("/api/result/selected", params={"pair": "C01,C09"}).text
    )
    body = client.post("/api/scenario", json={"excluded": [{"id": "C99", "reason": "", "category": "other"}]}).text
    assert not FORBIDDEN.search(body), body


def test_standalone_map_has_no_internal_wording(tmp_path):
    env = {**os.environ, "PYTHONUTF8": "1"}
    r = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    m = FORBIDDEN.search((tmp_path / "map.html").read_text(encoding="utf-8"))
    assert not m, m.group(0)


def test_cli_help_has_no_internal_wording():
    r = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert r.returncode == 0
    assert not FORBIDDEN.search(r.stdout), FORBIDDEN.search(r.stdout).group(0)
