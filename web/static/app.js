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
    map = L.map("map", { preferCanvas: true, zoomControl: true });
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
    const size = map.getSize();
    if (!fitTarget || size.x === 0 || size.y === 0) return;
    map.fitBounds(fitTarget, { padding: [10, 10] });
    fitted = true;
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

  function buildLayers(d) {
    clearLayers();
    const m = d.meta;
    const cellTip = (c) =>
      `<b>${esc(c.id)}</b><br>медиана: ${fmt(c.m, 2)} дБм<br>замеров: ${c.n}<br>класс: ${c.counted === false ? "не учитывается (n&lt;" + m.min_samples + ")" : CLS_RU[c.cls]}`;

    // 1. Сетка (только n >= min_samples)
    const grid = L.featureGroup();
    const insufficient = L.featureGroup();
    d.cells.forEach((c) => {
      if (c.counted) {
        rect(c.b, { color: "#555", weight: 0.4, fillColor: COLORS[c.cls], fillOpacity: 0.5 }).bindTooltip(cellTip(c)).addTo(grid);
      } else {
        rect(c.b, { color: "#777", weight: 0.4, dashArray: "2 2", fillColor: COLORS.grey, fillOpacity: 0.35 }).bindTooltip(cellTip(c)).addTo(insufficient);
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

    // 4. Кандидаты
    const cands = L.featureGroup();
    const chosen = L.featureGroup();
    d.candidates.forEach((c) => {
      const popup =
        `<b>Площадка ${esc(c.id)}</b><br>` +
        (c.admissible ? "допустима" : `исключена: ${esc(c.reason)}`) +
        `<br>ближайшая БС: ${esc(c.nearest_tower ?? "—")} (${fmt(c.nearest_tower_m, 0)} м)` +
        `<br>покрывает красных: ${c.covers_red}`;
      if (c.selected) {
        L.circle([c.lat, c.lon], { radius: m.coverage_radius_m, color: "#059669", weight: 2, fillColor: "#10b981", fillOpacity: 0.07 }).addTo(chosen);
        L.marker([c.lat, c.lon], { icon: L.divIcon({ className: "star-icon", html: "★", iconSize: [28, 28] }), zIndexOffset: 1000 })
          .bindPopup(popup)
          .bindTooltip(c.id, { permanent: true, direction: "top", offset: [0, -12] })
          .addTo(chosen);
      } else {
        L.circleMarker([c.lat, c.lon], {
          radius: 6, weight: 2, color: c.admissible ? "#1d4ed8" : "#6b7280",
          fillColor: c.admissible ? "#3b82f6" : "#d1d5db", fillOpacity: 0.9,
        }).bindPopup(popup).bindTooltip(c.id).addTo(cands);
      }
    });

    // 5. Красные клетки, покрытые выбранной парой
    const covered = L.featureGroup();
    d.cells.filter((c) => c.covered).forEach((c) => {
      rect(c.b, { color: "#1d4ed8", weight: 1.6, fill: true, fillColor: "#1d4ed8", fillOpacity: 0.04 }).bindTooltip(cellTip(c)).addTo(covered);
    });

    // 6. Бонусы: тепловая карта, день/ночь
    const heat = L.heatLayer ? L.heatLayer(d.heat, { radius: 14, blur: 16, maxZoom: 17, minOpacity: 0.25 }) : L.layerGroup();
    const dn = d.daynight || {};
    const dnLayer = (cells) => {
      const g = L.featureGroup();
      (cells || []).forEach((c) => rect(c.b, { color: "#555", weight: 0.4, fillColor: COLORS[c.cls], fillOpacity: 0.55 }).bindTooltip(cellTip(c)).addTo(g));
      return g;
    };
    const day = dnLayer(dn.day_cells);
    const night = dnLayer(dn.night_cells);
    const delta = L.featureGroup();
    (dn.delta || []).forEach((c) => {
      const v = c.d;
      const col = v <= -3 ? "#b91c1c" : v < 0 ? "#f87171" : v < 3 ? "#93c5fd" : "#1d4ed8";
      rect(c.b, { color: "#555", weight: 0.3, fillColor: col, fillOpacity: 0.6 }).bindTooltip(`<b>${esc(c.id)}</b><br>Δ ночь − день: ${fmt(v, 2)} дБ`).addTo(delta);
    });
    const worseNight = L.featureGroup();
    const onlyNight = new Set(dn.red_only_night || []);
    d.cells.filter((c) => onlyNight.has(c.id)).forEach((c) => {
      rect(c.b, { color: "#7c3aed", weight: 2, fillColor: "#7c3aed", fillOpacity: 0.25 }).bindTooltip(`<b>${esc(c.id)}</b><br>красная только ночью`).addTo(worseNight);
    });

    groups = { grid, insufficient, worst, towers, cands, chosen, covered, heat, day, night, delta, worseNight };
    [grid, covered, towers, cands, chosen, worst].forEach((g) => g.addTo(map));

    layerControl = L.control.layers(null, {
      "Сетка 200 м (медиана RSRP)": grid,
      "Топ-3 худших": worst,
      "Существующие БС, запрет 500 м": towers,
      "Кандидаты": cands,
      "Выбранная пара + 1 км": chosen,
      "Покрытые красные клетки": covered,
      "Тепловая карта": heat,
      "День (08–20)": day,
      "Ночь": night,
      "Δ ночь − день": delta,
      "Красные только ночью": worseNight,
      [`Недостаточно данных (n<${m.min_samples})`]: insufficient,
    }, { collapsed: window.innerWidth < 760 }).addTo(map);

    legend = L.control({ position: "bottomright" });
    legend.onAdd = () => {
      const div = L.DomUtil.create("div", "legend");
      div.innerHTML =
        `<b>Медиана RSRP</b><br>` +
        `<i style="background:${COLORS.green}"></i>≥ ${m.green_min} дБм<br>` +
        `<i style="background:${COLORS.yellow}"></i>${m.red_below} … ${m.green_min}<br>` +
        `<i style="background:${COLORS.red}"></i>&lt; ${m.red_below} (слабая зона)<br>` +
        `<i style="background:${COLORS.grey}"></i>n &lt; ${m.min_samples}, не учитывается<br>` +
        `<span class="line" style="border-top:2px solid #1d4ed8"></span>покрыта парой<br>` +
        `<span class="line" style="border-top:2px dashed #374151"></span>запрет ${m.tower_exclusion_m} м от БС<br>` +
        `<span style="color:#059669">★</span> выбранная площадка, круг ${m.coverage_radius_m / 1000} км`;
      return div;
    };
    legend.addTo(map);

    const b = m.bounds;
    fitTarget = [[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]];
    fitted = false;
    tryFit();
    L.rectangle([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], { color: "#111", weight: 1, dashArray: "4 4", fill: false, interactive: false }).addTo(grid);
  }

  function fillPanel(d) {
    const r = d.result;
    $("#k-pair").textContent = r.best_candidates.join(" + ") || "—";
    $("#k-covered").textContent = r.covered_weak_cells;
    $("#k-dropped").textContent = r.dropped_rows;

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

    $("#t-cands tbody").innerHTML = d.candidates
      .map((c) => {
        const tag = c.selected ? '<span class="tag best">выбрана</span>' : c.admissible ? '<span class="tag ok">допуст.</span>' : '<span class="tag no">исключ.</span>';
        return `<tr data-lat="${c.lat}" data-lon="${c.lon}" class="${c.selected ? "sel" : ""}" title="${esc(c.reason || "")}"><td>${esc(c.id)}</td><td>${tag}</td><td>${fmt(c.nearest_tower_m, 0)}</td><td>${c.covers_red}</td></tr>`;
      })
      .join("");
    document.querySelectorAll("#t-cands tbody tr").forEach((tr) => {
      tr.addEventListener("click", () => map.flyTo([+tr.dataset.lat, +tr.dataset.lon], 16, { duration: 0.8 }));
    });

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

  function render(d) {
    current = d;
    $("#err").classList.add("hidden");
    buildLayers(d);
    fillPanel(d);
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

  async function loadDefault() {
    const res = await fetch("/api/mapdata");
    if (!res.ok) throw new Error(`Ошибка API ${res.status}: ${await res.text()}`);
    analysisId = null;
    render(await res.json());
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
        render(body.map_data);
        status.textContent = `Готово: dropped_rows=${body.result.dropped_rows}, пара ${body.result.best_candidates.join("+")}.`;
      } catch (e) {
        status.textContent = "";
        showError(e.message);
      }
    });
    $("#reset-default").addEventListener("click", () => loadDefault().catch((e) => showError(e.message)));
  }

  document.addEventListener("DOMContentLoaded", () => {
    initMap();
    bindButtons();
    bindUpload();
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
