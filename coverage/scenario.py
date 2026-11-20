"""Сценарии «а если владелец против?»: отказ или фиксация площадок, альтернативные пары, критичность.

Чистые функции без глобального состояния. Покрытие (`covers`) берётся из уже посчитанного пайплайна,
ничего не пересчитывается. `result.json` сценарии не меняют: оптимум по ТЗ всегда считается без ограничений.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ScenarioError

if TYPE_CHECKING:
    from .pipeline import PipelineResult
    from .selection import CandidateInfo

log = logging.getLogger(__name__)

CATEGORIES = ("owner", "permit", "technical", "other")
MAX_REQUIRED = 2
MAX_REASON_LEN = 200
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_TOP_N = 10

Covers = Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class Refusal:
    id: str
    reason: str = ""  # «собственник против», «нет разрешения», …
    category: str = "other"  # owner | permit | technical | other


@dataclass(frozen=True)
class Constraints:
    excluded: tuple[Refusal, ...] = ()
    required: tuple[str, ...] = ()  # 0, 1 или 2 id

    @property
    def excluded_ids(self) -> frozenset[str]:
        return frozenset(r.id for r in self.excluded)

    @property
    def is_empty(self) -> bool:
        return not self.excluded and not self.required

    def to_dict(self) -> dict:
        return {
            "excluded": [{"id": r.id, "reason": r.reason, "category": r.category} for r in self.excluded],
            "required": list(self.required),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Constraints:
        """Формат constraints.json: {"excluded": [{"id","reason","category"}|"C09"], "required": ["C14"]}."""
        if not isinstance(data, Mapping):
            raise ScenarioError("ограничения: ожидается JSON-объект с ключами excluded и required")
        excluded: list[Refusal] = []
        for item in data.get("excluded") or []:
            if isinstance(item, str):
                excluded.append(Refusal(id=item))
            elif isinstance(item, Mapping) and isinstance(item.get("id"), str):
                excluded.append(
                    Refusal(
                        id=item["id"],
                        reason=str(item.get("reason") or ""),
                        category=str(item.get("category") or "other"),
                    )
                )
            else:
                raise ScenarioError(f"ограничения: некорректная запись отказа {item!r}")
        required = data.get("required") or []
        if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
            raise ScenarioError("ограничения: required должен быть списком id площадок")
        return cls(excluded=tuple(excluded), required=tuple(required))

    @classmethod
    def from_file(cls, path: str | Path) -> Constraints:
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ScenarioError(f"файл ограничений не найден: {p}") from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScenarioError(f"файл ограничений {p}: {exc}") from exc
        return cls.from_dict(data)

    def merged(self, other: Constraints) -> Constraints:
        """Объединение ограничений (для файла + флагов CLI); отказ по тому же id берётся из other."""
        by_id = {r.id: r for r in self.excluded}
        by_id.update({r.id: r for r in other.excluded})
        required = tuple(dict.fromkeys([*self.required, *other.required]))
        return Constraints(excluded=tuple(by_id.values()), required=required)


@dataclass(frozen=True)
class PairOption:
    pair: tuple[str, str]  # отсортирована
    covered: int  # |A ∪ B|
    covered_ids: frozenset[str]
    only_a: int  # |A − B|
    only_b: int  # |B − A|
    overlap: int  # |A ∩ B|
    delta_vs_optimum: int  # covered − covered(оптимума без ограничений), ≤ 0
    lost_vs_optimum: frozenset[str]  # клетки оптимума, которые эта пара не покрывает
    gained_vs_optimum: frozenset[str]

    def to_dict(self, with_ids: bool = True) -> dict:
        d: dict[str, Any] = {
            "pair": list(self.pair),
            "covered": self.covered,
            "delta_vs_optimum": self.delta_vs_optimum,
            "overlap": self.overlap,
            "only_a": self.only_a,
            "only_b": self.only_b,
        }
        if with_ids:
            d["covered_ids"] = sorted(self.covered_ids)
            d["lost_vs_optimum"] = sorted(self.lost_vs_optimum)
            d["gained_vs_optimum"] = sorted(self.gained_vs_optimum)
        return d


@dataclass(frozen=True)
class ReplacementOption:
    kind: str  # keep_partner | rebuild
    pair: tuple[str, str]
    covered: int
    delta: int
    recommended: bool
    kept: str | None = None
    replaced: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"kind": self.kind}
        if self.kind == "keep_partner":
            d["kept"] = self.kept
            d["replaced"] = self.replaced
        d.update(
            {"pair": list(self.pair), "covered": self.covered, "delta": self.delta, "recommended": self.recommended}
        )
        return d


@dataclass
class ScenarioResult:
    feasible: bool
    message: str | None
    constraints: Constraints
    optimum: PairOption  # без ограничений (= result.json)
    best: PairOption | None  # лучший с ограничениями
    ties: list[PairOption] = field(default_factory=list)  # все пары с тем же covered, что best
    alternatives: list[PairOption] = field(default_factory=list)  # top_n
    replacements: list[ReplacementOption] = field(default_factory=list)
    candidate_impact: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "message": self.message,
            "constraints": self.constraints.to_dict(),
            "optimum": self.optimum.to_dict(),
            "best": None if self.best is None else self.best.to_dict(),
            "ties": [{"pair": list(o.pair), "covered": o.covered} for o in self.ties],
            "alternatives": [o.to_dict(with_ids=False) for o in self.alternatives],
            "replacements": [r.to_dict() for r in self.replacements],
            "candidate_impact": dict(sorted(self.candidate_impact.items())),
            "result_json_unchanged": True,
        }


# ----------------------------------------------------------------------------- валидация


def validate_constraints(
    constraints: Constraints,
    candidates_info: Iterable[CandidateInfo],
    tower_exclusion_m: float | None = None,
) -> list[str]:
    """Список человеческих ошибок (пустой — ограничения корректны)."""
    info = {c.id: c for c in candidates_info}
    errors: list[str] = []
    for r in constraints.excluded:
        if r.id not in info:
            errors.append(f"неизвестная площадка в отказах: {r.id}")
        if r.category not in CATEGORIES:
            errors.append(f"{r.id}: неизвестная категория отказа «{r.category}» (допустимы: {', '.join(CATEGORIES)})")
        if len(r.reason) > MAX_REASON_LEN:
            errors.append(f"{r.id}: причина длиннее {MAX_REASON_LEN} символов")
    if len(set(constraints.required)) != len(constraints.required):
        errors.append("одна и та же площадка зафиксирована дважды")
    if len(constraints.required) > MAX_REQUIRED:
        errors.append(f"можно зафиксировать не больше {MAX_REQUIRED} площадок, передано {len(constraints.required)}")
    excluded_ids = constraints.excluded_ids
    for cid in constraints.required:
        c = info.get(cid)
        if c is None:
            errors.append(f"неизвестная площадка в фиксации: {cid}")
            continue
        if cid in excluded_ids:
            errors.append(f"{cid} одновременно в отказах и в фиксации")
        if not c.admissible:
            if c.nearest_tower is not None and c.nearest_tower_m is not None and tower_exclusion_m is not None:
                errors.append(
                    f"{cid} нельзя зафиксировать: {c.nearest_tower_m:.0f} м до {c.nearest_tower} (< {tower_exclusion_m:g} м)"
                )
            else:
                errors.append(f"{cid} нельзя зафиксировать: {c.reason}")
    return errors


# ----------------------------------------------------------------------------- пары


def evaluate_pair(covers: Covers, a: str, b: str, optimum_cells: frozenset[str] | None = None) -> PairOption:
    """Оценка любой пары допустимых площадок (в том числе исключённых пользователем)."""
    for cid in (a, b):
        if cid not in covers:
            raise ScenarioError(f"площадка {cid} неизвестна или недопустима по ТЗ")
    if a == b:
        raise ScenarioError("нужны две разные площадки")
    first, second = sorted((a, b))
    ca, cb = covers[first], covers[second]
    union = ca | cb
    opt = optimum_cells if optimum_cells is not None else union
    return PairOption(
        pair=(first, second),
        covered=len(union),
        covered_ids=union,
        only_a=len(ca - cb),
        only_b=len(cb - ca),
        overlap=len(ca & cb),
        delta_vs_optimum=len(union) - len(opt),
        lost_vs_optimum=frozenset(opt - union),
        gained_vs_optimum=frozenset(union - opt),
    )


def _allowed_ids(covers: Covers, constraints: Constraints) -> list[str]:
    return sorted(set(covers) - constraints.excluded_ids)


def _pair_ids(covers: Covers, constraints: Constraints) -> list[tuple[str, str]]:
    """Все пары не исключённых площадок, содержащие все required (отсортированы лексикографически)."""
    allowed = _allowed_ids(covers, constraints)
    required = sorted(constraints.required)
    if len(required) == 2:
        return [(required[0], required[1])] if all(r in allowed for r in required) else []
    if len(required) == 1:
        r = required[0]
        if r not in allowed:
            return []
        return [tuple(sorted((r, x))) for x in allowed if x != r]  # type: ignore[misc]
    return list(itertools.combinations(allowed, 2))


def enumerate_pairs(
    covers: Covers, constraints: Constraints, optimum_cells: frozenset[str] | None = None
) -> list[PairOption]:
    """Все допустимые пары, отсортированные: покрытие по убыванию, затем пара лексикографически."""
    options = [evaluate_pair(covers, a, b, optimum_cells) for a, b in _pair_ids(covers, constraints)]
    options.sort(key=lambda o: (-o.covered, o.pair))
    return options


def rank_pairs(
    covers: Covers,
    constraints: Constraints,
    top_n: int = DEFAULT_TOP_N,
    optimum_cells: frozenset[str] | None = None,
) -> list[PairOption]:
    """Топ-N пар допустимых, не исключённых площадок, содержащих все required."""
    return enumerate_pairs(covers, constraints, optimum_cells)[:top_n]


def best_covered_excluding(covers: Covers, excluded: Iterable[str]) -> int:
    """Максимум |A ∪ B| по парам без excluded (быстро: без PairOption)."""
    skip = set(excluded)
    ids = sorted(set(covers) - skip)
    return max((len(covers[a] | covers[b]) for a, b in itertools.combinations(ids, 2)), default=0)


def impact_of_refusals(covers: Covers, optimum_covered: int) -> dict[str, int]:
    """impact[c] = best_covered(excluded={c}) − оптимум (≤ 0): насколько хуже, если откажет именно c."""
    return {cid: best_covered_excluding(covers, [cid]) - optimum_covered for cid in sorted(covers)}


# ----------------------------------------------------------------------------- замена


def _replacements(covers: Covers, constraints: Constraints, optimum: PairOption) -> list[ReplacementOption]:
    """Если отказала ровно одна площадка оптимальной пары: «заменить только её» или «пересобрать пару»."""
    excluded = constraints.excluded_ids
    refused = [c for c in optimum.pair if c in excluded]
    if len(refused) != 1:
        return []
    x = refused[0]
    y = next(c for c in optimum.pair if c != x)
    base = Constraints(excluded=constraints.excluded)
    rebuild = rank_pairs(covers, base, 1, optimum.covered_ids)
    keep = rank_pairs(covers, Constraints(excluded=constraints.excluded, required=(y,)), 1, optimum.covered_ids)
    out: list[ReplacementOption] = []
    keep_wins = bool(keep) and (not rebuild or keep[0].covered >= rebuild[0].covered)
    if keep:
        k = keep[0]
        out.append(ReplacementOption("keep_partner", k.pair, k.covered, k.delta_vs_optimum, keep_wins, y, x))
    if rebuild:
        r = rebuild[0]
        out.append(ReplacementOption("rebuild", r.pair, r.covered, r.delta_vs_optimum, not keep_wins))
    return out


# ----------------------------------------------------------------------------- сценарий


def _covers_of(infos: Iterable[CandidateInfo]) -> dict[str, frozenset[str]]:
    return {c.id: c.covers for c in infos if c.admissible}


def run_scenario(
    pr: PipelineResult, constraints: Constraints, top_n: int = DEFAULT_TOP_N, validate: bool = True
) -> ScenarioResult:
    """Сценарий целиком. Ничего не пишет в pr: result.json остаётся по ТЗ."""
    if not 1 <= top_n <= 50:
        raise ScenarioError("top_n должен быть от 1 до 50")
    if validate:
        errors = validate_constraints(constraints, pr.candidates, pr.settings.selection.tower_exclusion_m)
        if errors:
            raise ScenarioError("; ".join(errors))
    covers = _covers_of(pr.candidates)
    optimum_cells = frozenset(pr.brute.covered_cells)
    if len(pr.brute.pair) == 2:
        optimum = evaluate_pair(covers, pr.brute.pair[0], pr.brute.pair[1], optimum_cells)
    else:  # меньше двух допустимых площадок: оптимума-пары нет
        optimum = PairOption(
            pair=tuple(pr.brute.pair),  # type: ignore[arg-type]
            covered=pr.brute.covered,
            covered_ids=optimum_cells,
            only_a=0,
            only_b=0,
            overlap=0,
            delta_vs_optimum=0,
            lost_vs_optimum=frozenset(),
            gained_vs_optimum=frozenset(),
        )
    impact = impact_of_refusals(covers, optimum.covered)

    ranked = enumerate_pairs(covers, constraints, optimum_cells)
    available = _allowed_ids(covers, constraints)
    if not ranked:
        message = (
            f"сценарий невыполним: после отказов осталось {len(available)} доступных площадок, нужно не меньше двух"
            if len(available) < 2
            else "сценарий невыполним: нет пары, удовлетворяющей фиксации"
        )
        log.info("Сценарий невыполним: %s", message)
        return ScenarioResult(
            False, message, constraints, optimum, None, [], [], _replacements(covers, constraints, optimum), impact
        )
    best = ranked[0]
    ties = [o for o in ranked if o.covered == best.covered]
    log.info(
        "Сценарий: отказов %d, фиксаций %d -> %s (%d, Δ %d)",
        len(constraints.excluded),
        len(constraints.required),
        "+".join(best.pair),
        best.covered,
        best.delta_vs_optimum,
    )
    return ScenarioResult(
        True,
        None,
        constraints,
        optimum,
        best,
        ties,
        ranked[:top_n],
        _replacements(covers, constraints, optimum),
        impact,
    )


def evaluate_manual(pr: PipelineResult, a: str, b: str, constraints: Constraints, top_n: int = DEFAULT_TOP_N) -> dict:
    """Ручная оценка пары: PairOption + нарушает ли она ограничения + Δ к лучшей с ограничениями."""
    covers = _covers_of(pr.candidates)
    optimum_cells = frozenset(pr.brute.covered_cells)
    option = evaluate_pair(covers, a, b, optimum_cells)
    excluded = constraints.excluded_ids
    violates = bool(set(option.pair) & excluded) or not set(constraints.required) <= set(option.pair)
    scenario = run_scenario(pr, constraints, top_n, validate=False)
    out = option.to_dict()
    out["violates_constraints"] = violates
    out["delta_vs_best"] = None if scenario.best is None else option.covered - scenario.best.covered
    return out
