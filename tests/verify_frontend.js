/* Render the GridWise frontend in a real browser and verify it works.
 *
 * Checks what static analysis cannot: that the JS actually executes, the chart
 * produces SVG geometry, and pressing the button fills the result panel.
 * Writes a screenshot for visual review.
 *
 * Usage:
 *   node verify_frontend.js <base-url> <out-dir>
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:10000";
const OUT = process.argv[3] || ".";

const failures = [];
const check = (label, ok, detail = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  — " + detail : ""}`);
  if (!ok) failures.push(label);
};

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1400 } });

  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("requestfailed", (r) => failedRequests.push(`${r.url()} ${r.failure()?.errorText}`));

  console.log(`\n=== load ${BASE}/ ===`);
  const resp = await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 30000 });
  check("GET / returns 200", resp.status() === 200, `status ${resp.status()}`);
  check("no JS exceptions on load", pageErrors.length === 0, pageErrors.join("; "));
  check("no console errors on load", consoleErrors.length === 0, consoleErrors.join("; "));
  check("no failed requests", failedRequests.length === 0, failedRequests.join("; "));

  const title = await page.title();
  check("title rendered", title.includes("GridWise"), title);

  // The hours table must be built by JS: 24 rows with 3 inputs each.
  const rowCount = await page.locator("#hours-body tr").count();
  check("hours table has 24 rows", rowCount === 24, `got ${rowCount}`);
  const inputCount = await page.locator("#hours-body input").count();
  check("hours table has 72 inputs", inputCount === 72, `got ${inputCount}`);

  // Health pill must be driven by the live /health call, not hard-coded.
  await page.waitForTimeout(1200);
  const statusText = (await page.locator("#status-text").textContent()).trim();
  const statusClass = await page.locator("#status").getAttribute("class");
  check("health pill resolved", statusText.length > 0 && statusText !== "checking…", statusText);
  check("health pill shows ready state", statusClass.includes("pill-ok"), statusClass);

  await page.screenshot({ path: path.join(OUT, "01-initial.png"), fullPage: true });

  // Light theme must be reachable and legible.
  console.log(`\n=== theme toggle ===`);
  await page.click("#theme-toggle");
  await page.waitForTimeout(250);
  const theme = await page.getAttribute("html", "data-theme");
  check("theme toggles to light", theme === "light", theme);
  await page.screenshot({ path: path.join(OUT, "02-light.png"), fullPage: true });
  await page.click("#theme-toggle");
  await page.waitForTimeout(250);
  const theme2 = await page.getAttribute("html", "data-theme");
  check("theme toggles back to dark", theme2 === "dark", theme2);

  // The trade-show button must replace the notes.
  console.log(`\n=== sample notes button ===`);
  const before = await page.inputValue("#notes");
  await page.click("#load-sample");
  await page.waitForTimeout(200);
  const after = await page.inputValue("#notes");
  check("sample notes replaced the textarea", before !== after && after.includes("rooftop solar"));

  // Restore the default notes for the run.
  await page.click("#reset");
  await page.waitForTimeout(200);
  const noteCount = await page.locator("#note-count").textContent();
  check("note counter updated", noteCount.includes("3 / 3"), noteCount.trim());

  // --- the real thing: press Optimize and wait for the API ------------------
  console.log(`\n=== run the optimizer (live API) ===`);
  const t0 = Date.now();
  await page.click("#run");
  await page.waitForSelector("#results:not([hidden])", { timeout: 90000 }).catch(() => {});
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

  const resultVisible = await page.locator("#results").isVisible();
  const errorVisible = await page.locator("#error").isVisible();
  if (errorVisible) {
    const errText = (await page.locator("#error").textContent()).trim();
    check("no error shown after run", false, errText.slice(0, 300));
  }
  check("result panel appeared", resultVisible, `${elapsed}s`);
  check("no JS exceptions during run", pageErrors.length === 0, pageErrors.join("; "));

  if (resultVisible) {
    const cost = (await page.locator("#m-cost").textContent()).trim();
    const grid = (await page.locator("#m-grid").textContent()).trim();
    const peak = (await page.locator("#m-peak").textContent()).trim();
    check("cost metric populated", cost !== "–" && cost.length > 0, cost);
    check("grid metric populated", grid !== "–" && grid.length > 0, grid);
    check("peak metric populated", peak !== "–" && peak.length > 0, peak);

    const summary = (await page.locator("#plan-summary").textContent()).trim();
    check("plan summary rendered", summary.length > 20, summary.slice(0, 90));

    const dirCount = await page.locator("#directives li").count();
    check("directive cards rendered", dirCount >= 1, `${dirCount} cards`);

    const planRows = await page.locator("#plan-body tr").count();
    check("schedule table has 24 rows", planRows === 24, `got ${planRows}`);

    // The chart must have produced real geometry, not an empty SVG.
    const rects = await page.locator("#chart rect").count();
    const paths = await page.locator("#chart path").count();
    const circles = await page.locator("#chart circle").count();
    check("chart drew bars", rects > 0, `${rects} rects`);
    check("chart drew the battery line", paths > 0, `${paths} paths`);
    check("chart drew battery points", circles === 24, `${circles} circles`);

    // Directive hours must be highlighted in the schedule table.
    const highlighted = await page.locator("#plan-body tr[class^='dir-']").count();
    check("directive hours highlighted in table", highlighted > 0, `${highlighted} rows`);

    const rawJson = (await page.locator("#raw-json").textContent()).trim();
    let parsed = null;
    try { parsed = JSON.parse(rawJson); } catch { /* handled below */ }
    check("raw JSON is valid", parsed !== null);
    if (parsed) {
      check("raw JSON has 24 hourly entries", parsed.hourly_plan.length === 24);
      const sumGrid = parsed.hourly_plan.reduce((a, e) => a + e.grid_kwh, 0);
      check(
        "sum(grid_kwh) equals total_grid_kwh",
        Math.abs(sumGrid - parsed.total_grid_kwh) < 0.05,
        `${sumGrid.toFixed(2)} vs ${parsed.total_grid_kwh}`
      );
      check(
        "battery returns to initial energy",
        Math.abs(parsed.hourly_plan[23].battery_energy_after_kwh - 200) < 0.05,
        String(parsed.hourly_plan[23].battery_energy_after_kwh)
      );
    }

    await page.screenshot({ path: path.join(OUT, "03-result.png"), fullPage: true });
    await page.locator("#chart").screenshot({ path: path.join(OUT, "04-chart.png") }).catch(() => {});
  }

  // Mobile layout must not overflow horizontally.
  console.log(`\n=== responsive ===`);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.waitForTimeout(400);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  check("no horizontal overflow at 390px", overflow <= 2, `${overflow}px`);

  // Name the offending element(s) so a regression is actionable rather than a bare
  // pixel count. Must run AFTER the width check, because measuring forces layout.
  if (overflow > 2) {
    const offenders = await page.evaluate(() => {
      const docWidth = document.documentElement.clientWidth;
      const out = [];
      for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.right > docWidth + 1 || r.left < -1) {
          out.push({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            cls: (el.getAttribute("class") || "").slice(0, 60) || null,
            left: Math.round(r.left),
            right: Math.round(r.right),
            width: Math.round(r.width),
          });
        }
      }
      // Keep only the outermost offenders; children inherit their parent's overflow.
      return out.sort((a, b) => b.right - a.right).slice(0, 8);
    });
    console.log("  elements extending past the viewport:");
    for (const o of offenders) {
      console.log(
        `    ${o.tag}${o.id ? "#" + o.id : ""}${o.cls ? "." + o.cls.split(/\s+/).join(".") : ""}` +
          `  left=${o.left} right=${o.right} w=${o.width}`
      );
    }
  }

  await page.screenshot({ path: path.join(OUT, "05-mobile.png"), fullPage: true });

  // ------------------------------------------------------------------------
  // Paste-a-whole-JSON panel. This is the judge-facing path, so exercise both
  // the happy route and the failure routes a mistake would land on.
  // ------------------------------------------------------------------------
  console.log(`\n=== paste-a-JSON panel ===`);
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.waitForTimeout(200);

  // The sample button must produce something that loads.
  await page.click("#json-sample");
  await page.waitForTimeout(300);
  const sampleVal = await page.inputValue("#json-input");
  let sampleObj = null;
  try { sampleObj = JSON.parse(sampleVal); } catch { /* stays null */ }
  check("sample button fills the textarea with valid JSON", sampleObj !== null);
  check("sample has 24 hours", sampleObj && sampleObj.hours && sampleObj.hours.length === 24);
  const statusKind = await page.getAttribute("#json-status", "class");
  check("sample loads with an OK status", statusKind.includes("alert-ok"), statusKind);
  check("Fill-the-form button is enabled after a good load",
    !(await page.isDisabled("#json-fill")));
  await page.screenshot({ path: path.join(OUT, "06-paste-panel.png"), fullPage: true });

  // Malformed JSON must be reported with a useful, honest message.
  //
  // V8 only reports an offset for some syntax errors, so assert the two things
  // that must always hold: the error is surfaced, and it either names a position
  // or explains that the input is malformed. Asserting a line/column
  // unconditionally would force the page to invent one.
  await page.fill("#json-input", '{"scenario_id": "x", "operator_notes": [}');
  await page.click("#json-load");
  await page.waitForTimeout(250);
  const badJsonStatus = await page.textContent("#json-status");
  check("malformed JSON is reported", badJsonStatus.includes("not valid JSON"), badJsonStatus.slice(0, 80));
  check("malformed JSON error is actionable",
    /line \d+, column \d+/.test(badJsonStatus) || /line|truncated|closing/i.test(badJsonStatus),
    badJsonStatus.slice(0, 130));
  check("Fill-the-form is disabled after a bad load", await page.isDisabled("#json-fill"));

  // A multi-line paste in the "Unexpected token" class has no engine position,
  // but the message does quote the offending character, which is what the reader
  // needs. Assert that, rather than a line/column the engine never provides.
  const multiLine = '{\n  "scenario_id": "x",\n  "operator_notes": ["a",],\n  "hours": []\n}';
  await page.fill("#json-input", multiLine);
  await page.click("#json-load");
  await page.waitForTimeout(250);
  const mlStatus = await page.textContent("#json-status");
  check("multi-line malformed JSON quotes the offending token",
    /Unexpected token/.test(mlStatus) && /line/.test(mlStatus), mlStatus.slice(0, 140));

  // ...and the "position" class must render an actual line/column. Accept either
  // shape: V8 sometimes appends its own "(line L column C)", and we supply one
  // when it does not. What must never happen is a positioned error with no
  // position shown, or one printed twice.
  await page.fill("#json-input", '{\n  "scenario_id": "x",\n  "operator_notes": ["a"]\n  "hours": []\n}');
  await page.click("#json-load");
  await page.waitForTimeout(250);
  const posStatus = await page.textContent("#json-status");
  const lineColHits = posStatus.match(/line \d+\)?[,\s]+column \d+/g) || [];
  check("a positioned syntax error renders line/column",
    lineColHits.length >= 1, posStatus.slice(0, 150));
  check("the position is not printed twice",
    lineColHits.length === 1, `found ${lineColHits.length}: ${lineColHits.join(" | ")}`);

  // A structurally-valid-but-wrong body must name the offending field.
  const wrongHourCount = JSON.stringify({
    scenario_id: "GRID-BAD",
    operator_notes: ["a note"],
    hours: [{ hour: 0, demand_kwh: 1, solar_kwh: 0, tariff_bdt_per_kwh: 1 }],
    battery: {
      capacity_kwh: 500, initial_energy_kwh: 200, minimum_energy_kwh: 50,
      max_charge_kwh_per_hour: 100, max_discharge_kwh_per_hour: 100,
    },
  });
  await page.fill("#json-input", wrongHourCount);
  await page.click("#json-load");
  await page.waitForTimeout(250);
  const wrongStatus = await page.textContent("#json-status");
  check("wrong hour count names 'hours'", wrongStatus.includes("hours"), wrongStatus.slice(0, 90));
  check("wrong hour count says 24", wrongStatus.includes("24"), wrongStatus.slice(0, 90));

  // An unknown key (a typo) must be named rather than surfacing as a bare 422.
  // Keep the valid body intact and ADD a stray key, which is what an actual
  // typo looks like — otherwise the missing-field error masks the real problem.
  const typoBody = JSON.parse(sampleVal);
  typoBody.noets = typoBody.operator_notes;
  await page.fill("#json-input", JSON.stringify(typoBody));
  await page.click("#json-load");
  await page.waitForTimeout(250);
  const typoStatus = await page.textContent("#json-status");
  check("an unknown key is named", typoStatus.includes("noets"), typoStatus.slice(0, 90));

  // Fill-the-form must move the pasted values into the visible inputs.
  await page.click("#json-clear");
  await page.waitForTimeout(150);
  check("Clear empties the textarea", (await page.inputValue("#json-input")) === "");

  await page.fill("#json-input", sampleVal);
  await page.click("#json-load");
  await page.waitForTimeout(250);
  await page.click("#json-fill");
  await page.waitForTimeout(300);
  const filledHours = await page.locator("#hours-body tr").count();
  check("Fill-the-form repopulates 24 rows", filledHours === 24, `got ${filledHours}`);
  const filledCapacity = await page.inputValue("#b-capacity");
  check("Fill-the-form sets the battery fields", Number(filledCapacity) === sampleObj.battery.capacity_kwh,
    `got ${filledCapacity}`);
  const filledNotes = await page.inputValue("#notes");
  check("Fill-the-form sets the notes", filledNotes.split("\n").filter(Boolean).length ===
    sampleObj.operator_notes.length, filledNotes.slice(0, 60));

  // And the round trip: send the pasted JSON verbatim.
  //
  // Observe the outgoing request rather than waiting on #results — that panel is
  // already visible from the earlier form run, so waiting on it reads a STALE
  // result and cannot tell which payload produced it.
  await page.fill("#json-input", sampleVal);
  const sentForPaste = page.waitForRequest(
    (r) => r.url().includes("optimize-energy") && r.method() === "POST",
    { timeout: 120000 }
  );
  await page.click("#json-optimize");
  const pasteReq = await sentForPaste;
  let pasteSent = null;
  try { pasteSent = JSON.parse(pasteReq.postData()); } catch { /* stays null */ }
  check("the pasted panel sends the pasted JSON, not the form",
    pasteSent !== null && pasteSent.scenario_id === sampleObj.scenario_id,
    pasteSent ? `sent scenario_id ${pasteSent.scenario_id}, expected ${sampleObj.scenario_id}` : "unparsable body");
  check("the pasted body carries its own 24 hours",
    pasteSent !== null && Array.isArray(pasteSent.hours) && pasteSent.hours.length === 24);
  check("the pasted body carries its own notes",
    pasteSent !== null && Array.isArray(pasteSent.operator_notes) &&
    pasteSent.operator_notes.length === sampleObj.operator_notes.length);

  // Now confirm the rendered result matches that same scenario_id.
  await page.waitForFunction(
    (sid) => {
      const raw = document.getElementById("raw-json");
      if (!raw || !raw.textContent) return false;
      try { return JSON.parse(raw.textContent).scenario_id === sid; } catch { return false; }
    },
    sampleObj.scenario_id,
    { timeout: 120000 }
  ).catch(() => {});
  const pastedRaw = await page.textContent("#raw-json");
  let pastedParsed = null;
  try { pastedParsed = JSON.parse(pastedRaw); } catch { /* stays null */ }
  check("Optimize-this-JSON returns a plan", pastedParsed !== null &&
    Array.isArray(pastedParsed.hourly_plan) && pastedParsed.hourly_plan.length === 24,
    pastedParsed ? `hours ${pastedParsed.hourly_plan && pastedParsed.hourly_plan.length}` : "no JSON");
  check("the pasted path echoes its own scenario_id",
    pastedParsed && pastedParsed.scenario_id === sampleObj.scenario_id,
    pastedParsed ? String(pastedParsed.scenario_id) : "n/a");
  await page.screenshot({ path: path.join(OUT, "07-paste-result.png"), fullPage: true });

  const finalErrors = pageErrors.concat(consoleErrors);
  check("no errors across the whole session", finalErrors.length === 0, finalErrors.join("; "));

  await browser.close();

  console.log(`\n${failures.length ? "FAILED: " + failures.join(", ") : "ALL BROWSER CHECKS PASSED"}`);
  process.exit(failures.length ? 1 : 0);
})().catch((err) => {
  console.error("harness error:", err);
  process.exit(2);
});
