"""API сценариев отказов: /api/scenario, /evaluate, /impact, /export. Сервер без состояния."""

import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.conftest import OPEN_DATA, SMALL
from tests.test_api import files_from


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(data_dir=OPEN_DATA)) as c:
        yield c


def refusal(cid, reason="", category="other"):
    return {"id": cid, "reason": reason, "category": category}


def test_no_constraints_is_optimum(client):
    r = client.post("/api/scenario", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] and body["best"]["pair"] == ["C09", "C14"] and body["best"]["covered"] == 111
    assert body["optimum"]["pair"] == ["C09", "C14"] and body["optimum"]["covered"] == 111
    assert body["result_json_unchanged"] is True and body["message"] is None


def test_refusal_response_shape(client):
    body = client.post(
        "/api/scenario", json={"excluded": [refusal("C09", "собственник против", "owner")], "top_n": 5}
    ).json()
    assert body["constraints"] == {"excluded": [refusal("C09", "собственник против", "owner")], "required": []}
    assert body["best"]["pair"] == ["C08", "C11"] and body["best"]["delta_vs_optimum"] == -3
    assert [t["pair"] for t in body["ties"]] == [["C08", "C11"], ["C08", "C14"], ["C16", "C17"]]
    assert len(body["alternatives"]) == 5 and "covered_ids" not in body["alternatives"][0]
    assert {"covered_ids", "lost_vs_optimum", "gained_vs_optimum", "overlap"} <= set(body["best"])
    kinds = {x["kind"]: x for x in body["replacements"]}
    assert kinds["keep_partner"]["pair"] == ["C08", "C14"] and kinds["keep_partner"]["recommended"] is True
    assert kinds["rebuild"]["pair"] == ["C08", "C11"] and kinds["rebuild"]["recommended"] is False
    assert body["candidate_impact"]["C09"] == -3 and body["candidate_impact"]["C08"] == 0


def test_required_scenario(client):
    body = client.post("/api/scenario", json={"excluded": [refusal("C09")], "required": ["C14"]}).json()
    assert body["best"]["pair"] == ["C08", "C14"]
    assert body["alternatives"][1]["pair"] == ["C06", "C14"] and body["alternatives"][1]["covered"] == 101


def test_result_untouched_after_scenarios(client):
    before = client.get("/api/result").content
    download_before = client.get("/api/download/result.json").content
    for payload in (
        {"excluded": [refusal("C09")]},
        {"excluded": [refusal("C09"), refusal("C14")]},
        {"required": ["C14"]},
    ):
        assert client.post("/api/scenario", json=payload).status_code == 200
    client.post("/api/scenario/evaluate", json={"pair": ["C06", "C17"]})
    client.post("/api/scenario/export", json={"excluded": [refusal("C14")]})
    client.get("/api/candidates/impact")
    assert client.get("/api/result").content == before
    assert client.get("/api/download/result.json").content == download_before
    assert json.loads(before) == json.loads((OPEN_DATA / "expected.json").read_text())


def test_deterministic(client):
    payload = {"excluded": [refusal("C09")], "top_n": 10}
    assert client.post("/api/scenario", json=payload).content == client.post("/api/scenario", json=payload).content


@pytest.mark.parametrize(
    "payload",
    [
        {"excluded": [refusal("C99")]},
        {"required": ["C99"]},
        {"required": ["C09", "C14", "C08"]},
        {"excluded": [refusal("C09")], "required": ["C09"]},
        {"required": ["C01"]},
        {"excluded": [refusal("C09", category="bogus")]},
        {"excluded": [refusal("C09", reason="x" * 201)]},
        {"excluded": [{"id": "bad id!"}]},
        {"top_n": 0},
        {"top_n": 51},
    ],
)
def test_invalid_constraints_422(client, payload):
    r = client.post("/api/scenario", json=payload)
    assert r.status_code == 422 and r.json()["detail"]


def test_inadmissible_message_in_russian(client):
    d = client.post("/api/scenario", json={"required": ["C01"]}).json()["detail"]
    assert "C01 нельзя зафиксировать" in d and "(< 500 м)" in d


def test_too_many_refusals_422(client):
    ids = [f"X{i}" for i in range(40)]
    r = client.post("/api/scenario", json={"excluded": [refusal(i) for i in ids]})
    assert r.status_code == 422


def test_infeasible_is_200(client):
    cands = client.get("/api/candidates").json()
    ids = [c["id"] for c in cands if c["admissible"]]
    body = client.post("/api/scenario", json={"excluded": [refusal(i) for i in ids[:-1]]}).json()
    assert body["feasible"] is False and body["best"] is None and "доступно площадок: 1" in body["message"]


def test_evaluate_pair(client):
    ok = client.post("/api/scenario/evaluate", json={"pair": ["C14", "C09"]}).json()
    assert ok["pair"] == ["C09", "C14"] and ok["covered"] == 111 and ok["violates_constraints"] is False
    assert ok["delta_vs_best"] == 0
    bad = client.post("/api/scenario/evaluate", json={"pair": ["C09", "C14"], "excluded": [refusal("C09")]}).json()
    assert bad["violates_constraints"] is True and bad["delta_vs_best"] == 3 and bad["delta_vs_optimum"] == 0
    assert client.post("/api/scenario/evaluate", json={"pair": ["C09", "C09"]}).status_code == 422
    assert client.post("/api/scenario/evaluate", json={"pair": ["C09", "C01"]}).status_code == 422
    assert client.post("/api/scenario/evaluate", json={"pair": ["C09"]}).status_code == 422


def test_impact_endpoint(client):
    impact = client.get("/api/candidates/impact").json()
    assert impact["C09"] == -3 and impact["C14"] == -3 and sum(1 for v in impact.values() if v) == 2


def test_export_attachment(client):
    r = client.post("/api/scenario/export", json={"excluded": [refusal("C09")]})
    assert r.status_code == 200 and "scenario.json" in r.headers["content-disposition"]
    assert r.json()["best"]["pair"] == ["C08", "C11"]


def test_scenario_on_uploaded_dataset(client):
    up = client.post("/api/analyze", files=files_from(SMALL))
    assert up.status_code == 200
    body = up.json()
    key = body["analysis_id"]
    pair = body["result"]["best_candidates"]
    r = client.post("/api/scenario", json={"analysis_id": key, "excluded": [refusal(pair[0])]})
    assert r.status_code == 200
    s = r.json()
    assert s["optimum"]["pair"] == pair and pair[0] not in (s["best"]["pair"] if s["best"] else [])
    assert client.get("/api/candidates/impact", params={"analysis_id": key}).status_code == 200
    # отказ по id открытого набора, которого нет в загруженном, — 422; а чужой анализ — 404
    assert client.post("/api/scenario", json={"analysis_id": key, "excluded": [refusal("C99")]}).status_code == 422
    assert client.post("/api/scenario", json={"analysis_id": "nope"}).status_code == 404
    assert client.get("/api/result").json() == json.loads((OPEN_DATA / "expected.json").read_text())
