import pandas as pd
import pytest

from coverage.geo import LocalProjection
from coverage.selection import CandidateInfo, brute_force, evaluate_candidates, greedy


def cand(cid, covers, admissible=True):
    return CandidateInfo(cid, 0.0, 0.0, admissible, None, None, None, frozenset(covers))


def site_at(settings, cid, x, y):
    p = LocalProjection(settings.grid.origin_lat, settings.grid.origin_lon)
    lat, lon = p.to_latlon(x, y)
    return {"id": cid, "lat": float(lat), "lon": float(lon)}


def red_at(settings, cid, x, y):
    s = site_at(settings, cid, x, y)
    return {"cell_id": cid, "center_lat": s["lat"], "center_lon": s["lon"], "median_rsrp": -110.0}


def test_tower_exclusion_499_vs_500(settings):
    towers = pd.DataFrame([site_at(settings, "T1", 2000, 2000)])
    # Расстояние в метрах по гаверсинусу; подбираем точку так, чтобы было 499 и 500.5 м по y.
    cands = pd.DataFrame([site_at(settings, "A", 2000, 2499), site_at(settings, "B", 2000, 2500.5)])
    infos = {
        c.id: c
        for c in evaluate_candidates(
            cands, towers, pd.DataFrame(columns=["cell_id", "center_lat", "center_lon"]), settings
        )
    }
    assert infos["A"].nearest_tower_m == pytest.approx(499, abs=0.2)
    assert not infos["A"].admissible
    assert "T1" in infos["A"].reason
    assert infos["B"].admissible


def test_coverage_radius_inclusive(settings):
    towers = pd.DataFrame(columns=["id", "lat", "lon"])
    cands = pd.DataFrame([site_at(settings, "A", 0, 0)])
    red = pd.DataFrame([red_at(settings, "near", 0, 999.5), red_at(settings, "far", 0, 1000.6)])
    (info,) = evaluate_candidates(cands, towers, red, settings)
    assert info.covers == frozenset({"near"})


def test_union_without_double_count():
    infos = [cand("A", {1, 2, 3}), cand("B", {2, 3, 4})]
    res = brute_force(infos, {})
    assert res.covered == 4
    assert res.pair == ["A", "B"]


def test_greedy_loses_to_brute_force():
    infos = [cand("A", {1, 2, 3, 4}), cand("B", {1, 2, 5}), cand("C", {3, 4, 6})]
    g = greedy(infos, {})
    b = brute_force(infos, {})
    assert g.pair == ["A", "B"] and g.covered == 5
    assert b.pair == ["B", "C"] and b.covered == 6


def test_ties_are_lexicographic_and_reported():
    infos = [cand("C3", {1}), cand("C1", {1}), cand("C2", {1})]
    res = brute_force(infos, {})
    assert res.pair == ["C1", "C2"]
    assert res.ties == [["C1", "C2"], ["C1", "C3"], ["C2", "C3"]]


def test_tie_break_by_depth():
    infos = [cand("A", {"x"}), cand("B", {"y"}), cand("C", {"z"})]
    depth = {"x": 1.0, "y": 5.0, "z": 9.0}
    assert brute_force(infos, depth, "depth_then_lexicographic").pair == ["B", "C"]
    assert brute_force(infos, depth, "lexicographic").pair == ["A", "B"]


def test_inadmissible_are_skipped():
    infos = [cand("A", {1, 2, 3}, admissible=False), cand("B", {1}), cand("C", {2})]
    assert brute_force(infos, {}).pair == ["B", "C"]


def test_less_than_two_admissible():
    res = brute_force([cand("A", {1, 2}), cand("B", {3}, admissible=False)], {})
    assert res.pair == ["A"]
    assert res.covered == 2
    assert brute_force([], {}).pair == []


def test_no_red_cells():
    res = brute_force([cand("B", set()), cand("A", set()), cand("C", set())], {})
    assert res.covered == 0
    assert res.pair == ["A", "B"]
