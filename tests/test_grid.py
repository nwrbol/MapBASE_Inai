import pandas as pd
import pytest

from coverage.geo import LocalProjection
from coverage.grid import build_grid, cell_indices, classify, make_cell_id, worst_cells


def pts(settings, x, y, values):
    p = LocalProjection(settings.grid.origin_lat, settings.grid.origin_lon, settings.grid.earth_radius_m)
    lat, lon = p.to_latlon([x] * len(values), [y] * len(values))
    return pd.DataFrame({"lat": lat, "lon": lon, "rsrp_dbm": values})


def test_cell_boundary(settings):
    p = LocalProjection(settings.grid.origin_lat, settings.grid.origin_lon)
    lat, lon = p.to_latlon([199.99, 200.01, 0.0], [0.5, 399.99, 400.01])
    ix, iy = cell_indices(lat, lon, settings)
    assert ix.tolist() == [0, 1, 0]
    assert iy.tolist() == [0, 1, 2]


def test_cell_id_format(settings):
    assert make_cell_id(6, 13, settings) == "X006_Y013"


def test_median_odd_and_even(settings):
    df = pd.concat(
        [
            pts(settings, 50, 50, [-100, -110, -90, -95, -120]),
            pts(settings, 250, 50, [-100, -101, -102, -103, -104, -110]),
        ]
    )
    g = build_grid(df, settings).set_index("cell_id")
    assert g.loc["X000_Y000", "median_rsrp"] == -100
    assert g.loc["X001_Y000", "median_rsrp"] == pytest.approx(-102.5)


def test_min_samples(settings):
    df = pd.concat([pts(settings, 50, 50, [-130] * 4), pts(settings, 250, 50, [-100] * 5)])
    g = build_grid(df, settings).set_index("cell_id")
    assert not g.loc["X000_Y000", "counted"]
    assert g.loc["X001_Y000", "counted"]
    w = worst_cells(build_grid(df, settings), 3)
    assert w["cell_id"].tolist() == ["X001_Y000"]


@pytest.mark.parametrize(
    "value,cls",
    [(-90, "green"), (-89.9, "green"), (-90.01, "yellow"), (-105, "yellow"), (-105.01, "red"), (-140, "red")],
)
def test_classify(settings, value, cls):
    assert classify(value, settings) == cls


def test_worst_sort_ties_by_cell_id(settings):
    df = pd.concat(
        [pts(settings, 450, 50, [-120] * 5), pts(settings, 50, 50, [-120] * 5), pts(settings, 250, 50, [-125] * 5)]
    )
    w = worst_cells(build_grid(df, settings), 3)
    assert w["cell_id"].tolist() == ["X001_Y000", "X000_Y000", "X002_Y000"]


def test_center_is_middle_of_cell(settings):
    g = build_grid(pts(settings, 50, 50, [-100] * 5), settings)
    p = LocalProjection(settings.grid.origin_lat, settings.grid.origin_lon)
    x, y = p.to_xy(g["center_lat"].iloc[0], g["center_lon"].iloc[0])
    assert x == pytest.approx(100, abs=1e-6)
    assert y == pytest.approx(100, abs=1e-6)
