#!/usr/bin/env python3
"""Единая точка входа: python run.py [--data-dir DIR] [--out DIR] [--serve].

Коды выхода: 0 — успех, 2 — ошибка входных данных/конфига, 1 — прочие ошибки.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from coverage.config import ConfigError, load_settings  # noqa: E402
from coverage.errors import DataValidationError  # noqa: E402
from coverage.pipeline import Inputs, PipelineResult, run_pipeline  # noqa: E402

log = logging.getLogger("coverage")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ТЗ 7 «Карта покрытия»: очистка замеров, сетка 200 м, выбор двух площадок для БС."
    )
    p.add_argument("--data-dir", default=str(ROOT / "data"), help="каталог с measurements/towers/candidates.csv")
    p.add_argument("--measurements", help="путь к measurements.csv (перекрывает --data-dir)")
    p.add_argument("--towers", help="путь к towers.csv")
    p.add_argument("--candidates", help="путь к candidates.csv")
    p.add_argument("--bounds", help="путь к bounds.json (по умолчанию <data-dir>/bounds.json, если есть)")
    p.add_argument("--config", default=None, help="config.yaml (по умолчанию из корня репозитория)")
    p.add_argument("--out", default=str(ROOT), help="куда писать result.json, map.html, report.json")
    p.add_argument("--no-map", action="store_true", help="не генерировать map.html")
    p.add_argument("--serve", action="store_true", help="после расчёта запустить веб-интерфейс FastAPI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return p.parse_args(argv)


def build_inputs(args: argparse.Namespace, bounds_file: str) -> Inputs:
    base = Inputs.from_dir(args.data_dir, bounds_file)
    return Inputs(
        measurements=Path(args.measurements) if args.measurements else base.measurements,
        towers=Path(args.towers) if args.towers else base.towers,
        candidates=Path(args.candidates) if args.candidates else base.candidates,
        bounds=Path(args.bounds) if args.bounds else base.bounds,
    )


def summary(pr: PipelineResult, written: list[str]) -> str:
    c = pr.cleaning
    g = pr.report["grid"]
    reasons = ", ".join(f"{k}: {v}" for k, v in c.by_reason.items())
    worst = ", ".join(f"{r.cell_id} ({r.median_rsrp:.2f})" for r in pr.worst.itertuples(index=False))
    bf, gr = pr.brute, pr.greedy
    gap = bf.covered - gr.covered
    cmp = f"brute force better by {gap}" if gap > 0 else "same as brute force"
    lines = [
        f"Rows total: {c.total} | kept: {c.kept} | dropped: {c.dropped}",
        f"  {reasons}",
        f"Cells: {g['cells_total']} (n>={pr.settings.grid.min_samples}: {g['cells_counted']}) -> "
        f"green {g['green']}, yellow {g['yellow']}, red {g['red']}",
        f"Worst-{len(pr.worst)}: {worst}",
        f"Best pair (brute force): {' + '.join(bf.pair) or '-'} -> {bf.covered} red cells covered",
        f"Greedy: {' + '.join(gr.pair) or '-'} -> {gr.covered} ({cmp})",
    ]
    if len(bf.ties) > 1:
        lines.append(
            f"  note: {len(bf.ties)} pairs tie at {bf.covered}; chosen by rule '{pr.settings.selection.tie_break}'"
        )
    dn = pr.report["daynight"]
    if dn["available"]:
        lines.append(
            f"Day/night: day {dn['rows_day']} rows, night {dn['rows_night']} rows; "
            f"median delta (night - day) = {dn['median_delta_db']} dB"
        )
    lines.append(f"Written: {', '.join(written)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings(args.config)
        inputs = build_inputs(args, settings.bounds.bounds_file)
        pr = run_pipeline(inputs, settings)

        from coverage.result import write_json

        out = Path(args.out)
        o = pr.settings.output
        written = [str(write_json(out / o.result_file, pr.result)), str(write_json(out / o.report_file, pr.report))]
        if not args.no_map:
            from coverage.render import write_map_html

            written.insert(1, str(write_map_html(pr, out / o.map_file)))
        print(summary(pr, [Path(w).name if Path(w).parent == ROOT else w for w in written]))
    except (DataValidationError, ConfigError) as exc:
        print(f"Ошибка входных данных: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        log.exception("Непредвиденная ошибка")
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if args.serve:
        try:
            import uvicorn
        except ImportError:
            print("Для --serve установите зависимости: pip install -r requirements.txt", file=sys.stderr)
            return 1
        import os

        os.environ["COVERAGE_DATA_DIR"] = str(Path(args.data_dir).resolve())
        if args.config:
            os.environ["COVERAGE_CONFIG"] = str(Path(args.config).resolve())
        print(f"Веб-интерфейс: http://{args.host}:{args.port}  (OpenAPI: /docs)")
        uvicorn.run("api.main:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
