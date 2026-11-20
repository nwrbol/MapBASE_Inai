"""Импорт CSV из инженерных приложений (G-NetTrack, Network Cell Info и подобных).

Принципы: не привязываться к одному формату (автоопределение + ручное сопоставление столбцов), импорт в два шага
(предпросмотр без записи, затем сохранение), ничего не «чинить» автоматически. Каждая строка проходит ту же проверку,
что и остальные полевые замеры (`field_ingest.validate_field_rows`).
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any

import pandas as pd

from .config import Settings
from .errors import DataValidationError
from .field_ingest import NormRow, Rejection, rejected_by_reason, validate_field_rows
from .timeutil import get_zone, parse_one

log = logging.getLogger(__name__)

ENCODINGS = ("utf-8-sig", "cp1251", "latin-1")
DELIMITERS = (",", ";", "\t", "|")
SNIFF_LINES = 50
COMMENT_PREFIXES = ("#", "//")
MAX_POINTS = 2000

# Синонимы столбцов (регистр, пробелы и «_» не учитываются). «Слабые» подставляются с пометкой «проверьте».
SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "lat": {"strong": ("lat", "latitude", "широта", "gpslat"), "weak": ()},
    "lon": {"strong": ("lon", "lng", "long", "longitude", "долгота", "gpslon"), "weak": ()},
    "rsrp": {"strong": ("rsrp", "rsrpdbm", "ltersrp", "rsrp(dbm)"), "weak": ("level", "signal", "dbm")},
    "timestamp": {"strong": ("timestamp", "datetime", "time", "utc"), "weak": ()},
    "date": {"strong": ("date",), "weak": ()},
    "tech": {"strong": ("networktech", "networktype", "tech", "rat", "networkmode"), "weak": ()},
}
TECH_RE = re.compile(r"(?<![a-z0-9])(lte|4g|nr|5g)(?![a-z0-9])", re.IGNORECASE)

# формат: (метка для UI, шаблон strptime | None для ISO | "unix")
TIME_FORMATS: tuple[tuple[str, str | None], ...] = (
    ("ISO 8601", None),
    ("YYYY.MM.DD_HH.mm.ss", "%Y.%m.%d_%H.%M.%S"),
    ("DD.MM.YYYY HH:mm:ss", "%d.%m.%Y %H:%M:%S"),
    ("DD.MM.YYYY HH:mm", "%d.%m.%Y %H:%M"),
    ("YYYY.MM.DD HH:mm:ss", "%Y.%m.%d %H:%M:%S"),
    ("unix epoch", "unix"),
)
TIME_BY_LABEL = dict(TIME_FORMATS)
SAMPLE_VALUES = 50
DETECT_MIN_SHARE = 0.8
DECIMAL_COMMA_RE = re.compile(r"^\s*[+-]?\d+,\d+\s*$")


class ImportTooLarge(DataValidationError):
    """Файл больше лимита по размеру или числу строк (HTTP 413)."""


class UnitsNotConfirmed(DataValidationError):
    """Столбец RSRP похож на другие единицы, а пользователь не подтвердил импорт (HTTP 422)."""


def _norm(name: str) -> str:
    return re.sub(r"[\s_]", "", str(name).lower())


# --- опции -------------------------------------------------------------------------------------------


@dataclass
class ImportOptions:
    delimiter: str | None = None
    decimal_comma: bool | None = None
    time_format: str | None = None  # метка из TIME_FORMATS, "unix", "iso" или шаблон strptime (с %)
    tz: str | None = None
    mapping: dict[str, str | None] | None = None
    tech_filter: bool | None = None
    encoding: str | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> ImportOptions:
        raw = dict(raw or {})
        unknown = set(raw) - {"delimiter", "decimal_comma", "time_format", "tz", "mapping", "tech_filter", "encoding"}
        if unknown:
            raise DataValidationError(f"options: неизвестные параметры {sorted(unknown)}")
        delim = raw.get("delimiter")
        if delim == "\\t":
            delim = "\t"
        if delim not in (None, "") and delim not in DELIMITERS:
            raise DataValidationError(f"options.delimiter: допустимы {list(DELIMITERS)}")
        mapping = raw.get("mapping")
        if mapping is not None and not isinstance(mapping, Mapping):
            raise DataValidationError("options.mapping: ожидается объект {поле: столбец}")
        return cls(
            delimiter=delim or None,
            decimal_comma=raw.get("decimal_comma"),
            time_format=raw.get("time_format") or None,
            tz=raw.get("tz") or None,
            mapping=dict(mapping) if mapping else None,
            tech_filter=raw.get("tech_filter"),
            encoding=raw.get("encoding") or None,
        )


# --- разбор файла ------------------------------------------------------------------------------------


@dataclass
class ParsedFile:
    encoding: str
    delimiter: str
    header_row: int  # сколько строк (комментарии, пустые) пропущено до заголовка
    columns: list[str]
    rows: list[list[str]]  # непустые строки данных
    decimal_comma_auto: bool = False


def decode_bytes(data: bytes, forced: str | None = None) -> tuple[str, str]:
    for enc in [forced] if forced else ENCODINGS:
        try:
            return data.decode(enc), ("utf-8" if enc == "utf-8-sig" else enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise DataValidationError("не удалось определить кодировку файла (пробовали UTF-8, cp1251, latin-1)")


def _is_skipped(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith(COMMENT_PREFIXES)


def detect_delimiter(lines: Sequence[str]) -> str:
    sample = [ln for ln in lines if not _is_skipped(ln)][:SNIFF_LINES]
    if not sample:
        raise DataValidationError("файл не содержит строк с данными")
    try:
        found = csv.Sniffer().sniff("\n".join(sample), delimiters="".join(DELIMITERS)).delimiter
        if found in DELIMITERS and all(ln.count(found) > 0 for ln in sample[:3]):
            return found
    except csv.Error:
        pass
    # запасной вариант: разделитель с наиболее стабильным ненулевым числом вхождений
    best, best_score = ",", -1.0
    for d in DELIMITERS:
        counts = [ln.count(d) for ln in sample]
        top = max(set(counts), key=counts.count)
        score = (counts.count(top) / len(counts)) * (1 if top > 0 else 0) + top * 1e-3
        if score > best_score:
            best, best_score = d, score
    return best


def _synonym_group(name: str) -> str | None:
    n = _norm(name)
    for fld, groups in SYNONYMS.items():
        if n in groups["strong"] or n in groups["weak"]:
            return fld
    return None


def parse_file(data: bytes, opts: ImportOptions, settings: Settings) -> ParsedFile:
    fs = settings.field_data
    if len(data) > fs.import_max_bytes:
        raise ImportTooLarge(f"файл больше {fs.import_max_bytes // (1024 * 1024)} МБ")
    if not data.strip():
        raise DataValidationError("файл пустой")
    text, encoding = decode_bytes(data, opts.encoding)
    lines = text.splitlines()
    delimiter = opts.delimiter or detect_delimiter(lines)
    # пропускаем пустые строки и комментарии в начале; заголовок: первая строка, где >= 2 столбцов — синонимы
    records: list[tuple[int, list[str]]] = []
    for i, ln in enumerate(lines):
        if _is_skipped(ln):
            if records:
                continue
            continue
        records.append((i, next(csv.reader([ln], delimiter=delimiter))))
    if not records:
        raise DataValidationError("файл не содержит строк с данными")
    header_pos = next(
        (k for k, (_, rec) in enumerate(records[:SNIFF_LINES]) if sum(_synonym_group(c) is not None for c in rec) >= 2),
        None,
    )
    if header_pos is None:
        raise DataValidationError(
            "не найден заголовок: нужна строка со столбцами широты, долготы, RSRP и времени (например, Latitude, Longitude, Level, Timestamp)"
        )
    header_line, header = records[header_pos]
    rows = [rec for _, rec in records[header_pos + 1 :]]
    if len(rows) > fs.import_max_rows:
        raise ImportTooLarge(f"в файле {len(rows)} строк данных, допустимо не больше {fs.import_max_rows}")
    columns = [c.strip() for c in header]
    return ParsedFile(encoding=encoding, delimiter=delimiter, header_row=header_line, columns=columns, rows=rows)


# --- сопоставление столбцов ----------------------------------------------------------------------------


def detect_mapping(columns: Sequence[str]) -> tuple[dict[str, str | None], dict[str, str], list[str]]:
    """(mapping, confidence, warnings); confidence: high | low | none."""
    by_norm = {_norm(c): c for c in columns}
    mapping: dict[str, str | None] = {}
    conf: dict[str, str] = {}
    warnings: list[str] = []
    for fld, groups in SYNONYMS.items():
        hit = next((by_norm[s] for s in groups["strong"] if s in by_norm), None)
        level = "high"
        if hit is None:
            hit = next((by_norm[s] for s in groups["weak"] if s in by_norm), None)
            level = "low"
        mapping[fld] = hit
        conf[fld] = level if hit else "none"
        if hit and level == "low":
            warnings.append(
                f"Столбец {fld.upper() if fld == 'rsrp' else fld} угадан по слабому синониму '{hit}': проверьте"
            )
    # время: отдельные «date» + «time» склеиваются, если нет явного timestamp/datetime/utc
    has_explicit_ts = any(s in by_norm for s in ("timestamp", "datetime", "utc"))
    if mapping["date"] and "time" in by_norm and not has_explicit_ts:
        mapping["timestamp"] = None
        mapping["time"] = by_norm["time"]
        conf["timestamp"] = "none"
        conf["time"] = "high"
    else:
        mapping["date"] = None
        mapping["time"] = None
    return mapping, conf, warnings


def resolve_mapping(
    columns: Sequence[str], user: Mapping[str, str | None] | None
) -> tuple[dict[str, str | None], dict[str, str], list[str]]:
    mapping, conf, warnings = detect_mapping(columns)
    if user:
        valid = set(columns)
        for fld, col in user.items():
            if fld not in {"lat", "lon", "rsrp", "timestamp", "date", "time", "tech"}:
                raise DataValidationError(f"mapping: неизвестное поле {fld!r}")
            if col in (None, ""):
                mapping[fld], conf[fld] = None, "none"
            elif col not in valid:
                raise DataValidationError(f"mapping: столбца {col!r} нет в файле (есть: {', '.join(columns)})")
            else:
                mapping[fld], conf[fld] = col, "high"
        warnings = [w for w in warnings if not any(user.get(k) for k in ("lat", "lon", "rsrp", "timestamp"))]
    return mapping, conf, warnings


def missing_required(mapping: Mapping[str, str | None]) -> list[str]:
    out = [k for k in ("lat", "lon", "rsrp") if not mapping.get(k)]
    if not mapping.get("timestamp") and not (mapping.get("date") and mapping.get("time")):
        out.append("timestamp")
    return out


# --- числа и время ---------------------------------------------------------------------------------------


def detect_decimal_comma(parsed: ParsedFile, mapping: Mapping[str, str | None]) -> bool:
    """Десятичная запятая допустима только при разделителе ; или табуляции и только в числовых столбцах."""
    if parsed.delimiter not in (";", "\t"):
        return False
    idx = [parsed.columns.index(mapping[k]) for k in ("lat", "lon", "rsrp") if mapping.get(k)]
    return any(any(i < len(rec) and DECIMAL_COMMA_RE.match(rec[i]) for i in idx) for rec in parsed.rows[:SAMPLE_VALUES])


def _parse_with(value: str, key: str | None, tz: Any) -> datetime | None:
    s = value.strip()
    if not s:
        return None
    if key is None:  # ISO 8601
        return parse_one(s, tz, accept_epoch=False)
    if key == "unix":
        try:
            num = float(s)
        except ValueError:
            return None
        if abs(num) > 1e11:
            num /= 1000.0
        if abs(num) < 1e8:  # слишком мало для epoch (меньше 1973 года): скорее всего, не время
            return None
        return datetime.fromtimestamp(num, tz=timezone.utc)
    try:
        return datetime.strptime(s, key).replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_time_format(spec: str | None) -> tuple[str, str | None]:
    """Метка/шаблон пользователя -> (метка, ключ разбора)."""
    if spec is None:
        raise ValueError("spec")
    if spec in TIME_BY_LABEL:
        return spec, TIME_BY_LABEL[spec]
    low = spec.strip().lower()
    if low == "iso":
        return "ISO 8601", None
    if low == "unix":
        return "unix epoch", "unix"
    if "%" in spec:
        try:
            datetime.strptime(datetime(2000, 1, 2, 3, 4, 5).strftime(spec), spec)
        except ValueError as exc:
            raise DataValidationError(f"options.time_format: некорректный шаблон strptime {spec!r}") from exc
        return spec, spec
    raise DataValidationError(
        f"options.time_format: неизвестный формат {spec!r}; допустимы {[lbl for lbl, _ in TIME_FORMATS]} или шаблон strptime"
    )


def detect_time_format(values: Sequence[str], tz: Any) -> tuple[str, str | None]:
    sample = [v for v in values if v.strip()][:SAMPLE_VALUES]
    if not sample:
        return TIME_FORMATS[0]
    for label, key in TIME_FORMATS:
        ok = sum(_parse_with(v, key, tz) is not None for v in sample)
        if ok / len(sample) >= DETECT_MIN_SHARE:
            return label, key
    return TIME_FORMATS[0]


# --- основной разбор --------------------------------------------------------------------------------------


@dataclass
class ImportAnalysis:
    parsed: ParsedFile
    mapping: dict[str, str | None]
    confidence: dict[str, str]
    warnings: list[str]
    decimal_comma: bool
    time_label: str
    tech_column: str | None
    tech_filter: bool
    tz_name: str
    total: int = 0
    accepted: list[NormRow] = field(default_factory=list)
    accepted_pos: list[int] = field(default_factory=list)  # номер строки данных (с 0) для каждого принятого
    rejected: list[Rejection] = field(default_factory=list)
    filtered: list[int] = field(default_factory=list)  # номера строк, отфильтрованных по технологии
    rsrp_median: float | None = None
    units_suspect: bool = False
    row_values: dict[int, dict] = field(default_factory=dict)  # удобное представление строк для предпросмотра
    incomplete: list[str] = field(default_factory=list)  # чего не хватает для импорта

    @property
    def rejected_counts(self) -> dict[str, int]:
        return rejected_by_reason(self.rejected)


def _to_number(raw: str, decimal_comma: bool) -> float | None:
    s = raw.strip()
    if decimal_comma:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def analyze(data: bytes, opts: ImportOptions, settings: Settings, existing: pd.DataFrame) -> ImportAnalysis:
    """Разбор + проверка всех строк файла без записи. Один и тот же код для предпросмотра и сохранения."""
    parsed = parse_file(data, opts, settings)
    mapping, conf, warnings = resolve_mapping(parsed.columns, opts.mapping)
    tz_name = opts.tz or settings.daynight.timezone
    tz = get_zone(tz_name)
    decimal = opts.decimal_comma if opts.decimal_comma is not None else detect_decimal_comma(parsed, mapping)
    tech_col = mapping.get("tech")
    tech_filter = bool(tech_col) and opts.tech_filter is not False
    a = ImportAnalysis(
        parsed=parsed,
        mapping=mapping,
        confidence=conf,
        warnings=list(warnings),
        decimal_comma=decimal,
        time_label="",
        tech_column=tech_col,
        tech_filter=tech_filter,
        tz_name=tz_name,
        total=len(parsed.rows),
    )
    a.incomplete = missing_required(mapping)
    cols = {c: i for i, c in enumerate(parsed.columns)}

    def cell(rec: list[str], col: str | None) -> str:
        i = cols.get(col) if col else None
        return rec[i] if i is not None and i < len(rec) else ""

    # время: формат определяется по значениям столбца (или склеенных «дата + время»)
    if mapping.get("timestamp"):
        ts_raw = [cell(r, mapping["timestamp"]) for r in parsed.rows]
    elif mapping.get("date") and mapping.get("time"):
        ts_raw = [f"{cell(r, mapping['date'])} {cell(r, mapping['time'])}".strip() for r in parsed.rows]
    else:
        ts_raw = [""] * len(parsed.rows)
    if opts.time_format:
        a.time_label, key = resolve_time_format(opts.time_format)
    else:
        a.time_label, key = detect_time_format(ts_raw, tz)
    if a.incomplete:
        a.warnings.append("Не найдены столбцы: " + ", ".join(a.incomplete) + ". Укажите их вручную.")
        return a

    # единицы: медиана столбца RSRP по строкам нужной технологии
    rsrp_vals = []
    items: list[dict] = []
    positions: list[int] = []
    bad_shape: list[Rejection] = []
    width = len(parsed.columns)
    for pos, rec in enumerate(parsed.rows):
        if len(rec) != width:
            bad_shape.append(Rejection(pos, "unparseable", f"Неверное число полей: {len(rec)} вместо {width}"))
            a.row_values[pos] = {"raw": ",".join(rec)}
            continue
        if tech_filter and not TECH_RE.search(cell(rec, tech_col)):
            a.filtered.append(pos)
            continue
        lat_raw, lon_raw, rsrp_raw = (cell(rec, mapping[k]) for k in ("lat", "lon", "rsrp"))
        if decimal:
            lat_raw, lon_raw, rsrp_raw = (x.replace(",", ".") for x in (lat_raw, lon_raw, rsrp_raw))
        dt = _parse_with(ts_raw[pos], key, tz)
        iso = dt.astimezone(tz).isoformat() if dt else ts_raw[pos]
        r = _to_number(rsrp_raw, False)
        if r is not None:
            rsrp_vals.append(r)
        items.append({"lat": lat_raw, "lon": lon_raw, "rsrp_dbm": rsrp_raw, "timestamp": iso, "source": "import"})
        positions.append(pos)

    if rsrp_vals:
        med = float(median(rsrp_vals))
        a.rsrp_median = med
        fs = settings.field_data
        if med > 0 or not (fs.units_median_min <= med <= fs.units_median_max):
            a.units_suspect = True
            a.warnings.append(
                f"Похоже, это не RSRP в дБм (медиана столбца {med:g}): ASU, RSSI или RSRQ? Проверьте столбец и подтвердите импорт."
            )

    accepted, rejected = validate_field_rows(items, settings, existing)
    a.accepted = accepted
    a.accepted_pos = [positions[x.index] for x in accepted]
    a.rejected = sorted(
        [Rejection(positions[r.index], r.reason, r.message) for r in rejected] + bad_shape, key=lambda r: r.index
    )
    for pos, it in zip(positions, items):
        a.row_values[pos] = {
            "raw_lat": it["lat"],
            "raw_lon": it["lon"],
            "raw_rsrp": it["rsrp_dbm"],
            "raw_ts": it["timestamp"],
        }
    log.info(
        "Импорт: строк %d, принять %d, отклонить %d, не та технология %d",
        a.total,
        len(a.accepted),
        len(a.rejected),
        len(a.filtered),
    )
    return a


# --- ответы ------------------------------------------------------------------------------------------------


def _points(a: ImportAnalysis) -> list[list[float]]:
    """Точки файла для карты предпросмотра: [lat, lon, 1|0 (принимается)], не больше MAX_POINTS (остальные прорежены)."""
    ok_pos = set(a.accepted_pos)
    skipped = set(a.filtered)
    pts: list[list[float]] = []
    lat_i = a.parsed.columns.index(a.mapping["lat"]) if a.mapping.get("lat") else None
    lon_i = a.parsed.columns.index(a.mapping["lon"]) if a.mapping.get("lon") else None
    if lat_i is None or lon_i is None:
        return pts
    for pos, rec in enumerate(a.parsed.rows):
        if len(rec) <= max(lat_i, lon_i) or pos in skipped:
            continue
        la, lo = _to_number(rec[lat_i], a.decimal_comma), _to_number(rec[lon_i], a.decimal_comma)
        if la is not None and lo is not None and abs(la) <= 90 and abs(lo) <= 180:
            pts.append([la, lo, 1 if pos in ok_pos else 0])
    if len(pts) > MAX_POINTS:
        step = len(pts) / MAX_POINTS
        pts = [pts[int(i * step)] for i in range(MAX_POINTS)]
    return pts


def preview_response(a: ImportAnalysis, settings: Settings) -> dict:
    fs = settings.field_data
    pts = _points(a)
    bbox = None
    if pts:
        bbox = [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]
    by_pos = {x.index: x for x in a.rejected}
    acc_by_pos = dict(zip(a.accepted_pos, a.accepted))
    filtered = set(a.filtered)

    def row_info(pos: int) -> dict:
        base = {"row": pos + 1}
        if pos in acc_by_pos:
            n = acc_by_pos[pos]
            return {
                **base,
                "lat": n.lat,
                "lon": n.lon,
                "rsrp_dbm": n.rsrp_dbm,
                "timestamp": n.timestamp,
                "status": "ok",
            }
        if pos in by_pos:
            r = by_pos[pos]
            rv = a.row_values.get(pos, {})
            return {
                **base,
                **{k: v for k, v in rv.items()},
                "status": "rejected",
                "reason": r.reason,
                "message": r.message,
            }
        if pos in filtered:
            return {
                **base,
                "status": "filtered",
                "reason": "filtered_tech",
                "message": "Не LTE/NR: у других технологий нет RSRP",
            }
        return {**base, "status": "incomplete"}

    sample = [row_info(p) for p in range(min(fs.sample_rows, a.total))]
    shown = {s["row"] - 1 for s in sample}
    extra = [row_info(r.index) for r in a.rejected if r.index not in shown][: fs.sample_rejected]
    stats = None
    if not a.incomplete:
        stats = {
            "rows_total": a.total,
            "would_accept": len(a.accepted),
            "rejected_by_reason": a.rejected_counts,
            "filtered_tech": len(a.filtered),
        }
    return {
        "detected": {
            "encoding": a.parsed.encoding,
            "delimiter": a.parsed.delimiter,
            "header_row": a.parsed.header_row,
            "decimal_comma": a.decimal_comma,
            "time_format": a.time_label,
            "timezone": a.tz_name,
            "tech_column": a.tech_column,
            "tech_filter": a.tech_filter,
        },
        "columns": a.parsed.columns,
        "mapping": a.mapping,
        "mapping_confidence": a.confidence,
        "warnings": a.warnings,
        "units_suspect": a.units_suspect,
        "rsrp_median": None if a.rsrp_median is None else round(a.rsrp_median, 2),
        "missing": a.incomplete,
        "time_formats": [lbl for lbl, _ in TIME_FORMATS],
        "stats": stats,
        "sample": sample,
        "rejected_sample": extra,
        "points": pts,
        "bbox": bbox,
    }


def require_complete(a: ImportAnalysis, confirm_units: bool) -> None:
    if a.incomplete:
        raise DataValidationError("не заданы столбцы: " + ", ".join(a.incomplete))
    if a.units_suspect and not confirm_units:
        raise UnitsNotConfirmed(
            "Столбец RSRP похож на другие единицы (не дБм). Проверьте сопоставление и подтвердите импорт галочкой (confirm_units)."
        )
    if not a.accepted:
        raise DataValidationError("нечего импортировать: ни одна строка не прошла проверку")
