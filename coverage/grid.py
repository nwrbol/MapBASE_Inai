"""Сетка 200×200 м в локальной проекции, медиана RSRP по клетке, фильтр n ≥ 5, цвета."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Settings
from .geo import LocalProjection

GREEN, YELLOW, RED = "green", "yellow", "red"

GRID_COLUMNS = [
    "cell_id", "ix", "iy", "n", "median_rsrp", "counted", "cls",
    "center_lat", "center_lon", "south", "west", "north", "east",
]  # fmt: skip


def projection(settings: Settings) -> LocalProjection:
    g = settings.grid
    return LocalProjection(lat0=g.origin_lat, lon0=g.origin_lon, radius=g.earth_radius_m)


def classify(median: float, settings: Settings) -> str:
    """>= -90 green; [-105, -90) yellow; < -105 red."""
    if median >= settings.classes.green_min:
        return GREEN
    if median < settings.classes.red_below:
        return RED
    return YELLOW


def cell_indices(lat: np.ndarray, lon: np.ndarray, settings: Settings) -> tuple[np.ndarray, np.ndarray]:
    """(ix, iy) = (floor(x/size), floor(y/size)); на max-границе — тоже floor, без обрезки индекса."""
    x, y = projection(settings).to_xy(lat, lon)
    size = settings.grid.cell_size_m
    return np.floor(np.asarray(x) / size).astype(np.int64), np.floor(np.asarray(y) / size).astype(np.int64)


def make_cell_id(ix: int, iy: int, settings: Settings) -> str:
    return settings.grid.cell_id_format.format(ix=int(ix), iy=int(iy))


def assign_cells(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Добавляет столбцы ix, iy, cell_id к замерам."""
    out = df.copy()
    if out.empty:
        out["ix"] = pd.Series(dtype=np.int64)
        out["iy"] = pd.Series(dtype=np.int64)
        out["cell_id"] = pd.Series(dtype=object)
        return out
    ix, iy = cell_indices(out["lat"].to_numpy(float), out["lon"].to_numpy(float), settings)
    out["ix"] = ix
    out["iy"] = iy
    out["cell_id"] = [make_cell_id(a, b, settings) for a, b in zip(ix, iy)]
    return out


def build_grid(measurements: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Агрегат по клеткам: n, медиана, флаг учёта (n >= min_samples), класс, центр и углы."""
    if measurements.empty:
        return pd.DataFrame(columns=GRID_COLUMNS)
    m = measurements if "cell_id" in measurements else assign_cells(measurements, settings)
    agg = m.groupby(["cell_id", "ix", "iy"], sort=True)["rsrp_dbm"].agg(n="count", median_rsrp="median").reset_index()
    agg["n"] = agg["n"].astype(int)
    agg["counted"] = agg["n"] >= settings.grid.min_samples
    agg["cls"] = [classify(v, settings) for v in agg["median_rsrp"]]
    size = settings.grid.cell_size_m
    proj = projection(settings)
    ix = agg["ix"].to_numpy(float)
    iy = agg["iy"].to_numpy(float)
    agg["center_lat"], agg["center_lon"] = proj.to_latlon((ix + 0.5) * size, (iy + 0.5) * size)
    agg["south"], agg["west"] = proj.to_latlon(ix * size, iy * size)
    agg["north"], agg["east"] = proj.to_latlon((ix + 1) * size, (iy + 1) * size)
    return agg[GRID_COLUMNS].sort_values("cell_id", kind="mergesort").reset_index(drop=True)


def counted_cells(grid: pd.DataFrame) -> pd.DataFrame:
    return grid[grid["counted"].astype(bool)]


def red_cells(grid: pd.DataFrame) -> pd.DataFrame:
    """Учитываемые красные клетки (слабые зоны)."""
    g = counted_cells(grid)
    return g[g["cls"] == RED].reset_index(drop=True)


def worst_cells(grid: pd.DataFrame, count: int) -> pd.DataFrame:
    """Топ-N худших учитываемых клеток: по медиане по возрастанию, затем по cell_id."""
    g = counted_cells(grid)
    return g.sort_values(["median_rsrp", "cell_id"], kind="mergesort").head(count).reset_index(drop=True)


def grid_stats(grid: pd.DataFrame) -> dict[str, int]:
    g = counted_cells(grid)
    return {
        "cells_total": len(grid),
        "cells_counted": len(g),
        "cells_insufficient": int(len(grid) - len(g)),
        "green": int((g["cls"] == GREEN).sum()),
        "yellow": int((g["cls"] == YELLOW).sum()),
        "red": int((g["cls"] == RED).sum()),
    }
