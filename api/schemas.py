"""Pydantic-модели ответов API (видны в OpenAPI /docs)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field


class Health(BaseModel):
    status: str = "ok"


class WorstCell(BaseModel):
    cell_id: str
    rsrp_median: float


class ResultModel(BaseModel):
    """Ровно 4 ключа result.json. worst_cells может быть в другом формате, если так задано в config.yaml."""

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


# --- полевые замеры ---------------------------------------------------------------------------

FieldSource = Literal["map_pin", "gps", "manual", "import"]


class FieldItem(BaseModel):
    lat: float
    lon: float
    rsrp_dbm: float
    timestamp: str = Field(min_length=1, max_length=64, description="ISO 8601; без пояса считается Asia/Bishkek")
    source: FieldSource = "manual"
    accuracy_m: float | None = Field(None, ge=0, description="точность GPS, м")
    author_label: str | None = Field(None, max_length=50, validation_alias=AliasChoices("author_label", "device_label"))


class FieldAddRequest(BaseModel):
    items: list[FieldItem] = Field(min_length=1, max_length=500)
    dry_run: bool = False


class FieldPasteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000, description="строки lat,lon,rsrp_dbm,timestamp")
    source: FieldSource = "manual"
    has_header: bool | None = Field(None, description="None: определить автоматически")
    dry_run: bool = False


class FieldAccepted(BaseModel):
    index: int
    id: str | None = None
    cell_id: str
    n_after: int
    median_after: float
    class_after: str
    counted_after: bool
    became_counted: bool
    warning: str | None = None


class FieldRejected(BaseModel):
    index: int
    reason: str
    message: str


class FieldAddResponse(BaseModel):
    accepted: list[FieldAccepted]
    rejected: list[FieldRejected]
    version: int = Field(description="версия общей базы после запроса")
    field_version: int = Field(description="то же, что version (прежнее имя)")
    dry_run: bool = False
    batch_id: str | None = None
    summary: dict[str, int] | None = Field(None, description="cells_changed / became_counted / class_changed")
    rows: list[dict[str, Any]] | None = Field(None, description="построчный разбор вставленного текста (paste)")


class FieldVersion(BaseModel):
    version: int
    count: int = Field(description="сколько замеров сейчас в общей базе")
    by_source: dict[str, int] = Field(default_factory=dict, description="map_pin / gps / manual / import")
    authors: int = Field(0, description="сколько устройств добавляли замеры")
    last_hour: int = Field(0, description="добавлено за последний час")
    server_time: str
