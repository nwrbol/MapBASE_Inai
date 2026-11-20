"""Проверка полевых замеров, обратная связь по клетке и пересчёт карты «с полевыми».

Правила проверки те же, что для строк measurements.csv: их единственный источник — `cleaning.classify_rows`.
Полевые замеры НИКОГДА не попадают в result.json: пересчёт возвращает отдельный PipelineResult.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .cleaning import classify_rows
from .config import Settings
from .grid import assign_cells, classify
from .pipeline import PipelineResult, analyze_clean
from .timeutil import get_zone, parse_timestamps

log = logging.getLogger(__name__)

FIELD_COLS = ("lat", "lon", "rsrp_dbm", "timestamp")
CLEAN_COLS = ("lat", "lon", "rsrp_dbm", "timestamp", "ts_utc", "row")
LOW_ACCURACY = "low_accuracy"
MINUS = "−"


@dataclass(frozen=True)
class NormRow:
    index: int
    lat: float
    lon: float
    rsrp_dbm: float
    timestamp: str  # ISO 8601 со смещением (по умолчанию +06:00)
    source: str
    accuracy_m: float | None = None
    device_label: str | None = None
    warning: str | None = None

    def to_store(self, batch_id: str | None = None) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "rsrp_dbm": self.rsrp_dbm,
            "timestamp": self.timestamp,
            "source": self.source,
            "accuracy_m": self.accuracy_m,
            "device_label": self.device_label,
            "batch_id": batch_id,
        }


@dataclass(frozen=True)
class Rejection:
    index: int
    reason: str
    message: str

    def to_dict(self) -> dict:
        return {"index": self.index, "reason": self.reason, "message": self.message}


def _num(v: float) -> str:
    """Число для сообщений: целые без хвоста, настоящий минус."""
    return f"{v:g}".replace("-", MINUS)


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def reject_message(reason: str, lat: float, lon: float, rsrp: float, raw_ts: str, settings: Settings) -> str:
    c = settings.cleaning
    if reason == "unparseable":
        return "Широта, долгота или RSRP не являются числом"
    if reason == "bad_timestamp":
        return f"Время «{raw_ts}» не в формате ISO 8601"
    if reason == "rsrp_out_of_range":
        return f"RSRP {_num(rsrp)} вне {_num(c.rsrp_min)}…{_num(c.rsrp_max)}"
    if reason == "out_of_bounds":
        return f"Точка вне района ({lat:.4f}, {lon:.4f})"
    return "Дубль: такой замер уже есть"


def existing_frame(base_clean: pd.DataFrame, field_df: pd.DataFrame | None) -> pd.DataFrame:
    """Все уже учтённые замеры (очищенные исходные + сохранённые полевые) для поиска дублей и статистики."""
    parts = [base_clean[list(FIELD_COLS)]]
    if field_df is not None and len(field_df):
        parts.append(field_df[list(FIELD_COLS)])
    return pd.concat(parts, ignore_index=True)


def validate_field_rows(
    rows: Sequence[Mapping[str, Any]], settings: Settings, existing: pd.DataFrame
) -> tuple[list[NormRow], list[Rejection]]:
    """Проверяет замеры теми же правилами, что и CSV; дубли ищутся среди существующих и внутри пакета."""
    fs = settings.field_data
    tz = get_zone(settings.daynight.timezone)
    rejected: list[Rejection] = []
    cand_pos: list[int] = []
    cand_raw: list[list[str]] = []
    for i, row in enumerate(rows):
        missing = [k for k in FIELD_COLS if _blank(row.get(k))]
        if missing:
            rejected.append(Rejection(i, "missing_field", f"Не заполнено: {', '.join(missing)}"))
            continue
        acc = row.get("accuracy_m")
        if acc is not None:
            try:
                ok_acc = np.isfinite(float(acc)) and float(acc) >= 0
            except (TypeError, ValueError):
                ok_acc = False
            if not ok_acc:
                rejected.append(Rejection(i, "unparseable", "Некорректная точность GPS"))
                continue
        cand_pos.append(i)
        cand_raw.append([str(row[k]).strip() for k in FIELD_COLS])

    accepted: list[NormRow] = []
    if cand_pos:
        n_exist = len(existing)
        frame = pd.concat(
            [existing[list(FIELD_COLS)].astype(object), pd.DataFrame(cand_raw, columns=list(FIELD_COLS), dtype=object)],
            ignore_index=True,
        )
        verdict = classify_rows(frame, np.zeros(len(frame), dtype=bool), settings)
        subset = list(settings.cleaning.dedup_subset)
        existing_keys = set(verdict.norm.iloc[:n_exist][subset].itertuples(index=False, name=None))
        for k, i in enumerate(cand_pos):
            j = n_exist + k
            reason = str(verdict.reasons[j])
            lat, lon, rsrp = float(verdict.lat.iloc[j]), float(verdict.lon.iloc[j]), float(verdict.rsrp.iloc[j])
            if reason:
                msg = reject_message(reason, lat, lon, rsrp, cand_raw[k][3], settings)
                if reason == "duplicates":
                    same = tuple(verdict.norm.iloc[j][subset]) in existing_keys
                    msg = "Дубль: такой замер уже есть" if same else "Дубль внутри этого пакета"
                rejected.append(Rejection(i, reason, msg))
                continue
            row = rows[i]
            acc = None if row.get("accuracy_m") is None else float(row["accuracy_m"])
            label = str(row.get("device_label") or "").strip()[: fs.device_label_max] or None
            accepted.append(
                NormRow(
                    index=i,
                    lat=lat,
                    lon=lon,
                    rsrp_dbm=rsrp,
                    timestamp=verdict.ts.iloc[j].tz_convert(tz).isoformat(),
                    source=str(row.get("source") or "manual"),
                    accuracy_m=acc,
                    device_label=label,
                    warning=LOW_ACCURACY if acc is not None and acc > fs.accuracy_warn_m else None,
                )
            )
    rejected.sort(key=lambda r: r.index)
    log.info("Проверка полевых замеров: принято %d, отклонено %d", len(accepted), len(rejected))
    return accepted, rejected


def rejected_by_reason(rejected: Sequence[Rejection]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rejected:
        out[r.reason] = out.get(r.reason, 0) + 1
    return out


# --- обратная связь по клетке -------------------------------------------------------------------


def cell_feedback(existing: pd.DataFrame, accepted: Sequence[NormRow], settings: Settings) -> list[dict]:
    """Для каждого принятого замера: клетка, n и медиана после добавления, класс, стала ли клетка учитываемой."""
    if not accepted:
        return []
    min_n = settings.grid.min_samples
    ex = assign_cells(existing[["lat", "lon", "rsrp_dbm"]].astype(float), settings)
    new = assign_cells(
        pd.DataFrame(
            {
                "lat": [a.lat for a in accepted],
                "lon": [a.lon for a in accepted],
                "rsrp_dbm": [a.rsrp_dbm for a in accepted],
            }
        ),
        settings,
    )
    before = ex.groupby("cell_id").size().to_dict() if len(ex) else {}
    after = pd.concat([ex, new], ignore_index=True).groupby("cell_id")["rsrp_dbm"].agg(n="count", med="median")
    out = []
    for a, cell in zip(accepted, new["cell_id"]):
        n_after = int(after.loc[cell, "n"])
        med = float(after.loc[cell, "med"])
        out.append(
            {
                "index": a.index,
                "cell_id": cell,
                "n_after": n_after,
                "median_after": round(med, 4),
                "class_after": classify(med, settings),
                "counted_after": n_after >= min_n,
                "became_counted": before.get(cell, 0) < min_n <= n_after,
                **({"warning": a.warning} if a.warning else {}),
            }
        )
    return out


def cell_lookup(pr: PipelineResult, lat: float, lon: float) -> dict:
    """В какую клетку попадает точка и что в ней сейчас (по пересчёту pr)."""
    from .cleaning import in_bounds
    from .grid import cell_indices, make_cell_id

    s = pr.settings
    ix, iy = cell_indices(np.array([lat]), np.array([lon]), s)
    cell_id = make_cell_id(ix[0], iy[0], s)
    inside = bool(in_bounds(pd.Series([lat]), pd.Series([lon]), s)[0])
    row = pr.grid[pr.grid["cell_id"] == cell_id]
    n = int(row["n"].iloc[0]) if len(row) else 0
    out = {"cell_id": cell_id, "in_bounds": inside, "n": n, "min_samples": s.grid.min_samples}
    if len(row):
        med = float(row["median_rsrp"].iloc[0])
        out.update(median=round(med, 4), cls=str(row["cls"].iloc[0]), counted=bool(row["counted"].iloc[0]))
    else:
        out.update(median=None, cls=None, counted=False)
    return out


# --- пересчёт карты с полевыми замерами -----------------------------------------------------------


def field_clean_frame(field_df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Сохранённые полевые замеры в формате очищенных исходных (lat, lon, rsrp_dbm, timestamp, ts_utc, row)."""
    if field_df is None or field_df.empty:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in CLEAN_COLS})
    ts = parse_timestamps(field_df["timestamp"].astype(str), settings.daynight.timezone)
    return pd.DataFrame(
        {
            "lat": field_df["lat"].astype(float).to_numpy(),
            "lon": field_df["lon"].astype(float).to_numpy(),
            "rsrp_dbm": field_df["rsrp_dbm"].astype(float).to_numpy(),
            "timestamp": field_df["timestamp"].astype(str).to_numpy(),
            "ts_utc": ts.to_numpy(),
            "row": -1,
        }
    )


def run_pipeline_with_field(
    base: PipelineResult, field_df: pd.DataFrame, settings: Settings | None = None
) -> PipelineResult:
    """К уже очищенным исходным замерам добавляются проверенные полевые; дальше всё пересчитывается.

    dropped_rows (CleaningReport) не меняется: это метрика исходного файла. Официальный result.json — у `base`.
    """
    s = settings or base.settings
    extra = field_clean_frame(field_df, s)
    combined = pd.concat([base.clean[list(CLEAN_COLS)], extra], ignore_index=True)
    combined["ts_utc"] = pd.to_datetime(combined["ts_utc"], utc=True)
    cands = pd.DataFrame(
        {
            "id": [c.id for c in base.candidates],
            "lat": [c.lat for c in base.candidates],
            "lon": [c.lon for c in base.candidates],
        }
    )
    now = time.perf_counter()
    return analyze_clean(combined, base.cleaning, base.towers, cands, s, base.bounds_source, {}, now, now)


def _cell_states(grid: pd.DataFrame) -> dict[str, dict]:
    return {
        str(r.cell_id): {
            "n": int(r.n),
            "median": round(float(r.median_rsrp), 4),
            "cls": str(r.cls),
            "counted": bool(r.counted),
        }
        for r in grid.itertuples(index=False)
    }


def field_diff(base: PipelineResult, withf: PipelineResult) -> dict:
    """Что изменилось из-за полевых замеров: клетки (n, медиана, класс, «учитывается») и лучшая пара."""
    a, b = _cell_states(base.grid), _cell_states(withf.grid)
    cells = []
    became_counted = class_changed = 0
    for cid in sorted(set(a) | set(b)):
        before, after = a.get(cid), b.get(cid)
        if before == after:
            continue
        cells.append({"cell_id": cid, "before": before, "after": after})
        if after and after["counted"] and not (before and before["counted"]):
            became_counted += 1
        elif before and after and before["counted"] and after["counted"] and before["cls"] != after["cls"]:
            class_changed += 1
    best = {
        "before": list(base.brute.pair),
        "after": list(withf.brute.pair),
        "changed": list(base.brute.pair) != list(withf.brute.pair),
        "covered_before": base.brute.covered,
        "covered_after": withf.brute.covered,
    }
    return {
        "cells": cells,
        "best_pair": best,
        "summary": {"cells_changed": len(cells), "became_counted": became_counted, "class_changed": class_changed},
    }


def field_points(field_df: pd.DataFrame, settings: Settings) -> list[dict]:
    """Точки для слоя карты: координаты, RSRP, источник, время, точность, клетка."""
    if field_df is None or field_df.empty:
        return []
    cells = assign_cells(field_df[["lat", "lon", "rsrp_dbm"]].astype(float), settings)["cell_id"].tolist()
    d = settings.output.coord_decimals
    return [
        {
            "id": r.id,
            "lat": round(float(r.lat), d),
            "lon": round(float(r.lon), d),
            "rsrp": round(float(r.rsrp_dbm), 2),
            "source": r.source,
            "timestamp": r.timestamp,
            "accuracy_m": None if pd.isna(r.accuracy_m) else float(r.accuracy_m),
            "batch_id": None if r.batch_id is None or pd.isna(r.batch_id) else r.batch_id,
            "cell_id": cell,
        }
        for r, cell in zip(field_df.itertuples(index=False), cells)
    ]


def build_field_map_data(
    base_map: dict,
    base: PipelineResult,
    withf: PipelineResult,
    field_df: pd.DataFrame,
    diff: dict,
    version: int,
    by_source: dict[str, int],
) -> dict:
    """Данные карты «с полевыми» (тот же формат, что у /api/mapdata) + n_field, field_points, field_diff.

    `result` остаётся официальным (по исходным данным); результат с полевыми лежит отдельно в `field_result`.
    """
    from .render import build_map_data
    from .result import to_jsonable

    data = build_map_data(withf)
    data["result"] = base_map["result"]
    data["field_result"] = withf.result
    counts: dict[str, int] = {}
    if field_df is not None and len(field_df):
        cells = assign_cells(field_df[["lat", "lon", "rsrp_dbm"]].astype(float), withf.settings)["cell_id"]
        counts = cells.value_counts().to_dict()
    for c in data["cells"]:
        c["n_field"] = int(counts.get(c["id"], 0))
    data["field_points"] = field_points(field_df, withf.settings)
    data["field_diff"] = diff
    data["meta"] = {
        **data["meta"],
        "field_included": True,
        "field_count": len(field_df),
        "field_version": int(version),
        "field_by_source": by_source,
    }
    return to_jsonable(data)
