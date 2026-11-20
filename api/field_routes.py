"""Полевые замеры: /field (страница) и /api/field/*. result.json эти эндпоинты не затрагивают."""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from coverage.errors import DataValidationError
from coverage.field_import import ImportOptions, ImportTooLarge, analyze, preview_response, require_complete
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
from coverage.field_store import device_tag
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


SOURCE_RU = {"map_pin": "метка", "gps": "GPS", "manual": "вручную", "import": "импорт"}
MINUS = "−"


def _device(value: str | None) -> str | None:
    """X-Device-Id в каноническом виде (uuid) или None, если заголовка нет или он неверный."""
    try:
        return str(uuid.UUID(value)) if value else None
    except ValueError:
        return None


def optional_device(x_device_id: str | None = Header(None)) -> str | None:
    return _device(x_device_id)


def require_device(x_device_id: str | None = Header(None)) -> str:
    """Для всех изменений: без валидного uuid в X-Device-Id ответ 400."""
    dev = _device(x_device_id)
    if dev is None:
        raise HTTPException(status_code=400, detail="Нужен заголовок X-Device-Id (uuid устройства)")
    return dev


def _public_row(row: dict, me: str | None) -> dict:
    """Замер для ответа: полный device_id наружу не отдаётся (это ключ владельца), только короткий след."""
    out = {k: v for k, v in row.items() if k != "device_id"}
    out["device_tag"] = device_tag(row["device_id"])
    out["is_mine"] = me is not None and row["device_id"] == me
    return out


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
                a.map_data, a.pr, view.pr, view.field_df, view.diff, view.version, st.field_store.count()["by_source"]
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


def _add_summary(accepted: list[NormRow], feedback: list[dict]) -> str | None:
    """Текст события для ленты; для пачки и импорта текст строит хранилище."""
    if len(accepted) != 1 or accepted[0].source == "import":
        return None
    a = accepted[0]
    rsrp = f"{a.rsrp_dbm:g}".replace("-", MINUS)
    return f"+1 замер в {feedback[0]['cell_id']} ({rsrp} дБм) · {SOURCE_RU.get(a.source, a.source)}"


def _commit(
    request: Request,
    rows: list[dict],
    dry_run: bool,
    batch_id: str | None,
    device_id: str,
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
            label = next((x.author_label for x in accepted if x.author_label), None)
            ids = store.add_many(
                [x.to_store(batch_id) for x in accepted],
                device_id,
                label,
                batch_id,
                _add_summary(accepted, feedback),
            ).ids
            summary = _view(st).diff["summary"]
        version = store.version()
    for fb, rid in zip(feedback, ids):
        fb["id"] = rid
    log.info("Полевые замеры: принято %d, отклонено %d, dry_run=%s", len(accepted), len(rejected), dry_run)
    return accepted, rejected, feedback, batch_id, version, summary


@router.post("/measurements", response_model=FieldAddResponse, response_model_exclude_none=True)
def add_measurements(request: Request, body: FieldAddRequest, device: str = Depends(require_device)) -> dict:
    """Добавить замеры (метка, GPS, ручной ввод). dry_run=true — только проверка."""
    _check_batch_size(len(body.items), _state(request))
    rows = [i.model_dump() for i in body.items]
    _, rejected, feedback, _, version, summary = _commit(request, rows, body.dry_run, None, device)
    return {
        "accepted": feedback,
        "rejected": [r.to_dict() for r in rejected],
        "version": version,
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
def paste_measurements(request: Request, body: FieldPasteRequest, device: str = Depends(require_device)) -> dict:
    """Вставка CSV-строк формата measurements.csv (одна или несколько); у пакета общий batch_id."""
    st = _state(request)
    items, parse_rejected, raws = parse_paste(body.text, body.has_header)
    _check_batch_size(len(items), st)
    for it in items:
        it["source"] = body.source
    batch_id = None if body.dry_run else uuid.uuid4().hex
    accepted, rejected, feedback, _, version, summary = _commit(
        request, items, body.dry_run, batch_id, device, parse_rejected
    )
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
        "version": version,
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
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None, pattern="^(map_pin|gps|manual|import)$"),
    mine: bool = False,
    since_seq: int | None = Query(None, ge=0),
    device: str | None = Depends(optional_device),
) -> JSONResponse:
    """Все замеры (видят все); mine=true — только свои (по X-Device-Id). Новые сверху."""
    if mine and device is None:
        raise HTTPException(status_code=400, detail="Для mine=true нужен заголовок X-Device-Id (uuid устройства)")
    store = _store(request)
    own = device if mine else None
    items = store.list(limit, offset, own, since_seq, source)
    st = _state(request)
    with st.field_lock:
        cells = (
            {p["id"]: p["cell_id"] for p in field_points(pd.DataFrame(items), st.default.pr.settings)} if items else {}
        )
    rows = [_public_row(it, device) | {"cell_id": cells.get(it["id"])} for it in items]
    return JSONResponse(
        to_jsonable({"items": rows, "total": store.total(own, source), "version": store.version(), "mine": mine})
    )


@router.delete("/measurements/{measurement_id}")
def delete_measurement(request: Request, measurement_id: str, device: str = Depends(require_device)) -> dict:
    st = _state(request)
    with st.field_lock:
        try:
            ch = _store(request).delete(measurement_id, device)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"замер {measurement_id} не найден") from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
    return {"deleted": measurement_id, "version": ch.seq, "field_version": ch.seq}


@router.delete("/batches/{batch_id}")
def delete_batch(request: Request, batch_id: str, device: str = Depends(require_device)) -> dict:
    st = _state(request)
    with st.field_lock:
        try:
            ch = _store(request).delete_batch(batch_id, device)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"пакет {batch_id} не найден") from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
    return {"deleted": ch.count, "batch_id": batch_id, "version": ch.seq, "field_version": ch.seq}


@router.delete("/all")
def clear_all(request: Request, x_admin_token: str | None = Header(None)) -> dict:
    """Полный сброс (перед демо). Нужен X-Admin-Token == env ADMIN_TOKEN; без ADMIN_TOKEN эндпоинт отключён."""
    st = _state(request)
    expected = st.admin_token
    if not expected:
        raise HTTPException(status_code=403, detail="Сброс отключён: на сервере не задан ADMIN_TOKEN")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Неверный токен администратора")
    with st.field_lock:
        ch = _store(request).clear()
    return {"deleted": ch.count, "version": ch.seq, "field_version": ch.seq}


@router.get("/version", response_model=FieldVersion)
def get_version(request: Request) -> dict:
    """Лёгкий опрос со стороны карты и формы."""
    store = _store(request)
    c = store.count()
    return {
        "version": store.version(),
        "count": c["total"],
        "by_source": c["by_source"],
        "authors": c["authors"],
        "last_hour": c["last_hour"],
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.get("/events")
def get_events(
    request: Request,
    since_seq: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    device: str | None = Depends(optional_device),
) -> JSONResponse:
    """Лента изменений: кто, что и когда. Самые новые сверху."""
    store = _store(request)
    events = []
    for e in store.events(since_seq, limit):
        dev = e.pop("device_id")
        e["device_tag"] = device_tag(dev)
        e["is_mine"] = device is not None and dev == device
        events.append(e)
    return JSONResponse({"events": events, "version": store.version()})


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


@router.get("/config")
def get_field_config(request: Request) -> JSONResponse:
    """Всё, что нужно странице /field: границы, пороги, часовой пояс и клетки (с учётом сохранённых полевых замеров)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    st = _state(request)
    _default(request)
    with st.field_lock:
        view = _view(st)
    s = view.pr.settings
    d = s.output.coord_decimals
    offset = datetime.now(ZoneInfo(s.daynight.timezone)).utcoffset()
    b = s.bounds
    cells = [
        {
            "id": r.cell_id,
            "n": int(r.n),
            "counted": bool(r.counted),
            "cls": r.cls,
            "m": round(float(r.median_rsrp), 2),
            "b": [round(float(x), d) for x in (r.south, r.west, r.north, r.east)],
        }
        for r in view.pr.grid.itertuples(index=False)
    ]
    return JSONResponse(
        to_jsonable(
            {
                "version": view.version,
                "meta": {
                    "bounds": {"lat_min": b.lat_min, "lat_max": b.lat_max, "lon_min": b.lon_min, "lon_max": b.lon_max},
                    "polygon": b.polygon,
                    "green_min": s.classes.green_min,
                    "red_below": s.classes.red_below,
                    "min_samples": s.grid.min_samples,
                    "cell_size_m": s.grid.cell_size_m,
                    "rsrp_min": s.cleaning.rsrp_min,
                    "rsrp_max": s.cleaning.rsrp_max,
                    "accuracy_warn_m": s.field_data.accuracy_warn_m,
                    "max_batch": s.field_data.max_batch,
                    "timezone": s.daynight.timezone,
                    "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
                },
                "cells": cells,
            }
        )
    )


# --- импорт CSV из инженерных приложений ----------------------------------------------------------------


def _options(raw: str | None) -> ImportOptions:
    if not raw or not raw.strip():
        return ImportOptions()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataValidationError(f"options: некорректный JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise DataValidationError("options: ожидается JSON-объект")
    return ImportOptions.from_json(data)


async def _read_import(file: UploadFile, st: AppState) -> bytes:
    limit = st.settings.field_data.import_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ImportTooLarge(f"файл больше {limit // (1024 * 1024)} МБ")
    if not data:
        raise DataValidationError("файл пустой")
    return data


@router.post("/import/preview", tags=["field-import"])
async def import_preview(
    request: Request, file: UploadFile = File(...), options: str | None = Form(None)
) -> JSONResponse:
    """Шаг 1: разбор файла и проверка всех строк БЕЗ записи. Состояния нет: на шаге 2 файл передаётся повторно."""
    st = _state(request)
    a_ = _default(request)
    opts = _options(options)
    data = await _read_import(file, st)
    with st.field_lock:
        ex = existing_frame(a_.pr.clean, st.field_store.as_dataframe())
    analysis = await run_in_threadpool(analyze, data, opts, a_.pr.settings, ex)
    return JSONResponse(to_jsonable(preview_response(analysis, a_.pr.settings)))


@router.post("/import/commit", tags=["field-import"])
async def import_commit(
    request: Request,
    file: UploadFile = File(...),
    options: str | None = Form(None),
    confirm_units: bool = Form(False),
    device: str = Depends(require_device),
) -> JSONResponse:
    """Шаг 2: сохранение под одним batch_id (его можно отменить целиком через DELETE /batches/{id})."""
    st = _state(request)
    a_ = _default(request)
    opts = _options(options)
    data = await _read_import(file, st)

    def work() -> dict:
        with st.field_lock:
            ex = existing_frame(a_.pr.clean, st.field_store.as_dataframe())
            analysis = analyze(data, opts, a_.pr.settings, ex)
            require_complete(analysis, confirm_units)
            batch_id = uuid.uuid4().hex
            label = next((x.author_label for x in analysis.accepted if x.author_label), None)
            st.field_store.add_many([x.to_store(batch_id) for x in analysis.accepted], device, label, batch_id)
            view = _view(st)
            return {
                "batch_id": batch_id,
                "accepted": len(analysis.accepted),
                "rejected_by_reason": analysis.rejected_counts,
                "filtered_tech": len(analysis.filtered),
                "field_diff": view.diff,
                "field_version": view.version,
            }

    result = await run_in_threadpool(work)
    log.info("Импорт сохранён: принято %d", result["accepted"])
    return JSONResponse(to_jsonable(result))
