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
from coverage.errors import DataValidationError, ScenarioError  # noqa: E402
from coverage.pipeline import Inputs, PipelineResult, run_pipeline  # noqa: E402
from coverage.scenario import DEFAULT_TOP_N, Constraints, Refusal, ScenarioResult, run_scenario  # noqa: E402

log = logging.getLogger("coverage")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Карта покрытия: очистка замеров, сетка 200 м, выбор двух площадок для БС.")
    p.add_argument("--data-dir", default=str(ROOT / "data"), help="каталог с measurements/towers/candidates.csv")
    p.add_argument("--measurements", help="путь к measurements.csv (перекрывает --data-dir)")
    p.add_argument("--towers", help="путь к towers.csv")
    p.add_argument("--candidates", help="путь к candidates.csv")
    p.add_argument("--bounds", help="путь к bounds.json (по умолчанию <data-dir>/bounds.json, если есть)")
    p.add_argument("--config", default=None, help="config.yaml (по умолчанию из корня репозитория)")
    p.add_argument("--out", default=str(ROOT), help="куда писать result.json, map.html, report.json")
    sc = p.add_argument_group("сценарии отказов (result.json от них не зависит; пишется отдельный scenario.json)")
    sc.add_argument(
        "--exclude", action="append", default=[], metavar="ID", help="площадка отказала (можно несколько раз)"
    )
    sc.add_argument("--exclude-reason", action="append", default=[], metavar="ID=ТЕКСТ", help="причина отказа")
    sc.add_argument(
        "--exclude-category",
        action="append",
        default=[],
        metavar="ID=КАТЕГОРИЯ",
        help="owner | permit | technical | other",
    )
    sc.add_argument("--require", action="append", default=[], metavar="ID", help="площадка зафиксирована (до двух)")
    sc.add_argument(
        "--constraints", metavar="FILE", help='JSON {"excluded": [{"id","reason","category"}], "required": [..]}'
    )
    sc.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="сколько альтернативных пар сохранить (1..50)")
    p.add_argument(
        "--pair",
        metavar="A,B",
        help="пара площадок пользователя: дополнительно пишет result_selected.json (формат result.json); "
        "result.json остаётся с оптимальной парой",
    )
    p.add_argument(
        "--include-field",
        metavar="DB",
        help="дополнительно посчитать карту с полевыми замерами из SQLite (map_with_field.html, field_report.json); result.json не меняется",
    )
    p.add_argument(
        "--check",
        nargs="?",
        const="",
        metavar="EXPECTED",
        help="сверить результат с эталоном (по умолчанию <data-dir>/expected.json): пара точно, dropped_rows ±5%%, "
        "минимум 2 из 3 худших клеток; печатает PASS или FAIL (код выхода 1 при FAIL)",
    )
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


def _kv(items: list[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ScenarioError(f"{flag}: ожидается формат ID=значение, получено {item!r}")
        out[key.strip()] = value.strip()
    return out


def build_constraints(args: argparse.Namespace) -> Constraints:
    """Ограничения из --constraints FILE и флагов (флаги дополняют файл и главнее него)."""
    base = Constraints.from_file(args.constraints) if args.constraints else Constraints()
    reasons = _kv(args.exclude_reason, "--exclude-reason")
    categories = _kv(args.exclude_category, "--exclude-category")
    ids = list(dict.fromkeys(args.exclude))
    stray = (set(reasons) | set(categories)) - set(ids) - base.excluded_ids
    if stray:
        raise ScenarioError(f"причина/категория указана для площадки без --exclude: {', '.join(sorted(stray))}")
    flags = Constraints(
        excluded=tuple(Refusal(i, reasons.get(i, ""), categories.get(i, "other")) for i in ids),
        required=tuple(args.require),
    )
    merged = base.merged(flags)
    # причина/категория из флагов применяются и к отказам из файла
    patched = tuple(
        Refusal(r.id, reasons.get(r.id, r.reason), categories.get(r.id, r.category)) for r in merged.excluded
    )
    return Constraints(excluded=patched, required=merged.required)


def _cell_ids(worst: list) -> list[str]:
    return [str(w["cell_id"]) if isinstance(w, dict) else str(w) for w in worst]


def check_against_expected(result: dict, expected: dict) -> list[str]:
    """Проверки сверки с эталоном; пустой список означает PASS."""
    problems = []
    if sorted(result["best_candidates"]) != sorted(expected["best_candidates"]):
        problems.append(f"пара {result['best_candidates']} != {expected['best_candidates']}")
    exp_dropped = expected["dropped_rows"]
    if abs(result["dropped_rows"] - exp_dropped) > 0.05 * exp_dropped:
        problems.append(f"dropped_rows {result['dropped_rows']} вне ±5% от {exp_dropped}")
    common = set(_cell_ids(result["worst_cells"])) & set(_cell_ids(expected["worst_cells"]))
    if len(common) < 2:
        problems.append(f"совпало худших клеток: {len(common)} из 3 (нужно не меньше 2)")
    return problems


def selected_summary(pr: PipelineResult, selected: dict, filename: str) -> str:
    """Строка итога для --pair (ASCII, как остальная сводка: консоль Windows может не знать «Δ»)."""
    pair, covered = selected["best_candidates"], selected["covered_weak_cells"]
    tz = pr.result
    return (
        f"Selected pair: {' + '.join(pair)} -> {covered} (optimum {' + '.join(tz['best_candidates'])} -> "
        f"{tz['covered_weak_cells']}, delta {covered - tz['covered_weak_cells']:+d}); written {filename}"
    )


def scenario_summary(s: ScenarioResult) -> str:
    c = s.constraints
    parts = []
    if c.excluded:
        parts.append("excluded " + ", ".join(f"{r.id} ({r.reason})" if r.reason else r.id for r in c.excluded))
    if c.required:
        parts.append("required " + ", ".join(c.required))
    lines = ["Scenario: " + "; ".join(parts)]
    if not s.feasible or s.best is None:
        lines.append(f"  Infeasible: {s.message}")
        return "\n".join(lines)
    b, o = s.best, s.optimum
    lines.append(
        f"  Best: {' + '.join(b.pair)} -> {b.covered} (optimum {' + '.join(o.pair)} -> {o.covered}, "
        f"delta {b.delta_vs_optimum:+d})"
    )
    other_ties = [" + ".join(t.pair) for t in s.ties if t.pair != b.pair]
    nxt = next((a for a in s.alternatives if a.covered < b.covered), None)
    ties = ", ".join(other_ties) if other_ties else "none"
    nxt_txt = f"{' + '.join(nxt.pair)} -> {nxt.covered}" if nxt else "-"
    lines.append(f"  Ties: {ties} | Next: {nxt_txt}")
    return "\n".join(lines)


def include_field(pr: PipelineResult, db: Path, out: Path, no_map: bool) -> tuple[list[str], list[str]]:
    """Карта «с полевыми»: отдельные файлы, result.json не затрагивается."""
    from coverage.field_ingest import build_field_map_data, field_diff, run_pipeline_with_field
    from coverage.field_store import FieldStore
    from coverage.render import build_map_data, render_page
    from coverage.result import write_json

    if not db.exists():
        raise DataValidationError(f"--include-field: файл базы не найден: {db}")
    store = FieldStore(db)
    try:
        df, by_source, version = store.as_dataframe(), store.count()["by_source"], store.version()
    finally:
        store.close()
    withf = run_pipeline_with_field(pr, df)
    diff = field_diff(pr, withf)
    data = build_field_map_data(build_map_data(pr), pr, withf, df, diff, version, by_source)
    report = {
        "field_count": len(df),
        "by_source": by_source,
        "official_result": pr.result,
        "result_with_field": withf.result,
        "field_diff": diff,
    }
    written = [str(write_json(out / "field_report.json", report))]
    if not no_map:
        html = out / "map_with_field.html"
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(render_page(data, server_mode=False), encoding="utf-8")
        written.append(str(html))
    s = diff["summary"]
    lines = [
        f"Field: {len(df)} measurements included ({', '.join(f'{k} {v}' for k, v in by_source.items() if v) or 'none'}); "
        f"cells changed {s['cells_changed']} (became counted {s['became_counted']}, colour changed {s['class_changed']})",
    ]
    bp = diff["best_pair"]
    if bp["changed"]:
        lines.append(
            f"  best pair with field: {' + '.join(bp['after'])} (official: {' + '.join(bp['before'])}); result.json unchanged"
        )
    for c in diff["cells"][:10]:
        b, a = c["before"], c["after"]
        lines.append(
            f"  {c['cell_id']}: n {b['n'] if b else 0} -> {a['n'] if a else 0}"
            + (f", {b['cls']} -> {a['cls']}" if a and b and a["cls"] != b["cls"] else "")
        )
    return lines, written


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
        constraints = build_constraints(args)
        # сценарий считаем до записи файлов: ошибка в ограничениях не оставляет частичный вывод
        scenario = None if constraints.is_empty else run_scenario(pr, constraints, args.top_n)
        # пара пользователя: ошибка не мешает записать официальный result.json, но даёт код выхода 2
        selected, pair_error = None, None
        if args.pair:
            try:
                from coverage.result import build_selected_result, parse_pair

                selected = build_selected_result(pr, parse_pair(args.pair))
            except ScenarioError as exc:
                pair_error = exc

        from coverage.result import write_json

        out = Path(args.out)
        o = pr.settings.output
        written = [str(write_json(out / o.result_file, pr.result)), str(write_json(out / o.report_file, pr.report))]
        if not args.no_map:
            from coverage.render import write_map_html

            written.insert(1, str(write_map_html(pr, out / o.map_file)))
        if scenario is not None:
            written.append(str(write_json(out / o.scenario_file, scenario.to_dict())))
        if selected is not None:
            written.append(str(write_json(out / o.selected_file, selected)))
        field_lines: list[str] = []
        if args.include_field:
            field_lines, field_written = include_field(pr, Path(args.include_field), out, args.no_map)
            written += field_written
        print(summary(pr, [Path(w).name if Path(w).parent == ROOT else w for w in written]))
        if scenario is not None:
            print(scenario_summary(scenario))
        if selected is not None:
            print(selected_summary(pr, selected, o.selected_file))
        if field_lines:
            print("\n".join(field_lines))
        if args.check is not None:
            import json as _json

            exp_path = Path(args.check) if args.check else Path(args.data_dir) / "expected.json"
            if not exp_path.exists():
                raise DataValidationError(f"--check: файл эталона не найден: {exp_path}")
            problems = check_against_expected(pr.result, _json.loads(exp_path.read_text(encoding="utf-8")))
            print("Check vs expected: " + ("PASS" if not problems else "FAIL: " + "; ".join(problems)))
            if problems:
                return 1
        if pair_error is not None:
            print(f"Ошибка входных данных: --pair: {pair_error}", file=sys.stderr)
            return 2
    except (DataValidationError, ConfigError, ScenarioError) as exc:
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
