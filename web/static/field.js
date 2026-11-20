/* Мобильная страница сбора полевых замеров. Чистый JS, без фреймворков. Без alert/confirm/prompt. */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MINUS = "−";
  const num = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d).replace("-", MINUS));
  const CLS_RU = { green: "зелёная", yellow: "жёлтая", red: "красная" };
  const SOURCE_RU = { map_pin: "метка", gps: "GPS", manual: "вручную", import: "импорт" };
  const KEYS = { rsrp: "field-last-rsrp", label: "field-device-label", recent: "field-recent-v1" };
  const RECENT_MAX = 50;
  const HINT_DELAY_MS = 200;
  const GPS_SETTLE_MS = 10000;
  const GPS_GOOD_ENOUGH_M = 10;
  const GPS_OPTIONS = { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 };
  const TOAST_MS = 8000;
  const DELETE_CONFIRM_MS = 4000;

  const store = {
    get(k, fallback) {
      try {
        const v = localStorage.getItem(k);
        return v === null ? fallback : JSON.parse(v);
      } catch (e) {
        return fallback;
      }
    },
    set(k, v) {
      try {
        localStorage.setItem(k, JSON.stringify(v));
      } catch (e) {
        /* приватный режим */
      }
    },
  };

  const S = {
    cfg: null,
    map: null,
    marker: null,
    markerKind: null,
    accCircle: null,
    cellsLayer: null,
    boundsRect: null,
    method: "pin",
    sub: "fields",
    pos: null, // {lat, lon, accuracy?}
    cellInfo: null, // ответ /api/field/cell
    rsrp: store.get(KEYS.rsrp, -100),
    timeMode: "now",
    sending: false,
    abort: null,
    hintTimer: null,
    hintAbort: null,
    gps: { watchId: null, timer: null, best: null },
    paste: { ok: 0, checkedText: "" },
    deleting: null,
  };

  // ---------------------------------------------------------------- вспомогательное

  function toast(kind, html, ms = TOAST_MS) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = html;
    el.addEventListener("click", () => el.remove());
    $("#toasts").appendChild(el);
    while ($("#toasts").children.length > 3) $("#toasts").firstChild.remove();
    setTimeout(() => el.remove(), ms);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = data.detail;
      const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : `Ошибка ${res.status}`;
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const isNetworkError = (e) => e instanceof TypeError || !navigator.onLine;
  const NO_NET = "Нет соединения, замер не отправлен. Введённые данные сохранены.";

  function inBoundsLocal(lat, lon) {
    const b = S.cfg.meta.bounds;
    return lat >= b.lat_min && lat <= b.lat_max && lon >= b.lon_min && lon <= b.lon_max;
  }

  function classOf(v) {
    const m = S.cfg.meta;
    return v >= m.green_min ? "green" : v < m.red_below ? "red" : "yellow";
  }

  // ---------------------------------------------------------------- время

  const pad = (n, l = 2) => String(n).padStart(l, "0");

  function isoWithOffset(date, offsetMin) {
    const t = new Date(date.getTime() + offsetMin * 60000);
    const a = Math.abs(offsetMin);
    const sign = offsetMin >= 0 ? "+" : "-";
    return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}T${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}:${pad(t.getUTCSeconds())}${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
  }

  function currentTimestamp() {
    const off = S.cfg.meta.utc_offset_minutes;
    if (S.timeMode === "custom" && $("#time-input").value) {
      const v = $("#time-input").value; // YYYY-MM-DDTHH:mm (местное время района)
      const a = Math.abs(off);
      return `${v}${v.length === 16 ? ":00" : ""}${off >= 0 ? "+" : "-"}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
    }
    return isoWithOffset(new Date(), off);
  }

  function renderTime() {
    if (S.timeMode === "custom") {
      $("#time-label").textContent = "Время задано вручную";
      return;
    }
    const iso = isoWithOffset(new Date(), S.cfg.meta.utc_offset_minutes);
    $("#time-label").textContent = `${iso.slice(0, 10)} ${iso.slice(11, 16)} (сейчас)`;
  }

  // ---------------------------------------------------------------- RSRP

  function setRsrp(v, from) {
    const m = S.cfg.meta;
    let x = Number(v);
    if (!Number.isFinite(x)) return;
    x = Math.min(m.rsrp_max, Math.max(m.rsrp_min, Math.round(x * 10) / 10));
    S.rsrp = x;
    if (from !== "input") $("#rsrp-abs").value = String(Math.abs(x));
    if (from !== "range") $("#rsrp-range").value = String(x);
    const dot = $("#rsrp-dot");
    dot.className = "dot " + classOf(x);
    dot.title = "Класс: " + CLS_RU[classOf(x)];
    store.set(KEYS.rsrp, x);
    updateSubmit();
  }

  // ---------------------------------------------------------------- точка и клетка

  function pinIcon(kind) {
    return L.divIcon({ className: "pin " + kind, html: "<i></i>", iconSize: [34, 34], iconAnchor: [17, 34] });
  }

  function setPos(lat, lon, extra = {}) {
    S.pos = { lat, lon, ...extra };
    const bad = !inBoundsLocal(lat, lon);
    const kind = bad ? "bad" : S.method === "gps" ? "gps" : "";
    if (!S.marker) {
      S.marker = L.marker([lat, lon], { draggable: draggable(), icon: pinIcon(kind) }).addTo(S.map);
      S.marker.on("drag", () => {
        const p = S.marker.getLatLng();
        setPos(p.lat, p.lng);
      });
    } else {
      S.marker.setLatLng([lat, lon]);
      // иконку меняем только при смене вида: setIcon во время перетаскивания прерывает drag
      if (S.markerKind !== kind) S.marker.setIcon(pinIcon(kind));
    }
    S.markerKind = kind;
    if (extra.accuracy) {
      const r = extra.accuracy;
      if (!S.accCircle) S.accCircle = L.circle([lat, lon], { radius: r, color: "#059669", weight: 1, fillOpacity: 0.12, interactive: false }).addTo(S.map);
      else S.accCircle.setLatLng([lat, lon]).setRadius(r);
    } else if (S.accCircle) {
      S.map.removeLayer(S.accCircle);
      S.accCircle = null;
    }
    $("#coords").textContent = `${lat.toFixed(6)}, ${lon.toFixed(6)}` + (extra.accuracy ? ` · ±${Math.round(extra.accuracy)} м` : "");
    if (S.method === "manual" && document.activeElement !== $("#in-lat") && document.activeElement !== $("#in-lon")) {
      $("#in-lat").value = lat.toFixed(6);
      $("#in-lon").value = lon.toFixed(6);
    }
    S.cellInfo = null;
    renderCellInfo(bad ? { in_bounds: false } : null);
    scheduleHint();
    updateSubmit();
  }

  function draggable() {
    return S.method === "pin" || (S.method === "manual" && S.sub === "fields");
  }

  function clearPos() {
    S.pos = null;
    S.cellInfo = null;
    if (S.marker) S.map.removeLayer(S.marker);
    if (S.accCircle) S.map.removeLayer(S.accCircle);
    S.marker = S.accCircle = null;
    $("#coords").textContent = "";
    renderCellInfo(null);
    updateSubmit();
  }

  function renderCellInfo(info) {
    const el = $("#cellinfo");
    el.classList.remove("bad");
    if (!S.pos) {
      el.textContent = S.method === "gps" ? "Нажмите «Определить моё местоположение»." : "Выберите точку на карте.";
      return;
    }
    if (info && info.in_bounds === false) {
      el.classList.add("bad");
      el.textContent = "Точка вне района. Отправка недоступна.";
      return;
    }
    if (!info || !info.cell_id) {
      el.textContent = "Клетка определяется…";
      return;
    }
    const m = S.cfg.meta;
    const stat =
      info.n > 0
        ? `n=${info.n}${info.median !== null ? ` · ${num(info.median)} (${CLS_RU[info.cls]})` : ""}`
        : "замеров нет";
    const need = info.counted ? "учитывается" : `не учитывается (нужно ${m.min_samples})`;
    el.textContent = `Клетка: ${info.cell_id} · ${stat} · ${need} · в районе ✓`;
  }

  function scheduleHint() {
    clearTimeout(S.hintTimer);
    if (!S.pos || !inBoundsLocal(S.pos.lat, S.pos.lon)) return;
    S.hintTimer = setTimeout(async () => {
      if (S.hintAbort) S.hintAbort.abort();
      S.hintAbort = new AbortController();
      const { lat, lon } = S.pos;
      try {
        const info = await api(`/api/field/cell?lat=${lat}&lon=${lon}`, { signal: S.hintAbort.signal });
        if (!S.pos || S.pos.lat !== lat || S.pos.lon !== lon) return;
        S.cellInfo = info;
        renderCellInfo(info);
        updateSubmit();
      } catch (e) {
        if (e.name === "AbortError") return;
        $("#cellinfo").textContent = isNetworkError(e) ? "Нет соединения: подсказка по клетке недоступна." : e.message;
      }
    }, HINT_DELAY_MS);
  }

  // ---------------------------------------------------------------- кнопка «Отправить»

  function formValid() {
    return !!S.pos && inBoundsLocal(S.pos.lat, S.pos.lon) && !(S.cellInfo && S.cellInfo.in_bounds === false) && Number.isFinite(S.rsrp);
  }

  function updateSubmit() {
    const btn = $("#submit");
    if (S.method === "manual" && S.sub === "paste") {
      const n = S.paste.ok;
      btn.textContent = n > 0 ? `Отправить ${n} ${plural(n, "строку", "строки", "строк")}` : "Отправить строки";
      btn.disabled = S.sending || n <= 0;
    } else {
      btn.textContent = S.sending ? "Отправляю…" : "Отправить замер";
      btn.disabled = S.sending || !formValid();
    }
  }

  function plural(n, one, few, many) {
    const k = n % 100;
    if (k >= 11 && k <= 14) return many;
    return n % 10 === 1 ? one : n % 10 >= 2 && n % 10 <= 4 ? few : many;
  }

  function feedbackText(a) {
    let t = `${esc(a.cell_id)}: ${a.n_after} ${plural(a.n_after, "замер", "замера", "замеров")}`;
    if (a.became_counted) t += ` — клетка теперь учитывается (${num(a.median_after)}, ${CLS_RU[a.class_after]})`;
    else if (a.counted_after) t += ` — клетка учитывается (${num(a.median_after)}, ${CLS_RU[a.class_after]})`;
    else t += ` — ещё нужно ${S.cfg.meta.min_samples - a.n_after} до ${S.cfg.meta.min_samples}`;
    return t;
  }

  async function submit() {
    if (S.sending) return;
    if (S.method === "manual" && S.sub === "paste") return sendPaste();
    if (!formValid()) return;
    const label = $("#device-label").value.trim();
    const item = {
      lat: S.pos.lat,
      lon: S.pos.lon,
      rsrp_dbm: S.rsrp,
      timestamp: currentTimestamp(),
      source: S.method === "gps" ? "gps" : S.method === "manual" ? "manual" : "map_pin",
    };
    if (S.method === "gps" && S.pos.accuracy) item.accuracy_m = Math.round(S.pos.accuracy * 10) / 10;
    if (label) item.device_label = label;
    S.sending = true;
    updateSubmit();
    S.abort = new AbortController();
    try {
      const r = await api("/api/field/measurements", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: [item] }),
        signal: S.abort.signal,
      });
      if (r.accepted.length) {
        const a = r.accepted[0];
        let html = `✓ Принято. ${feedbackText(a)}`;
        if (a.warning === "low_accuracy") html += `<br>⚠ Низкая точность GPS (±${Math.round(item.accuracy_m)} м)`;
        toast(a.warning ? "warn" : "ok", html);
        addRecent({ id: a.id, t: item.timestamp, source: item.source, cell: a.cell_id, rsrp: item.rsrp_dbm });
        refreshAfterChange();
      } else {
        const rej = r.rejected[0];
        toast("err", `✗ Отклонено: ${esc(rej ? rej.message : "неизвестная причина")}`);
      }
    } catch (e) {
      if (e.name === "AbortError") return;
      toast("err", isNetworkError(e) ? NO_NET : `✗ ${esc(e.message)}`);
    } finally {
      S.sending = false;
      updateSubmit();
    }
  }

  // ---------------------------------------------------------------- вставка строк

  async function pastePost(dry) {
    return api("/api/field/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("#paste-text").value, source: "manual", dry_run: dry }),
    });
  }

  function renderPasteResult(r, sent) {
    const rows = r.rows || [];
    const ok = rows.filter((x) => x.status === "ok").length;
    const bad = rows.length - ok;
    const body = rows
      .map((x) =>
        x.status === "ok"
          ? `<tr class="ok"><td>${x.index + 1}</td><td>${esc(x.raw)}</td><td>${esc(x.cell_id)} → n=${x.n_after}${x.became_counted ? ", станет учитываемой" : ""}</td></tr>`
          : `<tr class="rej"><td>${x.index + 1}</td><td>${esc(x.raw)}</td><td class="rej-msg">${esc(x.message)}</td></tr>`
      )
      .join("");
    $("#paste-result").innerHTML =
      `<div class="paste-summary">${sent ? "Отправлено" : "Принимается"}: ${ok} · отклонено: ${bad}</div>` +
      `<table><thead><tr><th>№</th><th>Строка</th><th>Результат</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  async function checkPaste() {
    if (!$("#paste-text").value.trim()) {
      toast("warn", "Вставьте хотя бы одну строку.");
      return;
    }
    $("#paste-check").disabled = true;
    try {
      const r = await pastePost(true);
      S.paste.ok = r.accepted.length;
      S.paste.checkedText = $("#paste-text").value;
      renderPasteResult(r, false);
    } catch (e) {
      S.paste.ok = 0;
      $("#paste-result").innerHTML = "";
      toast("err", isNetworkError(e) ? NO_NET : `✗ ${esc(e.message)}`);
    } finally {
      $("#paste-check").disabled = false;
      updateSubmit();
    }
  }

  async function sendPaste() {
    S.sending = true;
    updateSubmit();
    try {
      const r = await pastePost(false);
      renderPasteResult(r, true);
      const n = r.accepted.length;
      const extra = r.summary && r.summary.became_counted ? ` Клеток стало учитываемыми: ${r.summary.became_counted}.` : "";
      toast(n ? "ok" : "err", n ? `✓ Принято ${n} из ${r.rows.length}.${extra}` : "✗ Ни одна строка не принята.");
      r.accepted.forEach((a) => {
        const row = r.rows[a.index];
        const ts = (row.raw.split(",")[3] || "").trim();
        addRecent({ id: a.id, t: ts, source: "manual", cell: a.cell_id, rsrp: Number(row.raw.split(",")[2]) });
      });
      S.paste.ok = 0;
      if (n) $("#paste-text").value = "";
      refreshAfterChange();
    } catch (e) {
      toast("err", isNetworkError(e) ? NO_NET : `✗ ${esc(e.message)}`);
    } finally {
      S.sending = false;
      updateSubmit();
    }
  }

  // ---------------------------------------------------------------- последние замеры

  function recentList() {
    return store.get(KEYS.recent, []);
  }

  function addRecent(item) {
    const list = recentList();
    list.unshift(item);
    store.set(KEYS.recent, list.slice(0, RECENT_MAX));
    renderRecent();
  }

  function renderRecent() {
    const list = recentList();
    $("#recent-empty").classList.toggle("hidden", list.length > 0);
    $("#recent").innerHTML = list
      .map((r) => {
        const confirm = S.deleting === r.id;
        return `<li data-id="${esc(r.id)}"><div class="meta"><b>${num(r.rsrp, 0)} дБм</b> · ${esc(r.cell || "—")}<div class="sub">${esc((r.t || "").replace("T", " ").slice(0, 16))} · ${esc(SOURCE_RU[r.source] || r.source)}</div></div>` +
          `<button type="button" class="del${confirm ? " confirm" : ""}" data-id="${esc(r.id)}">${confirm ? "Точно удалить?" : "Удалить"}</button></li>`;
      })
      .join("");
  }

  async function deleteRecent(id) {
    if (S.deleting !== id) {
      S.deleting = id;
      renderRecent();
      setTimeout(() => {
        if (S.deleting === id) {
          S.deleting = null;
          renderRecent();
        }
      }, DELETE_CONFIRM_MS);
      return;
    }
    S.deleting = null;
    try {
      await api(`/api/field/measurements/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("ok", "Замер удалён.");
    } catch (e) {
      if (e.status !== 404) {
        toast("err", isNetworkError(e) ? "Нет соединения, замер не удалён." : `✗ ${esc(e.message)}`);
        renderRecent();
        return;
      }
      toast("warn", "Замера уже нет на сервере: убрали из списка.");
    }
    store.set(KEYS.recent, recentList().filter((r) => r.id !== id));
    renderRecent();
    refreshAfterChange();
  }

  // ---------------------------------------------------------------- карта

  function drawCells() {
    if (S.cellsLayer) S.map.removeLayer(S.cellsLayer);
    const g = L.featureGroup();
    S.cfg.cells
      .filter((c) => !c.counted)
      .forEach((c) =>
        L.rectangle([[c.b[0], c.b[1]], [c.b[2], c.b[3]]], { color: "#6b7280", weight: 0.6, fillColor: "#9ca3af", fillOpacity: 0.35, interactive: false }).addTo(g)
      );
    g.addTo(S.map);
    S.cellsLayer = g;
    if (S.marker) S.marker.bringToFront?.();
  }

  async function refreshAfterChange() {
    try {
      const [cfg, v] = await Promise.all([api("/api/field/config"), api("/api/field/version")]);
      S.cfg = cfg;
      drawCells();
      $("#count").textContent = v.count;
      if (S.pos && inBoundsLocal(S.pos.lat, S.pos.lon)) scheduleHint();
    } catch (e) {
      /* не критично: счётчик обновится при следующем действии */
    }
  }

  function fitDistrict() {
    const b = S.cfg.meta.bounds;
    S.map.fitBounds([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], { padding: [10, 10] });
  }

  function initMap() {
    const b = S.cfg.meta.bounds;
    S.map = L.map("fmap", { zoomControl: true, center: [(b.lat_min + b.lat_max) / 2, (b.lon_min + b.lon_max) / 2], zoom: 13 });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(S.map);
    S.boundsRect = L.rectangle([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]], { color: "#111", weight: 1.5, dashArray: "6 5", fill: false, interactive: false }).addTo(S.map);
    drawCells();
    fitDistrict();
    S.map.on("click", (e) => {
      if (S.method === "pin" || (S.method === "manual" && S.sub === "fields")) setPos(e.latlng.lat, e.latlng.lng);
    });
    // размер контейнера может измениться (поворот экрана, панель браузера)
    if (window.ResizeObserver) new ResizeObserver(() => S.map.invalidateSize()).observe($("#fmap"));
    window.addEventListener("orientationchange", () => setTimeout(() => S.map.invalidateSize(), 250));
  }

  // ---------------------------------------------------------------- GPS

  function gpsStatus(text, kind) {
    const el = $("#gps-status");
    el.className = "gps-status show" + (kind ? " " + kind : "");
    el.textContent = text;
  }

  function stopGps() {
    if (S.gps.watchId !== null) navigator.geolocation.clearWatch(S.gps.watchId);
    clearTimeout(S.gps.timer);
    S.gps.watchId = null;
  }

  function finishGps() {
    stopGps();
    const best = S.gps.best;
    if (!best) {
      gpsStatus("Не удалось определить местоположение. Попробуйте ещё раз или поставьте метку вручную.", "err");
    } else {
      const warnM = S.cfg.meta.accuracy_warn_m;
      const acc = Math.round(best.accuracy);
      if (best.accuracy > warnM) gpsStatus(`Точность ±${acc} м. Низкая точность, выйдите на открытое место. Замер всё равно можно отправить.`, "warn");
      else gpsStatus(`Точность ±${acc} м.`);
      if (!inBoundsLocal(best.lat, best.lon)) gpsStatus(`Вы вне района (${best.lat.toFixed(4)}, ${best.lon.toFixed(4)}). Отправка недоступна.`, "err");
    }
    $("#gps-btn").textContent = "Обновить позицию";
    $("#gps-btn").disabled = false;
  }

  function startGps() {
    if (!window.isSecureContext) {
      gpsStatus('Геолокация работает только по HTTPS. Откройте сайт по https://… (Render) или используйте способ "Метка".', "err");
      return;
    }
    if (!navigator.geolocation) {
      gpsStatus("Геолокация недоступна в этом браузере. Используйте способ «Метка».", "err");
      return;
    }
    stopGps();
    S.gps.best = null;
    $("#gps-btn").disabled = true;
    gpsStatus("Определяю местоположение…");
    S.gps.watchId = navigator.geolocation.watchPosition(
      (p) => {
        const c = p.coords;
        if (!S.gps.best || c.accuracy < S.gps.best.accuracy) {
          S.gps.best = { lat: c.latitude, lon: c.longitude, accuracy: c.accuracy };
          setPos(c.latitude, c.longitude, { accuracy: c.accuracy });
          S.map.setView([c.latitude, c.longitude], Math.max(S.map.getZoom(), 16));
        }
        gpsStatus(`Уточняю… ±${Math.round(S.gps.best.accuracy)} м`);
        if (S.gps.best.accuracy <= GPS_GOOD_ENOUGH_M) finishGps();
      },
      (err) => {
        stopGps();
        const msg =
          err.code === 1
            ? "Доступ к геолокации запрещён. Разрешите его в настройках браузера для этого сайта."
            : "Не удалось определить местоположение. Попробуйте ещё раз или поставьте метку вручную.";
        if (S.gps.best) finishGps();
        else {
          gpsStatus(msg, "err");
          $("#gps-btn").disabled = false;
        }
      },
      GPS_OPTIONS
    );
    S.gps.timer = setTimeout(finishGps, GPS_SETTLE_MS);
  }

  // ---------------------------------------------------------------- переключатели

  function setMethod(m) {
    if (S.method === "gps" && m !== "gps") stopGps();
    S.method = m;
    $$("#method-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.m === m)));
    ["pin", "gps", "manual"].forEach((k) => $("#m-" + k).classList.toggle("hidden", k !== m));
    if (S.marker) S.marker.dragging[draggable() ? "enable" : "disable"]();
    if (S.pos) setPos(S.pos.lat, S.pos.lon, m === "gps" && S.pos.accuracy ? { accuracy: S.pos.accuracy } : {});
    updateCommonVisibility();
    renderCellInfo(null);
    if (S.pos) scheduleHint();
    updateSubmit();
    setTimeout(() => S.map.invalidateSize(), 50);
  }

  function setSub(s) {
    S.sub = s;
    $$("#manual-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.s === s)));
    $("#manual-fields").classList.toggle("hidden", s !== "fields");
    $("#manual-paste").classList.toggle("hidden", s !== "paste");
    if (S.marker) S.marker.dragging[draggable() ? "enable" : "disable"]();
    updateCommonVisibility();
    updateSubmit();
  }

  function updateCommonVisibility() {
    // в режиме вставки строк RSRP и время берутся из самих строк
    $("#common").classList.toggle("hidden", S.method === "manual" && S.sub === "paste");
  }

  function parseCoord(text) {
    const v = Number(String(text).trim().replace(",", "."));
    return text.trim() !== "" && Number.isFinite(v) ? v : null;
  }

  function onCoordInput() {
    for (const id of ["#in-lat", "#in-lon"]) {
      const el = $(id);
      const fixed = el.value.replace(",", ".");
      if (fixed !== el.value) el.value = fixed;
    }
    const lat = parseCoord($("#in-lat").value);
    const lon = parseCoord($("#in-lon").value);
    if (lat !== null && lon !== null && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
      setPos(lat, lon);
      if (inBoundsLocal(lat, lon)) S.map.panTo([lat, lon]);
    } else if (S.pos) {
      clearPos();
    }
  }

  // ---------------------------------------------------------------- запуск

  function bind() {
    $$("#method-tabs button").forEach((b) => b.addEventListener("click", () => setMethod(b.dataset.m)));
    $$("#manual-tabs button").forEach((b) => b.addEventListener("click", () => setSub(b.dataset.s)));
    $("#center-btn").addEventListener("click", fitDistrict);
    $("#gps-btn").addEventListener("click", startGps);
    $("#submit").addEventListener("click", submit);
    $("#paste-check").addEventListener("click", checkPaste);
    $("#paste-example").addEventListener("click", () => {
      $("#paste-text").value = $("#paste-example").textContent;
      S.paste.ok = 0;
      $("#paste-result").innerHTML = "";
      updateSubmit();
    });
    $("#paste-text").addEventListener("input", () => {
      if ($("#paste-text").value !== S.paste.checkedText) {
        S.paste.ok = 0;
        updateSubmit();
      }
    });
    $("#in-lat").addEventListener("input", onCoordInput);
    $("#in-lon").addEventListener("input", onCoordInput);
    $$(".rsrp .step").forEach((b) => b.addEventListener("click", () => setRsrp(S.rsrp + Number(b.dataset.d))));
    $("#rsrp-abs").addEventListener("input", (ev) => {
      const raw = ev.target.value.replace(",", ".").replace(/[^0-9.]/g, "");
      if (raw !== ev.target.value) ev.target.value = raw;
      if (raw !== "" && Number.isFinite(Number(raw))) setRsrp(-Math.abs(Number(raw)), "input");
    });
    $("#rsrp-abs").addEventListener("blur", () => ($("#rsrp-abs").value = String(Math.abs(S.rsrp))));
    $("#rsrp-range").addEventListener("input", (ev) => setRsrp(Number(ev.target.value), "range"));
    $("#time-change").addEventListener("click", () => {
      const custom = S.timeMode !== "custom";
      S.timeMode = custom ? "custom" : "now";
      $("#time-input").classList.toggle("hidden", !custom);
      $("#time-change").textContent = custom ? "сейчас" : "изменить";
      if (custom && !$("#time-input").value) $("#time-input").value = isoWithOffset(new Date(), S.cfg.meta.utc_offset_minutes).slice(0, 16);
      renderTime();
    });
    $("#device-label").value = store.get(KEYS.label, "");
    $("#device-label").addEventListener("input", (ev) => store.set(KEYS.label, ev.target.value));
    $("#recent").addEventListener("click", (ev) => {
      const b = ev.target.closest("button.del");
      if (b) deleteRecent(b.dataset.id);
    });
    setInterval(() => {
      if (S.timeMode === "now" && !document.hidden) renderTime();
    }, 30000);
  }

  async function init() {
    try {
      S.cfg = await api("/api/field/config");
    } catch (e) {
      $("#cellinfo").textContent = isNetworkError(e) ? "Нет соединения с сервером. Обновите страницу, когда сеть появится." : e.message;
      $("#cellinfo").classList.add("bad");
      return;
    }
    $("#rsrp-range").min = S.cfg.meta.rsrp_min;
    $("#rsrp-range").max = S.cfg.meta.rsrp_max;
    initMap();
    bind();
    setRsrp(S.rsrp);
    renderTime();
    renderRecent();
    renderCellInfo(null);
    updateSubmit();
    api("/api/field/version").then((v) => ($("#count").textContent = v.count)).catch(() => {});
  }

  document.addEventListener("DOMContentLoaded", init);
})();
