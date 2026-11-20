"""Pydantic-модели ответов API (видны в OpenAPI /docs)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: str = "ok"


class WorstCell(BaseModel):
    cell_id: str
    rsrp_median: float


class ResultModel(BaseModel):
    """Строго 4 ключа ТЗ. worst_cells может быть в другом формате, если так задано в config.yaml."""

    worst_cells: list[WorstCell] | list[str] | list[list[float]]
    best_candidates: list[str]
    dropped_rows: int
    covered_weak_cells: int


class CandidateModel(BaseModel):
    id: str
    lat: float
    lon: float
    admissible: bool
    nearest_tower: str | None
    nearest_tower_m: float | None
    reason: str | None
    covers_red: int
    selected: bool
    covers: list[str] = Field(default_factory=list)


class TowerModel(BaseModel):
    id: str
    lat: float
    lon: float


class CompareModel(BaseModel):
    brute_force: dict[str, Any]
    greedy: dict[str, Any]
    greedy_gap: int


class AnalyzeResponse(BaseModel):
    analysis_id: str
    result: ResultModel
    report: dict[str, Any]
    map_data: dict[str, Any]
