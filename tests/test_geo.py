import numpy as np
import pytest

from coverage.geo import LocalProjection, haversine_m, points_in_polygon


def test_one_degree_latitude():
    assert haversine_m(42.0, 74.0, 43.0, 74.0) == pytest.approx(111_195, abs=1)


def test_zero_distance_and_symmetry():
    assert haversine_m(42.87, 74.6, 42.87, 74.6) == 0.0
    a = haversine_m(42.84, 74.56, 42.87, 74.61)
    b = haversine_m(42.87, 74.61, 42.84, 74.56)
    assert a == pytest.approx(b)


def test_vectorized():
    d = haversine_m(42.0, 74.0, np.array([42.0, 43.0]), np.array([74.0, 74.0]))
    assert d.shape == (2,)
    assert d[0] == 0.0


def test_projection_round_trip():
    p = LocalProjection(42.84, 74.56)
    lat = np.array([42.84, 42.85123, 42.87597])
    lon = np.array([74.56, 74.5789, 74.60905])
    x, y = p.to_xy(lat, lon)
    lat2, lon2 = p.to_latlon(x, y)
    x2, y2 = p.to_xy(lat2, lon2)
    assert np.max(np.abs(x2 - x)) < 0.01
    assert np.max(np.abs(y2 - y)) < 0.01


def test_projection_matches_task_formula():
    p = LocalProjection(42.84, 74.56, 6_371_000)
    x, y = p.to_xy(42.85, 74.57)
    assert x == pytest.approx(6_371_000 * np.cos(np.radians(42.84)) * np.radians(0.01))
    assert y == pytest.approx(6_371_000 * np.radians(0.01))


def test_point_in_polygon():
    square = [[0, 0], [0, 1], [1, 1], [1, 0]]
    res = points_in_polygon(np.array([0.5, 1.5, 0.2]), np.array([0.5, 0.5, 0.9]), square)
    assert res.tolist() == [True, False, True]
