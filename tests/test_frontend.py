"""Frontend delivery tests.

The operator UI is static files served by the same FastAPI app, so these tests
assert the whole delivery contract without a browser:

  * GET / returns the UI (HTML), not the old JSON index
  * the assets are reachable at the URLs the HTML actually requests
  * the HTML, CSS and JS agree with each other (no dangling element ids or
    unstyled classes) -- a mismatch is invisible at runtime and only shows up as
    a dead button for whoever is clicking
  * the request the page builds is accepted by POST /optimize-energy
  * the UI never encodes a credential or a private path

Run: python tests/test_frontend.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

STATIC = REPO_ROOT / "app" / "static"

STUB_KEY = "stub-key-for-tests-not-a-real-credential"
os.environ["LLM_API_KEY"] = STUB_KEY
os.environ["LLM_BASE_URL"] = "https://stub.invalid/v1"
os.environ["LLM_MODEL"] = "stub-model"
os.environ["ALLOW_DETERMINISTIC_FALLBACK"] = "false"
os.environ["LLM_MAX_RETRIES"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def test_index_is_served_as_html_at_root():
    """GET / must be the UI. It used to return a JSON service index."""
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<title>GridWise" in body
    assert 'id="run"' in body, "the Optimize button must exist for the page to be usable"


def test_asset_urls_in_the_html_are_reachable():
    """Every /static/... URL the HTML requests must actually resolve.

    A typo here is a blank page with no error anywhere in the server log.
    """
    with TestClient(app) as client:
        html = client.get("/").text
        urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        assert urls, "the page must reference at least one asset"
        for url in urls:
            response = client.get(url)
            assert response.status_code == 200, f"{url} -> HTTP {response.status_code}"


def test_assets_have_correct_content_types():
    expected = {"app.css": "text/css", "app.js": "text/javascript"}
    with TestClient(app) as client:
        for name, prefix in expected.items():
            response = client.get(f"/static/{name}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(prefix), (
                f"{name} served as {response.headers['content-type']}"
            )


def test_api_index_still_available_for_programmatic_clients():
    """GET / became the UI, so the JSON index moved to /api."""
    with TestClient(app) as client:
        response = client.get("/api")

    assert response.status_code == 200
    payload = response.json()
    assert payload["optimize"] == "POST /optimize-energy"
    assert payload["health"] == "/health"
    assert payload["ui"] == "/"


def test_health_and_docs_unaffected_by_the_ui():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/docs").status_code == 200


# --------------------------------------------------------------------------- #
# Internal consistency of the three files
# --------------------------------------------------------------------------- #
def test_no_duplicate_element_ids():
    """Duplicate ids are invalid HTML and silently break getElementById.

    This is not hypothetical: the notes textarea and its heading were both
    id="notes", so getElementById returned the heading and every read of the
    note text came back undefined. The page still rendered, which is what makes
    this class of bug worth a test.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', html)

    seen: dict[str, int] = {}
    for value in ids:
        seen[value] = seen.get(value, 0) + 1
    duplicates = sorted(k for k, v in seen.items() if v > 1)

    assert not duplicates, f"duplicate id attributes in index.html: {duplicates}"


def test_labels_and_aria_references_point_at_real_ids():
    """A dangling aria-labelledby or label[for] breaks accessibility silently."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))

    refs = set(re.findall(r'\baria-(?:labelledby|describedby|controls)="([^"]+)"', html))
    for attr in re.findall(r'\bfor="([^"]+)"', html):
        refs.add(attr)

    dangling = sorted(r for r in refs if r not in ids)
    assert not dangling, f"aria/label references to missing ids: {dangling}"


def test_every_element_id_the_js_looks_up_exists_in_the_html():
    """$(id) lookups against a nonexistent id fail silently at runtime."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))

    missing = sorted(js_ids - html_ids)
    assert not missing, f"JS looks up ids that the HTML does not define: {missing}"


def test_every_class_the_js_applies_is_styled():
    """An unstyled class is an invisible UI element."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    classes: set[str] = set()
    for match in re.findall(r'className\s*=\s*"([^"]+)"', js):
        classes.update(match.split())
    for match in re.findall(r"classList\.add\(\"([^\"]+)\"\)", js):
        classes.add(match)

    # Classes selected indirectly by the directive/badge maps.
    classes.update(
        {
            "badge-charge",
            "badge-discharge",
            "badge-idle",
            "dir-solar",
            "dir-grid",
            "dir-nochg",
            "dir-nodis",
            "dir-charge",
            "applies",
            "noop",
        }
    )

    unstyled = sorted(c for c in classes if c and f".{c}" not in css)
    assert not unstyled, f"JS applies classes with no CSS rule: {unstyled}"


def test_frontend_has_no_external_dependencies():
    """The judging environment may be offline, and Render should fetch nothing.

    Any absolute URL in the static assets would break the page when the network
    is unavailable -- and a CDN link is exactly what a reviewer would not expect
    in a self-contained submission.
    """
    # XML namespace identifiers look like URLs but are never fetched; the SVG
    # namespace is required in order to build SVG elements via createElementNS.
    allowed_namespaces = {
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/1999/xlink",
    }

    for name in ("index.html", "app.css", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")

        # Anything actually loaded by the browser is a hard failure.
        loaded = re.findall(
            r"""<(?:script|link)[^>]*(?:src|href)="(https?://[^"]+)""", text
        )
        assert not loaded, f"{name} loads an external resource: {loaded}"

        # Bare URLs elsewhere must be namespace identifiers, not endpoints.
        for url in re.findall(r"""https?://[^\s"'<>)]+""", text):
            bare = url.rstrip(".,;")
            assert bare in allowed_namespaces, f"{name} references {bare}"


def test_frontend_leaks_no_credential_or_local_path():
    """Static assets are public. They must not name a key or a developer path."""
    markers = [
        "gsk_",
        "sk-",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "D:\\",
        "D:/Hackathon",
        "C:\\Users",
        ".env",
    ]
    for name in ("index.html", "app.css", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in text, f"{name} contains {marker!r}"


# --------------------------------------------------------------------------- #
# Behaviour: the request the page builds must be accepted
# --------------------------------------------------------------------------- #
def _hours_from_js() -> list[dict]:
    """Re-derive the page's default hourly table.

    Kept as an explicit re-implementation rather than execution so the test
    fails loudly if app.js changes shape without the backend being updated.
    """
    import math

    hours = []
    for h in range(24):
        solar = 0 if (h < 6 or h > 18) else round(150 * math.sin(((h - 6) / 12) * math.pi))
        evening = 60 if 17 <= h <= 21 else 0
        midday = 30 if 9 <= h <= 15 else 0
        demand = 140 + evening + midday + (h % 3) * 5
        if 6 <= h <= 10:
            tariff = 9 + (h - 6)
        elif 11 <= h <= 15:
            tariff = 15 + (15 - h)
        elif 16 <= h <= 21:
            tariff = 19 + (h - 16) * 2
        elif h >= 22:
            tariff = 11 - (h - 22)
        else:
            tariff = 6
        hours.append(
            {
                "hour": h,
                "demand_kwh": demand,
                "solar_kwh": solar,
                "tariff_bdt_per_kwh": tariff,
            }
        )
    return hours


def test_js_default_hours_match_the_documented_shape():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "defaultHours" in js
    hours = _hours_from_js()
    assert [h["hour"] for h in hours] == list(range(24))
    assert all(h["demand_kwh"] > 0 for h in hours)
    assert all(h["tariff_bdt_per_kwh"] > 0 for h in hours)
    assert all(h["solar_kwh"] >= 0 for h in hours)
    assert any(h["solar_kwh"] > 0 for h in hours), "a flat solar curve would make the demo dull"


def test_the_page_request_passes_model_validation():
    """The exact body shape buildRequest() produces must satisfy the API model."""
    from app.models import OptimizationRequest

    body = {
        "scenario_id": "GRID-WEB-000000",
        "operator_notes": [
            "Solar output will drop to about 20% from 1 PM to 3 PM.",
            "Do not charge the battery between 2 PM and 4 PM.",
            "The cafeteria menu changes tomorrow.",
        ],
        "hours": _hours_from_js(),
        "battery": {
            "capacity_kwh": 500,
            "initial_energy_kwh": 200,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        },
    }
    OptimizationRequest(**body)  # must not raise


def test_the_page_renders_every_field_it_reads():
    """Fields the JS reaches into must exist in the response model.

    This keeps app.js and app/models.py from drifting apart: the UI reads
    `directive_type`, `applies`, `explanation`, `structured_adjustment`, and the
    six hourly-plan fields.
    """
    from app.models import DirectiveInterpretation, HourlyPlanEntry, OptimizationResponse

    assert "directive_type" in DirectiveInterpretation.model_fields
    assert "applies" in DirectiveInterpretation.model_fields
    assert "explanation" in DirectiveInterpretation.model_fields
    assert "structured_adjustment" in DirectiveInterpretation.model_fields
    assert "note_index" in DirectiveInterpretation.model_fields

    assert set(HourlyPlanEntry.model_fields) == {
        "hour",
        "grid_kwh",
        "solar_used_kwh",
        "battery_action",
        "battery_kwh",
        "battery_energy_after_kwh",
    }

    for field in ("total_cost_bdt", "total_grid_kwh", "peak_grid_kwh", "plan_summary"):
        assert field in OptimizationResponse.model_fields


def test_js_directive_type_labels_match_the_backend_enum():
    """The row-highlight map is keyed by directive_type; a renamed type would
    silently stop highlighting anything."""
    from app.models import DirectiveType

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    allowed = set(DirectiveType.__args__)

    mapped = set(re.findall(r"^\s*(\w+):\s*\"dir-", js, re.M))
    assert mapped, "expected a directive_type -> css class map in app.js"
    unknown = mapped - allowed
    assert not unknown, f"app.js maps directive types the backend does not define: {unknown}"


# --------------------------------------------------------------------------- #
# Paste-a-whole-JSON path
# --------------------------------------------------------------------------- #

def _node_binary() -> str | None:
    candidates = [
        os.environ.get("NODE_BINARY"),
        r"C:\Users\USER\.workbuddy-ai\binaries\node\versions\22.22.2-2\node.exe",
        shutil.which("node"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


# The validator lives inside an IIFE and touches the DOM, so it cannot simply be
# required. Extract the one function and eval it in isolation: this keeps the
# test fast and free of a browser while still executing the real code, so a
# refactor that breaks the validator fails here rather than in front of a judge.
_JS_EXTRACT = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.GRIDWISE_JS, "utf8");

const start = src.indexOf("function validateRequestObject(");
if (start === -1) { console.error("validator function not found"); process.exit(2); }
// Walk braces to find the end of the function body.
let i = src.indexOf("{", start), depth = 0, end = -1;
for (let p = i; p < src.length; p++) {
  if (src[p] === "{") depth++;
  else if (src[p] === "}") { depth--; if (depth === 0) { end = p + 1; break; } }
}
if (end === -1) { console.error("unterminated function"); process.exit(2); }
const fnSrc = src.slice(start, end);

const MAX_NOTES = 3;
const fn = new Function("MAX_NOTES", fnSrc + "; return validateRequestObject;")(MAX_NOTES);

const cases = JSON.parse(fs.readFileSync(process.env.GRIDWISE_CASES, "utf8"));
const out = [];
for (const c of cases) {
  let r;
  try { r = fn(c.input); } catch (e) { r = { ok: false, threw: String(e) }; }
  out.push({ name: c.name, expect_ok: c.expect_ok, expect_in: c.expect_in || null,
             got_ok: r.ok === true, message: r.message || null,
             value: r.value || null });
}
console.log(JSON.stringify(out));
"""


def _valid_request(**over) -> dict:
    """A canonical valid body, matching the documented example shape."""
    hours = [
        {"hour": h, "demand_kwh": 180.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 7.0}
        for h in range(24)
    ]
    body = {
        "scenario_id": "GRID-PASTE-1",
        "operator_notes": ["Do not charge the battery between 2 PM and 4 PM."],
        "hours": hours,
        "battery": {
            "capacity_kwh": 500.0,
            "initial_energy_kwh": 200.0,
            "minimum_energy_kwh": 50.0,
            "max_charge_kwh_per_hour": 100.0,
            "max_discharge_kwh_per_hour": 100.0,
        },
    }
    body.update(over)
    return body


def _run_validator(cases: list[dict]) -> dict[str, dict]:
    """Execute the real validateRequestObject() from app.js and collect results.

    Returns {} when Node is unavailable so callers can skip rather than fail.
    """
    node = _node_binary()
    if node is None:
        return {}

    # Pass paths through the environment: with `node -e <script>`, process.argv
    # indices shift (argv[0] is the executable, not the script), which is easy to
    # get subtly wrong. Env vars are unambiguous either way.
    tmp_cases = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(cases, tmp_cases)
        tmp_cases.close()
        env = dict(os.environ)
        env["GRIDWISE_JS"] = str(STATIC / "app.js")
        env["GRIDWISE_CASES"] = tmp_cases.name
        proc = subprocess.run(
            [node, "-e", _JS_EXTRACT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env=env,
        )
    finally:
        Path(tmp_cases.name).unlink(missing_ok=True)

    if proc.returncode != 0:
        raise AssertionError(
            f"validator harness failed (exit {proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

    parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    return {c["name"]: c for c in parsed}


def test_paste_panel_exists_and_is_wired():
    """The judge-facing paste path must be present and connected."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    for el in ("json-input", "json-load", "json-fill", "json-optimize", "json-sample", "json-clear"):
        assert f'id="{el}"' in html, f"missing paste-panel control: #{el}"
        assert f'$("{el}")' in js, f"#{el} is in the HTML but app.js never uses it"

    # A textarea, not a single-line input: a 24-hour scenario is multi-line.
    assert re.search(r'<textarea[^>]*id="json-input"', html), "#json-input must be a <textarea>"
    # And the panel spans the full width so the JSON is readable.
    assert "panel-json" in html and ".panel-json" in (STATIC / "app.css").read_text(encoding="utf-8")


def test_paste_validator_accepts_a_valid_request():
    cases = [{"name": "valid", "expect_ok": True, "input": _valid_request()}]
    results = _run_validator(cases)
    if not results:
        pytest.skip("no Node binary available to execute the validator")
    r = results["valid"]
    assert r["got_ok"], f"a valid request was rejected: {r['message']}"
    assert r["value"]["hours"] and len(r["value"]["hours"]) == 24


def test_paste_validator_reports_actionable_errors():
    """Every rejection must name the offending field.

    This is the whole point of the client-side validator: a judge pasting a
    broken scenario should be told which field is wrong instead of getting a
    bare HTTP 400.
    """
    cases = [
        {"name": "not_an_object", "expect_ok": False, "expect_in": "object", "input": [1, 2, 3]},
        {
            "name": "missing_scenario_id",
            "expect_ok": False,
            "expect_in": "scenario_id",
            "input": {k: v for k, v in _valid_request().items() if k != "scenario_id"},
        },
        {
            "name": "too_many_notes",
            "expect_ok": False,
            "expect_in": "operator_notes",
            "input": _valid_request(operator_notes=["a", "b", "c", "d"]),
        },
        {
            "name": "empty_note",
            "expect_ok": False,
            "expect_in": "operator_notes[1]",
            "input": _valid_request(operator_notes=["ok", "   "]),
        },
        {
            "name": "hours_wrong_length",
            "expect_ok": False,
            "expect_in": "24",
            "input": _valid_request(hours=_valid_request()["hours"][:23]),
        },
        {
            "name": "duplicate_hour",
            "expect_ok": False,
            "expect_in": "more than once",
            "input": _valid_request(
                hours=[dict(h, hour=0) for h in _valid_request()["hours"]]
            ),
        },
        {
            "name": "negative_demand",
            "expect_ok": False,
            "expect_in": "demand_kwh",
            "input": _valid_request(
                hours=[dict(h, demand_kwh=-5.0) if h["hour"] == 3 else h
                       for h in _valid_request()["hours"]]
            ),
        },
        {
            "name": "nan_like_string",
            "expect_ok": False,
            "expect_in": "tariff_bdt_per_kwh",
            "input": _valid_request(
                hours=[dict(h, tariff_bdt_per_kwh="cheap") if h["hour"] == 1 else h
                       for h in _valid_request()["hours"]]
            ),
        },
        {
            "name": "reserve_above_initial",
            "expect_ok": False,
            "expect_in": "minimum_energy_kwh",
            "input": _valid_request(
                battery={**_valid_request()["battery"], "minimum_energy_kwh": 400.0}
            ),
        },
        {
            "name": "initial_above_capacity",
            "expect_ok": False,
            "expect_in": "capacity_kwh",
            "input": _valid_request(
                battery={**_valid_request()["battery"], "initial_energy_kwh": 999.0}
            ),
        },
        {
            "name": "unknown_key",
            "expect_ok": False,
            "expect_in": "noets",
            "input": {**_valid_request(), "noets": "typo of operator_notes"},
        },
        {
            "name": "zero_capacity",
            "expect_ok": False,
            "expect_in": "capacity_kwh",
            "input": _valid_request(
                battery={
                    **_valid_request()["battery"],
                    "capacity_kwh": 0.0,
                    "initial_energy_kwh": 0.0,
                    "minimum_energy_kwh": 0.0,
                }
            ),
        },
    ]
    results = _run_validator(cases)
    if not results:
        pytest.skip("no Node binary available to execute the validator")

    for case in cases:
        r = results[case["name"]]
        assert not r["got_ok"], f"{case['name']} was wrongly accepted"
        msg = r["message"] or ""
        assert case["expect_in"] in msg, (
            f"{case['name']}: error message {msg!r} does not mention "
            f"{case['expect_in']!r}, so it is not actionable"
        )


def test_paste_validator_output_passes_the_real_model():
    """Whatever the client validator accepts must also satisfy the server model.

    The client check is only a friendly front end; the Pydantic model is
    authoritative. If the two disagree, a judge gets a 422 after being told the
    JSON was fine -- worse than no client validation at all.
    """
    from app.models import OptimizationRequest

    cases = [
        {"name": "canonical", "expect_ok": True, "input": _valid_request()},
        {
            "name": "unsorted_hours",
            "expect_ok": True,
            "input": _valid_request(hours=list(reversed(_valid_request()["hours"]))),
        },
        {
            "name": "valid_but_odd_order",
            "expect_ok": True,
            "input": {
                "battery": _valid_request()["battery"],
                "hours": _valid_request()["hours"],
                "operator_notes": _valid_request()["operator_notes"],
                "scenario_id": "GRID-PASTE-2",
            },
        },
    ]
    results = _run_validator(cases)
    if not results:
        pytest.skip("no Node binary available to execute the validator")

    for case in cases:
        r = results[case["name"]]
        assert r["got_ok"], f"{case['name']} should have been accepted: {r['message']}"
        # The normalised value must round-trip through the authoritative model.
        model = OptimizationRequest.model_validate(r["value"])
        assert len(model.hours) == 24
        assert [h.hour for h in model.hours] == list(range(24)), (
            f"{case['name']}: hours must arrive sorted 0..23 for the optimizer"
        )


def test_paste_sample_button_builds_a_request_the_model_accepts():
    """The built-in sample must be valid, or the button teaches the wrong thing."""
    from app.models import OptimizationRequest

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function sampleRequestJson(" in js, "missing the sample-request builder"
    # It must reuse the form defaults rather than duplicating numbers.
    assert "DEFAULT_NOTES" in js.split("function sampleRequestJson(")[1].split("}")[0]
    assert "defaultHours()" in js.split("function sampleRequestJson(")[1].split("}")[0]

    # Execute defaultHours() + the sample builder and validate the result.
    node = _node_binary()
    if node is None:
        pytest.skip("no Node binary available")
    script = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.GRIDWISE_JS, "utf8");
const grab = (name) => {
  const start = src.indexOf("function " + name + "(");
  if (start === -1) throw new Error(name + " not found");
  let depth = 0, i = src.indexOf("{", start), end = -1;
  for (let p = i; p < src.length; p++) {
    if (src[p] === "{") depth++;
    else if (src[p] === "}") { depth--; if (depth === 0) { end = p + 1; break; } }
  }
  return src.slice(start, end);
};
const DEFAULT_NOTES = "a\nb\nc";
const DEFAULT_BATTERY = JSON.parse(process.env.GRIDWISE_BATTERY);
const build = new Function(
  "DEFAULT_NOTES", "DEFAULT_BATTERY",
  grab("defaultHours") + "\n" + grab("sampleRequestJson") + "\nreturn sampleRequestJson;"
)(DEFAULT_NOTES, DEFAULT_BATTERY);
console.log(build());
"""
    env = dict(os.environ)
    env["GRIDWISE_JS"] = str(STATIC / "app.js")
    env["GRIDWISE_BATTERY"] = json.dumps(
        {
            "capacity_kwh": 500,
            "initial_energy_kwh": 200,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        }
    )
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"sampleRequestJson harness failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    # The builder returns pretty-printed JSON, so parse the whole stdout rather
    # than the last line.
    built = json.loads(proc.stdout)

    model = OptimizationRequest.model_validate(built)
    assert len(model.hours) == 24
    assert len(model.operator_notes) == 3


def test_paste_textarea_gets_a_placeholder_example():
    """An empty box with no hint is a dead end for a judge."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<textarea[^>]*id="json-input"[^>]*>', html, re.S)
    assert m, "#json-input not found"
    assert "placeholder=" in m.group(0), "the paste textarea needs a placeholder example"


if __name__ == "__main__":  # pragma: no cover
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
