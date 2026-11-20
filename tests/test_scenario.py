"""Сценарии отказов: ядро (coverage.scenario). Ожидания посчитаны независимой реализацией, см. CLAUDE_CODE_scenario_constraints.md."""

import json

import pytest

from coverage.config import load_settings
from coverage.errors import DataValidationError, ScenarioError
from coverage.pipeline import Inputs, run_pipeline
from coverage.result import dumps
from coverage.scenario import (
    Constraints,
    Refusal,
    enumerate_pairs,
    evaluate_manual,
    evaluate_pair,
    rank_pairs,
    run_scenario,
    validate_constraints,
)
from tests.conftest import OPEN_DATA, ROOT


@pytest.fixture(scope="module")
def pr():
    return run_pipeline(Inputs.from_dir(OPEN_DATA), load_settings(ROOT / "config.yaml"))


def cons(excluded=(), required=()) -> Constraints:
    return Constraints(tuple(Refusal(x) for x in excluded), tuple(required))


def pairs(options) -> list[tuple[str, str]]:
    return [o.pair for o in options]


# 1. без ограничений
def test_no_constraints_equals_optimum(pr):
    s = run_scenario(pr, cons())
    assert s.feasible and s.best.pair == ("C09", "C14") and s.best.covered == 111
    assert list(s.optimum.pair) == pr.result["best_candidates"]
    assert s.optimum.covered == pr.result["covered_weak_cells"]
    assert s.best.delta_vs_optimum == 0 and s.ties == [s.best]


# 2–6. таблица из §0
@pytest.mark.parametrize(
    ("excluded", "required", "best", "covered", "ties", "next_pair"),
    [
        (["C09"], [], ("C08", "C11"), 108, [("C08", "C11"), ("C08", "C14"), ("C16", "C17")], None),
        (["C14"], [], ("C08", "C11"), 108, [("C08", "C11"), ("C09", "C11"), ("C16", "C17")], None),
        (["C09", "C14"], [], ("C08", "C11"), 108, [("C08", "C11"), ("C16", "C17")], None),
        (["C09"], ["C14"], ("C08", "C14"), 108, [("C08", "C14")], (("C06", "C14"), 101)),
        (["C14"], ["C09"], ("C09", "C11"), 108, [("C09", "C11")], (("C09", "C16"), 105)),
    ],
)
def test_scenario_table(pr, excluded, required, best, covered, ties, next_pair):
    s = run_scenario(pr, cons(excluded, required))
    assert s.best.pair == best and s.best.covered == covered and s.best.delta_vs_optimum == -3
    assert pairs(s.ties) == ties
    if next_pair:
        nxt = s.alternatives[len(s.ties)]
        assert (nxt.pair, nxt.covered) == next_pair


# 7. варианты замены
def test_replacements_on_refusal(pr):
    s = run_scenario(pr, cons(["C09"]))
    keep, rebuild = s.replacements
    assert (keep.kind, keep.pair, keep.covered, keep.recommended, keep.kept, keep.replaced) == (
        "keep_partner",
        ("C08", "C14"),
        108,
        True,
        "C14",
        "C09",
    )
    assert (rebuild.kind, rebuild.pair, rebuild.covered, rebuild.recommended) == ("rebuild", ("C08", "C11"), 108, False)


def test_no_replacements_without_refusal_in_optimum(pr):
    assert run_scenario(pr, cons()).replacements == []
    assert run_scenario(pr, cons(["C01"])).replacements == []
    assert run_scenario(pr, cons(["C09", "C14"])).replacements == []


# 8. критичность
def test_candidate_impact(pr):
    impact = run_scenario(pr, cons()).candidate_impact
    assert impact["C09"] == -3 and impact["C14"] == -3
    assert all(v == 0 for k, v in impact.items() if k not in ("C09", "C14"))
    assert set(impact) == {c.id for c in pr.candidates if c.admissible}


# 9. валидация
@pytest.mark.parametrize(
    ("constraints", "fragment"),
    [
        (cons(["C99"]), "неизвестная площадка"),
        (cons(required=["C99"]), "неизвестная площадка"),
        (cons(required=["C09", "C14", "C08"]), "не больше 2"),
        (cons(["C09"], ["C09"]), "одновременно"),
        (cons(required=["C09", "C09"]), "дважды"),
        (Constraints((Refusal("C09", category="bogus"),)), "категория"),
        (Constraints((Refusal("C09", reason="x" * 201),)), "длиннее"),
    ],
)
def test_validation_errors(pr, constraints, fragment):
    assert any(fragment in e for e in validate_constraints(constraints, pr.candidates))
    with pytest.raises(ScenarioError, match=fragment):
        run_scenario(pr, constraints)


def test_cannot_require_inadmissible_candidate(pr):
    bad = next(c for c in pr.candidates if not c.admissible)
    with pytest.raises(
        ScenarioError, match=f"{bad.id} нельзя зафиксировать: .* м до {bad.nearest_tower} \\(< 500 м\\)"
    ):
        run_scenario(pr, cons(required=[bad.id]))


def test_scenario_error_is_data_validation_error():
    assert issubclass(ScenarioError, DataValidationError)  # CLI даёт код 2, API — 422


# 10. невыполнимый сценарий — не исключение
def test_infeasible_when_only_one_left(pr):
    ids = sorted(c.id for c in pr.candidates if c.admissible)
    s = run_scenario(pr, cons(ids[:-1]))
    assert not s.feasible and s.best is None and s.alternatives == []
    assert "осталось 1" in s.message
    s0 = run_scenario(pr, cons(ids))
    assert not s0.feasible and "осталось 0" in s0.message


# 11. синтетика: арифметика пар
SYN = {
    "A": frozenset({"1", "2", "3", "4"}),
    "B": frozenset({"1", "2", "5"}),
    "C": frozenset({"3", "4", "6"}),
}


def test_pair_option_arithmetic_and_diff():
    opt = frozenset({"1", "2", "3", "4", "5", "6"})  # оптимум B+C
    o = evaluate_pair(SYN, "B", "A", opt)  # порядок аргументов не важен
    assert o.pair == ("A", "B")
    assert (o.covered, o.only_a, o.only_b, o.overlap) == (5, 2, 1, 2)
    assert o.only_a + o.only_b + o.overlap == o.covered
    assert o.covered_ids == frozenset({"1", "2", "3", "4", "5"})
    assert o.delta_vs_optimum == -1
    assert o.lost_vs_optimum == frozenset({"6"}) and o.gained_vs_optimum == frozenset()


def test_gained_cells_synthetic():
    o = evaluate_pair(SYN, "A", "C", frozenset({"1", "2", "5"}))  # «оптимум» = только B
    assert o.gained_vs_optimum == frozenset({"3", "4", "6"})
    assert o.lost_vs_optimum == frozenset({"5"})


def test_rank_pairs_order_and_required_and_excluded():
    assert pairs(rank_pairs(SYN, cons())) == [("B", "C"), ("A", "B"), ("A", "C")]
    assert pairs(rank_pairs(SYN, cons(required=["A"]))) == [("A", "B"), ("A", "C")]
    assert pairs(rank_pairs(SYN, cons(required=["A", "B"]))) == [("A", "B")]
    assert pairs(rank_pairs(SYN, cons(excluded=["C"]))) == [("A", "B")]
    assert rank_pairs(SYN, cons(excluded=["A", "B"])) == []
    assert len(rank_pairs(SYN, cons(), top_n=2)) == 2


def test_ties_resolved_lexicographically():
    covers = {"X": frozenset({"1"}), "Y": frozenset({"2"}), "Z": frozenset({"3"})}
    assert pairs(enumerate_pairs(covers, cons())) == [("X", "Y"), ("X", "Z"), ("Y", "Z")]


def test_evaluate_pair_rejects_unknown_and_same():
    with pytest.raises(ScenarioError):
        evaluate_pair(SYN, "A", "Q")
    with pytest.raises(ScenarioError):
        evaluate_pair(SYN, "A", "A")


def test_manual_evaluation_flags_violation(pr):
    ok = evaluate_manual(pr, "C09", "C14", cons())
    assert ok["covered"] == 111 and not ok["violates_constraints"] and ok["delta_vs_best"] == 0
    bad = evaluate_manual(pr, "C09", "C14", cons(["C09"]))  # «что было бы», даже для исключённой
    assert bad["violates_constraints"] and bad["delta_vs_best"] == 3
    assert evaluate_manual(pr, "C09", "C11", cons(required=["C14"]))["violates_constraints"]


# детерминизм и независимость от result.json
def test_deterministic_and_result_untouched(pr):
    before = dumps(pr.result)
    a = run_scenario(pr, cons(["C09"], [])).to_dict()
    b = run_scenario(pr, cons(["C09"], [])).to_dict()
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)
    assert dumps(pr.result) == before
    assert a["result_json_unchanged"] is True


def test_constraints_from_dict_and_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(
        json.dumps(
            {
                "excluded": [{"id": "C09", "reason": "собственник против", "category": "owner"}, "C01"],
                "required": ["C14"],
            }
        ),
        encoding="utf-8",
    )
    c = Constraints.from_file(f)
    assert c.excluded_ids == {"C09", "C01"} and c.required == ("C14",)
    assert c.excluded[0].reason == "собственник против" and c.to_dict()["excluded"][0]["category"] == "owner"
    with pytest.raises(ScenarioError):
        Constraints.from_file(tmp_path / "nope.json")
    f.write_text("[1]", encoding="utf-8")
    with pytest.raises(ScenarioError):
        Constraints.from_file(f)
