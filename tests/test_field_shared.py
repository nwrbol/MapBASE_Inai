"""Общая база полевых замеров: два пользователя (разные X-Device-Id) работают с одним сервером."""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.conftest import OPEN_DATA

TS = "2026-11-20T22:15:00+06:00"
DEV_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEV_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TOKEN = "s3cret-token"
EXPECTED = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))
CSV = b"lat,lon,rsrp_dbm,timestamp\n{lat},{lon},-101,2026-11-20T21:00:00+06:00\n{lat},{lon},-102,2026-11-20T21:01:00+06:00\n"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    db = tmp_path_factory.mktemp("shared") / "field.db"
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=db, admin_token=TOKEN)) as c:
        yield c.app


@pytest.fixture
def a(app):
    return TestClient(app, headers={"X-Device-Id": DEV_A})


@pytest.fixture
def b(app):
    return TestClient(app, headers={"X-Device-Id": DEV_B})


@pytest.fixture(autouse=True)
def _clean(app):
    app.state.coverage.field_store.clear()
    yield
    app.state.coverage.field_store.clear()


@pytest.fixture(scope="module")
def cell(app):
    c = next(c for c in TestClient(app).get("/api/mapdata").json()["cells"] if c["n"] == 3)
    return {"id": c["id"], "lat": c["c"][0], "lon": c["c"][1]}


def item(cell, rsrp=-110, ts=TS, **kw):
    return {"lat": cell["lat"], "lon": cell["lon"], "rsrp_dbm": rsrp, "timestamp": ts, "source": "map_pin"} | kw


def add(client, cell, **kw):
    r = client.post("/api/field/measurements", json={"items": [item(cell, **kw)]})
    assert r.status_code == 200, r.text
    return r.json()


def version(client):
    return client.get("/api/field/version").json()["version"]


# 1. общая видимость ---------------------------------------------------------------------------------------


def test_everyone_sees_everything_with_is_mine(a, b, cell):
    mid = add(a, cell, author_label="Айбек")["accepted"][0]["id"]
    for who, mine in ((a, True), (b, False)):
        items = who.get("/api/field/measurements").json()["items"]
        assert [i["id"] for i in items] == [mid] and items[0]["is_mine"] is mine
        assert items[0]["author_label"] == "Айбек" and "device_id" not in items[0]  # чужой ключ наружу не уходит
    assert b.get("/api/field/measurements", params={"mine": "true"}).json()["items"] == []
    assert len(a.get("/api/field/measurements", params={"mine": "true"}).json()["items"]) == 1

    d = b.get("/api/mapdata", params={"include_field": "true"}).json()
    assert d["meta"]["field_count"] == 1 and [p["id"] for p in d["field_points"]] == [mid]
    assert d["field_points"][0]["author"] == "Айбек" and d["field_points"][0]["dev"] == DEV_A.replace("-", "")[:8]

    ev = b.get("/api/field/events").json()["events"]
    assert ev[0]["type"] == "add" and ev[0]["is_mine"] is False and ev[0]["author_label"] == "Айбек"
    assert "device_id" not in ev[0] and ev[0]["summary"].startswith("+1 замер в ")
    assert a.get("/api/field/events").json()["events"][0]["is_mine"] is True


def test_since_seq_returns_only_new(a, b, cell):
    add(a, cell, ts="2026-11-20T22:01:00+06:00")
    v = version(b)
    add(a, cell, ts="2026-11-20T22:02:00+06:00")
    new = b.get("/api/field/measurements", params={"since_seq": v}).json()["items"]
    assert len(new) == 1 and new[0]["seq"] == v + 1


# 2. версия ------------------------------------------------------------------------------------------------


def test_version_grows_by_one_per_change(a, cell):
    v = version(a)
    mid = add(a, cell)["accepted"][0]["id"]
    assert version(a) == v + 1
    assert a.delete(f"/api/field/measurements/{mid}").json()["version"] == v + 2
    files = {
        "file": (
            "log.csv",
            CSV.replace(b"{lat}", str(cell["lat"]).encode()).replace(b"{lon}", str(cell["lon"]).encode()),
        )
    }
    r = a.post("/api/field/import/commit", files=files, data={"options": "{}", "confirm_units": "false"})
    assert r.status_code == 200, r.text
    assert version(a) == v + 3 and a.get("/api/field/events").json()["events"][0]["type"] == "import"
    assert a.delete(f"/api/field/batches/{r.json()['batch_id']}").json()["version"] == v + 4
    assert a.delete("/api/field/all", headers={"X-Admin-Token": TOKEN}).json()["version"] == v + 5


def test_version_not_changed_by_rejected_or_dry_run(a, cell):
    add(a, cell)
    v = version(a)
    n_events = len(a.get("/api/field/events").json()["events"])
    again = a.post("/api/field/measurements", json={"items": [item(cell)]}).json()  # дубль
    assert again["accepted"] == [] and again["version"] == v
    bad = a.post("/api/field/measurements", json={"items": [item(cell, rsrp=-30, ts="2026-11-20T23:00:00+06:00")]})
    assert bad.json()["accepted"] == [] and version(a) == v
    dry = a.post(
        "/api/field/measurements", json={"items": [item(cell, ts="2026-11-20T23:30:00+06:00")], "dry_run": True}
    )
    assert dry.status_code == 200 and version(a) == v
    assert len(a.get("/api/field/events").json()["events"]) == n_events


# 3. права ------------------------------------------------------------------------------------------------


def test_foreign_delete_forbidden_own_allowed(a, b, cell):
    mid = add(a, cell)["accepted"][0]["id"]
    r = b.delete(f"/api/field/measurements/{mid}")
    assert r.status_code == 403 and r.json()["detail"] == "Можно удалять только свои замеры"
    assert b.delete("/api/field/measurements/unknown").status_code == 404
    assert len(a.get("/api/field/measurements").json()["items"]) == 1
    assert a.delete(f"/api/field/measurements/{mid}").status_code == 200


def test_foreign_batch_delete_forbidden(a, b, cell):
    p = a.post("/api/field/paste", json={"text": f"{cell['lat']},{cell['lon']},-100,{TS}"}).json()
    assert b.delete(f"/api/field/batches/{p['batch_id']}").status_code == 403
    assert a.delete(f"/api/field/batches/{p['batch_id']}").status_code == 200


def test_clear_requires_admin_token(a, cell, tmp_path):
    add(a, cell)
    v = version(a)
    assert a.delete("/api/field/all").status_code == 403
    assert a.delete("/api/field/all", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert version(a) == v
    r = a.delete("/api/field/all", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200 and r.json()["deleted"] == 1 and version(a) == v + 1
    # токен не задан в окружении -> эндпоинт отключён даже с «правильным» заголовком
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=tmp_path / "x.db")) as free:
        assert free.delete("/api/field/all", headers={"X-Admin-Token": TOKEN}).status_code == 403
        assert free.delete("/api/field/all", headers={"X-Admin-Token": ""}).status_code == 403


def test_admin_token_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "from-env")
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=tmp_path / "e.db")) as c:
        assert c.delete("/api/field/all", headers={"X-Admin-Token": "from-env"}).status_code == 200


# 4. заголовок X-Device-Id -------------------------------------------------------------------------------


def test_missing_or_bad_device_id_is_400_but_get_works(app, cell):
    anon = TestClient(app)
    assert anon.post("/api/field/measurements", json={"items": [item(cell)]}).status_code == 400
    assert anon.post("/api/field/paste", json={"text": "1,2,3,4"}).status_code == 400
    assert anon.delete("/api/field/measurements/x").status_code == 400
    assert anon.delete("/api/field/batches/x").status_code == 400
    bad = {"X-Device-Id": "not-a-uuid"}
    assert anon.post("/api/field/measurements", json={"items": [item(cell)]}, headers=bad).status_code == 400
    assert anon.get("/api/field/measurements").status_code == 200
    assert anon.get("/api/field/measurements", params={"mine": "true"}).status_code == 400
    assert anon.get("/api/field/version").status_code == 200 and anon.get("/api/field/events").status_code == 200


# 7. конкурентность ----------------------------------------------------------------------------------------


def test_eight_threads_by_25_adds(app, cell):
    errors, codes = [], []
    v0 = version(TestClient(app))

    def work(k):
        c = TestClient(app, headers={"X-Device-Id": f"{k:08d}-0000-4000-8000-000000000000"})
        try:
            for j in range(25):
                r = c.post(
                    "/api/field/measurements",
                    json={"items": [item(cell, ts=f"2026-11-20T10:{k:02d}:{j:02d}+06:00")]},
                )
                codes.append(r.status_code)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and set(codes) == {200}
    v = TestClient(app).get("/api/field/version").json()
    assert v["count"] == 200 and v["version"] == v0 + 200 and v["authors"] == 8


# 8. перезапуск ---------------------------------------------------------------------------------------------


def test_restart_keeps_data_and_version(tmp_path, cell):
    db = tmp_path / "persist.db"
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=db), headers={"X-Device-Id": DEV_A}) as first:
        add(first, cell)
        add(first, cell, ts="2026-11-20T22:20:00+06:00")
        v = version(first)
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=db), headers={"X-Device-Id": DEV_B}) as second:
        assert version(second) == v == 2
        items = second.get("/api/field/measurements").json()["items"]
        assert len(items) == 2 and all(i["is_mine"] is False for i in items)
        assert len(second.get("/api/field/events").json()["events"]) == 2


# 10. result.json не меняется ------------------------------------------------------------------------------


def test_result_json_unchanged_by_field_data(a, b, cell):
    add(a, cell)
    add(b, cell, rsrp=-120, ts="2026-11-20T22:30:00+06:00")
    a.get("/api/mapdata", params={"include_field": "true"})
    assert a.get("/api/result").json() == EXPECTED
    assert a.get("/api/mapdata").json()["cleaning"]["dropped"] == EXPECTED["dropped_rows"]


# 5. кэш пересчёта --------------------------------------------------------------------------------------------


def test_mapdata_cache_follows_version(app, a, cell):
    st = app.state.coverage
    before = a.get("/api/mapdata", params={"include_field": "true"}).json()
    n0 = next(c for c in before["cells"] if c["id"] == cell["id"])["n"]
    add(a, cell)
    after = a.get("/api/mapdata", params={"include_field": "true"}).json()
    assert after["meta"]["field_version"] == before["meta"]["field_version"] + 1
    assert [p["id"] for p in before["field_points"]] == [] and len(after["field_points"]) == 1
    assert next(c for c in after["cells"] if c["id"] == cell["id"])["n"] == n0 + 1
    # без изменений повторный запрос не запускает пересчёт и отдаёт тот же объект из кэша
    runs = st.field_recomputes
    for _ in range(3):
        assert a.get("/api/mapdata", params={"include_field": "true"}).json() == after
    assert st.field_recomputes == runs
    view = list(st.field_cache.values())[-1]
    assert view.map_data is not None and len(st.field_cache) <= 4
    # новая версия делает прежнюю запись невалидной сама собой
    add(a, cell, ts="2026-11-20T22:40:00+06:00")
    a.get("/api/mapdata", params={"include_field": "true"})
    assert st.field_recomputes > runs


def test_concurrent_mapdata_requests_compute_once(app, a, cell):
    st = app.state.coverage
    # запись напрямую в хранилище: кэш этой версии ещё не построен (через POST его строит ответ «summary»)
    st.field_store.add_many([item(cell)], DEV_A)
    runs = st.field_recomputes
    out = []

    def get():
        out.append(TestClient(app).get("/api/mapdata", params={"include_field": "true"}).status_code)

    threads = [threading.Thread(target=get) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert out == [200] * 10 and st.field_recomputes == runs + 1


# 6. заголовки ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/field/version",
        "/api/field/measurements",
        "/api/field/events",
        "/api/field/config",
        "/api/mapdata",
        "/api/mapdata?include_field=true",
    ],
)
def test_no_store_on_field_and_mapdata(a, path):
    assert a.get(path).headers["cache-control"] == "no-store"


def test_no_store_on_errors_and_writes(a, cell):
    assert (
        TestClient(a.app).post("/api/field/measurements", json={"items": [item(cell)]}).headers["cache-control"]
        == "no-store"
    )
    assert a.post("/api/field/measurements", json={"items": [item(cell)]}).headers["cache-control"] == "no-store"
    assert "cache-control" not in a.get("/health").headers or a.get("/health").headers["cache-control"] != "no-store"


def test_etag_and_304(a, cell):
    r = a.get("/api/mapdata", params={"include_field": "true"})
    etag = r.headers["etag"]
    assert etag.startswith('"default-') and etag.endswith('-true"')
    same = a.get("/api/mapdata", params={"include_field": "true"}, headers={"If-None-Match": etag})
    assert same.status_code == 304 and same.content == b"" and same.headers["cache-control"] == "no-store"
    assert same.headers["etag"] == etag
    add(a, cell)
    fresh = a.get("/api/mapdata", params={"include_field": "true"}, headers={"If-None-Match": etag})
    assert fresh.status_code == 200 and fresh.headers["etag"] != etag
    # без полевых замеров ETag постоянный и не зависит от версии базы
    base = a.get("/api/mapdata")
    assert a.get("/api/mapdata", headers={"If-None-Match": base.headers["etag"]}).status_code == 304
    # чужой ETag не даёт 304
    assert a.get("/api/mapdata", headers={"If-None-Match": '"other"'}).status_code == 200


# лимит частоты ----------------------------------------------------------------------------------------------------


def test_rate_limit_per_device_gives_429(tmp_path, cell):
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=tmp_path / "r.db", rate_limit_per_min=3)) as c:
        h = {"X-Device-Id": DEV_A}
        codes = [
            c.post(
                "/api/field/measurements", json={"items": [item(cell, ts=f"2026-11-20T22:0{i}:00+06:00")]}, headers=h
            ).status_code
            for i in range(5)
        ]
        assert codes == [200, 200, 200, 429, 429]
        r = c.post("/api/field/measurements", json={"items": [item(cell, ts="2026-11-20T22:09:00+06:00")]}, headers=h)
        assert "Слишком много запросов" in r.json()["detail"] and int(r.headers["retry-after"]) >= 1
        # другое устройство не страдает; dry_run и чтение не считаются
        other = {"X-Device-Id": DEV_B}
        assert (
            c.post(
                "/api/field/measurements", json={"items": [item(cell, ts="2026-11-20T21:00:00+06:00")]}, headers=other
            ).status_code
            == 200
        )
        assert (
            c.post("/api/field/measurements", json={"items": [item(cell)], "dry_run": True}, headers=h).status_code
            == 200
        )
        assert c.get("/api/field/version", headers=h).status_code == 200


def test_rate_limiter_window_slides():
    from api.ratelimit import RateLimiter, RateLimitExceeded

    t = [0.0]
    lim = RateLimiter(2, 60.0, clock=lambda: t[0])
    lim.check("d", "1.1.1.1")
    lim.check("d", "1.1.1.1")
    with pytest.raises(RateLimitExceeded) as e:
        lim.check("d", "1.1.1.1")
    assert e.value.retry_after == 60
    lim.check("d", "2.2.2.2")  # другой IP
    t[0] = 61.0
    lim.check("d", "1.1.1.1")  # окно сдвинулось
