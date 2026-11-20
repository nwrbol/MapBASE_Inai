"""Генерация самодостаточного map.html: Leaflet, стили и данные встраиваются в один файл."""

from __future__ import annotations

import base64
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .geo import points_in_polygon
from .grid import make_cell_id, projection
from .pipeline import PipelineResult
from .result import to_jsonable

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATIC = WEB / "static"
VENDOR = STATIC / "vendor"


def _cell_bounds(r: Any, d: int) -> list[float]:
    return [round(float(r.south), d), round(float(r.west), d), round(float(r.north), d), round(float(r.east), d)]


def _empty_cells(pr: PipelineResult) -> list[dict]:
    """Клетки без замеров для отображения на карте."""
    s = pr.settings
    b = s.bounds
    proj = projection(s)
    size = s.grid.cell_size_m
    digits = s.output.coord_decimals
    if b.polygon is None:
        south, north, west, east = b.lat_min, b.lat_max, b.lon_min, b.lon_max
    else:
        lat, lon = zip(*b.polygon)
        south, north, west, east = min(lat), max(lat), min(lon), max(lon)
    x0, y0 = proj.to_xy(south, west)
    x1, y1 = proj.to_xy(north, east)
    xs = range(math.floor(round(x0 / size, 9)), math.ceil(round(x1 / size, 9)))
    ys = range(math.floor(round(y0 / size, 9)), math.ceil(round(y1 / size, 9)))
    # Не материализуем огромную сетку при произвольном размере клетки из веб-формы.
    if len(xs) * len(ys) > 50_000:
        return []
    ix, iy = (axis.ravel() for axis in np.meshgrid(xs, ys))
    if b.polygon is not None:
        lat, lon = proj.to_latlon((ix + 0.5) * size, (iy + 0.5) * size)
        inside = points_in_polygon(lat, lon, b.polygon)
        ix, iy = ix[inside], iy[inside]
    occupied = set(zip(pr.grid["ix"], pr.grid["iy"]))
    south, west = proj.to_latlon(ix * size, iy * size)
    north, east = proj.to_latlon((ix + 1) * size, (iy + 1) * size)
    return [
        {"id": make_cell_id(x, y, s), "b": [round(float(v), digits) for v in bounds]}
        for x, y, *bounds in zip(ix, iy, south, west, north, east)
        if (x, y) not in occupied
    ]


def build_map_data(pr: PipelineResult) -> dict:
    """Все данные для карты и боковой панели одним JSON (общий формат для CLI и API)."""
    s = pr.settings
    d = s.output.coord_decimals
    dec = s.output.rsrp_decimals
    ranks = {cid: i + 1 for i, cid in enumerate(pr.worst["cell_id"])}
    covered = set(pr.brute.covered_cells)
    cells = [
        {
            "id": r.cell_id,
            "n": int(r.n),
            "m": round(float(r.median_rsrp), dec),
            "cls": r.cls,
            "counted": bool(r.counted),
            "rank": ranks.get(r.cell_id),
            "covered": r.cell_id in covered,
            "b": _cell_bounds(r, d),
            "c": [round(float(r.center_lat), d), round(float(r.center_lon), d)],
        }
        for r in pr.grid.itertuples(index=False)
    ]
    covers = {c.id: sorted(c.covers) for c in pr.candidates}
    candidates = [{**c, "covers": covers.get(c["id"], [])} for c in pr.report["candidates"]]

    dn = pr.daynight
    grids = dn["_grids"]

    def dn_cells(g: Any) -> list[dict]:
        return [
            {
                "id": r.cell_id,
                "n": int(r.n),
                "m": round(float(r.median_rsrp), dec),
                "cls": r.cls,
                "b": _cell_bounds(r, d),
            }
            for r in g.itertuples(index=False)
            if bool(r.counted)
        ]

    bounds_by_id = {c["id"]: c["b"] for c in cells}
    by_day = grids["day"].set_index("cell_id")
    by_night = grids["night"].set_index("cell_id")
    delta = []
    for cid in grids["delta"].index:
        if cid not in bounds_by_id:
            continue
        m_day = round(float(by_day.at[cid, "median_rsrp"]), dec)
        m_night = round(float(by_night.at[cid, "median_rsrp"]), dec)
        delta.append(
            {
                "id": cid,
                "d": round(m_night - m_day, dec),  # из уже округлённых медиан: d == m_night − m_day
                "m_day": m_day,
                "m_night": m_night,
                "n_day": int(by_day.at[cid, "n"]),
                "n_night": int(by_night.at[cid, "n"]),
                "b": bounds_by_id[cid],
            }
        )
    # Клетки, учитываемые в основной сетке (n >= min_samples), но без сравнения: в одной из половин суток замеров мало
    in_delta = {c["id"] for c in delta}

    def half_n(frame: Any, cid: str) -> int:
        return int(frame.at[cid, "n"]) if cid in frame.index else 0

    delta_insufficient = [
        {"id": c["id"], "b": c["b"], "n_day": half_n(by_day, c["id"]), "n_night": half_n(by_night, c["id"])}
        for c in cells
        if c["counted"] and c["id"] not in in_delta
    ]
    b = s.bounds
    return to_jsonable(
        {
            "meta": {
                "version": __version__,
                "bounds": {"lat_min": b.lat_min, "lat_max": b.lat_max, "lon_min": b.lon_min, "lon_max": b.lon_max},
                "polygon": b.polygon,
                "bounds_source": pr.bounds_source,
                "cell_size_m": s.grid.cell_size_m,
                "min_samples": s.grid.min_samples,
                "green_min": s.classes.green_min,
                "red_below": s.classes.red_below,
                "tower_exclusion_m": s.selection.tower_exclusion_m,
                "coverage_radius_m": s.selection.coverage_radius_m,
                "daynight_window": dn["window"],
                "daynight_delta_bins": list(s.daynight.delta_bins_db),
            },
            "result": pr.result,
            "cleaning": pr.report["cleaning"],
            "stats": pr.report["grid"],
            "worst": pr.report["worst_cells_detailed"],
            "cells": cells,
            "empty_cells": _empty_cells(pr),
            "towers": [
                {"id": t.id, "lat": float(t.lat), "lon": float(t.lon)} for t in pr.towers.itertuples(index=False)
            ],
            "candidates": candidates,
            "compare": {
                "brute_force": {k: v for k, v in pr.brute.to_dict().items() if k != "covered_cells"},
                "greedy": {k: v for k, v in pr.greedy.to_dict().items() if k != "covered_cells"},
                "greedy_covered_cells": pr.greedy.covered_cells,
            },
            "daynight": {
                **pr.report["daynight"],
                "day_cells": dn_cells(grids["day"]),
                "night_cells": dn_cells(grids["night"]),
                "delta": delta,
                "delta_insufficient": delta_insufficient,
            },
            "heat": pr.heat,
        }
    )


def _inline_css_images(css: str, base: Path) -> str:
    """url(images/x.png) -> data URI, чтобы map.html открывался без внешних файлов."""

    def repl(m: re.Match[str]) -> str:
        path = base / m.group(1)
        if not path.exists():
            return m.group(0)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"url(data:image/png;base64,{data})"

    return re.sub(r"url\((images/[\w.-]+\.png)\)", repl, css)


@lru_cache(maxsize=1)
def _assets() -> dict[str, str]:
    leaflet_dir = VENDOR / "leaflet"
    return {
        "leaflet_css": _inline_css_images((leaflet_dir / "leaflet.css").read_text(encoding="utf-8"), leaflet_dir),
        "leaflet_js": (leaflet_dir / "leaflet.js").read_text(encoding="utf-8"),
        "heat_js": (VENDOR / "leaflet-heat.js").read_text(encoding="utf-8"),
        "layers_js": (STATIC / "layers.js").read_text(encoding="utf-8"),
        "scenario_js": (STATIC / "scenario.js").read_text(encoding="utf-8"),
        "app_css": (STATIC / "style.css").read_text(encoding="utf-8"),
        "app_js": (STATIC / "app.js").read_text(encoding="utf-8"),
    }


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(WEB / "templates")),
        autoescape=select_autoescape(["html", "j2"]),
    )


def safe_json(data: Any) -> str:
    """JSON для <script type="application/json">: экранируем '</' и спецсимволы."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_page(map_data: dict | None, server_mode: bool = False) -> str:
    """map_data=None + server_mode -> интерактивная страница, данные берутся через API."""
    template = _env().get_template("map.html.j2")
    return template.render(
        inline=not server_mode,
        server_mode=server_mode,
        assets=_assets() if not server_mode else {},
        data_json=safe_json(map_data) if map_data is not None else None,
        version=__version__,
    )


def render_map_html(pr: PipelineResult) -> str:
    return render_page(build_map_data(pr), server_mode=False)


def write_map_html(pr: PipelineResult, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_map_html(pr), encoding="utf-8")
    return p
