// Чистые функции слоёв и легенды (web/static/layers.js). Запуск: node --test tests/js/*.mjs   (make test-js)
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

const L = createRequire(import.meta.url)("../../web/static/layers.js");
const BINS = [-3, 0, 3];

test("deltaClass: границы порогов", () => {
  assert.equal(L.deltaClass(-3, BINS), 0);
  assert.equal(L.deltaClass(-2.99, BINS), 1);
  assert.equal(L.deltaClass(-0.01, BINS), 1);
  assert.equal(L.deltaClass(0, BINS), 2);
  assert.equal(L.deltaClass(2.99, BINS), 2);
  assert.equal(L.deltaClass(3, BINS), 3);
  assert.equal(L.deltaClass(-10, BINS), 0);
});

test("deltaColor берёт цвет из палитры, подписи строятся из тех же порогов", () => {
  assert.equal(L.deltaColor(-4.25, BINS), L.DELTA_PALETTE[0]);
  assert.equal(L.deltaColor(5, BINS), L.DELTA_PALETTE[3]);
  const items = L.deltaLegendItems(BINS);
  assert.equal(items.length, 4);
  assert.deepEqual(items.map((i) => i.color), L.DELTA_PALETTE);
  assert.equal(items[0].text, "ночью хуже на ≥ 3 дБ");
  assert.equal(items[1].text, "ночью хуже на 0…3 дБ");
  assert.equal(items[3].text, "ночью лучше на ≥ 3 дБ");
  assert.match(L.deltaLegendItems([-5, 0, 2.5])[2].text, /до 2\.5 дБ/);
});

test("окно дня берётся из данных", () => {
  assert.equal(L.dayWindowText("[08:00, 20:00) Asia/Bishkek"), "08:00–20:00");
  assert.equal(L.dayWindowText("[8:30, 21:00) UTC"), "08:30–21:00");
  assert.equal(L.dayShort("[08:00, 20:00) Asia/Bishkek"), "08–20");
  assert.equal(L.labelOf("day", { window: "[07:00, 19:00) X" }), "День (07–19)");
});

test("legendSections: пусто, grid, delta, worseNight", () => {
  const empty = L.legendSections(new Set());
  assert.equal(empty.empty, true);
  assert.equal(empty.rsrp || empty.daynight || empty.heat, false);

  const grid = L.legendSections(new Set(["grid"]));
  assert.equal(grid.rsrp, true);
  assert.equal(grid.daynight, false);

  const delta = L.legendSections(new Set(["delta"]));
  assert.equal(delta.daynight, true);
  assert.equal(delta.delta, true);
  assert.equal(delta.onlyNight, false);
  assert.equal(delta.window, false);
  assert.equal(delta.rsrp, false);

  const worse = L.legendSections(new Set(["worseNight"]));
  assert.equal(worse.daynight, true);
  assert.equal(worse.onlyNight, true);
  assert.equal(worse.delta, false);
});

test("legendSections: день или ночь добавляют пояснение про окно, heat даёт свой раздел", () => {
  assert.equal(L.legendSections(new Set(["day"])).window, true);
  assert.equal(L.legendSections(new Set(["night"])).window, true);
  assert.equal(L.legendSections(new Set(["day"])).rsrp, true);
  assert.equal(L.legendSections(new Set(["heat"])).heat, true);
  assert.equal(L.legendSections(new Set(["grid"])).nodata, true);
  assert.equal(L.legendSections(new Set(["heat"])).nodata, false);
  assert.ok(!L.KEYS.includes("insufficient"));
  assert.equal(L.parseLayersParam("insufficient"), null);
});

test("хеш #layers=…: разбор и запись", () => {
  assert.deepEqual([...L.layersFromHash("#layers=grid,towers,delta")], ["grid", "towers", "delta"]);
  assert.deepEqual([...L.layersFromHash("#ex=C09:technical&layers=grid")], ["grid"]);
  assert.equal(L.layersFromHash("#layers=none").size, 0);
  assert.equal(L.layersFromHash("#layers=bogus"), null);
  assert.equal(L.layersFromHash(""), null);
  assert.deepEqual([...L.layersFromHash("#layers=grid,bogus")], ["grid"]);
  assert.equal(L.serializeLayersParam(new Set()), "none");
  assert.equal(L.serializeLayersParam(new Set(["delta", "grid"])), "grid,delta"); // порядок реестра
});

test("набор по умолчанию и сравнение наборов", () => {
  assert.ok(L.isDefaultSet(new Set(L.DEFAULT_KEYS)));
  assert.ok(!L.isDefaultSet(new Set()));
  for (const k of ["grid", "worst", "towers", "cands", "chosen", "covered"]) assert.ok(L.DEFAULT_KEYS.includes(k), k);
  for (const k of ["heat", "day", "night", "delta", "worseNight"]) assert.ok(!L.DEFAULT_KEYS.includes(k), k);
  const a = L.activeAvailable(new Set(["grid", "delta"]), new Set(["grid"]));
  assert.deepEqual([...a], ["grid"]); // недоступные слои не учитываются
});
