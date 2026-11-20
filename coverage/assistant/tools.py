"""Единый реестр функций: схема и проверка аргументов строятся по одной сигнатуре."""

import inspect
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from coverage.geo import haversine_m
from coverage.scenario import Constraints, Refusal, evaluate_manual, impact_of_refusals
from coverage.scenario import run_scenario as compute_scenario

from .prompt import visible

Layer = Literal[
    "grid",
    "worst",
    "towers",
    "cands",
    "chosen",
    "ghost",
    "lost",
    "gained",
    "covered",
    "heat",
    "day",
    "night",
    "delta",
    "worseNight",
    "field",
]
Tab = Literal["overview", "coverage", "planning", "data", "assistant"]
Cell = Annotated[str, Field(pattern=r"^X\d{3}_Y\d{3}$")]
Id = Annotated[str, Field(min_length=1, max_length=32)]
Terms = Literal[
    "rsrp", "median", "haversine", "grid", "greedy_vs_bruteforce", "exclusion_zone", "coverage_radius", "dropped_rows"
]
REGISTRY = {}
SCHEMAS = []


def tool(fn):
    params = list(inspect.signature(fn).parameters.values())[1:]
    model = create_model(
        fn.__name__,
        __config__=ConfigDict(extra="forbid", strict=True),
        **{p.name: (p.annotation, ... if p.default is inspect.Parameter.empty else p.default) for p in params},
    )
    REGISTRY[fn.__name__] = (fn, model)
    SCHEMAS.append(
        {
            "type": "function",
            "function": {"name": fn.__name__, "description": fn.__doc__, "parameters": model.model_json_schema()},
        }
    )
    return fn


class Context:
    def __init__(self, pr, data, state, field_stats=None, dataset=None):
        self.state, self.dataset = state, dataset
        self.use_data(pr, data)
        self.field_stats = field_stats or {}
        self.actions = []
        raw = state.get("constraints", {})
        self.constraints = Constraints(
            tuple(Refusal(**r) for r in raw.get("excluded", [])), tuple(raw.get("required", []))
        )

    def use_data(self, pr, data):
        self.pr, self.data = pr, data
        self.cells = {c["id"]: c for c in data["cells"]}
        self.sites = {c["id"]: c for c in data["candidates"]}

    def plan(self, pair, covered, source):
        self.state["plan"] = {"pair": pair, "covered": covered, "source": source}

    def action(self, name, args, label, effect=None):
        self.actions.append({"type": name, "args": args, "label": label})
        return effect or {"accepted": True}

    def execute(self, name, args):
        fn, model = REGISTRY[name]
        return fn(self, **model.model_validate(args).model_dump(exclude_none=True))


@tool
def get_summary(ctx):
    """Итог, очистка, сравнение и текущий план."""
    d = ctx.data
    return {
        **{k: d[k] for k in ("result", "cleaning", "stats", "worst")},
        "compare": {name: d["compare"][name] for name in ("brute_force", "greedy")},
        "field_result": d.get("field_result"),
        "plan": ctx.state.get("plan"),
    }


@tool
def get_cell(ctx, cell_id: Cell):
    """Статистика клетки, ближайшая станция и покрытие."""
    c = ctx.cells[cell_id]
    distances = [(float(haversine_m(*c["c"], t["lat"], t["lon"])), t["id"]) for t in ctx.data["towers"]]
    pair = (ctx.state.get("plan") or {}).get("pair") or []
    return {
        **c,
        "nearest_tower_m_id": min(distances, default=None),
        "covered_by_plan": any(cell_id in ctx.sites[i]["covers"] for i in pair if i in ctx.sites),
        "n_field": c.get("n_field", 0),
        "daynight": {
            k: next((v for v in ctx.data["daynight"][k] if v["id"] == cell_id), None)
            for k in ("day_cells", "night_cells", "delta")
        },
    }


@tool
def find_cells(
    ctx,
    color: Literal["red", "yellow", "green"] | None = None,
    in_view: bool = False,
    sort: Literal["worst", "best"] = "worst",
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
):
    """Поиск учтённых клеток по цвету и медиане."""
    bounds = ctx.state.get("view", {}).get("bounds") if in_view else None
    cells = [
        c for c in ctx.cells.values() if c["counted"] and (not color or c["cls"] == color) and visible(c["c"], bounds)
    ]
    return sorted(cells, key=lambda c: c["m"], reverse=sort == "best")[:limit]


@tool
def get_candidates(ctx, only_admissible: bool = False):
    """Площадки, допустимость и потери при отказе."""
    impacts = impact_of_refusals({c.id: c.covers for c in ctx.pr.candidates if c.admissible}, ctx.pr.brute.covered)
    return [
        {**{k: v for k, v in c.items() if k != "covers"}, "impact": impacts.get(c["id"])}
        for c in ctx.sites.values()
        if not only_admissible or c["admissible"]
    ]


@tool
def evaluate_pair(ctx, a: Id, b: Id):
    """Оценка покрытия пары, пересечения и ограничений."""
    result = evaluate_manual(ctx.pr, a, b, ctx.constraints)
    result.pop("covered_ids")
    for key in ("lost_vs_optimum", "gained_vs_optimum"):
        result[key] = len(result[key])
    return result


def scenario_summary(scenario):
    return {
        "best": scenario.best.to_dict(with_ids=False) if scenario.best else None,
        "optimum": scenario.optimum.to_dict(with_ids=False),
        "feasible": scenario.feasible,
        "message": scenario.message,
        "ties": [option.pair for option in scenario.ties],
        "alternatives": [option.to_dict(with_ids=False) for option in scenario.alternatives],
    }


@tool
def run_scenario(
    ctx,
    excluded: list[Id] | None = None,
    required: list[Id] | None = None,
    top_n: Annotated[int, Field(ge=1, le=10)] = 3,
):
    """Лучшие пары с заданными отказами и фиксациями."""
    cons = Constraints(
        ctx.constraints.excluded if excluded is None else tuple(Refusal(i) for i in excluded),
        ctx.constraints.required if required is None else tuple(required),
    )
    return scenario_summary(compute_scenario(ctx.pr, cons, top_n))


@tool
def get_daynight(ctx):
    """Дневное и ночное покрытие, изменения и пропуски."""
    d = ctx.data["daynight"]
    return {**ctx.pr.report["daynight"], "not_compared": len(d.get("delta_insufficient", []))}


@tool
def get_field_stats(ctx):
    """Сводка полевых замеров без персональных данных."""
    return {**ctx.field_stats, "changes": ctx.data.get("field_diff", {})}


@tool
def explain_term(ctx, term: Terms):
    """Определение термина с параметрами активной карты."""
    m = ctx.data["meta"]
    return {
        "definition": {
            "rsrp": "Мощность опорного сигнала в дБм; менее отрицательное значение означает более сильный сигнал.",
            "median": "Середина упорядоченных замеров, устойчивая к крайним значениям.",
            "haversine": "Расстояние по дуге сферы между координатами.",
            "grid": f"Сторона клетки {m['cell_size_m']} м, минимум {m['min_samples']} замеров.",
            "greedy_vs_bruteforce": "Жадный выбор максимизирует прирост на каждом шаге, перебор сравнивает все допустимые пары.",
            "exclusion_zone": f"Минимальное расстояние до БС {m['tower_exclusion_m']} м.",
            "coverage_radius": f"Радиус до центра слабой клетки {m['coverage_radius_m']} м.",
            "dropped_rows": "Число отброшенных строк при очистке.",
        }[term]
    }


@tool
def set_candidate_status(
    ctx,
    id: Id,
    status: Literal["available", "required", "excluded"],
    reason: Annotated[str, Field(max_length=200)] = "",
):
    """Изменить статус площадки и пересчитать предложение."""
    if not ctx.sites[id]["admissible"]:
        raise ValueError("Площадка недопустима")
    ex = [r for r in ctx.constraints.excluded if r.id != id]
    req = [i for i in ctx.constraints.required if i != id]
    if status == "required":
        req.append(id)
    if status == "excluded":
        ex.append(Refusal(id, reason))
    cons = Constraints(tuple(ex), tuple(req))
    result = scenario_summary(compute_scenario(ctx.pr, cons, 3))
    ctx.constraints = cons
    best = result["best"] or {}
    selected = ctx.state.get("selected_pair")
    if selected and len(selected) == 2:
        assessment = evaluate_pair(ctx, *selected)
        if not assessment["violates_constraints"]:
            best = assessment
        else:
            ctx.state["selected_pair"] = None
    ctx.plan(best.get("pair"), best.get("covered", 0), "scenario")
    label = {"excluded": "Отказ", "required": "Фиксация", "available": "Доступна"}[status]
    return ctx.action("set_candidate_status", dict(id=id, status=status, reason=reason), f"{label}: {id}", result)


@tool
def select_pair(ctx, a: Id, b: Id):
    """Выбрать пару в сценарии."""
    effect = evaluate_pair(ctx, a, b)
    if effect["violates_constraints"]:
        raise ValueError("Пара нарушает ограничения")
    ctx.state["selected_pair"] = effect["pair"]
    ctx.plan(effect["pair"], effect["covered"], "selected")
    return ctx.action("select_pair", dict(a=a, b=b), f"Пара: {a} + {b}", effect)


@tool
def reset_plan(ctx):
    """Сбросить ограничения и вернуть оптимальную пару."""
    ctx.constraints = Constraints()
    ctx.state["selected_pair"] = None
    ctx.plan(ctx.pr.result["best_candidates"], ctx.pr.result["covered_weak_cells"], "optimum")
    return ctx.action("reset_plan", {}, "Вернуть оптимальную пару", ctx.pr.result)


@tool
def set_layers(
    ctx,
    on: list[Layer] | None = None,
    off: list[Layer] | None = None,
    only: list[Layer] | None = None,
    reset: Literal["none", "default"] | None = None,
):
    """Включить или выключить слои карты."""
    layers = set(ctx.state.get("active_layers", []))
    if reset:
        layers = (
            set()
            if reset == "none"
            else set(["grid", "worst", "towers", "cands", "chosen", "ghost", "lost", "covered"])
        )
        if reset == "default" and ctx.state.get("field_included"):
            layers.add("field")
    if only is not None:
        layers = set(only)
    layers.update(on or [])
    layers.difference_update(off or [])
    ctx.state["active_layers"] = sorted(layers)
    return ctx.action(
        "set_layers",
        {"only": sorted(layers)},
        "Слои: " + ", ".join(LAYER_NAMES[k] for k in (on or only or off or [reset or "none"])),
        {"active_layers": sorted(layers)},
    )


LAYER_NAMES = dict(
    grid="сетка",
    worst="худшие клетки",
    towers="базовые станции",
    cands="площадки",
    chosen="выбранная пара",
    ghost="контур оптимума",
    lost="потерянные клетки",
    gained="новые клетки",
    covered="покрытые клетки",
    heat="тепловая карта",
    day="день",
    night="ночь",
    delta="разница день/ночь",
    worseNight="слабые ночью",
    field="полевые замеры",
    none="выключены",
    default="по умолчанию",
)


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]


@tool
def fly_to(ctx, target: Id | Location, zoom: Annotated[int, Field(ge=12, le=18)] = 16):
    """Показать клетку, площадку, БС или координаты."""
    if isinstance(target, str):
        tower = next((t for t in ctx.data["towers"] if t["id"] == target), None)
        obj = ctx.cells.get(target) or ctx.sites.get(target) or tower
        if not obj:
            raise ValueError("Объект не найден")
    else:
        obj = target
    return ctx.action(
        "fly_to", dict(target=target, zoom=zoom), "Показать " + (target if isinstance(target, str) else "точку"), obj
    )


@tool
def fit_view(ctx, what: Literal["district", "plan", "worst", "red_cells"]):
    """Уместить район, пару или клетки на карте."""
    return ctx.action("fit_view", dict(what=what), "Изменить вид карты")


@tool
def open_tab(ctx, tab: Tab):
    """Открыть раздел панели."""
    return ctx.action(
        "open_tab",
        dict(tab=tab),
        "Открыть "
        + {"overview": "обзор", "planning": "площадки", "coverage": "покрытие", "data": "данные", "assistant": "чат"}[
            tab
        ],
    )


@tool
def toggle_field(ctx, on: bool):
    """Включить учёт полевых замеров."""
    if ctx.state.get("uploaded"):
        raise ValueError("Полевые замеры доступны для исходного набора")
    if ctx.dataset and on != ctx.state.get("field_included", False):
        ctx.use_data(*ctx.dataset(on))
        ctx.state["field_included"] = on
        result = compute_scenario(ctx.pr, ctx.constraints, 1)
        best = result.best
        ctx.plan(best.pair if best else None, best.covered if best else 0, "field" if on else "scenario")
    return ctx.action("toggle_field", dict(on=on), "Полевые замеры: " + ("вкл" if on else "выкл"))


@tool
def highlight_cells(
    ctx,
    cell_ids: Annotated[list[Cell], Field(min_length=1, max_length=30)],
    seconds: Annotated[int, Field(ge=2, le=10)] = 5,
):
    """Временно выделить клетки."""
    if any(i not in ctx.cells for i in cell_ids):
        raise ValueError("Клетка не найдена")
    return ctx.action("highlight_cells", dict(cell_ids=cell_ids, seconds=seconds), "Выделить клетки")


@tool
def set_panel_width(ctx, px: Annotated[int, Field(ge=320, le=960)]):
    """Задать ширину панели на компьютере."""
    return ctx.action("set_panel_width", dict(px=px), f"Ширина панели {px} px")
