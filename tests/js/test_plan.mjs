// «Текущий план» для вкладки «Обзор» и result_selected (web/static/scenario.js). Запуск: node --test tests/js/*.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

const S = createRequire(import.meta.url)("../../web/static/scenario.js");

// Покрытие: A {1,2,3,4}, B {1,2,5}, C {3,4,6}, D {1,6}; E недопустима (100 м до T1). Оптимум по ТЗ: B + C = 6.
function makeData(extra = {}) {
  return {
    meta: { tower_exclusion_m: 500, ...extra },
    result: {
      worst_cells: [{ cell_id: "X1", rsrp_median: -120 }],
      best_candidates: ["B", "C"],
      dropped_rows: 200,
      covered_weak_cells: 6,
    },
    compare: { brute_force: { pair: ["B", "C"] } },
    candidates: [
      { id: "A", admissible: true, covers: ["1", "2", "3", "4"] },
      { id: "B", admissible: true, covers: ["1", "2", "5"] },
      { id: "C", admissible: true, covers: ["3", "4", "6"] },
      { id: "D", admissible: true, covers: ["1", "6"] },
      { id: "E", admissible: false, nearest_tower: "T1", nearest_tower_m: 100, reason: "близко", covers: [] },
    ],
  };
}
const data = makeData();
const empty = () => ({ selected: null, result: null, excluded: [], required: [] });

test("без выбора и ограничений: пара по ТЗ", () => {
  const p = S.currentPlan(empty(), data);
  assert.equal(p.source, "tz");
  assert.deepEqual(p.pair, ["B", "C"]);
  assert.equal(p.covered, 6);
  assert.equal(p.delta, 0);
  assert.equal(S.planDiffersFromTz(p), false);
});

test("отказ: план берёт лучшую пару с учётом отказов", () => {
  const constraints = { excluded: [{ id: "B", reason: "", category: "owner" }], required: [] };
  const result = S.runScenario(data, constraints, 10);
  const p = S.currentPlan({ selected: null, result, ...constraints }, data);
  assert.equal(p.source, "constraints");
  assert.deepEqual(p.pair, ["A", "C"]);
  assert.equal(p.covered, 5);
  assert.equal(p.delta, -1);
  assert.match(p.label, /с учётом отказов \(отказ: B\)/);
  assert.equal(S.planDiffersFromTz(p), true);
});

test("выбранная пара: приоритет над ограничениями, порядок нормализуется", () => {
  const constraints = { excluded: [{ id: "B", reason: "", category: "owner" }], required: [] };
  const result = S.runScenario(data, constraints, 10);
  const p = S.currentPlan({ selected: ["D", "A"], result, ...constraints }, data);
  assert.equal(p.source, "selected");
  assert.deepEqual(p.pair, ["A", "D"]);
  assert.equal(p.covered, 5);
  assert.equal(p.delta, -1);
  assert.equal(p.label, "ваш выбор");
  assert.equal(p.violates, false);
});

test("выбор, нарушающий ограничения, помечается violates", () => {
  const p = S.currentPlan({ selected: ["B", "C"], result: null, excluded: [{ id: "B" }], required: [] }, data);
  assert.equal(p.source, "selected");
  assert.equal(p.violates, true);
  const q = S.currentPlan({ selected: ["A", "D"], result: null, excluded: [], required: ["C"] }, data);
  assert.equal(q.violates, true);
});

test("предпросмотр без «Выбрать» план не меняет", () => {
  const base = S.currentPlan(empty(), data);
  const withPreview = S.currentPlan({ ...empty(), preview: ["A", "D"] }, data);
  assert.deepEqual(withPreview, base);
});

test("сценарий невыполним: пары нет, сообщение сохранено", () => {
  const constraints = { excluded: ["A", "B", "C"].map((id) => ({ id, reason: "", category: "other" })), required: [] };
  const result = S.runScenario(data, constraints, 10);
  assert.equal(result.feasible, false);
  const p = S.currentPlan({ selected: null, result, ...constraints }, data);
  assert.equal(p.pair, null);
  assert.equal(p.source, "constraints");
  assert.match(p.message, /невыполним/);
});

test("ограничения есть, результат ещё не пришёл: pending, а не пара по ТЗ", () => {
  const p = S.currentPlan({ selected: null, result: null, excluded: [{ id: "B" }], required: [] }, data);
  assert.equal(p.pair, null);
  assert.equal(p.pending, true);
});

test("выбор неизвестной площадки в этих данных не считается планом", () => {
  const p = S.currentPlan({ selected: ["A", "ZZ"], result: null, excluded: [], required: [] }, data);
  assert.equal(p.source, "tz");
});

test("при полевых замерах Δ считается к оптимуму карты", () => {
  const d = makeData({ field_included: true });
  const p = S.currentPlan({ selected: ["A", "B"], result: null, excluded: [], required: [] }, d);
  assert.equal(p.covered, 5);
  assert.equal(p.delta, -1); // 5 − 6 (оптимум карты)
});

test("result_selected: 4 ключа, пара отсортирована, покрытие по объединению", () => {
  const r = S.buildSelectedResult(data, ["C", "B"]);
  assert.deepEqual(Object.keys(r), ["worst_cells", "best_candidates", "dropped_rows", "covered_weak_cells"]);
  assert.deepEqual(r.best_candidates, ["B", "C"]);
  assert.equal(r.covered_weak_cells, 6);
  assert.equal(r.dropped_rows, 200);
  assert.deepEqual(r.worst_cells, data.result.worst_cells);
  assert.equal(S.buildSelectedResult(data, ["A", "B"]).covered_weak_cells, 5);
});

test("result_selected: ошибки как в Python", () => {
  assert.throws(() => S.buildSelectedResult(data, ["A", "A"]), /две разные/);
  assert.throws(() => S.buildSelectedResult(data, ["A"]), /две разные/);
  assert.throws(() => S.buildSelectedResult(data, ["A", "ZZ"]), /ZZ неизвестна/);
  assert.throws(() => S.buildSelectedResult(data, ["E", "A"]), /E недопустима по ТЗ: 100 м до T1 \(< 500 м\)/);
});
