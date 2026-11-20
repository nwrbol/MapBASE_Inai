import json

import pytest

from coverage.assistant.agent import answer
from coverage.assistant.prompt import OFF_TOPIC_REPLY, summarize_view
from coverage.assistant.tools import Context
from coverage.pipeline import Inputs, run_pipeline
from coverage.render import build_map_data
from tests.conftest import OPEN_DATA


@pytest.fixture
def ctx(settings):
    pr = run_pipeline(Inputs.from_dir(OPEN_DATA), settings)
    return Context(pr, build_map_data(pr), {})


class Fake:
    model = "fake"

    def __init__(self, calls, steps=2):
        self.calls = calls
        self.steps = steps
        self.results = []

    def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            self.results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            msg = {"content": "Готово"}
        else:
            assert messages[0]["role"] == messages[1]["role"] == "system"
            assert OFF_TOPIC_REPLY in messages[0]["content"]
            msg = {
                "tool_calls": [
                    {"id": str(i), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                    for i, (name, args) in enumerate(self.calls)
                ]
            }
        return {"choices": [{"message": msg}]}


def test_agent_effects(ctx):
    fake = Fake(
        [
            ("get_summary", {}),
            ("set_candidate_status", {"id": "C09", "status": "excluded"}),
            ("fly_to", {"target": "X999_Y999"}),
            ("set_candidate_status", {"id": "C01", "status": "required"}),
            ("set_layers", {"on": ["evil"]}),
        ]
    )
    r = answer(fake, [{"role": "user", "content": "Исключи C09"}], ctx)
    assert r["steps"] == 2 and len(r["actions"]) == 1
    assert r["actions"][0]["label"] == "Отказ: C09"
    assert fake.results[0]["result"]["covered_weak_cells"] == 111
    assert fake.results[1]["best"]["covered"] == 108
    assert all("error" in v for v in fake.results[2:])
    assert ctx.pr.result == json.loads((OPEN_DATA / "expected.json").read_text())


def test_step_limit_and_view(ctx):
    r = answer(Fake([("get_summary", {})], steps=1), [], ctx)
    assert "переформулируйте" in r["reply"]
    assert summarize_view(ctx.data, None)["colors"]["red"] == ctx.data["stats"]["red"]
    c = ctx.cells["X006_Y013"]
    assert summarize_view(ctx.data, c["b"])["worst"][0]["id"] == c["id"]
