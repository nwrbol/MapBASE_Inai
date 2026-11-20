"""Выбор двух площадок: допуск по 500 м, покрытие красных клеток в 1 км, перебор и жадный."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Settings
from .geo import haversine_m

log = logging.getLogger(__name__)


@dataclass
class CandidateInfo:
    id: str
    lat: float
    lon: float
    admissible: bool
    nearest_tower: str | None
    nearest_tower_m: float | None
    reason: str | None
    covers: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SelectionResult:
    method: str
    pair: list[str]
    covered: int
    covered_cells: list[str]
    depth: float
    elapsed_ms: float
    pairs_evaluated: int
    ties: list[list[str]] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "pair": self.pair,
            "covered": self.covered,
            "covered_cells": self.covered_cells,
            "depth_db": round(self.depth, 4),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "pairs_evaluated": self.pairs_evaluated,
            "ties": self.ties,
            "steps": self.steps,
        }


def evaluate_candidates(
    candidates: pd.DataFrame, towers: pd.DataFrame, red: pd.DataFrame, settings: Settings
) -> list[CandidateInfo]:
    """Для каждого кандидата: ближайшая БС, допуск (до КАЖДОЙ БС >= 500 м) и покрываемые клетки."""
    s = settings.selection
    radius = settings.grid.earth_radius_m
    red_ids = red["cell_id"].to_numpy(dtype=object)
    red_lat = red["center_lat"].to_numpy(float)
    red_lon = red["center_lon"].to_numpy(float)
    t_ids = towers["id"].to_numpy(dtype=object)
    t_lat = towers["lat"].to_numpy(float)
    t_lon = towers["lon"].to_numpy(float)
    out: list[CandidateInfo] = []
    for row in candidates.sort_values("id", kind="mergesort").itertuples(index=False):
        nearest, nearest_m = None, None
        if len(t_ids):
            d = np.atleast_1d(haversine_m(row.lat, row.lon, t_lat, t_lon, radius))
            k = int(np.argmin(d))
            nearest, nearest_m = str(t_ids[k]), float(d[k])
        admissible = nearest_m is None or nearest_m >= s.tower_exclusion_m
        reason = None
        if not admissible:
            reason = f"ближе {s.tower_exclusion_m:g} м к {nearest}: {nearest_m:.0f} м"
        covers: frozenset[str] = frozenset()
        if admissible and len(red_ids):
            dc = np.atleast_1d(haversine_m(row.lat, row.lon, red_lat, red_lon, radius))
            covers = frozenset(red_ids[dc <= s.coverage_radius_m].tolist())
        out.append(
            CandidateInfo(
                id=str(row.id),
                lat=float(row.lat),
                lon=float(row.lon),
                admissible=admissible,
                nearest_tower=nearest,
                nearest_tower_m=nearest_m,
                reason=reason,
                covers=covers,
            )
        )
    return out


def _depth(cells: set[str] | frozenset[str], depth_by_cell: dict[str, float]) -> float:
    return float(sum(depth_by_cell.get(c, 0.0) for c in cells))


def _fallback(admissible: list[CandidateInfo], method: str, depth_by_cell: dict[str, float]) -> SelectionResult:
    """Меньше двух допустимых площадок: возвращаем то, что есть, без падения."""
    log.error("Допустимых площадок меньше двух (%d) — пара не может быть выбрана", len(admissible))
    union: set[str] = set().union(*(c.covers for c in admissible)) if admissible else set()
    return SelectionResult(
        method=method,
        pair=sorted(c.id for c in admissible),
        covered=len(union),
        covered_cells=sorted(union),
        depth=_depth(union, depth_by_cell),
        elapsed_ms=0.0,
        pairs_evaluated=0,
    )


def brute_force(
    infos: list[CandidateInfo], depth_by_cell: dict[str, float], tie_break: str = "lexicographic"
) -> SelectionResult:
    """Полный перебор всех пар допустимых площадок; максимум |covers[a] ∪ covers[b]|."""
    admissible = sorted((c for c in infos if c.admissible), key=lambda c: c.id)
    if len(admissible) < 2:
        return _fallback(admissible, "brute_force", depth_by_cell)
    start = time.perf_counter()
    scored = []
    for a, b in itertools.combinations(admissible, 2):
        union = a.covers | b.covers
        scored.append((len(union), _depth(union, depth_by_cell), (a.id, b.id), union))
    best_n = max(s[0] for s in scored)
    top = [s for s in scored if s[0] == best_n]
    if tie_break == "depth_then_lexicographic":
        top.sort(key=lambda s: (-round(s[1], 9), s[2]))
    else:
        top.sort(key=lambda s: s[2])
    chosen = top[0]
    elapsed = (time.perf_counter() - start) * 1000
    ties = [list(s[2]) for s in top]
    if len(ties) > 1:
        log.warning("Ничья по покрытию (%d клеток) у %d пар: %s; выбрана %s", best_n, len(ties), ties, list(chosen[2]))
    return SelectionResult(
        method="brute_force",
        pair=list(chosen[2]),
        covered=chosen[0],
        covered_cells=sorted(chosen[3]),
        depth=chosen[1],
        elapsed_ms=elapsed,
        pairs_evaluated=len(scored),
        ties=ties,
    )


def greedy(infos: list[CandidateInfo], depth_by_cell: dict[str, float]) -> SelectionResult:
    """Жадный: сначала max |covers|, затем max прирост; ничьи — по id."""
    admissible = sorted((c for c in infos if c.admissible), key=lambda c: c.id)
    if len(admissible) < 2:
        return _fallback(admissible, "greedy", depth_by_cell)
    start = time.perf_counter()
    chosen: list[CandidateInfo] = []
    covered: set[str] = set()
    steps = []
    evaluated = 0
    for _ in range(2):
        pool = [c for c in admissible if c not in chosen]
        evaluated += len(pool)
        best = min(pool, key=lambda c: (-len(c.covers - covered), c.id))
        gain = len(best.covers - covered)
        chosen.append(best)
        covered |= best.covers
        steps.append({"pick": best.id, "gain": gain, "total": len(covered)})
    elapsed = (time.perf_counter() - start) * 1000
    return SelectionResult(
        method="greedy",
        pair=sorted(c.id for c in chosen),
        covered=len(covered),
        covered_cells=sorted(covered),
        depth=_depth(covered, depth_by_cell),
        elapsed_ms=elapsed,
        pairs_evaluated=evaluated,
        steps=steps,
    )


def depth_map(red: pd.DataFrame, settings: Settings) -> dict[str, float]:
    """«Глубина» красной клетки: насколько медиана ниже порога red_below (дБ)."""
    thr = settings.classes.red_below
    return {str(c): float(thr - m) for c, m in zip(red["cell_id"], red["median_rsrp"])}
