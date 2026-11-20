/* Карта покрытия: общий код для автономного map.html и веб-режима (FastAPI). Без фреймворков. */
(function () {
  "use strict";

  const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c", grey: "#9ca3af" };
  const CLS_RU = { green: "хорошо", yellow: "средне", red: "слабо" };
  const REASON_RU = {
    malformed: "пустые / неверное число полей",
    unparseable: "нечисловые lat/lon/RSRP",
    bad_timestamp: "время не ISO 8601",
    rsrp_out_of_range: "RSRP вне −140…−44",
    out_of_bounds: "вне границ района",
    duplicates: "дубли",
  };
  const SERVER = document.body.classList.contains("server");
  // На устройствах без наведения (телефоны) подсказки клеток открываются по тапу: popup вместо tooltip.
  const TOUCH = !!(window.matchMedia && window.matchMedia("(hover: none)").matches);
  L.Layer.include({
    bindInfo(html) {
      return TOUCH ? this.bindPopup(html) : this.bindTooltip(html);
    },
  });
  // Снимок страницы до инициализации карты — для «Скачать map.html» в автономном режиме.
  const ORIGINAL_HTML = "<!doctype html>\n" + document.documentElement.outerHTML;
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d).replace("-", "−"));

  let map, layerControl, legend;
  let groups = {};
  let current = null; // текущие данные карты
  let analysisId = null; // id анализа загруженных файлов (веб-режим)

  function initMap() {
    // Стартовый вид нужен, чтобы карта была «загружена» и сама отслеживала размер контейнера
    // (иначе при нулевом размере на первом рендере getSize() навсегда остаётся 0×0).
    map = L.map("map", { preferCanvas: true, zoomControl: true, center: [0, 0], zoom: 0 });
    const osmAttr = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
    if (!SERVER) {
      // Тайлы OSM блокируют запросы без Referer (map.html, открытый двойным кликом),
      // поэтому в автономной карте берём подложку Esri World Street Map: она без ключа и работает с file://.
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19,
        attribution: "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, " + osmAttr,
      }).addTo(map);
    } else {
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: osmAttr }).addTo(map);
    }
    L.control.scale({ imperial: false }).addTo(map);
    // Пока контейнер карты имеет нулевой размер (вёрстка ещё не готова, вкладка в фоне),
    // fitBounds уходит в максимальный зум. Поэтому подгоняем вид, когда размер появится.
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        map.invalidateSize();
        if (!fitted) tryFit();
      }).observe(map.getContainer());
    }
  }

  let fitTarget = null; // границы района для начального вида
  let fitted = false;

  function tryFit() {
    const el = map.getContainer();
    if (!fitTarget || !el.clientWidth || !el.clientHeight) return;
    map.invalidateSize(false);
    map.fitBounds(fitTarget, { padding: [10, 10] });
    fitted = true;
  }

  function cellTip(c) {
    const m = current.meta;
    return `<b>${esc(c.id)}</b><br>медиана: ${fmt(c.m, 2)} дБм<br>замеров: ${c.n}<br>класс: ${c.counted === false ? "не учитывается (n&lt;" + m.min_samples + ")" : CLS_RU[c.cls]}${c.n_field !== undefined ? "<br>в т.ч. полевых: " + c.n_field : ""}`;
  }

  function rect(b, style) {
    return L.rectangle([[b[0], b[1]], [b[2], b[3]]], style);
  }

  function clearLayers() {
    Object.values(groups).forEach((g) => map.removeLayer(g));
    groups = {};
    if (layerControl) map.removeControl(layerControl);
    if (legend) map.removeControl(legend);
  }

  function buildLayers(d, opts = {}) {
    // при обновлении данных без перезагрузки сохраняем, какие слои включил пользователь
    const keepVisible = opts.keepView ? Object.keys(groups).filter((k) => map.hasLayer(groups[k])) : null;
    clearLayers();
    const m = d.meta;

    // 1. Сетка (только n >= min_samples)
    const grid = L.featureGroup();
    const insufficient = L.featureGroup();
    const insufficientStyle = { color: "#777", weight: 0.4, dashArray: "2 2", fillColor: COLORS.grey, fillOpacity: 0.35 };
    (d.empty_cells || []).forEach((c) => {
      rect(c.b, insufficientStyle).bindInfo(`<b>${esc(c.id)}</b><br>Замеров: 0<br>Нет данных`).addTo(insufficient);
    });
    d.cells.forEach((c) => {
      if (c.counted) {
        rect(c.b, { color: "#555", weight: 0.4, fillColor: COLORS[c.cls], fillOpacity: 0.5 }).bindInfo(cellTip(c)).addTo(grid);
      } else {
        rect(c.b, insufficientStyle).bindInfo(cellTip(c)).addTo(insufficient);
      }
    });

    // 2. Топ-3 худших
    const worst = L.featureGroup();
    d.cells.filter((c) => c.rank).sort((a, b) => a.rank - b.rank).forEach((c) => {
      rect(c.b, { color: "#000", weight: 3.5, fill: false })
        .bindTooltip(`#${c.rank}`, { permanent: true, direction: "center", className: "rank-label" })
        .addTo(worst);
    });

    // 3. Существующие БС и зоны запрета 500 м
    const towers = L.featureGroup();
    d.towers.forEach((t) => {
      L.circle([t.lat, t.lon], { radius: m.tower_exclusion_m, color: "#374151", weight: 1.5, dashArray: "6 6", fillOpacity: 0.05 }).addTo(towers);
      L.marker([t.lat, t.lon], { icon: L.divIcon({ className: "tower-icon", html: "📡", iconSize: [22, 22] }) })
        .bindPopup(`<b>БС ${esc(t.id)}</b><br>зона запрета ${m.tower_exclusion_m} м`)
        .addTo(towers);
    });

    // 4-5. Кандидаты, пара, призрак оптимума, потерянные/новые/покрытые клетки.
    // Группы постоянные: renderScenarioLayers() перезаполняет их при каждом изменении сценария.
    const cands = L.featureGroup();
    const chosen = L.featureGroup();
    const covered = L.featureGroup();
    const ghost = L.featureGroup();
    const lost = L.featureGroup();
    const gained = L.featureGroup();

    // 6. Бонусы: тепловая карта, день/ночь
    const heat = L.heatLayer ? L.heatLayer(d.heat, { radius: 14, blur: 16, maxZoom: 17, minOpacity: 0.25 }) : L.layerGroup();
    const dn = d.daynight || {};
    const dnLayer = (cells) => {
      const g = L.featureGroup();
      (cells || []).forEach((c) => rect(c.b, { color: "#555", weight: 0.4, fillColor: COLORS[c.cls], fillOpacity: 0.55 }).bindInfo(cellTip(c)).addTo(g));
      return g;
    };
    const day = dnLayer(dn.day_cells);
    const night = dnLayer(dn.night_cells);
    const delta = L.featureGroup();
    (dn.delta || []).forEach((c) => {
      const v = c.d;
      const col = v <= -3 ? "#b91c1c" : v < 0 ? "#f87171" : v < 3 ? "#93c5fd" : "#1d4ed8";
      rect(c.b, { color: "#555", weight: 0.3, fillColor: col, fillOpacity: 0.6 }).bindInfo(`<b>${esc(c.id)}</b><br>Δ ночь − день: ${fmt(v, 2)} дБ`).addTo(delta);
    });
    const worseNight = L.featureGroup();
    const onlyNight = new Set(dn.red_only_night || []);
    d.cells.filter((c) => onlyNight.has(c.id)).forEach((c) => {
      rect(c.b, { color: "#7c3aed", weight: 2, fillColor: "#7c3aed", fillOpacity: 0.25 }).bindInfo(`<b>${esc(c.id)}</b><br>красная только ночью`).addTo(worseNight);
    });

    const fieldLayer = buildFieldLayer(d);
    groups = { field: fieldLayer, grid, insufficient, worst, towers, cands, chosen, covered, ghost, lost, gained, heat, day, night, delta, worseNight };
    [insufficient, grid, covered, lost, towers, cands, ghost, chosen, worst].forEach((g) => g.addTo(map));

    layerControl = L.control.layers(null, {
      "Сетка 200 м (медиана RSRP)": grid,
      "Топ-3 худших": worst,
      "Существующие БС, запрет 500 м": towers,
      "Кандидаты (✕ отказ, 🔒 фиксация)": cands,
      "Выбранная пара + 1 км": chosen,
      "Оптимум по ТЗ (призрак)": ghost,
      "Потерянные клетки": lost,
      "Новые клетки": gained,
      "Покрытые красные клетки": covered,
      "Тепловая карта": heat,
      "День (08–20)": day,
      "Ночь": night,
      "Δ ночь − день": delta,
      "Красные только ночью": worseNight,
      [`Недостаточно данных (n<${m.min_samples})`]: insufficient,
    }, { collapsed: window.innerWidth < 760 }).addTo(map);

    if (m.field_included) {
      layerControl.addOverlay(fieldLayer, "Полевые замеры (◆ метка, ● GPS, ■ вручную, ▲ импорт)");
      fieldLayer.addTo(map);
    }
    if (keepVisible) {
      Object.entries(groups).forEach(([k, g]) => {
        if (k !== "field" && !keepVisible.includes(k)) map.removeLayer(g);
      });
    }

    legend = L.control({ position: window.matchMedia("(max-width: 760px)").matches ? "bottomleft" : "bottomright" });
    legend.onAdd = () => {
      const div = L.DomUtil.create("div", "legend");
      let body =
        `<b>Медиана RSRP</b><br>` +
        `<i style="background:${COLORS.green}"></i>≥ ${m.green_min} дБм<br>` +
        `<i style="background:${COLORS.yellow}"></i>${m.red_below} … ${m.green_min}<br>` +
        `<i style="background:${COLORS.red}"></i>&lt; ${m.red_below} (слабая зона)<br>` +
        `<i style="background:${COLORS.grey}"></i>n &lt; ${m.min_samples}, не учитывается<br>` +
        `<span class="line" style="border-top:2px solid #1d4ed8"></span>покрыта парой<br>` +
        `<span class="line" style="border-top:2px dashed #374151"></span>запрет ${m.tower_exclusion_m} м от БС<br>` +
        `<span style="color:#059669">★</span> выбранная площадка, круг ${m.coverage_radius_m / 1000} км<br>` +
        `<span style="color:#b91c1c;font-weight:800">✕</span> отказ &nbsp; 🔒 зафиксирована<br>` +
        `<span class="line" style="border-top:2px dashed #6b7280"></span>оптимум по ТЗ («призрак»)<br>` +
        `<i style="background:rgba(124,58,237,.35);border-color:#7c3aed"></i>потерянные клетки<br>` +
        `<i style="background:rgba(8,145,178,.35);border-color:#0891b2"></i>новые клетки`;
      if (m.field_included) body += `<br>◆ метка · ● GPS · ■ вручную · ▲ импорт (полевые)`;
      div.innerHTML = `<button type="button" class="legend-toggle" aria-label="Легенда" aria-expanded="false">ⓘ</button><div class="legend-body">${body}</div>`;
      const tgl = div.querySelector(".legend-toggle");
      tgl.addEventListener("click", () => {
        const open = div.classList.toggle("open");
        tgl.setAttribute("aria-expanded", String(open));
      });
      L.DomEvent.disableClickPropagation(div);
      return div;
    };
    legend.addTo(map);

    const b = m.bounds;
    fitTarget = [[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]];
    if (!opts.keepView) {
      fitted = false;
      tryFit();
    }
    L.rectangle([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], { color: "#111", weight: 1, dashArray: "4 4", fill: false, interactive: false }).addTo(grid);
    renderScenarioLayers();
  }

  function fillPanel(d) {
    const r = d.result;
    $("#k-pair").textContent = r.best_candidates.join(" + ") || "—";
    $("#k-covered").textContent = r.covered_weak_cells;
    $("#k-dropped").textContent = r.dropped_rows;
    $("#ss-pair").textContent = r.best_candidates.join(" + ") || "—";
    $("#ss-cov").textContent = `${r.covered_weak_cells} красных`;
    $("#ss-drop").textContent = `dropped ${r.dropped_rows}`;

    const cl = d.cleaning;
    let rows = `<tr><td>Строк всего</td><td>${cl.total}</td></tr>`;
    Object.entries(cl.by_reason).forEach(([k, v]) => {
      rows += `<tr><td>&nbsp;&nbsp;${esc(REASON_RU[k] || k)}</td><td>${v}</td></tr>`;
    });
    rows += `<tr><td>Оставлено</td><td>${cl.kept}</td></tr><tr class="total"><td>dropped_rows</td><td>${cl.dropped}</td></tr>`;
    $("#t-clean").innerHTML = rows;

    const s = d.stats;
    $("#cell-size").textContent = `${d.meta.cell_size_m}×${d.meta.cell_size_m} м`;
    const tot = s.cells_counted || 1;
    $("#cls-bars").innerHTML = ["green", "yellow", "red"]
      .map((k) => `<span style="flex:${s[k]};background:${COLORS[k]}" title="${CLS_RU[k]}: ${s[k]}">${s[k] / tot > 0.06 ? s[k] : ""}</span>`)
      .join("");
    $("#cells-note").textContent = `Клеток с замерами: ${s.cells_total}; учитываются (n ≥ ${d.meta.min_samples}): ${s.cells_counted}; зелёных ${s.green}, жёлтых ${s.yellow}, красных ${s.red}; отброшено по n: ${s.cells_insufficient}.`;

    const byId = Object.fromEntries(d.cells.map((c) => [c.id, c]));
    $("#t-worst tbody").innerHTML = d.worst
      .map((w) => `<tr data-cell="${esc(w.cell_id)}"><td>${w.rank}</td><td>${esc(w.cell_id)}</td><td>${fmt(w.median_rsrp, 2)}</td><td>${w.n}</td></tr>`)
      .join("");
    document.querySelectorAll("#t-worst tbody tr").forEach((tr) => {
      tr.addEventListener("click", () => {
        const c = byId[tr.dataset.cell];
        if (c) map.flyTo(c.c, 17, { duration: 0.8 });
      });
    });

    const bf = d.compare.brute_force, gr = d.compare.greedy;
    $("#t-compare tbody").innerHTML =
      `<tr class="sel"><td>перебор (${bf.pairs_evaluated} пар)</td><td>${bf.pair.join("+")}</td><td>${bf.covered}</td><td>${fmt(bf.elapsed_ms, 2)}</td></tr>` +
      `<tr><td>жадный</td><td>${gr.pair.join("+")}</td><td>${gr.covered}</td><td>${fmt(gr.elapsed_ms, 2)}</td></tr>`;
    const gap = bf.covered - gr.covered;
    $("#compare-note").innerHTML =
      gap > 0
        ? `Перебор лучше жадного на <b>${gap}</b> клет. Жадный взял сначала ${esc(gr.steps[0].pick)} (+${gr.steps[0].gain}), затем ${esc(gr.steps[1].pick)} (+${gr.steps[1].gain}).`
        : "Жадный алгоритм совпал с оптимумом перебора.";
    if (bf.ties && bf.ties.length > 1) {
      $("#compare-note").innerHTML += `<br><span class="muted">Ничья у ${bf.ties.length} пар — выбрана лексикографически меньшая.</span>`;
    }

    renderCandidatesTable();

    const dn = d.daynight || {};
    if (!dn.available) {
      $("#dn-summary").innerHTML = '<span class="muted">Недостаточно замеров с временем для сравнения.</span>';
    } else {
      const sh = (x) => `${Math.round(x.shares.green * 100)}% / ${Math.round(x.shares.yellow * 100)}% / ${Math.round(x.shares.red * 100)}%`;
      $("#dn-summary").innerHTML =
        `<table class="grid-t"><thead><tr><th></th><th>замеров</th><th>клеток</th><th>зел/жёл/крас</th></tr></thead><tbody>` +
        `<tr><td>день</td><td>${dn.rows_day}</td><td>${dn.day.cells_counted}</td><td>${sh(dn.day)}</td></tr>` +
        `<tr><td>ночь</td><td>${dn.rows_night}</td><td>${dn.night.cells_counted}</td><td>${sh(dn.night)}</td></tr></tbody></table>` +
        `<p>Общих клеток: ${dn.common_cells}; медиана Δ (ночь − день): <b>${fmt(dn.median_delta_db, 2)} дБ</b>. ` +
        `Красные только ночью: ${dn.red_only_night.length}, только днём: ${dn.red_only_day.length}.</p>` +
        `<p class="muted">Окно дня: ${esc(dn.window)}. Слои «День», «Ночь», «Δ» — в переключателе.</p>`;
    }
  }

  function render(d, opts = {}) {
    current = d;
    $("#err").classList.add("hidden");
    resetScenarioForDataset(d);
    buildLayers(d, opts);
    fillPanel(d);
    afterDatasetRendered();
    renderFieldBlock(d);
    if (opts.highlight && d.meta.field_included) highlightDiff(d);
  }

  function showError(msg) {
    const e = $("#err");
    e.textContent = msg;
    e.classList.remove("hidden");
  }

  function download(name, text, type) {
    const blob = new Blob([text], { type });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  function bindButtons() {
    $("#dl-result").addEventListener("click", () => {
      if (current) download("result.json", JSON.stringify(current.result, null, 2) + "\n", "application/json");
    });
    $("#dl-map").addEventListener("click", () => {
      if (SERVER) {
        window.location.href = "/api/download/map.html" + (analysisId ? `?analysis_id=${encodeURIComponent(analysisId)}` : "");
      } else {
        download("map.html", ORIGINAL_HTML, "text/html");
      }
    });
    $("#panel-toggle").addEventListener("click", () => {
      $("#panel").classList.toggle("collapsed");
      setTimeout(() => map.invalidateSize(), 250);
    });
  }

  async function loadDefault(opts = {}) {
    const res = await fetch(fieldOn ? "/api/mapdata?include_field=true" : "/api/mapdata");
    if (!res.ok) throw new Error(`Ошибка API ${res.status}: ${await res.text()}`);
    analysisId = null;
    render(await res.json(), opts);
  }

  function bindUpload() {
    const form = $("#upload");
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const status = $("#upload-status");
      status.textContent = "Считаю…";
      const fd = new FormData(form);
      for (const [k, v] of [...fd.entries()]) {
        if ((v instanceof File && v.size === 0 && k === "bounds") || v === "") fd.delete(k);
      }
      try {
        const res = await fetch("/api/analyze", { method: "POST", body: fd });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
          throw new Error(`Ошибка ${res.status}: ${detail}`);
        }
        analysisId = body.analysis_id;
        setFieldToggle(false);
        render(body.map_data);
        status.textContent = `Готово: dropped_rows=${body.result.dropped_rows}, пара ${body.result.best_candidates.join("+")}.`;
      } catch (e) {
        status.textContent = "";
        showError(e.message);
      }
    });
    $("#reset-default").addEventListener("click", () => loadDefault().catch((e) => showError(e.message)));
  }

  // ===================================================================================
  // Сценарии отказов: состояние, пересчёт (API или встроенный движок), панель, слои, ручной режим
  // ===================================================================================
  const SC = window.CoverageScenario;
  const CATEGORY_RU = {
    owner: "собственник/арендатор против",
    permit: "нет разрешения",
    technical: "технически невозможно",
    other: "другое",
  };
  const STORAGE_KEY = "coverage-scenario-v1";
  const MAX_REQUIRED = 2;
  const TOP_N = 10;
  const DEBOUNCE_MS = 150;
  const MINUS = "−";

  const scenario = {
    excluded: [], // [{id, reason, category}]
    required: [], // [id]
    selected: null, // пара, выбранная кнопкой «Выбрать»
    preview: null, // пара для предпросмотра (клик по строке)
    manual: false,
    manualPick: [],
    manualResult: null,
    result: null, // ответ /api/scenario (или встроенного движка)
  };
  let impact = null; // {id: потеря при отказе}
  let scTimer = null;
  let scAbort = null;
  let scSeq = 0;
  let scCache = null; // {covers, optCells, cellById}

  const signed = (v) => (v < 0 ? MINUS + Math.abs(v) : v > 0 ? "+" + v : "0");
  const pairText = (p) => (p && p.length ? p.join(" + ") : "—");
  const samePair = (a, b) => !!a && !!b && a[0] === b[0] && a[1] === b[1];
  const plural = (n, one, few, many) => {
    const k = n % 100;
    if (k >= 11 && k <= 14) return many;
    return n % 10 === 1 ? one : n % 10 >= 2 && n % 10 <= 4 ? few : many;
  };
  const refusalOf = (id) => scenario.excluded.find((r) => r.id === id);
  const statusOf = (id) => (refusalOf(id) ? "excluded" : scenario.required.includes(id) ? "required" : "available");
  const constraintsPayload = () => ({ excluded: scenario.excluded.map((r) => ({ ...r })), required: [...scenario.required] });
  const hasConstraints = () => scenario.excluded.length > 0 || scenario.required.length > 0;

  function cacheFor(d) {
    if (!scCache || scCache.data !== d) {
      const covers = SC.coversOf(d);
      scCache = { data: d, covers, optCells: SC.optimumOf(d, covers).cells, cellById: Object.fromEntries(d.cells.map((c) => [c.id, c])) };
    }
    return scCache;
  }

  function pairInfo(pair) {
    const c = cacheFor(current);
    try {
      return SC.evaluatePair(c.covers, pair[0], pair[1], c.optCells);
    } catch (e) {
      return null;
    }
  }

  function displayPair() {
    const r = scenario.result;
    if (scenario.preview) return scenario.preview;
    if (scenario.selected) return scenario.selected;
    if (r) return r.feasible && r.best ? r.best.pair : null;
    return current.result.best_candidates;
  }

  // ---- ограничения: валидация, хеш, localStorage ----------------------------------------------

  function sanitize(raw, d) {
    /* Оставляет только корректные ограничения для набора d; возвращает {constraints, dropped[]}. */
    const known = new Map(d.candidates.map((c) => [c.id, c]));
    const dropped = [];
    const excluded = [];
    (raw.excluded || []).forEach((item) => {
      const r = typeof item === "string" ? { id: item } : item || {};
      if (!known.has(r.id)) return dropped.push(String(r.id));
      if (excluded.some((x) => x.id === r.id)) return;
      excluded.push({
        id: r.id,
        reason: String(r.reason || "").slice(0, 200),
        category: SC.CATEGORIES.includes(r.category) ? r.category : "other",
      });
    });
    const required = [];
    (raw.required || []).forEach((id) => {
      const c = known.get(id);
      if (!c || !c.admissible || excluded.some((x) => x.id === id) || required.includes(id) || required.length >= MAX_REQUIRED) {
        if (!dropped.includes(String(id))) dropped.push(String(id));
        return;
      }
      required.push(id);
    });
    return { constraints: { excluded, required }, dropped };
  }

  function hashFromState() {
    const parts = [];
    if (scenario.excluded.length) {
      parts.push("ex=" + scenario.excluded.map((r) => [r.id, r.category, r.reason ? encodeURIComponent(r.reason) : ""].join(":").replace(/:$/, "")).join(","));
    }
    if (scenario.required.length) parts.push("req=" + scenario.required.join(","));
    return parts.join("&");
  }

  function parseHash(hash) {
    const out = { excluded: [], required: [] };
    String(hash || "")
      .replace(/^#/, "")
      .split("&")
      .forEach((kv) => {
        const i = kv.indexOf("=");
        if (i < 0) return;
        const key = kv.slice(0, i);
        const val = kv.slice(i + 1);
        if (key === "ex") {
          val.split(",").filter(Boolean).forEach((e) => {
            const [id, category, reason] = e.split(":");
            let text = "";
            try {
              text = reason ? decodeURIComponent(reason) : "";
            } catch (err) {
              text = "";
            }
            out.excluded.push({ id, category: category || "other", reason: text });
          });
        } else if (key === "req") {
          out.required = val.split(",").filter(Boolean);
        }
      });
    return out;
  }

  function persist() {
    const h = hashFromState();
    try {
      history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
    } catch (e) {
      /* data:/file: — хеш недоступен, не критично */
    }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(constraintsPayload()));
    } catch (e) {
      /* приватный режим и т. п. */
    }
  }

  function loadStored() {
    const fromHash = parseHash(location.hash);
    if (fromHash.excluded.length || fromHash.required.length) return fromHash;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) {
      /* нет доступа к storage */
    }
    return { excluded: [], required: [] };
  }

  let firstDataset = true;

  function resetScenarioForDataset(d) {
    /* Вызывается из render(): при новом наборе сбрасываем ограничения с неизвестными id и сообщаем об этом. */
    const source = firstDataset ? loadStored() : constraintsPayload();
    const { constraints, dropped } = sanitize(source, d);
    scenario.excluded = constraints.excluded;
    scenario.required = constraints.required;
    scenario.selected = null;
    scenario.preview = null;
    scenario.result = null;
    scenario.manualPick = [];
    scenario.manualResult = null;
    impact = null;
    scCache = null;
    firstDataset = false;
    pendingNote = dropped.length
      ? `Сброшены ограничения с неизвестными или недопустимыми площадками: ${dropped.join(", ")}.`
      : "";
  }

  let pendingNote = "";

  function afterDatasetRendered() {
    renderManualCard();
    $("#manual-toggle").checked = scenario.manual;
    setStatusText(pendingNote);
    persist();
    scheduleScenario(true);
  }

  function setStatusText(text) {
    $("#sc-status").textContent = text || "";
  }

  // ---- пересчёт ---------------------------------------------------------------------------------

  async function postJSON(url, body, signal) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(detailText(data.detail !== undefined ? data.detail : data));
    return data;
  }

  function detailText(d) {
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && x.msg ? `${(x.loc || []).slice(1).join(".")}: ${x.msg}` : JSON.stringify(x))).join("; ");
    return JSON.stringify(d);
  }

  function setBusy(on) {
    document.body.classList.toggle("busy", on);
  }

  function scheduleScenario(immediate) {
    clearTimeout(scTimer);
    setBusy(true);
    scTimer = setTimeout(runScenarioNow, immediate ? 0 : DEBOUNCE_MS);
  }

  async function runScenarioNow() {
    const seq = ++scSeq;
    if (scAbort) scAbort.abort();
    const ctrl = new AbortController();
    scAbort = ctrl;
    try {
      const res = SERVER && !fieldOn
        ? await postJSON("/api/scenario", { analysis_id: analysisId, ...constraintsPayload(), top_n: TOP_N }, ctrl.signal)
        : SC.runScenario(current, constraintsPayload(), TOP_N);
      if (seq !== scSeq) return;
      scenario.result = res;
      scenarioError(null);
      const impactChanged = JSON.stringify(impact) !== JSON.stringify(res.candidate_impact);
      impact = res.candidate_impact;
      if (impactChanged) renderCandidatesTable();
      renderScenario();
    } catch (e) {
      if (e.name === "AbortError" || seq !== scSeq) return;
      scenarioError(e.message);
    } finally {
      if (seq === scSeq) setBusy(false);
    }
  }

  function scenarioError(msg) {
    const el = $("#sc-message");
    if (msg) {
      el.textContent = msg;
      el.classList.remove("hidden");
    } else if (!scenario.result || scenario.result.feasible) {
      el.classList.add("hidden");
    }
  }

  function constraintsChanged() {
    scenario.preview = null;
    const sel = scenario.selected;
    if (sel && (sel.some((id) => refusalOf(id)) || !scenario.required.every((id) => sel.includes(id)))) scenario.selected = null;
    persist();
    renderScenarioLayers();
    scheduleScenario(false);
  }

  function setStatus(id, status) {
    const c = current.candidates.find((x) => x.id === id);
    if (!c || !c.admissible) return;
    if (status === "required" && statusOf(id) !== "required" && scenario.required.length >= MAX_REQUIRED) return;
    scenario.excluded = scenario.excluded.filter((r) => r.id !== id);
    scenario.required = scenario.required.filter((x) => x !== id);
    if (status === "excluded") scenario.excluded.push({ id, reason: "", category: "other" });
    if (status === "required") scenario.required.push(id);
    renderCandidatesTable();
    constraintsChanged();
    if (status === "excluded") {
      const input = document.querySelector(`tr.refusal-row[data-for="${CSS.escape(id)}"] input`);
      if (input) input.focus();
    }
  }

  // ---- таблица кандидатов ----------------------------------------------------------------------

  function renderCandidatesTable() {
    if (!current) return;
    const full = scenario.required.length >= MAX_REQUIRED;
    const rows = [];
    current.candidates.forEach((c) => {
      const st = statusOf(c.id);
      let seg;
      if (c.admissible) {
        const btn = (key, label, title, disabled) =>
          `<button type="button" data-act="status" data-id="${esc(c.id)}" data-st="${key}" class="${st === key ? "on" : ""}" aria-pressed="${st === key}" title="${esc(title)}"${disabled ? " disabled" : ""}>${label}</button>`;
        seg =
          `<span class="seg" role="group" aria-label="Статус площадки ${esc(c.id)}">` +
          btn("available", "Доступна", "Площадка доступна", false) +
          btn("excluded", "Отказ", "Отказ: пересчёт без этой площадки", false) +
          btn("required", "Зафикс.", full && st !== "required" ? "Уже зафиксированы две площадки" : "Зафиксирована: подбирается партнёр", full && st !== "required") +
          `</span>`;
      } else {
        const tip = `исключена по ТЗ: ${fmt(c.nearest_tower_m, 0)} м до ${c.nearest_tower}`;
        seg = `<span class="seg" title="${esc(tip)}"><button type="button" disabled data-st="available">Доступна</button><button type="button" disabled data-st="excluded">Отказ</button><button type="button" disabled data-st="required">Зафикс.</button></span> <span class="tag no" title="${esc(tip)}">по ТЗ</span>`;
      }
      const v = impact && c.admissible ? impact[c.id] : undefined;
      const loss = v === undefined ? "—" : v < 0 ? MINUS + Math.abs(v) : "0";
      rows.push(
        `<tr class="cand st-${st}" data-id="${esc(c.id)}" data-lat="${c.lat}" data-lon="${c.lon}"><td class="id" title="Показать на карте">${esc(c.id)}</td><td>${seg}</td><td>${fmt(c.nearest_tower_m, 0)}</td><td>${c.covers_red}</td><td class="loss ${v < 0 ? "neg" : "zero"}">${loss}</td></tr>`
      );
      if (st === "excluded") {
        const r = refusalOf(c.id);
        const opts = SC.CATEGORIES.map((k) => `<option value="${k}"${r.category === k ? " selected" : ""}>${esc(CATEGORY_RU[k])}</option>`).join("");
        rows.push(
          `<tr class="refusal-row" data-for="${esc(c.id)}"><td colspan="5"><div class="refusal-form"><input type="text" maxlength="200" placeholder="причина отказа" aria-label="Причина отказа ${esc(c.id)}" value="${esc(r.reason)}"><select aria-label="Категория отказа ${esc(c.id)}">${opts}</select></div></td></tr>`
        );
      }
    });
    $("#t-cands tbody").innerHTML = rows.join("");
    updateCandidateHighlights();
  }

  function updateCandidateHighlights() {
    const pair = displayPair() || [];
    document.querySelectorAll("#t-cands tbody tr.cand").forEach((tr) => tr.classList.toggle("sel", pair.includes(tr.dataset.id)));
  }

  function bindCandidatesTable() {
    const tbody = $("#t-cands tbody");
    tbody.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-act=status]");
      if (b) return setStatus(b.dataset.id, b.dataset.st);
      const idCell = ev.target.closest("td.id");
      if (idCell) {
        const tr = idCell.closest("tr");
        map.flyTo([+tr.dataset.lat, +tr.dataset.lon], 16, { duration: 0.8 });
      }
    });
    tbody.addEventListener("input", (ev) => {
      const tr = ev.target.closest("tr.refusal-row");
      const r = tr && refusalOf(tr.dataset.for);
      if (!r || ev.target.tagName !== "INPUT") return;
      r.reason = ev.target.value.slice(0, 200);
      constraintsChanged();
    });
    tbody.addEventListener("change", (ev) => {
      const tr = ev.target.closest("tr.refusal-row");
      const r = tr && refusalOf(tr.dataset.for);
      if (!r || ev.target.tagName !== "SELECT") return;
      r.category = ev.target.value;
      constraintsChanged();
    });
  }

  // ---- панель «Сценарий» -----------------------------------------------------------------------

  function deltaHtml(delta, base) {
    if (!delta) return '<span class="delta zero">Δ 0</span>';
    const pct = base ? ` (${MINUS}${Math.abs((delta / base) * 100).toFixed(1)}%)` : "";
    return `<span class="delta neg">Δ ${signed(delta)}${delta < 0 ? pct : ""}</span>`;
  }

  function renderScenario() {
    const r = scenario.result;
    if (!r) return;
    const opt = r.optimum;
    const cons = hasConstraints();
    const shown = displayPair();
    const best = r.best;

    let card2;
    if (r.feasible && best) {
      const onMap = shown && !samePair(shown, best.pair) ? pairInfo(shown) : null;
      const where = onMap
        ? `<div class="sc-sub">На карте: <b>${esc(pairText(onMap.pair))}</b>, ${onMap.covered} (${scenario.preview ? "предпросмотр" : "выбрана"}), Δ ${signed(onMap.delta_vs_optimum)}</div>`
        : "";
      card2 = `<div class="sc-card scenario"><div class="sc-t">С учётом отказов</div><div class="sc-pair">${esc(pairText(best.pair))}</div><div class="sc-n">${best.covered} ${deltaHtml(best.delta_vs_optimum, opt.covered)}</div>${cons ? "" : '<div class="sc-sub">ограничений нет</div>'}${where}</div>`;
    } else {
      card2 = `<div class="sc-card infeasible"><div class="sc-t">С учётом отказов</div><div class="sc-pair">—</div><div class="sc-sub">пара не найдена</div></div>`;
    }
    $("#sc-cards").innerHTML =
      `<div class="sc-card"><div class="sc-t">${current.meta.field_included ? "Оптимум с полевыми" : "Оптимум по ТЗ"}</div><div class="sc-pair">${esc(pairText(opt.pair))}</div><div class="sc-n">${opt.covered} красных клеток</div><div class="sc-sub">${current.meta.field_included ? "с учётом полевых замеров; result.json: " + esc(current.result.best_candidates.join(" + ")) : "это значение в result.json"}</div></div>` + card2;

    $("#sc-notice").classList.toggle("hidden", !cons);
    const msg = $("#sc-message");
    if (!r.feasible) {
      msg.textContent = r.message || "Сценарий невыполним.";
      msg.classList.remove("hidden");
    } else {
      msg.classList.add("hidden");
    }

    // варианты замены
    const reps = r.replacements || [];
    $("#sc-replacements").innerHTML = reps.length
      ? "<h3>Варианты замены</h3>" +
        reps
          .map((x) => {
            const label = x.kind === "keep_partner" ? `Заменить только ${esc(x.replaced)} (оставляем ${esc(x.kept)})` : "Пересобрать пару целиком";
            return `<div class="rep${x.recommended ? " rec" : ""}" data-pair="${x.pair.join(",")}"><span>${label}: <b>${esc(pairText(x.pair))}</b>, ${x.covered} ${deltaHtml(x.delta, opt.covered)}</span>${x.recommended ? '<span class="rep-badge" title="меньше всего меняет план">★ рекомендуется</span>' : ""}<button class="mini" type="button" data-act="select-pair" data-pair="${x.pair.join(",")}">Выбрать</button></div>`;
          })
          .join("")
      : "";

    // равноценные пары
    const ties = r.ties || [];
    $("#sc-ties").innerHTML =
      ties.length > 1
        ? `<h3>${ties.length} ${plural(ties.length, "равноценная пара", "равноценные пары", "равноценных пар")} по ${ties[0].covered} клеток</h3><div class="chips">` +
          ties.map((t) => `<button type="button" class="chip${samePair(shown, t.pair) ? " on" : ""}" data-act="select-pair" data-pair="${t.pair.join(",")}">${esc(pairText(t.pair))}</button>`).join("") +
          "</div>"
        : "";

    // альтернативы
    const alts = r.alternatives || [];
    $("#sc-alts-wrap").classList.toggle("hidden", !alts.length);
    $("#sc-alts-count").textContent = alts.length ? `топ-${alts.length}` : "";
    $("#t-alts tbody").innerHTML = alts
      .map((a, i) => {
        const cls = [samePair(scenario.preview, a.pair) ? "preview" : "", samePair(shown, a.pair) ? "cur" : ""].join(" ");
        return `<tr class="${cls}" data-pair="${a.pair.join(",")}"><td>${i + 1}</td><td>${esc(pairText(a.pair))}</td><td>${a.covered}</td><td>${signed(a.delta_vs_optimum)}</td><td>${a.overlap}</td><td><button class="mini" type="button" data-act="select-pair" data-pair="${a.pair.join(",")}">Выбрать</button></td></tr>`;
      })
      .join("");

    renderScenarioLayers();
    updateCandidateHighlights();
  }

  function bindScenarioPanel() {
    const card = $("#scenario-card");
    card.addEventListener("click", (ev) => {
      const sel = ev.target.closest("[data-act=select-pair]");
      if (sel) {
        scenario.selected = sel.dataset.pair.split(",");
        scenario.preview = null;
        renderScenario();
        return;
      }
      const row = ev.target.closest("#t-alts tbody tr, .rep");
      if (row && row.dataset.pair) {
        const p = row.dataset.pair.split(",");
        scenario.preview = samePair(scenario.preview, p) ? null : p;
        renderScenario();
      }
    });
    $("#sc-reset").addEventListener("click", () => {
      scenario.excluded = [];
      scenario.required = [];
      scenario.selected = null;
      scenario.manualResult = null;
      renderManualCard();
      renderCandidatesTable();
      constraintsChanged();
      setStatusText("Ограничения сброшены.");
    });
    $("#sc-download").addEventListener("click", downloadScenario);
    $("#sc-upload").addEventListener("change", uploadScenario);
    $("#manual-toggle").addEventListener("change", (ev) => setManual(ev.target.checked));
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && scenario.manual) setManual(false);
    });
    document.addEventListener("click", (ev) => {
      const b = ev.target.closest(".popup-actions button");
      if (!b || b.disabled) return;
      const id = b.dataset.id;
      map.closePopup();
      if (b.dataset.sc === "pair-with") {
        setManual(true);
        pickCandidate(id);
      } else {
        setStatus(id, b.dataset.sc);
      }
    });
    window.addEventListener("hashchange", () => {
      if (!current || location.hash.replace(/^#/, "") === hashFromState()) return;
      const { constraints, dropped } = sanitize(parseHash(location.hash), current);
      applyConstraints(constraints, dropped);
    });
  }

  function applyConstraints(constraints, dropped) {
    scenario.excluded = constraints.excluded;
    scenario.required = constraints.required;
    scenario.selected = null;
    scenario.manualResult = null;
    renderManualCard();
    renderCandidatesTable();
    constraintsChanged();
    setStatusText(dropped && dropped.length ? `Пропущены неизвестные или недопустимые площадки: ${dropped.join(", ")}.` : "");
  }

  async function downloadScenario() {
    try {
      if (SERVER && !fieldOn) {
        const res = await fetch("/api/scenario/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ analysis_id: analysisId, ...constraintsPayload(), top_n: TOP_N }),
        });
        if (!res.ok) throw new Error(detailText((await res.json().catch(() => ({}))).detail));
        download("scenario.json", await res.text(), "application/json");
      } else {
        const r = SC.runScenario(current, constraintsPayload(), TOP_N);
        download("scenario.json", JSON.stringify(r, null, 2) + "\n", "application/json");
      }
    } catch (e) {
      scenarioError(e.message);
    }
  }

  function uploadScenario(ev) {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    file.text().then((text) => {
      try {
        const obj = JSON.parse(text);
        const raw = obj && obj.constraints ? obj.constraints : obj;
        if (!raw || typeof raw !== "object" || (!("excluded" in raw) && !("required" in raw))) throw new Error("нет ключей excluded / required");
        const { constraints, dropped } = sanitize(raw, current);
        applyConstraints(constraints, dropped);
        if (!dropped.length) setStatusText(`Загружено: ${file.name}.`);
      } catch (e) {
        scenarioError(`Не удалось прочитать ${file.name}: ${e.message}`);
      }
    });
  }

  // ---- слои карты -----------------------------------------------------------------------------

  function candPopup(c) {
    const st = statusOf(c.id);
    const ref = refusalOf(c.id);
    const v = impact && c.admissible ? impact[c.id] : undefined;
    let h =
      `<b>Площадка ${esc(c.id)}</b><br>` +
      (c.admissible ? "допустима" : `исключена по ТЗ: ${esc(c.reason)}`) +
      `<br>ближайшая БС: ${esc(c.nearest_tower ?? "—")} (${fmt(c.nearest_tower_m, 0)} м)` +
      `<br>покрывает красных: ${c.covers_red}`;
    if (ref) h += `<br><b style="color:#b91c1c">отказ:</b> ${esc(CATEGORY_RU[ref.category])}${ref.reason ? ": " + esc(ref.reason) : ""}`;
    if (st === "required") h += "<br>🔒 зафиксирована";
    if (v !== undefined) h += `<br>потеря при отказе: <b>${v < 0 ? MINUS + Math.abs(v) : 0}</b>`;
    if (c.admissible) {
      const btn = (act, label, disabled, title) => `<button type="button" data-sc="${act}" data-id="${esc(c.id)}"${disabled ? " disabled" : ""}${title ? ` title="${esc(title)}"` : ""}>${label}</button>`;
      const full = scenario.required.length >= MAX_REQUIRED && st !== "required";
      h +=
        '<div class="popup-actions">' +
        (st !== "excluded" ? btn("excluded", "Отказ") : "") +
        (st !== "required" ? btn("required", "Зафиксировать", full, full ? "Уже зафиксированы две площадки" : "") : "") +
        (st !== "available" ? btn("available", "Вернуть") : "") +
        btn("pair-with", "Оценить в паре с…") +
        "</div>";
    }
    return h;
  }

  function renderScenarioLayers() {
    if (!current || !groups.cands) return;
    ["cands", "chosen", "covered", "ghost", "lost", "gained"].forEach((k) => groups[k].clearLayers());
    const d = current;
    const m = d.meta;
    const cache = cacheFor(d);
    const pair = displayPair() || [];
    const info = pair.length === 2 ? pairInfo(pair) : null;
    const isPreview = !!scenario.preview;

    if (info) {
      info.covered_ids.forEach((id) => {
        const c = cache.cellById[id];
        if (c) rect(c.b, { color: "#1d4ed8", weight: 1.6, fill: true, fillColor: "#1d4ed8", fillOpacity: 0.04 }).bindInfo(cellTip(c)).addTo(groups.covered);
      });
      info.lost_vs_optimum.forEach((id) => {
        const c = cache.cellById[id];
        if (c) rect(c.b, { color: "#7c3aed", weight: 2, dashArray: "4 3", fillColor: "#7c3aed", fillOpacity: 0.28 }).bindInfo(`<b>${esc(c.id)}</b><br>потеряна: покрывал оптимум по ТЗ, эта пара нет`).addTo(groups.lost);
      });
      info.gained_vs_optimum.forEach((id) => {
        const c = cache.cellById[id];
        if (c) rect(c.b, { color: "#0891b2", weight: 2, dashArray: "4 3", fillColor: "#0891b2", fillOpacity: 0.28 }).bindInfo(`<b>${esc(c.id)}</b><br>новая: оптимум по ТЗ её не покрывал`).addTo(groups.gained);
      });
    }

    // «призрак» оптимума: где стояли бы площадки оптимума, которых нет в текущей паре
    const optPair = SC.optimumOf(d, cache.covers).pair;
    d.candidates
      .filter((c) => optPair.includes(c.id) && !pair.includes(c.id))
      .forEach((c) => {
        L.circle([c.lat, c.lon], { radius: m.coverage_radius_m, color: "#6b7280", weight: 2, dashArray: "8 6", fill: false, interactive: false }).addTo(groups.ghost);
        L.circleMarker([c.lat, c.lon], { radius: 3, color: "#6b7280", fillColor: "#6b7280", fillOpacity: 1, interactive: false })
          .bindTooltip(`оптимум по ТЗ: ${c.id}`, { permanent: true, direction: "bottom", offset: [0, 6] })
          .addTo(groups.ghost);
      });

    // кандидаты
    const color = isPreview ? "#7c3aed" : "#059669";
    d.candidates.forEach((c) => {
      const st = statusOf(c.id);
      const inPair = pair.includes(c.id);
      let layer;
      if (inPair) {
        L.circle([c.lat, c.lon], { radius: m.coverage_radius_m, color, weight: 2, fillColor: color, fillOpacity: 0.07, interactive: false }).addTo(groups.chosen);
        const badge = st === "required" ? '<span class="lock">🔒</span>' : st === "excluded" ? '<span class="lock" style="color:#b91c1c;font-weight:800">✕</span>' : "";
        layer = L.marker([c.lat, c.lon], { icon: L.divIcon({ className: "star-icon", html: "★" + badge, iconSize: [28, 28] }), zIndexOffset: 1000 })
          .bindTooltip(c.id, { permanent: true, direction: "top", offset: [0, -12] })
          .addTo(groups.chosen);
      } else if (st === "excluded") {
        const r = refusalOf(c.id);
        layer = L.marker([c.lat, c.lon], { icon: L.divIcon({ className: "refused-icon", html: "✕", iconSize: [22, 22] }), zIndexOffset: 500 })
          .bindTooltip(`${c.id}: отказ (${CATEGORY_RU[r.category]})${r.reason ? "<br>" + esc(r.reason) : ""}`)
          .addTo(groups.cands);
      } else if (st === "required") {
        layer = L.marker([c.lat, c.lon], { icon: L.divIcon({ className: "locked-icon", html: "🔒", iconSize: [22, 22] }), zIndexOffset: 500 })
          .bindTooltip(`${c.id}: зафиксирована`)
          .addTo(groups.cands);
      } else {
        layer = L.circleMarker([c.lat, c.lon], {
          radius: 6, weight: 2, color: c.admissible ? "#1d4ed8" : "#6b7280",
          fillColor: c.admissible ? "#3b82f6" : "#d1d5db", fillOpacity: 0.9,
        }).bindTooltip(c.id).addTo(groups.cands);
      }
      if (scenario.manual) {
        layer.on("click", () => pickCandidate(c.id));
      } else {
        layer.bindPopup(() => candPopup(c));
      }
      if (scenario.manual && scenario.manualPick.includes(c.id)) {
        L.circleMarker([c.lat, c.lon], { radius: 12, color: "#f59e0b", weight: 3, fill: false, interactive: false }).addTo(groups.chosen);
      }
    });
  }

  // ---- ручной режим ------------------------------------------------------------------------------

  function setManual(on) {
    scenario.manual = on;
    scenario.manualPick = [];
    if (!on) {
      scenario.preview = null;
      scenario.manualResult = null;
    }
    document.body.classList.toggle("manual", on);
    $("#manual-toggle").checked = on;
    map.closePopup();
    renderManualCard();
    if (scenario.result) renderScenario();
    else renderScenarioLayers();
  }

  async function pickCandidate(id) {
    const c = current.candidates.find((x) => x.id === id);
    if (!c) return;
    if (!c.admissible) {
      scenario.manualResult = { note: `${id}: площадка исключена по ТЗ (${fmt(c.nearest_tower_m, 0)} м до ${c.nearest_tower}), оценить нельзя.` };
      return renderManualCard();
    }
    if (scenario.manualPick.length === 0 || scenario.manualPick.length === 2) {
      scenario.manualPick = [id];
      scenario.manualResult = null;
      renderManualCard();
      return renderScenarioLayers();
    }
    if (scenario.manualPick[0] === id) return;
    const pair = [scenario.manualPick[0], id];
    scenario.manualPick = pair;
    setBusy(true);
    try {
      const res = SERVER && !fieldOn
        ? await postJSON("/api/scenario/evaluate", { analysis_id: analysisId, pair, ...constraintsPayload() })
        : SC.evaluateManual(current, pair[0], pair[1], constraintsPayload());
      scenario.manualResult = res;
      scenario.preview = res.pair;
    } catch (e) {
      scenario.manualResult = { note: e.message, error: true };
    } finally {
      setBusy(false);
    }
    scenario.manualPick = [];
    renderManualCard();
    renderScenario();
  }

  function renderManualCard() {
    const el = $("#manual-card");
    const res = scenario.manualResult;
    if (!scenario.manual && !res) return el.classList.add("hidden");
    el.classList.remove("hidden");
    el.classList.toggle("warn", !!(res && (res.error || res.violates_constraints)));
    if (res && res.note) {
      el.textContent = res.note;
    } else if (res) {
      const best = res.delta_vs_best === null || res.delta_vs_best === undefined ? "" : `, Δ ${signed(res.delta_vs_best)} к лучшей с учётом отказов`;
      el.innerHTML =
        `<b>${esc(pairText(res.pair))}</b>: ${res.covered} ${plural(res.covered, "клетка", "клетки", "клеток")}, Δ ${signed(res.delta_vs_optimum)} к оптимуму${best}.` +
        (res.violates_constraints ? "<br>⚠ Пара нарушает текущие ограничения (отказ или фиксация)." : "") +
        `<div class="btn-row"><button class="mini" type="button" data-act="select-pair" data-pair="${res.pair.join(",")}">Выбрать эту пару</button></div>`;
    } else if (scenario.manualPick.length === 1) {
      el.textContent = `Выбрана ${scenario.manualPick[0]}. Кликните вторую площадку на карте.`;
    } else {
      el.textContent = "Кликните первую площадку на карте (или в подсказке нажмите «Оценить в паре с…»).";
    }
  }

  // ===================================================================================
  // Полевые замеры на основной карте (только веб-режим) и нижняя шторка на телефонах
  // ===================================================================================
  const FIELD_POLL_MS = 10000;
  const FIELD_HL_MS = 3000;
  const SRC_GLYPH = { map_pin: "◆", gps: "●", manual: "■", import: "▲" };
  const SRC_RU = { map_pin: "метка", gps: "GPS", manual: "вручную", import: "импорт" };
  let fieldOn = false; // «Учитывать полевые замеры»: по умолчанию выключено
  let fieldVer = null;
  let fieldCountInfo = null; // {count, by_source}
  let fieldHL = null;

  function fieldPointColor(v, m) {
    return v >= m.green_min ? COLORS.green : v < m.red_below ? COLORS.red : COLORS.yellow;
  }

  function buildFieldLayer(d) {
    /* Слой «Полевые замеры»: цвет по RSRP, форма по источнику (◆ метка, ● GPS, ■ вручную, ▲ импорт). */
    const g = L.featureGroup();
    (d.field_points || []).forEach((p) => {
      const col = fieldPointColor(p.rsrp, d.meta);
      const html = `<b>${fmt(p.rsrp, 1)} дБм</b><br>источник: ${esc(SRC_RU[p.source] || p.source)}` +
        `<br>время: ${esc(String(p.timestamp).replace("T", " ").slice(0, 19))}` +
        (p.accuracy_m ? `<br>точность GPS: ±${Math.round(p.accuracy_m)} м` : "") +
        `<br>клетка: ${esc(p.cell_id)}`;
      L.marker([p.lat, p.lon], {
        icon: L.divIcon({ className: "fpt", html: `<span style="color:${col}">${SRC_GLYPH[p.source] || "●"}</span>`, iconSize: [18, 18] }),
      })
        .bindPopup(html)
        .addTo(g);
    });
    return g;
  }

  function showFieldCount() {
    const el = $("#field-count");
    if (!el) return;
    const i = fieldCountInfo;
    if (!i) {
      el.textContent = "—";
      return;
    }
    const bs = i.by_source || {};
    el.textContent = i.count
      ? `Всего ${i.count}: метка ${bs.map_pin || 0}, GPS ${bs.gps || 0}, вручную ${bs.manual || 0}, импорт ${bs.import || 0}.`
      : "Полевых замеров пока нет.";
  }

  function renderFieldBlock(d) {
    if (!SERVER || !$("#field-card")) return;
    const m = d.meta;
    const included = !!m.field_included;
    if (included) {
      fieldVer = m.field_version;
      fieldCountInfo = { count: m.field_count, by_source: m.field_by_source };
    }
    showFieldCount();
    const banner = $("#field-banner");
    banner.classList.toggle("hidden", !included);
    if (included) banner.textContent = `Карта включает ${m.field_count} ${plural(m.field_count, "полевой замер", "полевых замера", "полевых замеров")}. result.json считается по исходным данным.`;

    const bp = included && d.field_diff ? d.field_diff.best_pair : null;
    const bpEl = $("#field-bestpair");
    if (bp && bp.changed) {
      bpEl.textContent = `С учётом полевых замеров лучшая пара: ${bp.after.join(" + ")} (было ${bp.before.join(" + ")}). Официальный ответ в result.json не меняется.`;
      bpEl.classList.remove("hidden");
    } else {
      bpEl.classList.add("hidden");
    }

    const cells = included && d.field_diff ? d.field_diff.cells : [];
    $("#field-changes-wrap").classList.toggle("hidden", !cells.length);
    $("#field-changes").innerHTML = cells
      .slice(0, 50)
      .map((c) => {
        const b = c.before;
        const a = c.after;
        let t = `<b>${esc(c.cell_id)}</b>: n ${b ? b.n : 0}→${a ? a.n : 0}`;
        if (a && b && a.cls !== b.cls) t += `, ${CLS_RU[b.cls]}→${CLS_RU[a.cls]}`;
        if (a && a.counted && !(b && b.counted)) t += ", теперь учитывается";
        return `<li data-cell="${esc(c.cell_id)}">${t}</li>`;
      })
      .join("");
  }

  function highlightDiff(d) {
    if (fieldHL) {
      map.removeLayer(fieldHL);
      fieldHL = null;
    }
    const cells = d.field_diff ? d.field_diff.cells : [];
    if (!cells.length) return;
    const byId = Object.fromEntries(d.cells.map((c) => [c.id, c]));
    const g = L.featureGroup();
    cells.forEach((c) => {
      const cell = byId[c.cell_id];
      if (cell) rect(cell.b, { color: "#f59e0b", weight: 4, fill: false, interactive: false }).addTo(g);
    });
    g.addTo(map);
    fieldHL = g;
    setTimeout(() => {
      if (fieldHL === g) {
        map.removeLayer(g);
        fieldHL = null;
      }
    }, FIELD_HL_MS);
  }

  function setFieldToggle(on) {
    fieldOn = on;
    const t = $("#field-toggle");
    if (t) t.checked = on;
  }

  async function pollField() {
    /* Раз в 10 с: лёгкий опрос версии. Если переключатель включён и версия выросла, данные перезапрашиваются. */
    if (document.hidden) return;
    try {
      const res = await fetch("/api/field/version");
      if (!res.ok) return;
      const v = await res.json();
      const grew = fieldVer !== null && v.version !== fieldVer;
      if (fieldOn && grew && analysisId === null) {
        await loadDefault({ keepView: true, highlight: true });
        return;
      }
      fieldCountInfo = { count: v.count, by_source: v.by_source };
      if (!fieldOn) fieldVer = v.version;
      showFieldCount();
    } catch (e) {
      /* нет сети: попробуем в следующий раз */
    }
  }

  function initField() {
    if (!SERVER || !$("#field-card")) return;
    $("#field-toggle").addEventListener("change", async (ev) => {
      setFieldToggle(ev.target.checked);
      try {
        await loadDefault({ keepView: true, highlight: fieldOn });
      } catch (e) {
        setFieldToggle(false);
        showError(e.message);
      }
    });
    $("#field-changes").addEventListener("click", (ev) => {
      const li = ev.target.closest("li[data-cell]");
      const c = li && current.cells.find((x) => x.id === li.dataset.cell);
      if (c) map.flyTo(c.c, 17, { duration: 0.8 });
    });
    pollField();
    setInterval(pollField, FIELD_POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pollField();
    });
  }

  // ---- нижняя шторка (меньше 760 px): свёрнута / половина / весь экран ---------------------------------

  const MQ_MOBILE = window.matchMedia("(max-width: 760px)");
  const SHEET_ORDER = ["peek", "half", "full"];
  const SHEET_PEEK_PX = 86;
  const SHEET_TOP_GAP_PX = 12;
  const SHEET_DRAG_PX = 5;

  function updateSheetMetrics() {
    const h = $("#panel").getBoundingClientRect().height;
    document.documentElement.style.setProperty("--sheet-h", `${Math.round(h)}px`);
    if (map) map.invalidateSize();
  }

  function setSheet(state) {
    const p = $("#panel");
    p.style.height = "";
    p.dataset.sheet = state;
    $("#sheet-handle").setAttribute("aria-expanded", String(state !== "peek"));
    setTimeout(updateSheetMetrics, 260); // после анимации высоты
  }

  function initSheet() {
    const panel = $("#panel");
    const handle = $("#sheet-handle");
    let drag = null;
    let justDragged = false;
    const snapHeights = () => ({ peek: SHEET_PEEK_PX, half: window.innerHeight * 0.5, full: window.innerHeight - SHEET_TOP_GAP_PX });

    handle.addEventListener("click", () => {
      if (justDragged || !MQ_MOBILE.matches) return;
      const i = SHEET_ORDER.indexOf(panel.dataset.sheet);
      setSheet(SHEET_ORDER[(i + 1) % SHEET_ORDER.length]);
    });
    handle.addEventListener("pointerdown", (ev) => {
      if (!MQ_MOBILE.matches) return;
      drag = { y: ev.clientY, h: panel.getBoundingClientRect().height, moved: false };
      handle.setPointerCapture(ev.pointerId);
    });
    handle.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const dy = drag.y - ev.clientY;
      if (!drag.moved && Math.abs(dy) > SHEET_DRAG_PX) {
        drag.moved = true;
        panel.classList.add("dragging");
      }
      if (drag.moved) {
        const s = snapHeights();
        panel.style.height = Math.min(s.full, Math.max(s.peek, drag.h + dy)) + "px";
        updateSheetMetrics();
      }
    });
    const end = () => {
      if (!drag) return;
      const moved = drag.moved;
      drag = null;
      panel.classList.remove("dragging");
      if (!moved) return;
      justDragged = true;
      setTimeout(() => (justDragged = false), 0);
      const h = panel.getBoundingClientRect().height;
      const s = snapHeights();
      const nearest = SHEET_ORDER.reduce((best, k) => (Math.abs(s[k] - h) < Math.abs(s[best] - h) ? k : best), "peek");
      setSheet(nearest);
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    if (window.ResizeObserver) new ResizeObserver(() => MQ_MOBILE.matches && updateSheetMetrics()).observe(panel);
    const onChange = () => {
      panel.style.height = "";
      if (MQ_MOBILE.matches) updateSheetMetrics();
      else document.documentElement.style.removeProperty("--sheet-h");
      if (map) setTimeout(() => map.invalidateSize(), 50);
    };
    (MQ_MOBILE.addEventListener ? MQ_MOBILE.addEventListener.bind(MQ_MOBILE, "change") : MQ_MOBILE.addListener.bind(MQ_MOBILE))(onChange);
    window.addEventListener("orientationchange", () => setTimeout(onChange, 250));
    onChange();
  }

  document.addEventListener("DOMContentLoaded", () => {
    initMap();
    bindButtons();
    bindUpload();
    bindCandidatesTable();
    bindScenarioPanel();
    initSheet();
    initField();
    const el = document.getElementById("data");
    if (el) {
      try {
        window.__DATA__ = JSON.parse(el.textContent);
        render(window.__DATA__);
      } catch (e) {
        showError("Не удалось прочитать встроенные данные: " + e.message);
      }
    } else if (SERVER) {
      loadDefault().catch((e) => showError(e.message));
    }
  });
})();
