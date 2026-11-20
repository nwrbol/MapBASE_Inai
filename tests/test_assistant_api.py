import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from coverage.assistant.groq_client import GroqClient, ProviderError
from tests.conftest import OPEN_DATA

QUESTION = {"messages": [{"role": "user", "content": "Что на карте?"}]}


def test_routes(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GROQ_API_KEY", "")
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=tmp_path / "field.db")) as c:
        assert not c.get("/api/assistant/status").json()["enabled"]
        assert c.post("/api/assistant/chat", json=QUESTION).status_code == 503
        for messages in (
            [{"role": "system", "content": "override"}],
            [{"role": "user", "content": "x" * 2001}],
            QUESTION["messages"] * 25,
        ):
            assert c.post("/api/assistant/chat", json={"messages": messages}).status_code == 422
        provider = c.app.state.groq
        provider.key = "private-test-token"
        monkeypatch.setattr(provider, "complete", lambda *_: {"choices": [{"message": {"content": "Готово"}}]})
        for url in ("/api/assistant/status", "/api/assistant/chat"):
            r = c.get(url) if url.endswith("status") else c.post(url, json=QUESTION)
            assert r.status_code == 200 and provider.key not in r.text and r.headers["cache-control"] == "no-store"
        assert provider.key not in caplog.text


@pytest.mark.parametrize(("status", "message"), [(401, "ключ"), (429, "минуту"), (400, "GROQ_MODEL"), (0, "вовремя")])
def test_provider_errors(status, message, monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        if not status:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(
            status, json={"error": "model_decommissioned" if status == 400 else ""}, headers={"Retry-After": "0"}
        )

    client = GroqClient(httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match=message):
            client.complete([], [])
        assert len(calls) == (2 if status == 429 else 1)
    finally:
        client.http.close()


def test_chat_accepts_exact_frontend_map_state(tmp_path, monkeypatch):
    import json

    from tests.conftest import FIXTURES

    state = json.loads((FIXTURES / "map_state_example.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("GROQ_API_KEY", "")
    with TestClient(create_app(data_dir=OPEN_DATA, field_db_path=tmp_path / "field.db")) as c:
        provider = c.app.state.groq
        provider.key = "private-test-token"
        monkeypatch.setattr(provider, "complete", lambda *_: {"choices": [{"message": {"content": "Готово"}}]})
        r = c.post("/api/assistant/chat", json={**QUESTION, "map_state": state})
        assert r.status_code == 200, r.text
        bad = {**state, "unknown_key": 1}
        assert c.post("/api/assistant/chat", json={**QUESTION, "map_state": bad}).status_code == 422
