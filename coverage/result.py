"""Сборка result.json (ровно 4 ключа ТЗ) и подробного report.json."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import Settings

RESULT_KEYS = ("worst_cells", "best_candidates", "dropped_rows", "covered_weak_cells")


def to_jsonable(obj: Any) -> Any:
    """Рекурсивно приводит numpy/pandas-типы к обычным Python-типам."""
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = sorted(obj) if isinstance(obj, (set, frozenset)) else obj
        return [to_jsonable(v) for v in items]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return [to_jsonable(v) for v in obj.tolist()]
    return obj


def format_worst(worst: pd.DataFrame, settings: Settings) -> list:
    """worst_cells в формате из конфига (по умолчанию [{cell_id, rsrp_median}])."""
    o = settings.output
    fmt = settings.worst.format
    if fmt == "id":
        return [str(c) for c in worst["cell_id"]]
    if fmt == "latlon":
        return [
            [round(float(a), o.coord_decimals), round(float(b), o.coord_decimals)]
            for a, b in zip(worst["center_lat"], worst["center_lon"])
        ]
    return [
        {"cell_id": str(c), "rsrp_median": round(float(m), o.rsrp_decimals)}
        for c, m in zip(worst["cell_id"], worst["median_rsrp"])
    ]


def build_result(worst: list, pair: list[str], dropped: int, covered: int) -> dict:
    result = {
        "worst_cells": worst,
        "best_candidates": sorted(pair),
        "dropped_rows": int(dropped),
        "covered_weak_cells": int(covered),
    }
    assert tuple(result) == RESULT_KEYS
    return result


def dumps(data: Any) -> str:
    return json.dumps(to_jsonable(data), ensure_ascii=False, indent=2) + "\n"


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(data), encoding="utf-8")
    return p
