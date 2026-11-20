"""run_pipeline: единая функция, которую вызывают и CLI, и FastAPI (результаты всегда совпадают)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .cleaning import CleaningReport, clean_measurements
from .config import Settings, apply_bounds, apply_overrides, parse_bounds_json
from .daynight import compare_day_night
from .grid import assign_cells, build_grid, grid_stats, red_cells, worst_cells
from .io import Source, read_bytes, read_measurements, read_sites
from .result import build_result, format_worst
from .selection import CandidateInfo, SelectionResult, brute_force, depth_map, evaluate_candidates, greedy

log = logging.getLogger(__name__)


@dataclass
class Inputs:
    measurements: Source
    towers: Source
    candidates: Source
    bounds: Source | None = None

    @classmethod
    def from_dir(cls, data_dir: str | Path, bounds_file: str = "bounds.json") -> Inputs:
        d = Path(data_dir)
        bounds = d / bounds_file
        return cls(
            measurements=d / "measurements.csv",
            towers=d / "towers.csv",
            candidates=d / "candidates.csv",
            bounds=bounds if bounds.exists() else None,
        )


@dataclass
class PipelineResult:
    settings: Settings
    result: dict
    report: dict
    cleaning: CleaningReport
    clean: pd.DataFrame
    grid: pd.DataFrame
    worst: pd.DataFrame
    towers: pd.DataFrame
    candidates: list[CandidateInfo]
    brute: SelectionResult
    greedy: SelectionResult
    daynight: dict
    heat: list[list[float]] = field(default_factory=list)
    bounds_source: str = "config.yaml"


def heat_points(clean: pd.DataFrame, settings: Settings) -> list[list[float]]:
    """[[lat, lon, intensity]]: ярче там, где сигнал слабее."""
    h = settings.heatmap
    if clean.empty:
        return []
    inten = np.clip((h.rsrp_strong - clean["rsrp_dbm"].to_numpy(float)) / h.span_db, 0.0, 1.0)
    d = settings.output.coord_decimals
    return [
        [round(float(a), d), round(float(b), d), round(float(c), 3)]
        for a, b, c in zip(clean["lat"], clean["lon"], inten)
    ]


def run_pipeline(inputs: Inputs, settings: Settings, overrides: dict | None = None) -> PipelineResult:
    """CSV → очистка → сетка → выбор площадок → result/report.

    overrides ({"grid.cell_size_m": 250, ...}) применяются после bounds.json и главнее его.
    """
    t0 = time.perf_counter()
    timings: dict[str, float] = {}

    def lap(name: str, since: float) -> float:
        now = time.perf_counter()
        timings[name] = round((now - since) * 1000, 3)
        return now

    bounds_source = "config.yaml"
    if inputs.bounds is not None:
        raw_bounds = parse_bounds_json(read_bytes(inputs.bounds, "bounds.json"))
        settings = apply_bounds(settings, raw_bounds)
        bounds_source = str(inputs.bounds) if not isinstance(inputs.bounds, bytes) else "uploaded bounds.json"
    if overrides:
        settings = apply_overrides(settings, overrides)

    t = time.perf_counter()
    frame, malformed = read_measurements(inputs.measurements)
    towers = read_sites(inputs.towers, "towers.csv")
    cands = read_sites(inputs.candidates, "candidates.csv")
    t = lap("read", t)

    clean, creport = clean_measurements(frame, malformed, settings)
    t = lap("cleaning", t)

    return analyze_clean(clean, creport, towers, cands, settings, bounds_source, timings, t0, t)


def analyze_clean(
    clean: pd.DataFrame,
    creport: CleaningReport,
    towers: pd.DataFrame,
    cands: pd.DataFrame,
    settings: Settings,
    bounds_source: str,
    timings: dict[str, float],
    t0: float,
    t: float,
) -> PipelineResult:
    """Всё после очистки: сетка, выбор площадок, день/ночь, result и report.

    Общая часть для run_pipeline (исходные замеры) и run_pipeline_with_field (исходные + полевые).
    """

    def lap(name: str, since: float) -> float:
        now = time.perf_counter()
        timings[name] = round((now - since) * 1000, 3)
        return now

    clean = assign_cells(clean, settings)
    grid = build_grid(clean, settings)
    worst = worst_cells(grid, settings.worst.count)
    red = red_cells(grid)
    t = lap("grid", t)

    infos = evaluate_candidates(cands, towers, red, settings)
    depths = depth_map(red, settings)
    bf = brute_force(infos, depths, settings.selection.tie_break)
    gr = greedy(infos, depths)
    covered_set = set(bf.covered_cells)
    grid["covered"] = grid["cell_id"].isin(covered_set)
    t = lap("selection", t)

    dn = compare_day_night(clean, settings)
    t = lap("daynight", t)
    heat = heat_points(clean, settings)

    result = build_result(format_worst(worst, settings), bf.pair, creport.dropped, bf.covered)
    timings["total"] = round((time.perf_counter() - t0) * 1000, 3)

    report = {
        "version": __version__,
        "result": result,
        "bounds_source": bounds_source,
        "cleaning": creport.to_dict(),
        "grid": grid_stats(grid),
        "worst_cells_detailed": [
            {
                "rank": i + 1,
                "cell_id": r.cell_id,
                "median_rsrp": float(r.median_rsrp),
                "n": int(r.n),
                "center_lat": round(float(r.center_lat), 6),
                "center_lon": round(float(r.center_lon), 6),
            }
            for i, r in enumerate(worst.itertuples(index=False))
        ],
        "candidates": [
            {
                "id": c.id,
                "lat": c.lat,
                "lon": c.lon,
                "admissible": c.admissible,
                "nearest_tower": c.nearest_tower,
                "nearest_tower_m": None if c.nearest_tower_m is None else round(c.nearest_tower_m, 1),
                "reason": c.reason,
                "covers_red": len(c.covers),
                "selected": c.id in bf.pair,
            }
            for c in infos
        ],
        "selection": {
            "brute_force": bf.to_dict(),
            "greedy": gr.to_dict(),
            "greedy_gap": bf.covered - gr.covered,
            "ties": bf.ties,
        },
        "daynight": {k: v for k, v in dn.items() if not k.startswith("_")},
        "settings": settings.to_dict(),
        "timings_ms": timings,
    }
    log.info("Пайплайн завершён за %.1f мс", timings["total"])
    return PipelineResult(
        settings=settings,
        result=result,
        report=report,
        cleaning=creport,
        clean=clean,
        grid=grid,
        worst=worst,
        towers=towers,
        candidates=infos,
        brute=bf,
        greedy=gr,
        daynight=dn,
        heat=heat,
        bounds_source=bounds_source,
    )
