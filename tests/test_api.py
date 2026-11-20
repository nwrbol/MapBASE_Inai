import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.conftest import OPEN_DATA, SMALL


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(data_dir=OPEN_DATA)) as c:
        yield c


def files_from(d, with_bounds=True):
    names = ["measurements", "towers", "candidates"] + (["bounds"] if with_bounds else [])
    ext = {"bounds": "json"}
    return {n: (f"{n}.{ext.get(n, 'csv')}", (d / f"{n}.{ext.get(n, 'csv')}").read_bytes()) for n in names}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/static/app.js" in r.text


def test_result_matches_expected(client):
    import json

    assert client.get("/api/result").json() == json.loads((OPEN_DATA / "expected.json").read_text())


@pytest.mark.parametrize(
    "path",
    [
        "/api/report",
        "/api/grid",
        "/api/towers",
        "/api/candidates",
        "/api/heatmap",
        "/api/daynight",
        "/api/compare",
        "/api/mapdata",
    ],
)
def test_get_endpoints(client, path):
    assert client.get(path).status_code == 200


def test_grid_geojson(client):
    fc = client.get("/api/grid").json()
    assert fc["type"] == "FeatureCollection"
    props = fc["features"][0]["properties"]
    assert {"cell_id", "n", "median_rsrp", "cls", "is_worst", "covered"} <= set(props)


def test_analyze_valid(client):
    r = client.post("/api/analyze", files=files_from(SMALL))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["best_candidates"] == ["C2", "C3"]
    assert body["result"]["dropped_rows"] == 6
    dl = client.get("/api/download/result.json", params={"analysis_id": body["analysis_id"]})
    assert dl.json() == body["result"]
    html = client.get("/api/download/map.html", params={"analysis_id": body["analysis_id"]})
    assert html.status_code == 200 and "L.map" in html.text


def test_analyze_with_params(client):
    r = client.post("/api/analyze", files=files_from(SMALL), data={"coverage_radius_m": "100"})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["settings"]["selection"]["coverage_radius_m"] == 100


def test_analyze_broken_file_422(client):
    files = files_from(SMALL)
    files["measurements"] = ("measurements.csv", b"foo,bar\n1,2\n")
    r = client.post("/api/analyze", files=files)
    assert r.status_code == 422
    assert "measurements.csv" in r.json()["detail"]


def test_analyze_bad_encoding_422(client):
    files = files_from(SMALL)
    files["towers"] = ("towers.csv", "id,lat,lon\nТ1,42.8,74.5\n".encode("cp1251"))
    r = client.post("/api/analyze", files=files)
    assert r.status_code == 422


def test_unknown_analysis_404(client):
    assert client.get("/api/download/result.json", params={"analysis_id": "nope"}).status_code == 404
