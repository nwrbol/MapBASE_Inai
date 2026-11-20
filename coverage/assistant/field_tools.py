"""Функции ассистента для полевых замеров.

Данные читаются сервером. Добавление замера только ПРЕДЛАГАЕТСЯ: проверка идёт теми же правилами, что и у формы,
но в базу ничего не пишется. Решение принимает пользователь кнопкой в чате. Удаления, очистки и импорта файлов
у ассистента нет.
"""

from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import Field

from coverage.field_ingest import cell_feedback, cell_lookup, existing_frame, field_points, validate_field_rows
from coverage.geo import haversine_m

from .tools import Location, tool

MAX_PROPOSALS = 5
MINUS = "−"


def _need_field(ctx):
    if ctx.state.get("uploaded"):
        raise ValueError("Полевые замеры доступны только для исходного набора данных")
    if getattr(ctx, "field_store", None) is None:
        raise ValueError("Хранилище полевых замеров недоступно")


def _center(cell):
    if "c" in cell:
        return cell["c"][0], cell["c"][1]
    b = cell["b"]
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _origin(ctx, near):
    if near is not None:  # после model_dump это словарь
        return (near["lat"], near["lon"]) if isinstance(near, dict) else (near.lat, near.lon)
    view = ctx.state.get("view", {})
    if view.get("center"):
        return view["center"][0], view["center"][1]
    b = ctx.data["meta"]["bounds"]
    return (b["lat_min"] + b["lat_max"]) / 2, (b["lon_min"] + b["lon_max"]) / 2


@tool
def cells_needing_data(
    ctx,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
    near: Location | None = None,
    include_empty: bool = True,
):
    """Клетки района, где замеров меньше порога или нет вовсе, от ближайших к точке (по умолчанию к центру вида)."""
    min_n = ctx.data["meta"]["min_samples"]
    lat0, lon0 = _origin(ctx, near)

    def item(cell, n):
        lat, lon = _center(cell)
        return {
            "id": cell["id"],
            "n": n,
            "missing": min_n - n,
            "center": [round(lat, 6), round(lon, 6)],
            "distance_m": round(float(haversine_m(lat0, lon0, lat, lon))),
        }

    few = [item(c, c["n"]) for c in ctx.data["cells"] if not c["counted"]]
    empty = [item(c, 0) for c in ctx.data.get("empty_cells", [])]
    pool = few + (empty if include_empty else [])
    return {
        "total": len(few) + len(empty),
        "n_lt_min": len(few),
        "empty": len(empty),
        "min_samples": min_n,
        "cells": sorted(pool, key=lambda c: c["distance_m"])[:limit],
    }


@tool
def check_point(
    ctx,
    lat: Annotated[float, Field(ge=-90, le=90)],
    lon: Annotated[float, Field(ge=-180, le=180)],
):
    """Проверить точку: в районе ли она, её клетка, текущие замеры и станет ли клетка учитываемой после ещё одного."""
    info = cell_lookup(ctx.pr, lat, lon)
    info["will_become_counted"] = bool(
        info["in_bounds"] and not info["counted"] and info["n"] + 1 >= info["min_samples"]
    )
    return info


@tool
def list_field_measurements(
    ctx,
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
    mine: bool = False,
):
    """Последние полевые замеры (время, источник, клетка, RSRP, подпись автора); mine: только замеры этого пользователя."""
    _need_field(ctx)
    if mine and not getattr(ctx, "device_id", None):
        raise ValueError("Не удалось определить устройство пользователя")
    rows = ctx.field_store.list(limit, 0, ctx.device_id if mine else None)
    cells = {p["id"]: p["cell_id"] for p in field_points(pd.DataFrame(rows), ctx.pr.settings)} if rows else {}
    return {
        "items": [
            {
                "timestamp": r["timestamp"],
                "source": r["source"],
                "cell_id": cells.get(r["id"]),
                "rsrp_dbm": r["rsrp_dbm"],
                "author": r["author_label"],
            }
            for r in rows
        ],
        "total": ctx.field_store.total(ctx.device_id if mine else None),
    }


@tool
def get_field_events(ctx, limit: Annotated[int, Field(ge=1, le=20)] = 10):
    """Лента изменений полевых замеров: кто что добавил или удалил и когда."""
    _need_field(ctx)
    return {
        "version": ctx.field_store.version(),
        "events": [
            {
                "type": e["type"],
                "summary": e["summary"],
                "author": e["author_label"],
                "count": e["count"],
                "created_at": e["created_at"],
            }
            for e in ctx.field_store.events(0, limit)
        ],
    }


@tool
def propose_field_measurement(
    ctx,
    lat: Annotated[float, Field(ge=-90, le=90)],
    lon: Annotated[float, Field(ge=-180, le=180)],
    rsrp_dbm: float,
    timestamp: Annotated[str | None, Field(max_length=64)] = None,
    note: Annotated[str | None, Field(max_length=200)] = None,
):
    """Предложить добавить полевой замер. Значение RSRP должен назвать пользователь. В базу ничего не пишется: добавляет пользователь кнопкой."""
    _need_field(ctx)
    if ctx.proposals >= MAX_PROPOSALS:
        raise ValueError(f"За один ответ можно предложить не больше {MAX_PROPOSALS} замеров")
    settings = ctx.base_pr.settings
    ts = timestamp or datetime.now(ZoneInfo(settings.daynight.timezone)).isoformat(timespec="seconds")
    row = {"lat": lat, "lon": lon, "rsrp_dbm": rsrp_dbm, "timestamp": ts, "source": "manual"}
    existing = existing_frame(ctx.base_pr.clean, ctx.field_store.as_dataframe())
    accepted, rejected = validate_field_rows(
        [row], settings, existing
    )  # те же правила, что у формы; запись не выполняется
    if rejected:
        raise ValueError(rejected[0].message)
    fb = cell_feedback(existing, accepted, settings)[0]
    n_after, n_before = fb["n_after"], fb["n_after"] - 1
    ctx.proposals += 1
    rsrp_txt = f"{accepted[0].rsrp_dbm:g}".replace("-", MINUS)
    args = {
        "lat": accepted[0].lat,
        "lon": accepted[0].lon,
        "rsrp_dbm": accepted[0].rsrp_dbm,
        "timestamp": accepted[0].timestamp,
        "cell_id": fb["cell_id"],
        "n_before": n_before,
        "n_after": n_after,
        "becomes_counted": bool(fb["became_counted"]),
        "note": note or "",
    }
    label = f"Добавить замер: {lat:.4f}, {lon:.4f} · {rsrp_txt} дБм · клетка {fb['cell_id']} ({n_before}→{n_after})"
    ctx.action("propose_field_measurement", args, label)
    return {
        "proposed": True,
        "cell_id": fb["cell_id"],
        "n_before": n_before,
        "n_after": n_after,
        "becomes_counted": args["becomes_counted"],
        "note": "Замер НЕ добавлен: пользователь подтвердит кнопкой в чате",
    }


@tool
def open_field_form(
    ctx,
    lat: Annotated[float | None, Field(ge=-90, le=90)] = None,
    lon: Annotated[float | None, Field(ge=-180, le=180)] = None,
    rsrp_dbm: Annotated[float | None, Field(ge=-140, le=-44)] = None,
    method: Literal["pin", "gps", "manual"] | None = None,
):
    """Открыть форму полевых замеров с заполненными полями. Ничего не отправляет."""
    args = {k: v for k, v in dict(lat=lat, lon=lon, rsrp_dbm=rsrp_dbm, method=method).items() if v is not None}
    return ctx.action("open_field_form", args, "Открыть форму замеров")


@tool
def show_field_points(ctx, only_recent_minutes: Annotated[int | None, Field(ge=1, le=1440)] = None):
    """Показать полевые замеры на карте: включить слой и подогнать вид (за последние N минут, если задано)."""
    _need_field(ctx)
    if ctx.field_store.total() == 0:
        raise ValueError("Полевых замеров пока нет")
    if not ctx.state.get("field_included") and ctx.dataset:
        ctx.use_data(*ctx.dataset(True))
        ctx.state["field_included"] = True
    args = {} if only_recent_minutes is None else {"only_recent_minutes": only_recent_minutes}
    label = "Показать полевые замеры" + (f" за {only_recent_minutes} мин" if only_recent_minutes else "")
    return ctx.action("show_field_points", args, label, {"accepted": True, "count": ctx.field_store.total()})
