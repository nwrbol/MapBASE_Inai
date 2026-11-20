#!/usr/bin/env python3
"""Генерирует примеры для импорта полевых замеров (фиксированный seed, СИНТЕТИЧЕСКИЕ данные).

  samples/field_simple.csv            10 строк в формате measurements.csv, все внутри района
  samples/engineering_app_like.txt    имитация лога инженерного приложения (НЕ реальная выгрузка)

Точки размещаются в клетках открытого набора, где замеров меньше 5: после импорта эти клетки становятся
учитываемыми. Запуск: python scripts/make_field_samples.py [--out samples]
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coverage.config import load_settings  # noqa: E402
from coverage.pipeline import Inputs, run_pipeline  # noqa: E402

SEED = 7
TZ_OFFSET = timezone(timedelta(hours=6))  # Asia/Bishkek
START = datetime(2026, 11, 20, 21, 10, 0, tzinfo=TZ_OFFSET)


def points_in_cell(rng: random.Random, row, count: int) -> list[tuple[float, float]]:
    """Случайные точки строго внутри клетки (с отступом от границ)."""
    out = []
    for _ in range(count):
        u, v = rng.uniform(0.15, 0.85), rng.uniform(0.15, 0.85)
        out.append((row.south + (row.north - row.south) * u, row.west + (row.east - row.west) * v))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "samples"))
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()
    settings = load_settings(ROOT / "config.yaml")
    pr = run_pipeline(Inputs.from_dir(args.data_dir, settings.bounds.bounds_file), settings)
    thin = pr.grid[pr.grid["n"] < settings.grid.min_samples].sort_values(["n", "cell_id"], kind="mergesort")
    need = settings.grid.min_samples
    rng = random.Random(SEED)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # --- простой CSV: формат measurements.csv, 10 строк -------------------------------------------
    simple: list[tuple[float, float]] = []
    for row in thin.itertuples(index=False):
        if len(simple) >= 10:
            break
        k = min(need - int(row.n), 10 - len(simple))
        if row.n in (3, 4):
            simple += points_in_cell(rng, row, k)
    lines = ["lat,lon,rsrp_dbm,timestamp"]
    for i, (lat, lon) in enumerate(simple):
        ts = (START + timedelta(seconds=17 * i)).isoformat()
        lines.append(f"{lat:.6f},{lon:.6f},{rng.randint(-122, -104)},{ts}")
    (out / "field_simple.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # --- имитация лога инженерного приложения -------------------------------------------------------
    cells_1 = [r for r in thin.itertuples(index=False) if r.n == 1][:4]
    cell_3 = next(r for r in thin.itertuples(index=False) if r.n == 3)
    lte: list[tuple[float, float]] = []
    for r in cells_1:
        lte += points_in_cell(rng, r, need - int(r.n))
    lte += points_in_cell(rng, cell_3, need - int(cell_3.n))
    rng.shuffle(lte)

    def fmt_ts(i: int) -> str:
        return (START + timedelta(seconds=3 * i)).strftime("%Y.%m.%d_%H.%M.%S")

    rows: list[list[str]] = []
    n = 0

    def add(tech: str, lat: float, lon: float, level: int) -> list[str]:
        nonlocal n
        rec = [fmt_ts(n), f"{lon:.6f}", f"{lat:.6f}", f"{rng.uniform(0, 14):.1f}", tech, str(level), str(rng.randint(-14, -7)), str(25000 + rng.randint(0, 99))]
        n += 1
        rows.append(rec)
        return rec

    b = settings.bounds
    umts_pts = [(b.lat_min + 0.005 + 0.004 * i, b.lon_min + 0.01 + 0.006 * i) for i in range(5)]
    outside = [(b.lat_max + 0.02, b.lon_min + 0.02), (b.lat_min - 0.03, b.lon_max - 0.01), (b.lat_min + 0.01, b.lon_max + 0.05)]
    originals = []
    for i, (lat, lon) in enumerate(lte):
        originals.append(add("LTE", lat, lon, rng.randint(-125, -98)))
        if i in (2, 8):  # логгер дважды записал одну и ту же секунду: точный дубль
            rows.append(list(originals[-1]))
        if i % 4 == 1 and umts_pts:
            lat_u, lon_u = umts_pts.pop()
            add("UMTS", lat_u, lon_u, rng.randint(-90, -70))  # у UMTS «Level» — RSCP, не RSRP
        if i in (5, 11, 14) and outside:
            lat_o, lon_o = outside.pop()
            add("LTE", lat_o, lon_o, rng.randint(-120, -100))
    while umts_pts:
        lat_u, lon_u = umts_pts.pop()
        add("UMTS", lat_u, lon_u, rng.randint(-90, -70))
    head = "# SYNTHETIC log imitating an engineering app export (scripts/make_field_samples.py, seed=7); not a real capture"
    cols = ["Timestamp", "Longitude", "Latitude", "Speed", "NetworkTech", "Level", "Qual", "CellID"]
    text = head + "\n" + "\t".join(cols) + "\n" + "\n".join("\t".join(r) for r in rows) + "\n"
    (out / "engineering_app_like.txt").write_text(text, encoding="utf-8")
    print(f"field_simple.csv: {len(simple)} строк; engineering_app_like.txt: {len(rows)} строк -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
