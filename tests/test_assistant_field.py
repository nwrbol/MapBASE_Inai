"""ИИ-ассистент и полевые замеры: подсказки, проверка точки, предложение замера (без записи в базу)."""

import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from coverage.assistant.tools import Context
from coverage.field_store import FieldStore
from coverage.pipeline import Inputs, run_pipeline
from coverage.render import build_map_data
from tests.conftest import OPEN_DATA, ROOT

DEV = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TS = "2026-11-20T22:15:00+06:00"
EXPECTED = json.loads((OPEN_DATA / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    s = FieldStore(tmp_path / "f.db")
    yield s
    s.close()


@pytest.fixture
def ctx(settings, store):
    pr = run_pipeline(Inputs.from_dir(OPEN_DATA), settings)
    c = Context(pr, build_map_data(pr), {"view": {"center": [42.855, 74.58]}})
    c.field_store, c.base_pr, c.device_id = store, pr, DEV
    return c


def cell4(ctx):
    return next(c for c in ctx.data["cells"] if c["n"] == 4)


def test_cells_needing_data(ctx):
    r = ctx.execute("cells_needing_data", {"limit": 30})
    assert r["n_lt_min"] == 11 and r["empty"] == len(ctx.data["empty_cells"]) and r["total"] == 11 + r["empty"]
    d = [c["distance_m"] for c in r["cells"]]
    assert d == sorted(d) and all(c["missing"] == 5 - c["n"] for c in r["cells"])
    only = ctx.execute("cells_needing_data", {"limit": 30, "include_empty": False})
    assert len(only["cells"]) == 11
    near = cell4(ctx)["c"]
    first = ctx.execute("cells_needing_data", {"near": {"lat": near[0], "lon": near[1]}, "limit": 1})["cells"][0]
    assert first["distance_m"] < 200


def test_check_point(ctx):
    out = ctx.execute("check_point", {"lat": 0.0, "lon": 0.0})
    assert out["in_bounds"] is False and out["will_become_counted"] is False
    lat, lon = cell4(ctx)["c"]
    r = ctx.execute("check_point", {"lat": lat, "lon": lon})
    assert r["in_bounds"] and r["n"] == 4 and r["will_become_counted"] is True


def test_propose_valid_is_only_a_proposal(ctx, store):
    lat, lon = cell4(ctx)["c"]
    r = ctx.execute("propose_field_measurement", {"lat": lat, "lon": lon, "rsrp_dbm": -112, "timestamp": TS})
    assert r["proposed"] and r["n_before"] == 4 and r["n_after"] == 5 and r["becomes_counted"] is True
    (a,) = ctx.actions
    assert a["type"] == "propose_field_measurement" and a["args"]["rsrp_dbm"] == -112.0
    assert a["label"].startswith("Добавить замер: ") and "−112 дБм" in a["label"] and "(4→5)" in a["label"]
    assert store.total() == 0 and store.version() == 0  # в базу ничего не записано


def test_propose_rejects_invalid(ctx):
    lat, lon = cell4(ctx)["c"]
    with pytest.raises(ValueError, match="RSRP"):
        ctx.execute("propose_field_measurement", {"lat": lat, "lon": lon, "rsrp_dbm": -30, "timestamp": TS})
    with pytest.raises(ValueError, match="вне района"):
        ctx.execute("propose_field_measurement", {"lat": 0.0, "lon": 0.0, "rsrp_dbm": -100, "timestamp": TS})
    with pytest.raises(ValueError, match="ISO"):
        ctx.execute("propose_field_measurement", {"lat": lat, "lon": lon, "rsrp_dbm": -100, "timestamp": "вчера"})
    assert ctx.actions == []


def test_proposal_limit_per_answer(ctx):
    lat, lon = cell4(ctx)["c"]
    for i in range(5):
        ctx.execute(
            "propose_field_measurement",
            {"lat": lat, "lon": lon, "rsrp_dbm": -100 - i, "timestamp": f"2026-11-20T22:0{i}:00+06:00"},
        )
    with pytest.raises(ValueError, match="не больше 5"):
        ctx.execute("propose_field_measurement", {"lat": lat, "lon": lon, "rsrp_dbm": -111, "timestamp": TS})
    assert len(ctx.actions) == 5


def test_open_field_form_validates_and_has_no_side_effects(ctx, store):
    ctx.execute("open_field_form", {"lat": 42.85, "lon": 74.58, "rsrp_dbm": -110, "method": "gps"})
    ctx.execute("open_field_form", {})
    assert [a["args"] for a in ctx.actions] == [{"lat": 42.85, "lon": 74.58, "rsrp_dbm": -110, "method": "gps"}, {}]
    for bad in ({"rsrp_dbm": -30}, {"lat": 100}, {"method": "file"}):
        with pytest.raises(ValueError):
            ctx.execute("open_field_form", bad)
    assert store.total() == 0
    src = (ROOT / "web" / "static" / "assistant.js").read_text(encoding="utf-8")
    assert "URLSearchParams" in src  # значения в ссылке кодируются, а не склеиваются строкой


def test_list_events_and_stats_hide_device(ctx, store):
    store.add_many([{"lat": 42.85, "lon": 74.58, "rsrp_dbm": -100, "timestamp": TS, "source": "manual"}], DEV, "Айбек")
    items = ctx.execute("list_field_measurements", {})
    assert items["total"] == 1 and items["items"][0]["author"] == "Айбек" and "device" not in json.dumps(items)
    assert ctx.execute("list_field_measurements", {"mine": True})["total"] == 1
    ctx.device_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert ctx.execute("list_field_measurements", {"mine": True})["total"] == 0
    ev = ctx.execute("get_field_events", {})
    assert ev["events"][0]["type"] == "add" and DEV not in json.dumps(ev)
    assert ctx.execute("get_field_stats", {})["version"] == 1


def test_show_field_points_requires_data(ctx):
    with pytest.raises(ValueError, match="пока нет"):
        ctx.execute("show_field_points", {})


def test_no_dangerous_field_tools():
    from coverage.assistant.tools import REGISTRY

    for name in REGISTRY:
        assert not any(w in name for w in ("delete", "clear", "import", "remove"))


def test_chat_route_proposal_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=tmp_path / "f.db")) as c:
        provider = c.app.state.groq
        provider.key = "private-test-token"
        lat, lon = next(x for x in c.get("/api/mapdata").json()["cells"] if x["n"] == 4)["c"]
        calls = iter(
            [
                {
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {
                                "name": "propose_field_measurement",
                                "arguments": json.dumps({"lat": lat, "lon": lon, "rsrp_dbm": -112}),
                            },
                        }
                    ]
                },
                {"content": "Предлагаю добавить замер"},
            ]
        )
        monkeypatch.setattr(provider, "complete", lambda *_: {"choices": [{"message": next(calls)}]})
        r = c.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Добавь замер −112"}]},
            headers={"X-Device-Id": DEV},
        )
        assert r.status_code == 200, r.text
        assert r.json()["actions"][0]["type"] == "propose_field_measurement"
        assert c.get("/api/field/version").json()["count"] == 0
        assert c.get("/api/result").json() == EXPECTED
