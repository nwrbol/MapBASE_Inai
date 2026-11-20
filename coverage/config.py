"""Загрузка config.yaml в типизированный Settings и применение bounds.json."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, DataValidationError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


@dataclass(frozen=True)
class BoundsSettings:
    bounds_file: str = "bounds.json"
    lat_min: float = 42.84
    lat_max: float = 42.87597286423675
    lon_min: float = 74.56
    lon_max: float = 74.60905912302184
    inclusive: bool = True
    polygon: list[list[float]] | None = None


@dataclass(frozen=True)
class GridSettings:
    origin_lat: float = 42.84
    origin_lon: float = 74.56
    cell_size_m: float = 200.0
    earth_radius_m: float = 6_371_000.0
    min_samples: int = 5
    cell_id_format: str = "X{ix:03d}_Y{iy:03d}"


@dataclass(frozen=True)
class CleaningSettings:
    rsrp_min: float = -140.0
    rsrp_max: float = -44.0
    require_valid_timestamp: bool = True
    accept_unix_epoch: bool = False
    dedup_subset: list[str] = field(default_factory=lambda: ["lat", "lon", "rsrp_dbm", "timestamp"])
    dedup_timestamp_mode: str = "instant"


@dataclass(frozen=True)
class ClassSettings:
    green_min: float = -90.0
    red_below: float = -105.0


@dataclass(frozen=True)
class WorstSettings:
    count: int = 3
    format: str = "objects"


@dataclass(frozen=True)
class SelectionSettings:
    tower_exclusion_m: float = 500.0
    coverage_radius_m: float = 1000.0
    coverage_reference: str = "center"
    pair_size: int = 2
    tie_break: str = "lexicographic"


@dataclass(frozen=True)
class DayNightSettings:
    timezone: str = "Asia/Bishkek"
    day_start_hour: int = 8
    day_end_hour: int = 20
    # Пороги шкалы Δ = медиана ночью − медиана днём, дБ: четыре цвета на три порога (карта и легенда берут их отсюда)
    delta_bins_db: tuple = (-3, 0, 3)


@dataclass(frozen=True)
class HeatmapSettings:
    rsrp_strong: float = -44.0
    span_db: float = 96.0


@dataclass(frozen=True)
class OutputSettings:
    result_file: str = "result.json"
    report_file: str = "report.json"
    map_file: str = "map.html"
    scenario_file: str = "scenario.json"
    selected_file: str = "result_selected.json"
    coord_decimals: int = 6
    rsrp_decimals: int = 4


@dataclass(frozen=True)
class FieldSettings:
    """Полевые замеры (форма /field и импорт CSV из инженерных приложений)."""

    accuracy_warn_m: float = 50.0  # хуже этого GPS-точность даёт предупреждение (замер не отклоняется)
    max_batch: int = 500  # макс. замеров в одном запросе
    device_label_max: int = 50
    import_max_bytes: int = 10 * 1024 * 1024
    import_max_rows: int = 50_000
    sample_rows: int = 20  # строк в предпросмотре импорта
    sample_rejected: int = 10  # отклонённых строк в предпросмотре
    units_median_min: float = -150.0  # медиана столбца RSRP вне [min, 0] -> «похоже, не RSRP в дБм»
    units_median_max: float = -30.0


@dataclass(frozen=True)
class Settings:
    bounds: BoundsSettings = field(default_factory=BoundsSettings)
    grid: GridSettings = field(default_factory=GridSettings)
    cleaning: CleaningSettings = field(default_factory=CleaningSettings)
    classes: ClassSettings = field(default_factory=ClassSettings)
    worst: WorstSettings = field(default_factory=WorstSettings)
    selection: SelectionSettings = field(default_factory=SelectionSettings)
    daynight: DayNightSettings = field(default_factory=DayNightSettings)
    heatmap: HeatmapSettings = field(default_factory=HeatmapSettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    field_data: FieldSettings = field(default_factory=FieldSettings)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


_CHOICES = {
    ("cleaning", "dedup_timestamp_mode"): {"instant", "string"},
    ("worst", "format"): {"objects", "id", "latlon"},
    ("selection", "tie_break"): {"lexicographic", "depth_then_lexicographic"},
    ("selection", "coverage_reference"): {"center"},
}
_DEDUP_COLUMNS = {"lat", "lon", "rsrp_dbm", "timestamp"}


def _coerce(value: Any, target: Any, where: str) -> Any:
    """Приводит значение из YAML к типу поля dataclass (int/float/bool/str/list)."""
    if value is None:
        return None
    if isinstance(target, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "yes", "no"}:
            return value.lower() in {"true", "1", "yes"}
        raise ConfigError(f"{where}: ожидается true/false, получено {value!r}")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: ожидается число, получено {value!r}") from exc
        if isinstance(target, int):
            if not num.is_integer():
                raise ConfigError(f"{where}: ожидается целое число, получено {value!r}")
            return int(num)
        return num
    if isinstance(target, str):
        return str(value)
    return value


def _build(cls: type, data: dict[str, Any] | None, section: str) -> Any:
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"секция '{section}' должна быть словарём")
    defaults = cls()
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"неизвестные параметры в секции '{section}': {sorted(unknown)}")
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], getattr(defaults, f.name), f"{section}.{f.name}")
    return replace(defaults, **kwargs)


def _validate(s: Settings) -> None:
    for (section, name), allowed in _CHOICES.items():
        value = getattr(getattr(s, section), name)
        if value not in allowed:
            raise ConfigError(f"{section}.{name}={value!r}; допустимо: {sorted(allowed)}")
    if s.grid.cell_size_m <= 0:
        raise ConfigError("grid.cell_size_m должен быть > 0")
    if s.grid.min_samples < 1:
        raise ConfigError("grid.min_samples должен быть >= 1")
    if s.cleaning.rsrp_min > s.cleaning.rsrp_max:
        raise ConfigError("cleaning.rsrp_min больше rsrp_max")
    if s.classes.red_below > s.classes.green_min:
        raise ConfigError("classes.red_below больше classes.green_min")
    if s.bounds.lat_min > s.bounds.lat_max or s.bounds.lon_min > s.bounds.lon_max:
        raise ConfigError("bounds: min больше max")
    if s.selection.pair_size != 2:
        raise ConfigError("selection.pair_size: поддерживается только пара площадок (2)")
    bad = set(s.cleaning.dedup_subset) - _DEDUP_COLUMNS
    if bad or not s.cleaning.dedup_subset:
        raise ConfigError(f"cleaning.dedup_subset: допустимы столбцы {sorted(_DEDUP_COLUMNS)}")
    if not (0 <= s.daynight.day_start_hour <= 24 and 0 <= s.daynight.day_end_hour <= 24):
        raise ConfigError("daynight: часы должны быть в диапазоне 0..24")
    bins = s.daynight.delta_bins_db
    if (
        not isinstance(bins, (list, tuple))
        or len(bins) != 3
        or any(isinstance(b, bool) or not isinstance(b, (int, float)) for b in bins)
        or not bins[0] < bins[1] < bins[2]
    ):
        raise ConfigError("daynight.delta_bins_db: нужны три возрастающих числа, например [-3, 0, 3]")
    if s.bounds.polygon is not None:
        poly = s.bounds.polygon
        if not isinstance(poly, list) or len(poly) < 3 or any(len(p) != 2 for p in poly):
            raise ConfigError("bounds.polygon: нужен список из >= 3 пар [lat, lon]")
    try:
        s.grid.cell_id_format.format(ix=1, iy=2)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"grid.cell_id_format некорректен: {exc}") from exc


def settings_from_dict(raw: dict[str, Any] | None) -> Settings:
    """Строит Settings из словаря (структура как в config.yaml)."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("config: верхний уровень должен быть словарём")
    sections = {f.name: f.type for f in fields(Settings)}
    unknown = set(raw) - set(sections)
    if unknown:
        raise ConfigError(f"неизвестные секции конфига: {sorted(unknown)}")
    defaults = Settings()
    built = {name: _build(type(getattr(defaults, name)), raw.get(name), name) for name in sections}
    settings = Settings(**built)
    _validate(settings)
    return settings


def load_settings(path: str | Path | None = None) -> Settings:
    """Загружает config.yaml (по умолчанию из корня репозитория)."""
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if path is None:
            return settings_from_dict({})
        raise ConfigError(f"файл конфига не найден: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"ошибка разбора {cfg_path}: {exc}") from exc
    return settings_from_dict(raw)


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """Переопределения вида {"grid.cell_size_m": 250}; None-значения пропускаются."""
    result = settings
    for key, value in overrides.items():
        if value is None:
            continue
        if "." not in key:
            raise ConfigError(f"переопределение '{key}': ожидается формат секция.параметр")
        section, name = key.split(".", 1)
        sub = getattr(result, section, None)
        if sub is None or not is_dataclass(sub) or not hasattr(sub, name):
            raise ConfigError(f"неизвестный параметр '{key}'")
        coerced = _coerce(value, getattr(sub, name), key)
        result = replace(result, **{section: replace(sub, **{name: coerced})})
    _validate(result)
    return result


def parse_bounds_json(content: str | bytes, source: str = "bounds.json") -> dict[str, Any]:
    """Читает bounds.json жюри: lat/lon min/max, начало сетки, размер клетки, радиус, пояс."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataValidationError(f"{source}: некорректный JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise DataValidationError(f"{source}: ожидается JSON-объект")
    required = ("lat_min", "lat_max", "lon_min", "lon_max")
    missing = [k for k in required if k not in data]
    if missing:
        raise DataValidationError(f"{source}: нет полей {missing}")
    return data


def apply_bounds(settings: Settings, bounds: dict[str, Any]) -> Settings:
    """Переносит значения из bounds.json в Settings (bounds.json главнее config.yaml)."""
    mapping = {
        "lat_min": "bounds.lat_min",
        "lat_max": "bounds.lat_max",
        "lon_min": "bounds.lon_min",
        "lon_max": "bounds.lon_max",
        "polygon": "bounds.polygon",
        "cell_size_m": "grid.cell_size_m",
        "earth_radius_m": "grid.earth_radius_m",
        "timezone": "daynight.timezone",
    }
    overrides: dict[str, Any] = {dst: bounds.get(src) for src, dst in mapping.items()}
    overrides["grid.origin_lat"] = bounds.get("grid_origin_lat", bounds["lat_min"])
    overrides["grid.origin_lon"] = bounds.get("grid_origin_lon", bounds["lon_min"])
    try:
        return apply_overrides(settings, overrides)
    except ConfigError as exc:
        raise DataValidationError(f"bounds.json: {exc}") from exc
