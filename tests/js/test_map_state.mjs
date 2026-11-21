// Ключи MapApp.getState() (web/static/app.js) должны совпадать с примером состояния, который принимает API.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const root = new URL("../../", import.meta.url);
const example = JSON.parse(readFileSync(new URL("tests/fixtures/map_state_example.json", root), "utf8"));
const src = readFileSync(new URL("web/static/app.js", root), "utf8");
const block = src.slice(src.indexOf("getState() {"), src.indexOf("openTab(tab)"));

test("getState отправляет все ключи примера", () => {
  const keys = [...Object.keys(example), ...Object.keys(example.plan)];
  for (const k of keys) assert.ok(new RegExp(`\\b${k}\\b`).test(block), `в getState нет ключа ${k}`);
});
