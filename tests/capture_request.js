/* Capture the exact request body the page sends, and the response it gets.
 *
 * Used to debug a 400 from the UI: this prints the real payload rather than a
 * reconstruction, so a bug in buildRequest() cannot hide behind a stub.
 *
 * Usage: node capture_request.js <base-url>
 */
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:10000";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  page.on("pageerror", (e) => console.log("PAGEERROR:", String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") console.log("CONSOLE ERROR:", m.text());
  });

  await page.goto(BASE + "/", { waitUntil: "networkidle" });

  // Intercept the outgoing request so we can print exactly what was sent.
  const captured = [];
  page.on("request", (req) => {
    if (req.url().includes("/optimize-energy")) {
      captured.push({ url: req.url(), body: req.postData() });
    }
  });

  let response = null;
  page.on("response", async (res) => {
    if (res.url().includes("/optimize-energy")) {
      let text = "";
      try { text = await res.text(); } catch { /* ignore */ }
      response = { status: res.status(), text };
    }
  });

  await page.click("#run");
  await page.waitForTimeout(8000);

  console.log("\n=== request captured ===");
  if (!captured.length) {
    console.log("NO REQUEST WAS SENT — the handler threw before fetch()");
  }
  for (const c of captured) {
    console.log("URL:", c.url);
    if (c.body) {
      const parsed = JSON.parse(c.body);
      console.log("body keys      :", Object.keys(parsed));
      console.log("scenario_id    :", parsed.scenario_id);
      console.log("operator_notes :", JSON.stringify(parsed.operator_notes));
      console.log("hours length   :", parsed.hours ? parsed.hours.length : "MISSING");
      console.log("battery        :", JSON.stringify(parsed.battery));
      console.log("\n--- full body ---\n" + c.body);
    } else {
      console.log("(no body)");
    }
  }

  console.log("\n=== response ===");
  console.log(response ? `HTTP ${response.status}\n${response.text.slice(0, 500)}` : "(none)");

  const errVisible = await page.locator("#error").isVisible();
  if (errVisible) {
    console.log("\nUI error text:", (await page.locator("#error").textContent()).trim());
  }

  await browser.close();
})().catch((e) => { console.error("harness error:", e); process.exit(2); });
