/* GridWise frontend logic.
 *
 * Deliberately dependency-free and CDN-free: the judging environment may be
 * offline, and Render's free tier should not need to fetch anything to render
 * this page. Everything here talks to POST /optimize-energy on the same origin.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // Kept in sync with app/models.py. A drift here would show up as a 400/422.
  const MAX_NOTES = 3;

  const DEFAULT_NOTES = [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM.",
    "The cafeteria menu changes tomorrow.",
  ].join("\n");

  const TRADE_SHOW_NOTES = [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM for emergency operations.",
    "The library is extending book-return hours next week.",
  ].join("\n");

  const DEFAULT_BATTERY = {
    capacity_kwh: 500,
    initial_energy_kwh: 200,
    minimum_energy_kwh: 50,
    max_charge_kwh_per_hour: 100,
    max_discharge_kwh_per_hour: 100,
  };

  /* A plausible campus day: two demand peaks (midday, evening), solar through
     the middle of the day, and a tariff curve that peaks in the evening. */
  function defaultHours() {
    return Array.from({ length: 24 }, (_, h) => {
      const solar = h < 6 || h > 18 ? 0 : Math.round(150 * Math.sin(((h - 6) / 12) * Math.PI));
      const evening = h >= 17 && h <= 21 ? 60 : 0;
      const midday = h >= 9 && h <= 15 ? 30 : 0;
      const demand = 140 + evening + midday + (h % 3) * 5;
      let tariff = 6;
      if (h >= 6 && h <= 10) tariff = 9 + (h - 6);
      else if (h >= 11 && h <= 15) tariff = 15 + (15 - h);
      else if (h >= 16 && h <= 21) tariff = 19 + (h - 16) * 2;
      else if (h >= 22) tariff = 11 - (h - 22);
      return { hour: h, demand_kwh: demand, solar_kwh: solar, tariff_bdt_per_kwh: tariff };
    });
  }

  const fmt = (n, dp = 2) =>
    Number.isFinite(n) ? n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "–";

  const hhmm = (h) => `${String(h).padStart(2, "0")}:00`;

  /* Colour the graph with the live theme tokens rather than hard-coded hex, so
     the chart follows the light/dark toggle. */
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ------------------------------------------------------------------ hours */
  function buildHoursTable() {
    const tbody = $("hours-body");
    tbody.textContent = "";
    for (const row of defaultHours()) {
      const tr = document.createElement("tr");
      tr.dataset.hour = String(row.hour);
      tr.innerHTML =
        `<td>${hhmm(row.hour)}</td>` +
        ["demand_kwh", "solar_kwh", "tariff_bdt_per_kwh"]
          .map((k) => `<td><input type="number" step="any" min="0" value="${row[k]}" data-field="${k}" aria-label="${k} at ${hhmm(row.hour)}"></td>`)
          .join("");
      tbody.appendChild(tr);
    }
  }

  function readHours() {
    return [...$("hours-body").querySelectorAll("tr")].map((tr) => {
      const get = (f) => {
        const el = tr.querySelector(`input[data-field="${f}"]`);
        return el ? parseFloat(el.value) : NaN;
      };
      return {
        hour: parseInt(tr.dataset.hour, 10),
        demand_kwh: get("demand_kwh"),
        solar_kwh: get("solar_kwh"),
        tariff_bdt_per_kwh: get("tariff_bdt_per_kwh"),
      };
    });
  }

  function applyBattery(b) {
    $("b-capacity").value = b.capacity_kwh;
    $("b-initial").value = b.initial_energy_kwh;
    $("b-minimum").value = b.minimum_energy_kwh;
    $("b-charge").value = b.max_charge_kwh_per_hour;
    $("b-discharge").value = b.max_discharge_kwh_per_hour;
  }

  function readBattery() {
    return {
      capacity_kwh: parseFloat($("b-capacity").value),
      initial_energy_kwh: parseFloat($("b-initial").value),
      minimum_energy_kwh: parseFloat($("b-minimum").value),
      max_charge_kwh_per_hour: parseFloat($("b-charge").value),
      max_discharge_kwh_per_hour: parseFloat($("b-discharge").value),
    };
  }

  /* --------------------------------------------------------------- request */
  function buildRequest() {
    const noteLines = readNoteLines();
    return {
      scenario_id: "GRID-WEB-" + new Date().toISOString().slice(11, 19).replace(/:/g, ""),
      operator_notes: noteLines,
      hours: readHours(),
      battery: readBattery(),
    };
  }

  /* Read the notes textarea as a clean list of non-empty lines. */
  function readNoteLines() {
    return $("notes").value.split("\n").map((s) => s.trim()).filter(Boolean);
  }

  function updateNoteCount() {
    const n = $("notes").value.split("\n").map((s) => s.trim()).filter(Boolean).length;
    $("note-count").textContent = `${n} / ${MAX_NOTES} notes`;
    $("note-count").style.color = n > MAX_NOTES ? cssVar("--error") : "";
  }

  /* --------------------------------------------------------------- display */
  function showError(title, detail) {
    const box = $("error");
    box.textContent = "";
    const strong = document.createElement("strong");
    strong.textContent = title;
    box.appendChild(strong);
    if (detail) {
      const p = document.createElement("div");
      p.textContent = detail;
      box.appendChild(p);
    }
    box.hidden = false;
    $("empty").hidden = true;
    $("results").hidden = true;
  }

  function clearError() { $("error").hidden = true; }

  /* Turn a non-200 body into a readable explanation. The API uses a small,
     documented taxonomy, so we can be specific rather than dumping JSON. */
  function describeFailure(status, body) {
    const asText = typeof body === "string" ? body : JSON.stringify(body);
    if (status === 400) return "The request was rejected as malformed or structurally invalid.";
    if (status === 422) return typeof body === "object" && body && body.error
      ? body.error
      : "The request was well-formed but semantically invalid.";
    if (status === 500) return "The server hit a controlled internal error. Try again.";
    if (status === 404) return "Endpoint not found — check that the service is deployed at this address.";
    return asText ? asText.slice(0, 400) : `Unexpected HTTP ${status}.`;
  }

  function renderDirectives(list) {
    const ul = $("directives");
    ul.textContent = "";
    for (const d of list) {
      const li = document.createElement("li");
      li.className = d.applies ? "applies" : "noop";

      const head = document.createElement("div");
      head.className = "dir-head";
      const type = document.createElement("span");
      type.className = "dir-type";
      type.textContent = d.directive_type;
      head.appendChild(type);
      const note = document.createElement("span");
      note.className = "dir-note";
      note.textContent = `note ${d.note_index + 1}`;
      head.appendChild(note);
      if (!d.applies) {
        const off = document.createElement("span");
        off.className = "badge badge-idle";
        off.textContent = "not applicable";
        head.appendChild(off);
      }
      li.appendChild(head);

      if (d.structured_adjustment) {
        const adj = document.createElement("div");
        adj.className = "dir-adj";
        const a = d.structured_adjustment;
        const parts = [];
        if (Array.isArray(a.hours)) {
          parts.push(`hours ${a.hours.map(hhmm).join(", ")}`);
        }
        for (const [k, v] of Object.entries(a)) {
          if (k !== "hours") parts.push(`${k}=${v}`);
        }
        adj.textContent = parts.join("  ·  ");
        li.appendChild(adj);
      }

      const why = document.createElement("div");
      why.className = "dir-why";
      why.textContent = d.explanation;
      li.appendChild(why);

      ul.appendChild(li);
    }
  }

  function renderPlan(plan, directives) {
    const tbody = $("plan-body");
    tbody.textContent = "";

    // Hours touched by each directive type, so the table can show why a row matters.
    const marks = {};
    for (const d of directives) {
      if (!d.applies || !d.structured_adjustment || !Array.isArray(d.structured_adjustment.hours)) continue;
      const cls = {
        solar_reduction: "dir-solar",
        max_grid_window: "dir-grid",
        no_charge_window: "dir-nochg",
        no_discharge_window: "dir-nodis",
        minimum_battery_reserve: "dir-charge",
      }[d.directive_type];
      for (const h of d.structured_adjustment.hours) {
        marks[h] = marks[h] || new Set();
        if (cls) marks[h].add(cls);
      }
    }

    for (const e of plan) {
      const tr = document.createElement("tr");
      const cls = marks[e.hour] ? [...marks[e.hour]].join(" ") : "";
      if (cls) tr.className = cls;

      const badgeCls = { charge: "badge-charge", discharge: "badge-discharge", idle: "badge-idle" }[e.battery_action] || "badge-idle";
      tr.innerHTML =
        `<td class="c-hr">${hhmm(e.hour)}</td>` +
        `<td class="num">${fmt(e.grid_kwh)}</td>` +
        `<td class="num">${fmt(e.solar_used_kwh)}</td>` +
        `<td class="c-act"><span class="badge ${badgeCls}">${e.battery_action}</span></td>` +
        `<td class="num">${fmt(e.battery_kwh)}</td>` +
        `<td class="num">${fmt(e.battery_energy_after_kwh)}</td>`;
      tbody.appendChild(tr);
    }
  }

  /* A small hand-rolled SVG chart — avoids shipping a charting library for one
     job, and keeps working with no network. */
  function renderChart(plan) {
    const svg = $("chart");
    svg.textContent = "";
    const NS = "http://www.w3.org/2000/svg";
    const W = 720, H = 260;
    const pad = { l: 46, r: 12, t: 12, b: 26 };
    const iw = W - pad.l - pad.r;
    const ih = H - pad.t - pad.b;

    const mk = (tag, attrs = {}) => {
      const el = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      return el;
    };

    const maxGrid = Math.max(...plan.map((e) => e.grid_kwh), 1);
    const maxSolar = Math.max(...plan.map((e) => e.solar_used_kwh), 1);
    const maxBatt = Math.max(...plan.map((e) => e.battery_energy_after_kwh), 1);
    const scale = Math.max(maxGrid, maxSolar, maxBatt) * 1.08;

    const x = (h) => pad.l + (h / 23) * iw;
    const y = (v) => pad.t + ih - (v / scale) * ih;
    const bw = (iw / 24) * 0.6;

    const axisColor = cssVar("--border");
    const dimColor = cssVar("--text-dim");

    // horizontal gridlines + value labels
    for (let i = 0; i <= 4; i++) {
      const v = (scale / 4) * i;
      svg.appendChild(mk("line", {
        x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v),
        stroke: axisColor, "stroke-width": 1, "stroke-dasharray": i === 0 ? "0" : "3 4",
      }));
      const t = mk("text", { x: pad.l - 6, y: y(v) + 3.5, fill: dimColor, "font-size": 10, "text-anchor": "end", "font-family": "monospace" });
      t.textContent = Math.round(v);
      svg.appendChild(t);
    }

    // hour ticks every 3h
    for (let h = 0; h < 24; h += 3) {
      const t = mk("text", { x: x(h), y: H - 8, fill: dimColor, "font-size": 10, "text-anchor": "middle", "font-family": "monospace" });
      t.textContent = hhmm(h);
      svg.appendChild(t);
    }

    // grid bars
    for (const e of plan) {
      svg.appendChild(mk("rect", {
        x: x(e.hour) - bw / 2, y: y(e.grid_kwh), width: bw, height: pad.t + ih - y(e.grid_kwh),
        fill: cssVar("--grid"), opacity: 0.55, rx: 1.5,
      }));
    }
    // solar-used bars, overlaid
    for (const e of plan) {
      if (e.solar_used_kwh <= 0) continue;
      svg.appendChild(mk("rect", {
        x: x(e.hour) - bw / 2, y: y(e.solar_used_kwh), width: bw, height: pad.t + ih - y(e.solar_used_kwh),
        fill: cssVar("--solar"), opacity: 0.9, rx: 1.5,
      }));
    }
    // battery line
    const d = plan.map((e, i) => `${i ? "L" : "M"}${x(e.hour)},${y(e.battery_energy_after_kwh)}`).join(" ");
    svg.appendChild(mk("path", { d, fill: "none", stroke: cssVar("--batt"), "stroke-width": 2, "stroke-linejoin": "round" }));
    for (const e of plan) {
      svg.appendChild(mk("circle", { cx: x(e.hour), cy: y(e.battery_energy_after_kwh), r: 2.2, fill: cssVar("--batt") }));
    }
  }

  function renderResult(data) {
    $("plan-summary").textContent = data.plan_summary;
    $("m-cost").textContent = fmt(data.total_cost_bdt);
    $("m-grid").textContent = fmt(data.total_grid_kwh);
    $("m-peak").textContent = fmt(data.peak_grid_kwh);
    renderDirectives(data.directive_interpretation);
    renderPlan(data.hourly_plan, data.directive_interpretation);
    renderChart(data.hourly_plan);
    $("raw-json").textContent = JSON.stringify(data, null, 2);
    clearError();
    $("empty").hidden = true;
    $("results").hidden = false;
  }

  /* ---------------------------------------------------------------- health */
  async function checkHealth() {
    const pill = $("status");
    const text = $("status-text");
    try {
      const r = await fetch("/health", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      const j = await r.json();
      pill.className = "pill pill-ok";
      text.textContent = j.status === "ok" ? "service ready" : String(j.status);
    } catch {
      pill.className = "pill pill-bad";
      text.textContent = "service unreachable";
    }
  }

  /* ------------------------------------------------------------------ run */
  async function run() {
    const noteLines = readNoteLines();
    if (!noteLines.length) { showError("Add at least one operator note."); return; }
    if (noteLines.length > MAX_NOTES) { showError(`Too many notes (${noteLines.length}).`, `The maximum is ${MAX_NOTES}.`); return; }

    const btn = $("run");
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "Optimizing…";
    clearError();

    try {
      const resp = await fetch("/optimize-energy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildRequest()),
      });
      let body;
      const text = await resp.text();
      try { body = JSON.parse(text); } catch { body = text; }

      if (!resp.ok) {
        showError(`HTTP ${resp.status} — ${resp.statusText || "request failed"}`, describeFailure(resp.status, body));
        return;
      }
      renderResult(body);
    } catch (err) {
      // A network failure here usually means the free instance is cold-starting.
      showError("Could not reach the service.", String(err && err.message ? err.message : err));
    } finally {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.textContent = "Optimize schedule";
    }
  }

  /* ----------------------------------------------------------------- init */
  function init() {
    buildHoursTable();
    applyBattery(DEFAULT_BATTERY);
    $("notes").value = DEFAULT_NOTES;
    updateNoteCount();

    $("notes").addEventListener("input", updateNoteCount);
    $("run").addEventListener("click", run);
    $("load-sample").addEventListener("click", () => {
      $("notes").value = TRADE_SHOW_NOTES;
      updateNoteCount();
    });
    $("reset").addEventListener("click", () => {
      buildHoursTable();
      applyBattery(DEFAULT_BATTERY);
      $("notes").value = DEFAULT_NOTES;
      updateNoteCount();
      clearError();
    });
    $("export").addEventListener("click", async () => {
      const json = JSON.stringify(buildRequest(), null, 2);
      const btn = $("export");
      try {
        await navigator.clipboard.writeText(json);
        btn.textContent = "Copied";
      } catch {
        btn.textContent = "Copy failed";
      }
      setTimeout(() => { btn.textContent = "Copy request JSON"; }, 1400);
    });
    $("export").disabled = false;

    // Chart uses theme tokens; redraw it when the theme flips.
    let lastResult = null;
    const origRender = renderResult;
    window.__gridwise_last = null;
    $("theme-toggle").addEventListener("click", () => {
      const root = document.documentElement;
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("gridwise-theme", root.dataset.theme); } catch { /* ignore */ }
      // Redraw if a chart is currently on screen.
      const planBody = $("plan-body");
      if ($("results").hidden === false && planBody.children.length) {
        const raw = $("raw-json").textContent;
        try { renderChart(JSON.parse(raw).hourly_plan); } catch { /* ignore */ }
      }
    });
    try {
      const saved = localStorage.getItem("gridwise-theme");
      if (saved) document.documentElement.dataset.theme = saved;
    } catch { /* ignore */ }

    checkHealth();
    setInterval(checkHealth, 60000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
