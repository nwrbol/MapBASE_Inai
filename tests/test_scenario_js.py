"""JS-движок сценариев (web/static/scenario.js, работает в автономном map.html) должен совпадать с Python.

Нужен node; на GitHub Actions он есть, локально тест пропускается, если node не установлен.
"""

import json
import shutil
import subprocess

import pytest

from coverage.config import load_settings
from coverage.pipeline import Inputs, run_pipeline
from coverage.render import build_map_data
from coverage.scenario import Constraints, run_scenario
from tests.conftest import OPEN_DATA, ROOT

NODE = shutil.which("node")

RUNNER = """
const fs = require("fs");
const SC = require(process.argv[2]);
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const cases = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
process.stdout.write(JSON.stringify(cases.map((c) => SC.runScenario(data, c, 10))));
"""


def refusal(cid, category="other"):
    return {"id": cid, "reason": "", "category": category}


CASES = [
    {"excluded": [], "required": []},
    {"excluded": [refusal("C09", "owner")], "required": []},
    {"excluded": [refusal("C14")], "required": []},
    {"excluded": [refusal("C09"), refusal("C14")], "required": []},
    {"excluded": [refusal("C09")], "required": ["C14"]},
    {"excluded": [refusal("C14")], "required": ["C09"]},
    {"excluded": [], "required": ["C06", "C17"]},
    {"excluded": [refusal(f"C{i:02d}") for i in range(5, 21)], "required": []},  # невыполнимый
]


@pytest.mark.skipif(NODE is None, reason="node не установлен")
def test_js_engine_matches_python(tmp_path):
    pr = run_pipeline(Inputs.from_dir(OPEN_DATA), load_settings(ROOT / "config.yaml"))
    (tmp_path / "data.json").write_text(json.dumps(build_map_data(pr)), encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps(CASES), encoding="utf-8")
    (tmp_path / "run.js").write_text(RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [
            NODE,
            str(tmp_path / "run.js"),
            str(ROOT / "web" / "static" / "scenario.js"),
            str(tmp_path / "data.json"),
            str(tmp_path / "cases.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    js = json.loads(proc.stdout)
    expected = [json.loads(json.dumps(run_scenario(pr, Constraints.from_dict(c), 10).to_dict())) for c in CASES]
    assert js == expected
