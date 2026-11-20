"""API полевых замеров. Хранилище во временной папке; result.json от них не зависит."""

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.conftest import OPEN_DATA, ROOT

TS = "2026-11-20T22:15:00+06:00"
EXPECTED = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("field") / "field.db"
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=db)) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(client):
    client.delete("/api/field/measurements", params={"confirm": "yes"})
    yield
    client.delete("/api/field/measurements", params={"confirm": "yes"})


@pytest.fixture(scope="module")
def cell3(client):
    """Клетка с n=3 (не учитывается): id и центр."""
    cells = client.get("/api/mapdata").json()["cells"]
    c = next(c for c in cells if c["n"] == 3)
    return {"id": c["id"], "lat": c["c"][0], "lon": c["c"][1]}


def item(cell, rsrp=-110, ts=TS, **kw):
    return {"lat": cell["lat"], "lon": cell["lon"], "rsrp_dbm": rsrp, "timestamp": ts, "source": "map_pin"} | kw


def add(client, items, **kw):
    return client.post("/api/field/measurements", json={"items": items, **kw})


# --- основные эндпоинты -----------------------------------------------------------------------------


def test_version_empty_and_growing(client, cell3):
    assert client.get("/api/field/version").json() == {
        "version": client.get("/api/field/version").json()["version"],
        "count": 0,
    }
    v0 = client.get("/api/field/version").json()["version"]
    assert add(client, [item(cell3)]).status_code == 200
    assert client.get("/api/field/version").json() == {"version": v0 + 1, "count": 1}


def test_add_returns_cell_feedback(client, cell3):
    r = add(client, [item(cell3, -112, "2026-11-20T22:15:00+06:00"), item(cell3, -108, "2026-11-20T22:16:00+06:00")])
    assert r.status_code == 200
    body = r.json()
    assert body["rejected"] == [] and len(body["accepted"]) == 2
    a = body["accepted"][0]
    assert a["cell_id"] == cell3["id"] and a["n_after"] == 5 and a["counted_after"] and a["became_counted"]
    assert a["class_after"] in {"green", "yellow", "red"} and a["id"]
    assert body["summary"]["became_counted"] == 1


def test_still_below_threshold_not_became_counted(client, cell3):
    a = add(client, [item(cell3)]).json()["accepted"][0]
    assert a["n_after"] == 4 and a["counted_after"] is False and a["became_counted"] is False


def test_dry_run_writes_nothing(client, cell3):
    r = add(client, [item(cell3)], dry_run=True)
    assert r.status_code == 200 and r.json()["dry_run"] is True and "id" not in r.json()["accepted"][0]
    assert client.get("/api/field/version").json()["count"] == 0


def test_rejections_have_russian_messages(client, cell3):
    items = [
        item(cell3, -30, "2026-11-20T22:20:00+06:00"),
        {"lat": 0.0, "lon": 0.0, "rsrp_dbm": -100.0, "timestamp": TS, "source": "manual"},
        {"lat": cell3["lat"], "lon": cell3["lon"], "rsrp_dbm": -99.0, "timestamp": "вчера", "source": "manual"},
        item(cell3, -105, "2026-11-20T22:21:00+06:00", source="gps", accuracy_m=80),
    ]
    body = add(client, items).json()
    reasons = {r["index"]: (r["reason"], r["message"]) for r in body["rejected"]}
    assert reasons[0] == ("rsrp_out_of_range", "RSRP −30 вне −140…−44")
    assert reasons[1][0] == "out_of_bounds" and "Точка вне района" in reasons[1][1]
    assert reasons[2][0] == "bad_timestamp"
    assert [a["index"] for a in body["accepted"]] == [3] and body["accepted"][0]["warning"] == "low_accuracy"


def test_duplicate_with_stored_field_measurement(client, cell3):
    assert add(client, [item(cell3)]).json()["accepted"]
    again = add(client, [item(cell3, ts="2026-11-20T16:15:00Z")]).json()
    assert again["accepted"] == [] and again["rejected"][0]["reason"] == "duplicates"


def test_validation_errors_are_422(client, cell3):
    assert client.post("/api/field/measurements", json={"items": []}).status_code == 422
    assert client.post("/api/field/measurements", json={"items": [item(cell3, source="bogus")]}).status_code == 422
    assert add(client, [item(cell3, device_label="x" * 51)]).status_code == 422
    assert add(client, [item(cell3, accuracy_m=-1)]).status_code == 422
    assert add(client, [item(cell3)] * 501).status_code == 422
    assert client.post("/api/field/measurements", json={"items": [{"lat": "abc"}]}).status_code == 422


def test_list_newest_first_and_filter(client, cell3):
    add(client, [item(cell3, -100, "2026-11-20T22:01:00+06:00", source="gps", accuracy_m=9)])
    add(client, [item(cell3, -101, "2026-11-20T22:02:00+06:00")])
    body = client.get("/api/field/measurements").json()
    assert body["total"] == 2 and [i["rsrp_dbm"] for i in body["items"]] == [-101.0, -100.0]
    assert body["items"][0]["cell_id"] == cell3["id"]
    assert [i["source"] for i in client.get("/api/field/measurements", params={"source": "gps"}).json()["items"]] == [
        "gps"
    ]
    assert (
        client.get("/api/field/measurements", params={"limit": 1, "offset": 1}).json()["items"][0]["rsrp_dbm"] == -100.0
    )
    assert client.get("/api/field/measurements", params={"source": "bogus"}).status_code == 422


def test_delete_one_404_and_batch_and_clear(client, cell3):
    mid = add(client, [item(cell3)]).json()["accepted"][0]["id"]
    assert client.delete(f"/api/field/measurements/{mid}").status_code == 200
    assert client.delete(f"/api/field/measurements/{mid}").status_code == 404
    assert client.delete("/api/field/batches/nope").status_code == 404
    paste = client.post("/api/field/paste", json={"text": f"{cell3['lat']},{cell3['lon']},-100,{TS}"}).json()
    assert client.delete(f"/api/field/batches/{paste['batch_id']}").json()["deleted"] == 1
    add(client, [item(cell3)])
    assert client.delete("/api/field/measurements").status_code == 422  # без confirm=yes
    assert client.delete("/api/field/measurements", params={"confirm": "no"}).status_code == 422
    assert client.get("/api/field/version").json()["count"] == 1
    assert client.delete("/api/field/measurements", params={"confirm": "yes"}).json()["deleted"] == 1


def test_cell_lookup(client, cell3):
    r = client.get("/api/field/cell", params={"lat": cell3["lat"], "lon": cell3["lon"]}).json()
    assert r["cell_id"] == cell3["id"] and r["n"] == 3 and r["in_bounds"] is True and r["counted"] is False
    add(client, [item(cell3, -112, "2026-11-20T22:15:00+06:00"), item(cell3, -108, "2026-11-20T22:16:00+06:00")])
    r2 = client.get("/api/field/cell", params={"lat": cell3["lat"], "lon": cell3["lon"]}).json()
    assert r2["n"] == 5 and r2["counted"] is True and r2["cls"] in {"green", "yellow", "red"}
    out = client.get("/api/field/cell", params={"lat": 0, "lon": 0}).json()
    assert out["in_bounds"] is False and out["n"] == 0
    assert client.get("/api/field/cell", params={"lat": 999, "lon": 0}).status_code == 422


# --- вставка строк --------------------------------------------------------------------------------------


def paste(client, text, **kw):
    return client.post("/api/field/paste", json={"text": text, **kw})


def test_paste_with_and_without_header(client, cell3):
    line = f"{cell3['lat']},{cell3['lon']},-100,2026-11-20T22:30:00+06:00"
    with_header = paste(client, "lat,lon,rsrp_dbm,timestamp\n" + line, dry_run=True).json()
    without = paste(client, line, dry_run=True).json()
    assert len(with_header["accepted"]) == len(without["accepted"]) == 1
    assert with_header["rows"][0]["raw"] == without["rows"][0]["raw"]


def test_paste_messy_text_crlf_blank_lines_spaces_and_bad_row(client, cell3):
    lat, lon = cell3["lat"], cell3["lon"]
    text = (
        "lat,lon,rsrp_dbm,timestamp\r\n"
        f"  {lat} , {lon} , -100 , 2026-11-20T22:30:00+06:00 \r\n"
        "\r\n"
        f"{lat},{lon},-101\r\n"  # не хватает поля
        f"{lat},{lon},-102,2026-11-20T22:31:00+06:00\r\n"
        "\r\n"
    )
    r = paste(client, text)
    assert r.status_code == 200
    body = r.json()
    assert [a["index"] for a in body["accepted"]] == [0, 2] and body["rejected"][0]["index"] == 1
    assert "4 поля" in body["rejected"][0]["message"]
    assert [row["status"] for row in body["rows"]] == ["ok", "rejected", "ok"]
    assert body["batch_id"] and client.get("/api/field/version").json()["count"] == 2
    batch = {i["batch_id"] for i in client.get("/api/field/measurements").json()["items"]}
    assert batch == {body["batch_id"]}  # у пакета общий batch_id


def test_paste_has_header_conflicts_and_empty(client):
    assert paste(client, "1,2,3,4", has_header=True).status_code == 422
    assert paste(client, "\n  \n").status_code == 422
    assert client.post("/api/field/paste", json={"text": ""}).status_code == 422


def test_paste_example_from_ui(client):
    r = paste(client, "42.851200,74.580300,-112,2026-11-20T22:15:00+06:00", dry_run=True).json()
    assert len(r["accepted"]) == 1 and r["accepted"][0]["cell_id"]


# --- /api/mapdata -----------------------------------------------------------------------------------------


def test_mapdata_without_field_is_unchanged(client, cell3):
    base = client.get("/api/mapdata").content
    add(client, [item(cell3, -112, "2026-11-20T22:15:00+06:00"), item(cell3, -108, "2026-11-20T22:16:00+06:00")])
    assert client.get("/api/mapdata").content == base
    assert client.get("/api/mapdata", params={"include_field": "false"}).content == base
    assert b"field_points" not in base and b"n_field" not in base


def test_mapdata_with_field(client, cell3):
    add(
        client,
        [
            item(cell3, -112, "2026-11-20T22:15:00+06:00", source="gps", accuracy_m=7),
            item(cell3, -108, "2026-11-20T22:16:00+06:00"),
        ],
    )
    d = client.get("/api/mapdata", params={"include_field": "true"}).json()
    assert d["meta"]["field_count"] == 2 and d["meta"]["field_included"] is True
    assert d["meta"]["field_by_source"] == {"map_pin": 1, "gps": 1, "manual": 0, "import": 0}
    cell = next(c for c in d["cells"] if c["id"] == cell3["id"])
    assert cell["n"] == 5 and cell["n_field"] == 2 and cell["counted"] is True
    assert len(d["field_points"]) == 2 and {p["cell_id"] for p in d["field_points"]} == {cell3["id"]}
    assert d["field_diff"]["summary"]["became_counted"] == 1
    assert any(c["cell_id"] == cell3["id"] for c in d["field_diff"]["cells"])
    # официальный результат и dropped_rows не меняются
    assert d["result"] == EXPECTED and d["cleaning"]["dropped"] == 200
    assert "field_result" in d
    # кэш по версии: повторный запрос даёт то же самое
    assert client.get("/api/mapdata", params={"include_field": "true"}).json() == d
    # без замеров include_field=true возвращает карту с пустым diff
    client.delete("/api/field/measurements", params={"confirm": "yes"})
    empty = client.get("/api/mapdata", params={"include_field": "true"}).json()
    assert empty["meta"]["field_count"] == 0 and empty["field_diff"]["cells"] == []


# --- result.json неизменен -------------------------------------------------------------------------------------


def test_result_json_never_changes(client, cell3, tmp_path):
    before = client.get("/api/result").content
    download_before = client.get("/api/download/result.json").content
    ids = [
        x["id"]
        for x in add(
            client, [item(cell3, -112, "2026-11-20T22:15:00+06:00"), item(cell3, -108, "2026-11-20T22:16:00+06:00")]
        ).json()["accepted"]
    ]
    batch = paste(client, f"{cell3['lat']},{cell3['lon']},-120,2026-11-20T22:17:00+06:00").json()["batch_id"]
    client.get("/api/mapdata", params={"include_field": "true"})
    assert client.get("/api/result").content == before
    assert client.get("/api/result", params={"include_field": "true"}).content == before  # параметр не принимается
    assert client.get("/api/download/result.json", params={"include_field": "true"}).content == download_before
    client.delete(f"/api/field/measurements/{ids[0]}")
    client.delete(f"/api/field/batches/{batch}")
    assert client.get("/api/result").content == before
    assert json.loads(before) == EXPECTED

    # CLI: поле db не читается, result.json побайтово тот же
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    for out in (out_a, out_b):
        subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "--out", str(out), "--no-map"],
            check=True,
            capture_output=True,
            env=env,
        )
    assert (out_a / "result.json").read_bytes() == (out_b / "result.json").read_bytes()
    assert json.loads((out_a / "result.json").read_text(encoding="utf-8")) == EXPECTED


def test_field_page_route(client):
    r = client.get("/field")
    assert r.status_code in (200, 404)  # 404 до появления field.html (этап 3)
