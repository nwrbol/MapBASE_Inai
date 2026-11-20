"""Разбор timestamp: строго ISO 8601; без смещения — местное время (Asia/Bishkek)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from .errors import ConfigError

_EPOCH_RE = re.compile(r"^[+-]?\d{9,13}(\.\d+)?$")
# Дата YYYY-MM-DD, затем необязательно время через 'T' или пробел и смещение/Z.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?)?"
    r"(Z|z|[+-]\d{2}(:?\d{2})?)?$"
)


@lru_cache(maxsize=8)
def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"неизвестный часовой пояс {name!r} (установите пакет tzdata)") from exc


def parse_one(value: str, tz: ZoneInfo, accept_epoch: bool = False) -> datetime | None:
    """Строка -> aware datetime в UTC или None, если это не ISO 8601."""
    s = value.strip()
    if not s:
        return None
    if accept_epoch and _EPOCH_RE.match(s):
        num = float(s)
        if abs(num) > 1e11:  # миллисекунды
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc)
    if not _ISO_RE.match(s):
        return None
    norm = s[:-1] + "+00:00" if s[-1] in "Zz" else s
    if len(norm) > 19 and norm[10] == " ":
        norm = norm[:10] + "T" + norm[11:]
    # Дробные секунды длиннее 6 знаков fromisoformat в Python 3.10 не понимает — усечём.
    m = re.match(r"^(.*\.\d{6})\d+(.*)$", norm)
    if m:
        norm = m.group(1) + m.group(2)
    # Смещение вида +0600 или +06 приводим к +06:00 для Python 3.10.
    m = re.match(r"^(.*T[\d:.]+)([+-])(\d{2})(\d{2})?$", norm)
    if m:
        norm = f"{m.group(1)}{m.group(2)}{m.group(3)}:{m.group(4) or '00'}"
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def parse_timestamps(values: pd.Series, tz_name: str, accept_epoch: bool = False) -> pd.Series:
    """Серия строк -> datetime64[ns, UTC]; нераспознанные значения дают NaT."""
    tz = get_zone(tz_name)
    cache: dict[str, datetime | None] = {}
    out: list[datetime | None] = []
    for v in values.astype(str):
        if v not in cache:
            cache[v] = parse_one(v, tz, accept_epoch)
        out.append(cache[v])
    ts = pd.to_datetime(pd.Series(out, index=values.index, dtype=object), utc=True, errors="coerce")
    return ts.astype("datetime64[ns, UTC]")


def local_hours(ts_utc: pd.Series, tz_name: str) -> np.ndarray:
    """Час местного времени (0..23) для каждого UTC-момента; NaT -> -1."""
    local = ts_utc.dt.tz_convert(tz_name)
    return local.dt.hour.fillna(-1).astype(int).to_numpy()
