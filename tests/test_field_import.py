"""Импорт CSV из инженерных приложений: автоопределение, сопоставление, единицы, лимиты, отмена пакета."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from coverage.config import load_settings
from coverage.field_import import ImportOptions, analyze, detect_mapping, preview_response
from coverage.field_ingest import existing_frame
from coverage.pipeline import Inputs, run_pipeline
from tests.conftest import OPEN_DATA, ROOT

SAMPLES = ROOT / "samples"
EXPECTED = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))
LAT, LON = 42.8512, 74.5803  # внутри района


@pytest.fixture(scope="module")
def settings():
    return load_settings(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def pr(settings):
    return run_pipeline(Inputs.from_dir(OPEN_DATA), settings)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("imp") / "field.db"
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=db)) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(client):
    client.delete("/api/field/measurements", params={"confirm": "yes"})
    yield
    client.delete("/api/field/measurements", params={"confirm": "yes"})


def preview(settings, pr, data: bytes, **opts):
    a = analyze(data, ImportOptions.from_json(opts), settings, existing_frame(pr.clean, None))
    return a, preview_response(a, settings)


def api_preview(client, data: bytes, options=None, name="log.txt"):
    return client.post(
        "/api/field/import/preview", files={"file": (name, data)}, data={"options": json.dumps(options or {})}
    )


def api_commit(client, data: bytes, options=None, confirm=False):
    return client.post(
        "/api/field/import/commit",
        files={"file": ("log.txt", data)},
        data={"options": json.dumps(options or {}), "confirm_units": "true" if confirm else "false"},
    )


def utc_epoch(h=15, m=14, s=3) -> int:
    return int(datetime(2026, 11, 20, h, m, s, tzinfo=timezone.utc).timestamp())


# --- примеры из репозитория -----------------------------------------------------------------------------------


def test_simple_csv_is_recognized_with_high_confidence(settings, pr):
    _a, r = preview(settings, pr, (SAMPLES / "field_simple.csv").read_bytes())
    assert r["detected"]["delimiter"] == "," and r["detected"]["time_format"] == "ISO 8601"
    assert r["mapping"] == {
        "lat": "lat",
        "lon": "lon",
        "rsrp": "rsrp_dbm",
        "timestamp": "timestamp",
        "date": None,
        "tech": None,
        "time": None,
    }
    assert all(r["mapping_confidence"][k] == "high" for k in ("lat", "lon", "rsrp", "timestamp"))
    assert r["warnings"] == [] and r["detected"]["tech_filter"] is False
    assert r["stats"] == {"rows_total": 10, "would_accept": 10, "rejected_by_reason": {}, "filtered_tech": 0}


def test_engineering_like_log(settings, pr):
    _a, r = preview(settings, pr, (SAMPLES / "engineering_app_like.txt").read_bytes())
    d = r["detected"]
    assert d["delimiter"] == "\t" and d["time_format"] == "YYYY.MM.DD_HH.mm.ss" and d["tech_column"] == "NetworkTech"
    assert d["tech_filter"] is True and d["header_row"] == 1  # первая строка — комментарий
    assert r["mapping"]["rsrp"] == "Level" and r["mapping_confidence"]["rsrp"] == "low"  # слабый синоним
    assert any("Level" in w and "проверьте" in w for w in r["warnings"])
    assert r["stats"] == {
        "rows_total": 28,
        "would_accept": 18,
        "rejected_by_reason": {"duplicates": 2, "out_of_bounds": 3},
        "filtered_tech": 5,
    }
    assert r["units_suspect"] is False
    assert {s["status"] for s in r["sample"]} <= {"ok", "rejected", "filtered"}
    assert all(p[2] in (0, 1) for p in r["points"]) and r["bbox"][0] < r["bbox"][2]
    first_ok = next(s for s in r["sample"] if s["status"] == "ok")
    assert first_ok["timestamp"].endswith("+06:00") and first_ok["timestamp"].startswith("2026-11-20T21:10")


def test_engineering_import_through_api_and_cancel(client):
    data = (SAMPLES / "engineering_app_like.txt").read_bytes()
    client.post(
        "/api/field/measurements",
        json={
            "items": [
                {"lat": LAT, "lon": LON, "rsrp_dbm": -100, "timestamp": "2026-11-20T10:00:00+06:00", "source": "manual"}
            ]
        },
    )
    assert api_preview(client, data).status_code == 200
    assert client.get("/api/field/version").json()["count"] == 1  # предпросмотр ничего не пишет
    r = api_commit(client, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 18 and body["filtered_tech"] == 5
    assert body["rejected_by_reason"] == {"duplicates": 2, "out_of_bounds": 3}
    assert body["field_diff"]["summary"]["became_counted"] == 5  # 5 клеток стали учитываемыми
    assert client.get("/api/field/version").json()["count"] == 19
    assert {i["source"] for i in client.get("/api/field/measurements", params={"limit": 50}).json()["items"]} == {
        "manual",
        "import",
    }
    # отмена импорта удаляет ровно его строки
    assert client.delete(f"/api/field/batches/{body['batch_id']}").json()["deleted"] == 18
    left = client.get("/api/field/measurements").json()
    assert left["total"] == 1 and left["items"][0]["source"] == "manual"
    # официальный результат не тронут
    assert client.get("/api/result").json() == EXPECTED
    # повторный импорт того же файла: всё — дубли (в базе уже были бы), но после отмены снова принимается
    assert api_commit(client, data).json()["accepted"] == 18
    again = api_commit(client, data)
    assert again.status_code == 422  # теперь все строки — дубли, импортировать нечего


# --- форматы файлов ---------------------------------------------------------------------------------------------


def test_cp1251_with_russian_headers(settings, pr):
    text = f"Широта,Долгота,RSRP,Timestamp\n{LAT},{LON},-105,2026-11-20T22:00:00+06:00\n"
    _a, r = preview(settings, pr, text.encode("cp1251"))
    assert r["detected"]["encoding"] == "cp1251"
    assert r["mapping"]["lat"] == "Широта" and r["mapping"]["lon"] == "Долгота"
    assert r["stats"]["would_accept"] == 1


def test_utf8_bom_is_handled(settings, pr):
    data = b"\xef\xbb\xbflat,lon,rsrp_dbm,timestamp\n" + f"{LAT},{LON},-105,2026-11-20T22:00:00+06:00\n".encode()
    _, r = preview(settings, pr, data)
    assert r["detected"]["encoding"] == "utf-8" and r["stats"]["would_accept"] == 1


def test_decimal_comma_with_semicolon(settings, pr):
    data = b"lat;lon;rsrp;timestamp\n42,8512;74,5803;-110,5;2026-11-20T22:00:00+06:00\n"
    a, r = preview(settings, pr, data)
    assert r["detected"]["delimiter"] == ";" and r["detected"]["decimal_comma"] is True
    assert r["stats"]["would_accept"] == 1 and a.accepted[0].rsrp_dbm == -110.5 and a.accepted[0].lat == 42.8512


def test_decimal_comma_not_applied_with_comma_delimiter(settings, pr):
    data = b'lat,lon,rsrp,timestamp\n"42,8512","74,5803",-110,2026-11-20T22:00:00+06:00\n'
    _, r = preview(settings, pr, data)
    assert r["detected"]["decimal_comma"] is False and r["stats"]["rejected_by_reason"] == {"unparseable": 1}


@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_delimiter_detection(settings, pr, delim):
    rows = [["lat", "lon", "rsrp", "timestamp"]] + [
        [str(LAT + i * 1e-4), str(LON), "-105", f"2026-11-20T22:0{i}:00+06:00"] for i in range(3)
    ]
    data = "\n".join(delim.join(r) for r in rows).encode()
    _, r = preview(settings, pr, data)
    assert r["detected"]["delimiter"] == delim and r["stats"]["would_accept"] == 3


def test_leading_comments_blank_lines_and_late_header(settings, pr):
    data = (
        f"# exported by app\n\n// v1.2\nlat,lon,rsrp,timestamp\n{LAT},{LON},-105,2026-11-20T22:00:00+06:00\n".encode()
    )
    _, r = preview(settings, pr, data)
    assert r["detected"]["header_row"] == 3 and r["stats"]["would_accept"] == 1


@pytest.mark.parametrize(
    ("date", "time"), [("2026-11-20", "21:14:03"), ("20.11.2026", "21:14:03"), ("2026.11.20", "21:14:03")]
)
def test_separate_date_and_time_columns(settings, pr, date, time):
    data = f"Date,Time,Latitude,Longitude,RSRP\n{date},{time},{LAT},{LON},-105\n".encode()
    a, r = preview(settings, pr, data)
    assert r["mapping"]["date"] == "Date" and r["mapping"]["time"] == "Time" and r["mapping"]["timestamp"] is None
    assert a.accepted[0].timestamp == "2026-11-20T21:14:03+06:00"


@pytest.mark.parametrize("scale", [1, 1000], ids=["seconds", "milliseconds"])
def test_unix_epoch_seconds_and_milliseconds(settings, pr, scale):
    ts = utc_epoch() * scale
    data = f"lat,lon,rsrp,time\n{LAT},{LON},-105,{ts}\n".encode()
    a, r = preview(settings, pr, data)
    assert r["detected"]["time_format"] == "unix epoch"
    assert a.accepted[0].timestamp == "2026-11-20T21:14:03+06:00"


def test_custom_time_format_and_timezone(settings, pr):
    data = f"lat,lon,rsrp,timestamp\n{LAT},{LON},-105,20/11/2026 21:14\n".encode()
    a, _ = preview(settings, pr, data, time_format="%d/%m/%Y %H:%M", tz="UTC")
    assert a.accepted[0].timestamp == "2026-11-21T03:14:00+06:00"  # время в UTC пересчитано в +06:00
    with pytest.raises(Exception, match="time_format"):
        preview(settings, pr, data, time_format="nonsense")


def test_tech_filter_can_be_disabled(settings, pr):
    data = (SAMPLES / "engineering_app_like.txt").read_bytes()
    _, r = preview(settings, pr, data, tech_filter=False)
    assert r["stats"]["filtered_tech"] == 0 and r["stats"]["would_accept"] == 23


def test_manual_mapping_overrides_detection(settings, pr):
    data = f"a,b,c,d\n{LAT},{LON},-105,2026-11-20T22:00:00+06:00\n".encode()
    with pytest.raises(Exception, match="заголовок"):
        preview(settings, pr, data)  # синонимов нет совсем
    data2 = f"lat,lon,x,timestamp\n{LAT},{LON},-105,2026-11-20T22:00:00+06:00\n".encode()
    _, r = preview(settings, pr, data2)
    assert r["missing"] == ["rsrp"] and r["stats"] is None
    a, r2 = preview(settings, pr, data2, mapping={"rsrp": "x"})
    assert r2["mapping"]["rsrp"] == "x" and r2["stats"]["would_accept"] == 1 and a.accepted
    with pytest.raises(Exception, match="нет в файле"):
        preview(settings, pr, data2, mapping={"rsrp": "nope"})


def test_detect_mapping_synonyms_and_case():
    m, conf, warn = detect_mapping(["GPS_Lat", "GPS Lon", "LTE_RSRP", "DateTime", "RAT"])
    assert (
        m["lat"] == "GPS_Lat"
        and m["lon"] == "GPS Lon"
        and m["rsrp"] == "LTE_RSRP"
        and m["timestamp"] == "DateTime"
        and m["tech"] == "RAT"
    )
    assert conf["rsrp"] == "high" and warn == []
    _, conf2, warn2 = detect_mapping(["lat", "lon", "signal", "time"])
    assert conf2["rsrp"] == "low" and "signal" in warn2[0]


# --- единицы ------------------------------------------------------------------------------------------------------


def asu_file() -> bytes:
    rows = "\n".join(f"{LAT + i * 1e-4},{LON},{20 + i},2026-11-20T22:0{i}:00+06:00" for i in range(5))
    return ("lat,lon,Level,timestamp\n" + rows + "\n").encode()


def test_positive_values_warn_and_block_commit(settings, pr, client):
    _, r = preview(settings, pr, asu_file())
    assert r["units_suspect"] is True and r["rsrp_median"] > 0
    assert any("не RSRP" in w for w in r["warnings"])
    blocked = api_commit(client, asu_file())
    assert blocked.status_code == 422 and "подтвердите" in blocked.json()["detail"]
    assert client.get("/api/field/version").json()["count"] == 0
    # с подтверждением значения всё равно проходят обычную проверку диапазона: ничего не «чинится»
    confirmed = api_commit(client, asu_file(), confirm=True)
    assert confirmed.status_code == 422 and "нечего импортировать" in confirmed.json()["detail"]


def test_confirm_units_allows_import_of_valid_rows(client):
    levels = [-160, -160, -160, -100, -101]  # медиана -160: выглядит как другие единицы
    rows = "\n".join(f"{LAT + i * 1e-4},{LON},{lv},2026-11-20T22:0{i}:00+06:00" for i, lv in enumerate(levels))
    data = ("lat,lon,Level,timestamp\n" + rows + "\n").encode()
    assert api_commit(client, data).status_code == 422
    r = api_commit(client, data, confirm=True)
    assert (
        r.status_code == 200
        and r.json()["accepted"] == 2
        and r.json()["rejected_by_reason"] == {"rsrp_out_of_range": 3}
    )


# --- ошибки и лимиты ----------------------------------------------------------------------------------------------


def test_errors_are_human_422(client):
    assert api_preview(client, b"").status_code == 422
    assert api_preview(client, b"   \n  ").status_code == 422
    assert api_preview(client, b"x,y,z\n1,2,3\n").status_code == 422
    r = api_preview(client, b"lat,lon,rsrp,timestamp\n1,2,3,4\n", options={"delimiter": "#"})
    assert r.status_code == 422 and "delimiter" in r.json()["detail"]
    assert (
        client.post("/api/field/import/preview", files={"file": ("a", b"x")}, data={"options": "{bad"}).status_code
        == 422
    )
    assert api_commit(client, b"lat,lon,x,timestamp\n1,2,3,4\n").status_code == 422  # нет столбца RSRP


def test_missing_required_column_preview_is_200_with_missing(client):
    r = api_preview(client, f"lat,lon,rsrp\n{LAT},{LON},-100\n".encode())
    body = r.json()
    assert r.status_code == 200 and body["missing"] == ["timestamp"] and body["stats"] is None


def test_wrong_field_count_row_is_rejected_not_fatal(settings, pr):
    data = f"lat,lon,rsrp,timestamp\n{LAT},{LON},-100,2026-11-20T22:00:00+06:00\n1,2\n".encode()
    _, r = preview(settings, pr, data)
    assert r["stats"]["would_accept"] == 1 and r["stats"]["rejected_by_reason"] == {"unparseable": 1}
    assert any("Неверное число полей" in x.get("message", "") for x in r["rejected_sample"] + r["sample"])


def test_sample_and_points_are_limited(settings, pr):
    b = settings.bounds
    n = 3000
    rows = [
        f"{b.lat_min + 0.001 + (i % 55) * 6e-4:.6f},{b.lon_min + 0.001 + (i // 55) * 8e-4:.6f},-105,2026-11-20T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+06:00"
        for i in range(n)
    ]
    rows += ["0,0,-100,2026-11-21T00:00:00+06:00"] * 25  # вне района (+ дубли)
    data = ("lat,lon,rsrp,timestamp\n" + "\n".join(rows) + "\n").encode()
    _, r = preview(settings, pr, data)
    assert len(r["sample"]) == settings.field_data.sample_rows
    assert len(r["rejected_sample"]) <= settings.field_data.sample_rejected
    assert len(r["points"]) <= 2000 and r["stats"]["rows_total"] == n + 25


def test_size_and_row_limits_give_413(tmp_path):
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("import_max_bytes: 10485760", "import_max_bytes: 400").replace(
        "import_max_rows: 50000", "import_max_rows: 4"
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(cfg, encoding="utf-8")
    with TestClient(create_app(data_dir=OPEN_DATA, config_path=cfg_path, field_db_path=tmp_path / "f.db")) as c:
        too_big = b"lat,lon,rsrp,timestamp\n" + b"1,2,3,2026-11-20T22:00:00+06:00\n" * 40
        for call in (api_preview, api_commit):
            r = call(c, too_big)
            assert r.status_code == 413 and "МБ" in r.json()["detail"]
        many_rows = b"lat,lon,rsrp,timestamp\n" + b"1,2,3,4\n" * 6  # маленький файл, но строк больше лимита
        r = api_preview(c, many_rows)
        assert r.status_code == 413 and "строк" in r.json()["detail"]
        assert api_preview(c, b"lat,lon,rsrp,timestamp\n1,2,3,4\n").status_code == 200


def test_result_json_untouched_by_import(client):
    before = client.get("/api/result").content
    api_commit(client, (SAMPLES / "engineering_app_like.txt").read_bytes())
    api_commit(client, (SAMPLES / "field_simple.csv").read_bytes())
    d = client.get("/api/mapdata", params={"include_field": "true"}).json()
    assert d["meta"]["field_by_source"]["import"] == 28 and d["result"] == EXPECTED
    assert client.get("/api/result").content == before
