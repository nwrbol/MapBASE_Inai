"""Чтение и валидация входных CSV. Всё читается строками: pandas ничего не угадывает."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .errors import DataValidationError

log = logging.getLogger(__name__)

Source = str | Path | bytes

MEASUREMENT_COLUMNS = ("lat", "lon", "rsrp_dbm", "timestamp")
SITE_COLUMNS = ("id", "lat", "lon")


def read_bytes(source: Source, name: str) -> bytes:
    """Возвращает содержимое файла/байтов с понятной ошибкой, если файла нет."""
    if isinstance(source, bytes):
        return source
    path = Path(source)
    if not path.exists():
        raise DataValidationError(f"{name}: файл не найден: {path}")
    if not path.is_file():
        raise DataValidationError(f"{name}: это не файл: {path}")
    return path.read_bytes()


def _decode(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataValidationError(
            f"{name}: файл не в кодировке UTF-8 (байт {exc.start}); сохраните CSV в UTF-8"
        ) from exc


@dataclass
class RawTable:
    """Физические записи CSV: заголовок и все строки данных (включая пустые и битые)."""

    name: str
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)


def read_raw_csv(source: Source, name: str, required: tuple[str, ...]) -> RawTable:
    """Читает CSV модулем csv: пустая физическая строка тоже считается строкой данных."""
    text = _decode(read_bytes(source, name), name)
    if not text.strip():
        raise DataValidationError(f"{name}: файл пустой")
    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise DataValidationError(f"{name}: ошибка разбора CSV: {exc}") from exc
    header = [h.strip().lower() for h in records[0]]
    missing = [c for c in required if c not in header]
    if missing:
        raise DataValidationError(f"{name}: нет обязательных столбцов {missing}; найдены {header}")
    dup = sorted({h for h in header if header.count(h) > 1 and h in required})
    if dup:
        raise DataValidationError(f"{name}: столбцы повторяются в заголовке: {dup}")
    return RawTable(name=name, header=header, rows=records[1:])


def to_string_frame(raw: RawTable, columns: tuple[str, ...]) -> tuple[pd.DataFrame, np.ndarray]:
    """DataFrame строк нужных столбцов + маска «битых» записей (пустая/неверное число полей)."""
    width = len(raw.header)
    idx = [raw.header.index(c) for c in columns]
    malformed = np.zeros(len(raw.rows), dtype=bool)
    values: list[list[str]] = []
    for i, row in enumerate(raw.rows):
        if len(row) != width or all(not cell.strip() for cell in row):
            malformed[i] = True
            values.append([""] * len(columns))
        else:
            values.append([row[j].strip() for j in idx])
    frame = pd.DataFrame(values, columns=list(columns), dtype=object)
    return frame, malformed


def to_number(series: pd.Series) -> pd.Series:
    """Строки в float; пустые и нечисловые («abc», «-95 dBm») превращаются в NaN."""
    return pd.to_numeric(series.astype(str).str.strip(), errors="coerce").astype(float)


def read_measurements(source: Source) -> tuple[pd.DataFrame, np.ndarray]:
    """Замеры как строки + маска битых записей. Отбрасывание делает cleaning.py."""
    raw = read_raw_csv(source, "measurements.csv", MEASUREMENT_COLUMNS)
    return to_string_frame(raw, MEASUREMENT_COLUMNS)


def read_sites(source: Source, name: str) -> pd.DataFrame:
    """БС или кандидаты: id, lat, lon. Битые строки — warning и пропуск (не входят в dropped_rows)."""
    raw = read_raw_csv(source, name, SITE_COLUMNS)
    frame, malformed = to_string_frame(raw, SITE_COLUMNS)
    lat = to_number(frame["lat"])
    lon = to_number(frame["lon"])
    ids = frame["id"].astype(str)
    ok = ~malformed & (ids != "") & np.isfinite(lat) & np.isfinite(lon) & lat.between(-90, 90) & lon.between(-180, 180)
    for i in np.flatnonzero(~ok.to_numpy()):
        if malformed[i] and not any(c.strip() for c in raw.rows[i]):
            continue  # пустая строка в конце файла — не повод для предупреждения
        log.warning("%s: строка %d пропущена (некорректные id/lat/lon): %s", name, i + 2, raw.rows[i])
    out = pd.DataFrame({"id": ids, "lat": lat, "lon": lon})[ok.to_numpy()]
    dup = out["id"].duplicated(keep="first")
    if dup.any():
        log.warning("%s: повторяющиеся id пропущены: %s", name, sorted(out.loc[dup, "id"]))
        out = out[~dup]
    if out.empty:
        log.warning("%s: нет ни одной корректной строки", name)
    return out.reset_index(drop=True)
