/* Реестр слоёв карты, шкала Δ «ночь − день» и разделы легенды: чистые функции без DOM.
   Подключается и в серверный index, и в автономный map.html; тесты: tests/js/test_legend.mjs (node --test). */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CoverageLayers = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MINUS = "−"; // настоящий минус, не дефис

  // key — стабильный идентификатор (хеш #layers=…, localStorage); label — подпись в переключателе.
  // Подписи зависят от данных (окно дня, n<min_samples, размер клетки), поэтому это функции от ctx.
  const OVERLAYS = [
    { key: "grid", defaultOn: true, label: (c) => `Сетка ${c.cellSize} м (медиана RSRP)` },
    { key: "worst", defaultOn: true, label: () => "Топ-3 худших" },
    { key: "towers", defaultOn: true, label: (c) => `Существующие БС, запрет ${c.exclusionM} м` },
    { key: "cands", defaultOn: true, label: () => "Кандидаты (✕ отказ, 🔒 фиксация)" },
    { key: "chosen", defaultOn: true, label: (c) => `Выбранная пара + ${c.radiusKm} км` },
    // слои сценариев: пустые, пока нет ограничений; включены, как и раньше
    { key: "ghost", defaultOn: true, label: () => "Оптимум по ТЗ (призрак)" },
    { key: "lost", defaultOn: true, label: () => "Потерянные клетки" },
    { key: "gained", defaultOn: false, label: () => "Новые клетки" },
    { key: "covered", defaultOn: true, label: () => "Покрытые красные клетки" },
    { key: "heat", defaultOn: false, label: () => "Тепловая карта" },
    { key: "day", defaultOn: false, label: (c) => `День (${dayShort(c.window)})` },
    { key: "night", defaultOn: false, label: () => "Ночь" },
    { key: "delta", defaultOn: false, label: () => `Δ ночь ${MINUS} день` },
    { key: "worseNight", defaultOn: false, label: () => "Красные только ночью" },
    { key: "insufficient", defaultOn: false, label: (c) => `Недостаточно данных (n<${c.minSamples})` },
    // полевые замеры (только когда они включены в карту)
    { key: "field", defaultOn: true, label: () => "Полевые замеры (◆ метка, ● GPS, ■ вручную, ▲ импорт)" },
  ];
  const KEYS = OVERLAYS.map((o) => o.key);
  const DEFAULT_KEYS = OVERLAYS.filter((o) => o.defaultOn).map((o) => o.key);
  const DAYNIGHT_KEYS = ["day", "night", "delta", "worseNight"];

  // ---- шкала Δ = медиана ночью − медиана днём (дБ): один источник цветов для слоя, легенды и панели ----------

  const DELTA_PALETTE = ["#b91c1c", "#f87171", "#93c5fd", "#1d4ed8"]; // 4 цвета на 3 порога
  const ONLY_NIGHT_COLOR = "#7c3aed";
  const DEFAULT_BINS = [-3, 0, 3];

  function deltaClass(v, bins) {
    const b = bins || DEFAULT_BINS;
    if (v <= b[0]) return 0;
    if (v < b[1]) return 1;
    if (v < b[2]) return 2;
    return 3;
  }

  const deltaColor = (v, bins) => DELTA_PALETTE[deltaClass(v, bins)];

  function deltaLegendItems(bins) {
    const b = bins || DEFAULT_BINS;
    const a = Math.abs(b[0]);
    const c = b[2];
    return [
      { color: DELTA_PALETTE[0], text: `ночью хуже на ≥ ${a} дБ` },
      { color: DELTA_PALETTE[1], text: `ночью хуже на 0…${a} дБ` },
      { color: DELTA_PALETTE[2], text: `без изменений / лучше до ${c} дБ` },
      { color: DELTA_PALETTE[3], text: `ночью лучше на ≥ ${c} дБ` },
    ];
  }

  // ---- окно дня: «[08:00, 20:00) Asia/Bishkek» -> «08:00–20:00» ------------------------------------------------

  function windowParts(w) {
    const m = /\[\s*(\d{1,2}:\d{2})\s*,\s*(\d{1,2}:\d{2})\s*\)/.exec(String(w || ""));
    if (!m) return null;
    const pad = (t) => (t.length === 4 ? "0" + t : t);
    return { start: pad(m[1]), end: pad(m[2]) };
  }

  function dayWindowText(w) {
    const p = windowParts(w);
    return p ? `${p.start}–${p.end}` : String(w || "");
  }

  function dayShort(w) {
    const p = windowParts(w);
    if (!p) return String(w || "день");
    const s = (t) => t.replace(/:00$/, "");
    return `${s(p.start)}–${s(p.end)}`;
  }

  // ---- какие слои доступны и включены ----------------------------------------------------------------------

  /** Подпись слоя по ctx = {cellSize, exclusionM, radiusKm, minSamples, window}. */
  function labelOf(key, ctx) {
    const o = OVERLAYS.find((x) => x.key === key);
    return o ? o.label(ctx || {}) : key;
  }

  /** Включённые слои среди доступных (недоступные в переключателе не показываются и в легенде не учитываются). */
  function activeAvailable(active, available) {
    return new Set(KEYS.filter((k) => active.has(k) && (!available || available.has(k))));
  }

  const sameSet = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));
  const isDefaultSet = (s) => sameSet(s, new Set(DEFAULT_KEYS));

  // ---- хеш #layers=… и значение для localStorage ----------------------------------------------------------

  /** "grid,towers" -> Set; "none" -> пустой Set; мусор -> null. Неизвестные ключи отбрасываются. */
  function parseLayersParam(value) {
    if (value === null || value === undefined) return null;
    const v = String(value).trim();
    if (v === "none") return new Set();
    const keys = v.split(",").filter((k) => KEYS.includes(k));
    return keys.length ? new Set(keys) : null;
  }

  function serializeLayersParam(set) {
    const keys = KEYS.filter((k) => set.has(k));
    return keys.length ? keys.join(",") : "none";
  }

  /** Достаёт значение layers=… из хеша вида «#ex=…&layers=grid,towers». */
  function layersFromHash(hash) {
    for (const kv of String(hash || "").replace(/^#/, "").split("&")) {
      const i = kv.indexOf("=");
      if (i > 0 && kv.slice(0, i) === "layers") return parseLayersParam(kv.slice(i + 1));
    }
    return null;
  }

  // ---- разделы легенды: что показывать при данном наборе включённых слоёв -------------------------------------

  function legendSections(active) {
    const on = (k) => active.has(k);
    const s = {
      rsrp: on("grid") || on("day") || on("night"),
      insufficient: on("insufficient"),
      worst: on("worst"),
      covered: on("covered"),
      towers: on("towers"),
      chosen: on("chosen"),
      cands: on("cands"),
      ghost: on("ghost"),
      lost: on("lost"),
      gained: on("gained"),
      field: on("field"),
      heat: on("heat"),
      daynight: DAYNIGHT_KEYS.some(on),
      delta: on("delta"),
      onlyNight: on("worseNight"),
      window: on("day") || on("night"),
      deltaNote: on("delta"),
    };
    s.empty = ![...active].length;
    return s;
  }

  return {
    MINUS,
    OVERLAYS,
    KEYS,
    DEFAULT_KEYS,
    DAYNIGHT_KEYS,
    DELTA_PALETTE,
    ONLY_NIGHT_COLOR,
    DEFAULT_BINS,
    deltaClass,
    deltaColor,
    deltaLegendItems,
    windowParts,
    dayWindowText,
    dayShort,
    labelOf,
    activeAvailable,
    sameSet,
    isDefaultSet,
    parseLayersParam,
    serializeLayersParam,
    layersFromHash,
    legendSections,
  };
});
