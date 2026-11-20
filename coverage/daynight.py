"""Бонус: сравнение покрытия днём [08:00, 20:00) и ночью по местному времени."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Settings
from .grid import GREEN, RED, YELLOW, build_grid, counted_cells, grid_stats
from .timeutil import local_hours


def split_day_night(clean: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Делит очищенные замеры на день и ночь; строки без времени в сравнение не попадают."""
    dn = settings.daynight
    has_ts = clean["ts_utc"].notna().to_numpy()
    hours = local_hours(clean["ts_utc"], dn.timezone)
    if dn.day_start_hour <= dn.day_end_hour:
        is_day = (hours >= dn.day_start_hour) & (hours < dn.day_end_hour)
    else:  # интервал через полночь
        is_day = (hours >= dn.day_start_hour) | (hours < dn.day_end_hour)
    day = clean[has_ts & is_day]
    night = clean[has_ts & ~is_day]
    return day, night, int((~has_ts).sum())


def _shares(stats: dict[str, int]) -> dict[str, float]:
    total = stats["cells_counted"] or 1
    return {k: round(stats[k] / total, 4) for k in (GREEN, YELLOW, RED)}


def compare_day_night(clean: pd.DataFrame, settings: Settings) -> dict:
    """Своя сетка (n ≥ 5) для дня и ночи + разница медиан по клеткам, учитываемым в обеих."""
    day, night, no_ts = split_day_night(clean, settings)
    g_day = build_grid(day, settings)
    g_night = build_grid(night, settings)
    s_day, s_night = grid_stats(g_day), grid_stats(g_night)
    cd = counted_cells(g_day).set_index("cell_id")
    cn = counted_cells(g_night).set_index("cell_id")
    common = sorted(set(cd.index) & set(cn.index))
    delta = (cn.loc[common, "median_rsrp"] - cd.loc[common, "median_rsrp"]) if common else pd.Series(dtype=float)
    red_day = set(cd.index[cd["cls"] == RED])
    red_night = set(cn.index[cn["cls"] == RED])
    dec = settings.output.rsrp_decimals
    return {
        "available": bool(len(day) and len(night)),
        "window": f"[{settings.daynight.day_start_hour:02d}:00, {settings.daynight.day_end_hour:02d}:00) "
        f"{settings.daynight.timezone}",
        "rows_day": len(day),
        "rows_night": len(night),
        "rows_without_timestamp": no_ts,
        "day": {**s_day, "shares": _shares(s_day)},
        "night": {**s_night, "shares": _shares(s_night)},
        "common_cells": len(common),
        "median_delta_db": round(float(np.median(delta)), dec) if len(delta) else None,
        "mean_delta_db": round(float(np.mean(delta)), dec) if len(delta) else None,
        "red_only_night": sorted(red_night - red_day),
        "red_only_day": sorted(red_day - red_night),
        "_grids": {"day": g_day, "night": g_night, "delta": delta},
    }
