"""Pydantic-модели ответов API (видны в OpenAPI /docs)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

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


# --- сценарии отказов -------------------------------------------------------------------------

_ID = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")


class RefusalIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$", description="id площадки, например C09")
    reason: str = Field("", max_length=200, description="причина: «собственник против», «нет разрешения», …")
    category: Literal["owner", "permit", "technical", "other"] = "other"


class ScenarioRequest(BaseModel):
    analysis_id: str | None = Field(None, description="id загруженного анализа; без него — открытый набор")
    excluded: list[RefusalIn] = Field(default_factory=list)
    required: list[Annotated[str, _ID]] = Field(default_factory=list, description="зафиксированные площадки (до 2)")
    top_n: int = Field(10, ge=1, le=50)


class EvaluateRequest(BaseModel):
    analysis_id: str | None = None
    pair: list[Annotated[str, _ID]] = Field(min_length=2, max_length=2)
    excluded: list[RefusalIn] = Field(default_factory=list)
    required: list[Annotated[str, _ID]] = Field(default_factory=list)


class PairBrief(BaseModel):
    pair: list[str]
    covered: int


class PairOptionOut(BaseModel):
    pair: list[str]
    covered: int
    delta_vs_optimum: int
    overlap: int
    only_a: int
    only_b: int
    covered_ids: list[str] | None = None
    lost_vs_optimum: list[str] | None = None
    gained_vs_optimum: list[str] | None = None


class ReplacementOut(BaseModel):
    kind: Literal["keep_partner", "rebuild"]
    kept: str | None = None
    replaced: str | None = None
    pair: list[str]
    covered: int
    delta: int
    recommended: bool


class ConstraintsOut(BaseModel):
    excluded: list[RefusalIn]
    required: list[str]


class ScenarioResponse(BaseModel):
    feasible: bool
    message: str | None
    constraints: ConstraintsOut
    optimum: PairOptionOut
    best: PairOptionOut | None
    ties: list[PairBrief]
    alternatives: list[PairOptionOut]
    replacements: list[ReplacementOut]
    candidate_impact: dict[str, int]
    result_json_unchanged: bool = True


class EvaluateResponse(PairOptionOut):
    violates_constraints: bool
    delta_vs_best: int | None = Field(None, description="covered − covered лучшей пары с ограничениями")
