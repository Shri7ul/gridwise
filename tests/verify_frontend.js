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

  const finalErrors = pageErrors.concat(consoleErrors);
  check("no errors across the whole session", finalErrors.length === 0, finalErrors.join("; "));

  await browser.close();

  console.log(`\n${failures.length ? "FAILED: " + failures.join(", ") : "ALL BROWSER CHECKS PASSED"}`);
  process.exit(failures.length ? 1 : 0);
})().catch((err) => {
  console.error("harness error:", err);
  process.exit(2);
});
