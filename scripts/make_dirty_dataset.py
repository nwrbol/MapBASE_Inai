#!/usr/bin/env python3
"""Самопроверка «как у жюри»: внедряем известный мусор в measurements.csv и сверяем dropped_rows.

python scripts/make_dirty_dataset.py [--data-dir data] [--out selfcheck_out] [--seed 42]

Проверяется: dropped_rows == baseline_dropped + injected_bad, пара площадок не меняется,
и из топ-3 худших клеток совпадает хотя бы 2 (критерий автопроверки).
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coverage.config import load_settings  # noqa: E402
from coverage.pipeline import Inputs, run_pipeline  # noqa: E402


def _reformat_number(s: str) -> str:
    """-95.1 -> -95.10, -95 -> -95.0: то же число в другой записи."""
    s = s.strip()
    return s + "0" if "." in s else s + ".0"


def inject(
    lines: list[str], kept_rows: list[int], rng: random.Random, settings
) -> tuple[list[str], dict[str, int], int]:
    """Возвращает новые строки данных, счётчики внедрённого мусора и число граничных строк (они остаются)."""
    b = settings.bounds
    mid_lat = (b.lat_min + b.lat_max) / 2
    mid_lon = (b.lon_min + b.lon_max) / 2
    sample = rng.sample(kept_rows, 40)
    src = [lines[r - 1] for r in sample]  # r — номер строки в файле (заголовок = 1)
    bad: list[str] = []
    counts: dict[str, int] = {}

    def add(kind: str, rows: list[str]) -> None:
        bad.extend(rows)
        counts[kind] = counts.get(kind, 0) + len(rows)

    # 1) точные дубли и то же число в другой записи
    add("duplicates_exact", src[0:8])
    reformatted = []
    for line in src[8:14]:
        lat, lon, rsrp, ts = line.split(",")
        reformatted.append(",".join([_reformat_number(lat), lon, _reformat_number(rsrp), ts]))
    add("duplicates_reformatted", reformatted)

    # 2) RSRP вне диапазона (координаты и время валидны и уникальны)
    def fresh(i: int, rsrp: str, lat: float | None = None, lon: float | None = None, ts: str | None = None) -> str:
        la = mid_lat + rng.uniform(-0.005, 0.005) if lat is None else lat
        lo = mid_lon + rng.uniform(-0.005, 0.005) if lon is None else lon
        t = ts if ts is not None else f"2026-11-21T0{i % 9}:{rng.randint(10, 59)}:{rng.randint(10, 59)}+06:00"
        return f"{la:.6f},{lo:.6f},{rsrp},{t}"

    add("rsrp_out_of_range", [fresh(i, v) for i, v in enumerate(["-150", "-30", "0", "+10", "-140.1", "-43.9"])])

    # 3) координаты вне района: 0,0; перепутанные lat/lon; далеко за границей
    add(
        "out_of_bounds",
        [
            fresh(1, "-100", 0.0, 0.0),
            fresh(2, "-100", mid_lon, mid_lat),
            fresh(3, "-100", b.lat_max + 0.01, mid_lon),
            fresh(4, "-100", mid_lat, b.lon_min - 0.0001),
        ],
    )

    # 4) пустые значения, текст в числах, мусорное время, пустые и битые строки
    add(
        "unparseable",
        [
            fresh(5, ""),
            fresh(6, "abc"),
            fresh(7, "-95 dBm"),
            ",74.58,-100,2026-11-21T10:00:00+06:00",
            "NaN,74.58,-90,x",
        ],
    )
    add(
        "bad_timestamp",
        [fresh(8, "-100", ts="bad-time"), fresh(8, "-100", ts=""), fresh(8, "-100", ts="21.11.2026 10:00")],
    )
    add("malformed", ["", "   ", "42.85,74.58,-100", "42.85,74.58,-100,2026-11-21T10:00:00+06:00,extra"])

    # 5) граничные значения — должны ОСТАТЬСЯ; плюс лишние пробелы вокруг валидных значений
    keep = [fresh(9, "-140"), fresh(9, "-44"), "  " + fresh(9, " -99.5 ").replace(",", " , ")]

    data = lines[1:] + bad + keep
    rng.shuffle(data)
    return [lines[0], *data], counts, len(keep)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(ROOT / "data"))
    p.add_argument("--out", default=str(ROOT / "selfcheck_out"))
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    settings = load_settings()
    data_dir = Path(args.data_dir)
    base = run_pipeline(Inputs.from_dir(data_dir, settings.bounds.bounds_file), settings)
    lines = (data_dir / "measurements.csv").read_text(encoding="utf-8-sig").splitlines()

    rng = random.Random(args.seed)
    new_lines, counts, kept_extra = inject(lines, base.clean["row"].tolist(), rng, base.settings)
    injected = sum(counts.values())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "measurements.csv").write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    for name in ("towers.csv", "candidates.csv", settings.bounds.bounds_file):
        if (data_dir / name).exists():
            shutil.copy(data_dir / name, out / name)

    dirty = run_pipeline(Inputs.from_dir(out, settings.bounds.bounds_file), settings)
    exp_dropped = base.result["dropped_rows"] + injected
    got_dropped = dirty.result["dropped_rows"]
    base_worst = _worst_ids(base.result["worst_cells"])
    dirty_worst = _worst_ids(dirty.result["worst_cells"])
    overlap = len(base_worst & dirty_worst)

    checks = {
        f"dropped_rows {got_dropped} == baseline {base.result['dropped_rows']} + injected {injected}": got_dropped
        == exp_dropped,
        f"kept boundary rows: +{kept_extra} kept": dirty.cleaning.kept == base.cleaning.kept + kept_extra,
        f"best pair stable: {base.result['best_candidates']} -> {dirty.result['best_candidates']}": base.result[
            "best_candidates"
        ]
        == dirty.result["best_candidates"],
        f"worst-3 overlap {overlap}/3 (нужно >= 2)": overlap >= 2,
    }
    print(f"Injected garbage: {counts} (total {injected}); boundary rows kept: {kept_extra}")
    print(f"Dirty by_reason: {dirty.cleaning.by_reason}")
    ok = True
    for text, passed in checks.items():
        print(f"[{'OK' if passed else 'FAIL'}] {text}")
        ok &= passed
    print("SELF-CHECK PASSED" if ok else "SELF-CHECK FAILED")
    return 0 if ok else 1


def _worst_ids(worst: list) -> set[str]:
    """Идентификаторы худших клеток при любом worst.format (objects / id / latlon)."""
    return {w["cell_id"] if isinstance(w, dict) else str(w) for w in worst}


if __name__ == "__main__":
    sys.exit(main())
