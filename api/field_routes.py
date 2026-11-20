"""Полевые замеры: /field (страница) и /api/field/*. result.json эти эндпоинты не затрагивают."""

from __future__ import annotations

import csv
import io
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from coverage.errors import DataValidationError
from coverage.field_ingest import (
    FIELD_COLS,
    NormRow,
    Rejection,
    build_field_map_data,
    cell_feedback,
    cell_lookup,
    existing_frame,
    field_diff,
    field_points,
    run_pipeline_with_field,
    validate_field_rows,
)
from coverage.pipeline import PipelineResult
from coverage.result import to_jsonable

from .routes import AppState, _default, _state
from .schemas import FieldAddRequest, FieldAddResponse, FieldPasteRequest, FieldVersion

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
MAX_CACHED_VIEWS = 4
PASTE_HEADER = list(FIELD_COLS)

router = APIRouter(prefix="/api/field", tags=["field"])
page_router = APIRouter()


@dataclass
class FieldView:
    """Пересчёт «исходные + полевые» для одной версии хранилища."""

    version: int
    field_df: pd.DataFrame
    pr: PipelineResult
    diff: dict
    map_data: dict | None = None


def _store(request: Request):
    st = _state(request)
    if st.field_store is None:  # pragma: no cover - защитная ветка
        raise HTTPException(status_code=503, detail="хранилище полевых замеров не инициализировано")
    return st.field_store


def _view(st: AppState) -> FieldView:
    """Пересчёт с полевыми замерами, кэш по версии хранилища. Вызывать под st.field_lock."""
    store = st.field_store
    version = store.version()
    cached = st.field_cache.get(version)
    if cached is not None:
        return cached
    base = st.default.pr
    df = store.as_dataframe()
    if df.empty:
        view = FieldView(version, df, base, field_diff(base, base))
    else:
        pr = run_pipeline_with_field(base, df)
        view = FieldView(version, df, pr, field_diff(base, pr))
    st.field_cache[version] = view
    while len(st.field_cache) > MAX_CACHED_VIEWS:
        st.field_cache.popitem(last=False)
    return view


def field_map_data(request: Request) -> dict:
    """/api/mapdata?include_field=true."""
    st = _state(request)
    a = _default(request)
    with st.field_lock:
        view = _view(st)
        if view.map_data is None:
            view.map_data = build_field_map_data(
                a.map_data, a.pr, view.pr, view.field_df, view.diff, view.version, st.field_store.count_by_source()
            )
        return view.map_data


@page_router.get("/field", include_in_schema=False)
def field_page() -> FileResponse:
    page = STATIC / "field.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="страница /field ещё не собрана")
    return FileResponse(page, media_type="text/html; charset=utf-8")


# --- добавление -----------------------------------------------------------------------------------


def _check_batch_size(n: int, st: AppState) -> None:
    limit = st.settings.field_data.max_batch
    if n > limit:
        raise DataValidationError(f"за один запрос можно отправить не больше {limit} замеров, передано {n}")


def _commit(
    request: Request,
    rows: list[dict],
    dry_run: bool,
    batch_id: str | None,
    parse_rejected: list[Rejection] | None = None,
) -> tuple[list[NormRow], list[Rejection], list[dict], str | None, int, dict | None]:
    """Проверка (+ запись, если не dry_run). Проверка и запись под одной блокировкой: дубли не проскочат."""
    st = _state(request)
    a = _default(request)
    store = _store(request)
    skip = {r.index for r in (parse_rejected or [])}
    with st.field_lock:
        ex = existing_frame(a.pr.clean, store.as_dataframe())
        accepted, rejected = validate_field_rows(rows, a.pr.settings, ex)
        accepted = [x for x in accepted if x.index not in skip]
        rejected = sorted(
            [r for r in rejected if r.index not in skip] + list(parse_rejected or []), key=lambda r: r.index
        )
        feedback = cell_feedback(ex, accepted, a.pr.settings)
        ids: list[str | None] = [None] * len(accepted)
        summary = None
        if not dry_run and accepted:
            ids = store.add_many([x.to_store(batch_id) for x in accepted])
            summary = _view(st).diff["summary"]
        version = store.version()
    for fb, rid in zip(feedback, ids):
        fb["id"] = rid
    log.info("Полевые замеры: принято %d, отклонено %d, dry_run=%s", len(accepted), len(rejected), dry_run)
    return accepted, rejected, feedback, batch_id, version, summary


@router.post("/measurements", response_model=FieldAddResponse, response_model_exclude_none=True)
def add_measurements(request: Request, body: FieldAddRequest) -> dict:
    """Добавить замеры (метка, GPS, ручной ввод). dry_run=true — только проверка."""
    _check_batch_size(len(body.items), _state(request))
    rows = [i.model_dump() for i in body.items]
    _, rejected, feedback, _, version, summary = _commit(request, rows, body.dry_run, None)
    return {
        "accepted": feedback,
        "rejected": [r.to_dict() for r in rejected],
        "field_version": version,
        "dry_run": body.dry_run,
        "summary": summary,
    }


def parse_paste(text: str, has_header: bool | None) -> tuple[list[dict], list[Rejection], list[str]]:
    """Вставленные строки lat,lon,rsrp_dbm,timestamp -> (items, ошибки разбора, исходные строки)."""
    try:
        records = [
            r
            for r in csv.reader(io.StringIO(text.replace("\r\n", "\n").replace("\r", "\n")))
            if any(c.strip() for c in r)
        ]
    except csv.Error as exc:
        raise DataValidationError(f"не удалось разобрать текст: {exc}") from exc
    if not records:
        raise DataValidationError("нет ни одной строки с данными")
    first = [c.strip().lower() for c in records[0]]
    header = first == PASTE_HEADER
    if has_header is True and not header:
        raise DataValidationError("заголовок указан, но первая строка не равна lat,lon,rsrp_dbm,timestamp")
    if header and has_header is not False:
        records = records[1:]
    items: list[dict] = []
    raws: list[str] = []
    parse_rejected: list[Rejection] = []
    for i, rec in enumerate(records):
        raws.append(",".join(rec))
        if len(rec) != len(FIELD_COLS):
            items.append({})
            parse_rejected.append(
                Rejection(i, "unparseable", f"Ожидается 4 поля lat,lon,rsrp_dbm,timestamp, найдено {len(rec)}")
            )
            continue
        items.append(dict(zip(FIELD_COLS, (c.strip() for c in rec))))
    return items, parse_rejected, raws


@router.post("/paste", response_model=FieldAddResponse, response_model_exclude_none=True)
def paste_measurements(request: Request, body: FieldPasteRequest) -> dict:
    """Вставка CSV-строк формата measurements.csv (одна или несколько); у пакета общий batch_id."""
    st = _state(request)
    items, parse_rejected, raws = parse_paste(body.text, body.has_header)
    _check_batch_size(len(items), st)
    for it in items:
        it["source"] = body.source
    batch_id = None if body.dry_run else uuid.uuid4().hex
    accepted, rejected, feedback, _, version, summary = _commit(request, items, body.dry_run, batch_id, parse_rejected)
    fb_by_index = {fb["index"]: fb for fb in feedback}
    rej_by_index = {r.index: r for r in rejected}
    rows = []
    for i, raw in enumerate(raws):
        if i in fb_by_index:
            rows.append(
                {"index": i, "raw": raw, "status": "ok", **{k: v for k, v in fb_by_index[i].items() if k != "index"}}
            )
        else:
            r = rej_by_index[i]
            rows.append({"index": i, "raw": raw, "status": "rejected", "reason": r.reason, "message": r.message})
    return {
        "accepted": feedback,
        "rejected": [r.to_dict() for r in rejected],
        "field_version": version,
        "dry_run": body.dry_run,
        "batch_id": batch_id if accepted else None,
        "summary": summary,
        "rows": rows,
    }


# --- чтение и удаление ------------------------------------------------------------------------------


@router.get("/measurements")
def list_measurements(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None, pattern="^(map_pin|gps|manual|import)$"),
) -> JSONResponse:
    store = _store(request)
    items = store.list(limit, offset, source)
    st = _state(request)
    with st.field_lock:
        cells = (
            {p["id"]: p["cell_id"] for p in field_points(pd.DataFrame(items), st.default.pr.settings)} if items else {}
        )
    for it in items:
        it["cell_id"] = cells.get(it["id"])
    return JSONResponse(to_jsonable({"items": items, "total": store.count(source), "version": store.version()}))


@router.delete("/measurements/{measurement_id}")
def delete_measurement(request: Request, measurement_id: str) -> dict:
    st = _state(request)
    with st.field_lock:
        if not _store(request).delete(measurement_id):
            raise HTTPException(status_code=404, detail=f"замер {measurement_id} не найден")
        version = _store(request).version()
    return {"deleted": measurement_id, "field_version": version}


@router.delete("/batches/{batch_id}")
def delete_batch(request: Request, batch_id: str) -> dict:
    st = _state(request)
    with st.field_lock:
        n = _store(request).delete_batch(batch_id)
        if not n:
            raise HTTPException(status_code=404, detail=f"пакет {batch_id} не найден")
        version = _store(request).version()
    return {"deleted": n, "batch_id": batch_id, "field_version": version}


@router.delete("/measurements")
def clear_measurements(request: Request, confirm: str | None = None) -> dict:
    """Сброс всех полевых замеров (перед демо). Требует confirm=yes."""
    if confirm != "yes":
        raise DataValidationError("для очистки всех полевых замеров добавьте ?confirm=yes")
    st = _state(request)
    with st.field_lock:
        n = _store(request).clear()
        version = _store(request).version()
    return {"deleted": n, "field_version": version}


@router.get("/version", response_model=FieldVersion)
def get_version(request: Request) -> dict:
    """Лёгкий опрос со стороны основной карты."""
    store = _store(request)
    return {"version": store.version(), "count": store.count()}


@router.get("/cell")
def get_cell(
    request: Request, lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)
) -> dict:
    """В какую клетку попадает точка и что в ней сейчас (с учётом уже сохранённых полевых замеров)."""
    st = _state(request)
    _default(request)
    with st.field_lock:
        view = _view(st)
    return cell_lookup(view.pr, lat, lon)
