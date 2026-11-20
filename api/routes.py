"""HTTP-эндпоинты. Вся логика — в coverage.run_pipeline, здесь только транспорт."""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from coverage.config import Settings, load_settings
from coverage.errors import DataValidationError, ScenarioError
from coverage.field_store import FieldStore
from coverage.pipeline import Inputs, PipelineResult, run_pipeline
from coverage.render import build_map_data, render_page
from coverage.result import dumps, to_jsonable
from coverage.scenario import Constraints, Refusal, evaluate_manual, impact_of_refusals, run_scenario

from .schemas import (
    AnalyzeResponse,
    CandidateModel,
    CompareModel,
    EvaluateRequest,
    EvaluateResponse,
    Health,
    RefusalIn,
    ResultModel,
    ScenarioRequest,
    ScenarioResponse,
    TowerModel,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_STORED_ANALYSES = 16

router = APIRouter()


@dataclass
class Analysis:
    pr: PipelineResult
    map_data: dict
    _html: str | None = None

    @property
    def html(self) -> str:
        if self._html is None:
            self._html = render_page(self.map_data, server_mode=False)
        return self._html


@dataclass
class AppState:
    data_dir: Path
    config_path: Path | None
    settings: Settings
    default: Analysis | None = None
    analyses: OrderedDict[str, Analysis] = field(default_factory=OrderedDict)
    lock: Lock = field(default_factory=Lock)
    # полевые замеры: отдельная БД, пересчёт «с полевыми» кэшируется по версии (см. api/field_routes.py)
    field_store: FieldStore | None = None
    field_lock: Lock = field(default_factory=Lock)
    field_cache: OrderedDict = field(default_factory=OrderedDict)
    admin_token: str | None = None  # env ADMIN_TOKEN: сброс общей базы; не задан -> сброс отключён

    def store(self, analysis: Analysis) -> str:
        key = uuid.uuid4().hex
        with self.lock:
            self.analyses[key] = analysis
            while len(self.analyses) > MAX_STORED_ANALYSES:
                self.analyses.popitem(last=False)
        return key


def create_state(
    data_dir: Path,
    config_path: Path | None,
    field_db_path: str | Path | None = None,
    admin_token: str | None = None,
) -> AppState:
    settings = load_settings(config_path)
    state = AppState(data_dir=data_dir, config_path=config_path, settings=settings)
    state.field_store = FieldStore(field_db_path)
    state.admin_token = admin_token or None
    pr = run_pipeline(Inputs.from_dir(data_dir, settings.bounds.bounds_file), settings)
    state.default = Analysis(pr=pr, map_data=build_map_data(pr))
    return state


def _state(request: Request) -> AppState:
    return request.app.state.coverage


def _default(request: Request) -> Analysis:
    st = _state(request)
    if st.default is None:
        raise HTTPException(status_code=503, detail="датасет по умолчанию не загружен")
    return st.default


def _pick(request: Request, analysis_id: str | None) -> Analysis:
    if not analysis_id:
        return _default(request)
    st = _state(request)
    with st.lock:
        found = st.analyses.get(analysis_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail=f"анализ {analysis_id} не найден (хранятся последние {MAX_STORED_ANALYSES})"
        )
    return found


async def _read_upload(upload: UploadFile, name: str) -> bytes:
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise DataValidationError(f"{name}: файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    if not data:
        raise DataValidationError(f"{name}: файл пустой")
    return data


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse(render_page(None, server_mode=True))


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health()


@router.get("/api/result", response_model=ResultModel)
def get_result(request: Request) -> dict:
    return _default(request).pr.result


@router.get("/api/report")
def get_report(request: Request) -> JSONResponse:
    return JSONResponse(to_jsonable(_default(request).pr.report))


@router.get("/api/mapdata")
def get_mapdata(request: Request, include_field: bool = False) -> JSONResponse:
    """Все данные карты одним JSON (тот же формат, что встроен в map.html).

    include_field=true добавляет полевые замеры в сетку (result остаётся официальным, по исходным данным).
    """
    if include_field:
        from .field_routes import field_map_data

        return JSONResponse(field_map_data(request))
    return JSONResponse(_default(request).map_data)


@router.get("/api/grid")
def get_grid(request: Request) -> JSONResponse:
    """GeoJSON FeatureCollection клеток (полигоны по углам в локальной сетке)."""
    a = _default(request)
    features = []
    for c in a.map_data["cells"]:
        s, w, n, e = c["b"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
                "properties": {
                    "cell_id": c["id"],
                    "n": c["n"],
                    "median_rsrp": c["m"],
                    "cls": c["cls"] if c["counted"] else "insufficient",
                    "counted": c["counted"],
                    "is_worst": c["rank"] is not None,
                    "worst_rank": c["rank"],
                    "covered": c["covered"],
                },
            }
        )
    return JSONResponse({"type": "FeatureCollection", "features": features})


@router.get("/api/towers", response_model=list[TowerModel])
def get_towers(request: Request) -> list[dict]:
    return _default(request).map_data["towers"]


@router.get("/api/candidates", response_model=list[CandidateModel])
def get_candidates(request: Request) -> list[dict]:
    return _default(request).map_data["candidates"]


@router.get("/api/heatmap")
def get_heatmap(request: Request) -> JSONResponse:
    return JSONResponse(_default(request).map_data["heat"])


@router.get("/api/daynight")
def get_daynight(request: Request) -> JSONResponse:
    return JSONResponse(to_jsonable(_default(request).pr.report["daynight"]))


@router.get("/api/compare", response_model=CompareModel)
def get_compare(request: Request) -> dict:
    return to_jsonable(_default(request).pr.report["selection"])


@router.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: Request,
    measurements: UploadFile = File(..., description="measurements.csv"),
    towers: UploadFile = File(..., description="towers.csv"),
    candidates: UploadFile = File(..., description="candidates.csv"),
    bounds: UploadFile | None = File(None, description="bounds.json (необязательно)"),
    cell_size_m: float | None = Form(None),
    min_samples: int | None = Form(None),
    coverage_radius_m: float | None = Form(None),
    tower_exclusion_m: float | None = Form(None),
    green_min: float | None = Form(None),
    red_below: float | None = Form(None),
) -> dict:
    """Пересчёт на загруженных CSV. Ошибки валидации — 422 с понятным detail."""
    st = _state(request)
    m = await _read_upload(measurements, "measurements.csv")
    t = await _read_upload(towers, "towers.csv")
    c = await _read_upload(candidates, "candidates.csv")
    b: bytes | Path | None = None
    if bounds is not None and bounds.filename:
        b = await _read_upload(bounds, "bounds.json")
    else:
        default_bounds = st.data_dir / st.settings.bounds.bounds_file
        b = default_bounds if default_bounds.exists() else None
    overrides = {
        "grid.cell_size_m": cell_size_m,
        "grid.min_samples": min_samples,
        "selection.coverage_radius_m": coverage_radius_m,
        "selection.tower_exclusion_m": tower_exclusion_m,
        "classes.green_min": green_min,
        "classes.red_below": red_below,
    }
    pr = run_pipeline(Inputs(measurements=m, towers=t, candidates=c, bounds=b), st.settings, overrides)
    analysis = Analysis(pr=pr, map_data=build_map_data(pr))
    key = st.store(analysis)
    return {"analysis_id": key, "result": pr.result, "report": to_jsonable(pr.report), "map_data": analysis.map_data}


@router.get("/api/download/result.json")
def download_result(request: Request, analysis_id: str | None = None) -> Response:
    a = _pick(request, analysis_id)
    return Response(
        dumps(a.pr.result),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="result.json"'},
    )


@router.get("/api/download/map.html")
def download_map(request: Request, analysis_id: str | None = None) -> Response:
    a = _pick(request, analysis_id)
    return Response(
        a.html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="map.html"'},
    )


# --- сценарии отказов: сервер без состояния, ограничения приходят в каждом запросе ----------------


def _constraints(excluded: list[RefusalIn], required: list[str], analysis: Analysis) -> Constraints:
    if len(excluded) > len(analysis.pr.candidates):
        raise ScenarioError(f"отказов больше, чем площадок ({len(analysis.pr.candidates)})")
    return Constraints(
        excluded=tuple(Refusal(r.id, r.reason, r.category) for r in excluded),
        required=tuple(required),
    )


@router.post("/api/scenario", response_model=ScenarioResponse, response_model_exclude_unset=True, tags=["scenario"])
def post_scenario(request: Request, body: ScenarioRequest) -> dict:
    """Пересчёт пары с учётом отказов и фиксаций. result.json и /api/result не меняются."""
    a = _pick(request, body.analysis_id)
    return run_scenario(a.pr, _constraints(body.excluded, body.required, a), body.top_n).to_dict()


@router.post("/api/scenario/evaluate", response_model=EvaluateResponse, tags=["scenario"])
def post_evaluate(request: Request, body: EvaluateRequest) -> dict:
    """Ручная оценка любой пары допустимых площадок (даже исключённых: «что было бы»)."""
    a = _pick(request, body.analysis_id)
    cons = _constraints(body.excluded, body.required, a)
    return evaluate_manual(a.pr, body.pair[0], body.pair[1], cons)


@router.get("/api/candidates/impact", response_model=dict[str, int], tags=["scenario"])
def get_impact(request: Request, analysis_id: str | None = None) -> dict[str, int]:
    """Потеря покрытия, если откажет именно эта площадка (≤ 0)."""
    a = _pick(request, analysis_id)
    covers = {c.id: c.covers for c in a.pr.candidates if c.admissible}
    return impact_of_refusals(covers, a.pr.brute.covered)


@router.post("/api/scenario/export", tags=["scenario"])
def export_scenario(request: Request, body: ScenarioRequest) -> Response:
    a = _pick(request, body.analysis_id)
    scenario = run_scenario(a.pr, _constraints(body.excluded, body.required, a), body.top_n)
    return Response(
        dumps(scenario.to_dict()),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="scenario.json"'},
    )
