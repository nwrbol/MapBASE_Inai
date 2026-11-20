"""Геометрия: гаверсинус, локальная равнопромежуточная проекция, point-in-polygon."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_371_000.0

ArrayLike = float | np.ndarray


def haversine_m(
    lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike, radius: float = EARTH_RADIUS_M
) -> ArrayLike:
    """Расстояние по большому кругу в метрах (векторизовано, поддерживает broadcasting)."""
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    d = 2 * radius * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    return float(d) if np.ndim(d) == 0 else d


@dataclass(frozen=True)
class LocalProjection:
    """x = R·cos(lat0)·rad(lon−lon0), y = R·rad(lat−lat0) — сетка задания (не Web Mercator)."""

    lat0: float
    lon0: float
    radius: float = EARTH_RADIUS_M

    @property
    def _kx(self) -> float:
        return self.radius * np.cos(np.radians(self.lat0))

    def to_xy(self, lat: ArrayLike, lon: ArrayLike) -> tuple[ArrayLike, ArrayLike]:
        x = self._kx * np.radians(np.asarray(lon, dtype=float) - self.lon0)
        y = self.radius * np.radians(np.asarray(lat, dtype=float) - self.lat0)
        return x, y

    def to_latlon(self, x: ArrayLike, y: ArrayLike) -> tuple[ArrayLike, ArrayLike]:
        lat = self.lat0 + np.degrees(np.asarray(y, dtype=float) / self.radius)
        lon = self.lon0 + np.degrees(np.asarray(x, dtype=float) / self._kx)
        return lat, lon


def points_in_polygon(lat: np.ndarray, lon: np.ndarray, polygon: list[list[float]]) -> np.ndarray:
    """Ray casting: True для точек внутри полигона [[lat, lon], ...] (векторизовано по точкам)."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    inside = np.zeros(lat.shape, dtype=bool)
    n = len(polygon)
    for i in range(n):
        ya, xa = polygon[i]
        yb, xb = polygon[(i - 1) % n]
        crosses = (ya > lat) != (yb > lat)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_int = (xb - xa) * (lat - ya) / (yb - ya) + xa
        inside ^= crosses & (lon < x_int)
    return inside
