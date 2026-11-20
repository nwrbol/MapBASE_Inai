"""Очистка замеров. Данные не «чинятся», а отбрасываются; каждая причина считается отдельно."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Settings
from .geo import points_in_polygon
from .io import to_number
from .timeutil import parse_timestamps

log = logging.getLogger(__name__)

# Порядок важен: строка относится к первой сработавшей причине.
REASONS = (
    "malformed",  # пустая физическая строка или неверное число полей
    "unparseable",  # lat/lon/rsrp пусты, не числа, NaN или inf
    "bad_timestamp",  # timestamp не ISO 8601
    "rsrp_out_of_range",  # вне [-140, -44]
    "out_of_bounds",  # вне границ района
    "duplicates",  # повтор по (lat, lon, rsrp_dbm, timestamp) после нормализации
)


@dataclass
class CleaningReport:
    total: int
    kept: int
    dropped: int
    by_reason: dict[str, int]
    alt_duplicates: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "by_reason": dict(self.by_reason),
            "alt_duplicates": dict(self.alt_duplicates),
        }


def in_bounds(lat: pd.Series, lon: pd.Series, settings: Settings) -> np.ndarray:
    """Маска «внутри района»: полигон (если задан) или bbox (включительно/строго по конфигу)."""
    b = settings.bounds
    lat_a = lat.to_numpy(dtype=float)
    lon_a = lon.to_numpy(dtype=float)
    if b.polygon:
        return points_in_polygon(lat_a, lon_a, b.polygon)
    with np.errstate(invalid="ignore"):
        if b.inclusive:
            return (lat_a >= b.lat_min) & (lat_a <= b.lat_max) & (lon_a >= b.lon_min) & (lon_a <= b.lon_max)
        return (lat_a > b.lat_min) & (lat_a < b.lat_max) & (lon_a > b.lon_min) & (lon_a < b.lon_max)


@dataclass
class RowVerdict:
    """Результат классификации строк: первая сработавшая причина отбрасывания ("" — строка годна)."""

    reasons: np.ndarray
    lat: pd.Series
    lon: pd.Series
    rsrp: pd.Series
    ts: pd.Series
    norm: pd.DataFrame

    @property
    def alive(self) -> np.ndarray:
        return self.reasons == ""

    def count(self, reason: str) -> int:
        return int((self.reasons == reason).sum())


def classify_rows(frame: pd.DataFrame, malformed: np.ndarray, settings: Settings) -> RowVerdict:
    """Единственный источник правил очистки: и CSV-замеры, и полевые замеры проходят через эту функцию.

    Каждая строка получает первую сработавшую причину (порядок задан в REASONS).
    """
    c = settings.cleaning
    total = len(frame)
    lat = to_number(frame["lat"])
    lon = to_number(frame["lon"])
    rsrp = to_number(frame["rsrp_dbm"])
    ts = parse_timestamps(frame["timestamp"], settings.daynight.timezone, c.accept_unix_epoch)

    reasons = np.full(total, "", dtype=object)

    def mark(reason: str, bad: np.ndarray) -> None:
        reasons[(reasons == "") & bad] = reason

    mark("malformed", np.asarray(malformed, dtype=bool))
    finite = np.isfinite(lat.to_numpy()) & np.isfinite(lon.to_numpy()) & np.isfinite(rsrp.to_numpy())
    mark("unparseable", ~finite)
    if c.require_valid_timestamp:
        mark("bad_timestamp", ts.isna().to_numpy())
    with np.errstate(invalid="ignore"):
        r = rsrp.to_numpy()
        mark("rsrp_out_of_range", ~((r >= c.rsrp_min) & (r <= c.rsrp_max)))
    mark("out_of_bounds", ~in_bounds(lat, lon, settings))

    norm = pd.DataFrame(
        {
            "lat": lat,
            "lon": lon,
            "rsrp_dbm": rsrp,
            "timestamp": _timestamp_key(frame["timestamp"], ts, c.dedup_timestamp_mode),
        }
    )
    valid_idx = np.flatnonzero(reasons == "")
    dup_mask = np.zeros(total, dtype=bool)
    dup_mask[valid_idx] = norm.iloc[valid_idx].duplicated(subset=list(c.dedup_subset), keep="first").to_numpy()
    mark("duplicates", dup_mask)
    return RowVerdict(reasons=reasons, lat=lat, lon=lon, rsrp=rsrp, ts=ts, norm=norm)


def clean_measurements(
    frame: pd.DataFrame, malformed: np.ndarray, settings: Settings
) -> tuple[pd.DataFrame, CleaningReport]:
    """Возвращает очищенные замеры (lat, lon, rsrp_dbm, timestamp, ts_utc, row) и отчёт."""
    total = len(frame)
    v = classify_rows(frame, malformed, settings)
    lat, lon, rsrp, ts, norm = v.lat, v.lon, v.rsrp, v.ts, v.norm
    alive = v.alive
    counts = {reason: v.count(reason) for reason in REASONS}
    valid_idx = np.flatnonzero(alive)

    alt = {
        "raw_full_row": int(frame[~malformed].duplicated().sum()),
        "lat_lon_timestamp": int(norm.iloc[valid_idx].duplicated(subset=["lat", "lon", "timestamp"]).sum()),
        "configured": counts["duplicates"],
    }
    log.info("Альтернативные определения дублей: %s", alt)

    clean = pd.DataFrame(
        {
            "lat": lat[alive].to_numpy(),
            "lon": lon[alive].to_numpy(),
            "rsrp_dbm": rsrp[alive].to_numpy(),
            "timestamp": frame["timestamp"][alive].astype(str).to_numpy(),
            "ts_utc": ts[alive].to_numpy(),
            "row": np.flatnonzero(alive) + 2,  # номер строки в файле (заголовок = 1)
        }
    )
    clean["ts_utc"] = pd.to_datetime(clean["ts_utc"], utc=True)
    kept = int(alive.sum())
    report = CleaningReport(
        total=total,
        kept=kept,
        dropped=total - kept,
        by_reason={k: counts.get(k, 0) for k in REASONS},
        alt_duplicates=alt,
    )
    assert sum(report.by_reason.values()) == report.dropped, "разбивка причин не сходится"
    return clean, report


def _timestamp_key(raw: pd.Series, ts: pd.Series, mode: str) -> pd.Series:
    """Ключ timestamp для дедупликации: момент времени (UTC) или нормализованная строка."""
    if mode == "instant":
        # Нераспознанное время (если require_valid_timestamp=false) сравниваем как строку.
        stripped = raw.astype(str).str.strip()
        iso_utc = ts.astype(str)
        return pd.Series(np.where(ts.isna().to_numpy(), "raw:" + stripped, iso_utc), index=raw.index, dtype=object)
    return raw.astype(str).str.strip()
