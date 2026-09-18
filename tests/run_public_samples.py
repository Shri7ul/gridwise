"""Local parity harness for the GridWise public sample pack.

Replays every rule the official judge uses (per the Problem Statement and the
Participant Guide), independently of the app's own code, so a passing run here
means the response would pass an independent judge:

  * coverage        -- one interpretation entry per note, note_index order 0..N-1
  * directive match -- applies / directive_type / hours / numeric values vs ground truth
  * shape           -- structured_adjustment matches the required shape for the type
  * guardrails      -- no_op semantics, hour ordering, factor and value ranges
  * energy replay   -- balance, effective solar, battery bounds, rate limits,
                       no-charge, no-discharge, grid cap, end-of-day neutrality
  * reported totals -- total_grid_kwh, total_cost_bdt, peak_grid_kwh vs hourly_plan
  * cost quality    -- min(1, organizer_optimal_cost / recalculated_team_cost)

Usage:
    python tests/run_public_samples.py                      # hits a live service
    python tests/run_public_samples.py --base-url http://127.0.0.1:8000
    python tests/run_public_samples.py --offline            # in-process, no server

NOTE --offline still exercises the *real* interpretation stage, so it needs either a
configured LLM credential or the opt-in deterministic fallback:

    # no credential on this machine -> use the labelled fallback interpreter
    ALLOW_DETERMINISTIC_FALLBACK=true python tests/run_public_samples.py --offline

    # with a credential, the real LLM path is exercised without any extra flag
    GROQ_API_KEY=... python tests/run_public_samples.py --offline

Leaving the flag off without a credential is correct behaviour, not a bug: every case
fails with a controlled 422 rather than the service inventing an answer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "pdf" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"

HOURS = list(range(24))
TOL = 0.01  # Problem Statement s11.5: absolute tolerance 0.01 kWh / 0.01 BDT

REQUIRED_ADJUSTMENT_KEYS = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}
ALLOWED_TYPES = set(REQUIRED_ADJUSTMENT_KEYS) | {"no_op"}
ALLOWED_ACTIONS = {"charge", "discharge", "idle"}

PASS = "PASS"
FAIL = "FAIL"


# --------------------------------------------------------------------------- #
# Effective-constraint derivation (mirrors Problem Statement s5.3)
# --------------------------------------------------------------------------- #
def effective_constraints(problem_input: dict, interpretation: list[dict]) -> dict:
    """Fold directives into the constraint set the optimizer must respect."""
    hours = {h["hour"]: h for h in problem_input["hours"]}
    battery = problem_input["battery"]

    solar = {h: float(hours[h]["solar_kwh"]) for h in HOURS}
    reserve = {h: float(battery["minimum_energy_kwh"]) for h in HOURS}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}

    def add_grid_cap(h: int, value: float) -> None:
        grid_cap[h] = min(grid_cap.get(h, math.inf), value)

    for entry in interpretation:
        if not entry.get("applies"):
            continue
        dtype = entry.get("directive_type")
        adjustment = entry.get("structured_adjustment") or {}
        affected = [h for h in adjustment.get("hours", []) if isinstance(h, int) and 0 <= h <= 23]
        if dtype == "solar_reduction":
            for h in affected:
                solar[h] *= float(adjustment["factor"])
        elif dtype == "minimum_battery_reserve":
            for h in affected:
                reserve[h] = max(reserve[h], float(adjustment["minimum_energy_kwh"]))
        elif dtype == "no_charge_window":
            no_charge.update(affected)
        elif dtype == "no_discharge_window":
            no_discharge.update(affected)
        elif dtype == "max_grid_window":
            for h in affected:
                add_grid_cap(h, float(adjustment["max_grid_kwh"]))

    return {
        "hours": hours,
        "battery": battery,
        "solar": solar,
        "reserve": reserve,
        "no_charge": no_charge,
        "no_discharge": no_discharge,
        "grid_cap": grid_cap,
    }


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_shape(problem_input: dict, response: dict, expected: dict) -> list[str]:
    errors: list[str] = []
    notes = problem_input["operator_notes"]
    interp = response.get("directive_interpretation")

    if not isinstance(interp, list):
        return ["directive_interpretation is not a list"]
    if len(interp) != len(notes):
        errors.append(f"coverage: got {len(interp)} entries for {len(notes)} notes")

    for index, entry in enumerate(interp):
        if not isinstance(entry, dict):
            errors.append(f"[{index}] entry is not an object")
            continue
        if entry.get("note_index") != index:
            errors.append(f"[{index}] note_index is {entry.get('note_index')}, expected {index}")
        if not isinstance(entry.get("applies"), bool):
            errors.append(f"[{index}] applies is not a bool")
        if not isinstance(entry.get("explanation"), str) or not entry["explanation"].strip():
            errors.append(f"[{index}] explanation missing/empty")

        dtype = entry.get("directive_type")
        if dtype not in ALLOWED_TYPES:
            errors.append(f"[{index}] directive_type {dtype!r} not in the supported set")
            continue

        adjustment = entry.get("structured_adjustment")
        if dtype == "no_op":
            if entry.get("applies") is not False:
                errors.append(f"[{index}] no_op must use applies=false")
            if adjustment is not None:
                errors.append(f"[{index}] no_op must use structured_adjustment=null")
            continue

        if entry.get("applies") is not True:
            errors.append(f"[{index}] {dtype} must use applies=true")
        if not isinstance(adjustment, dict):
            errors.append(f"[{index}] {dtype} needs a structured_adjustment object")
            continue

        required = REQUIRED_ADJUSTMENT_KEYS[dtype]
        if set(adjustment) != required:
            errors.append(f"[{index}] {dtype} keys {sorted(adjustment)} != {sorted(required)}")

        affected = adjustment.get("hours")
        if not isinstance(affected, list) or not affected:
            errors.append(f"[{index}] {dtype} hours must be a non-empty list")
        else:
            if any(not isinstance(h, int) or isinstance(h, bool) for h in affected):
                errors.append(f"[{index}] hours must be integers")
            elif any(h < 0 or h > 23 for h in affected):
                errors.append(f"[{index}] hours out of range 0-23")
            elif len(set(affected)) != len(affected):
                errors.append(f"[{index}] hours contain duplicates")
            elif affected != sorted(affected):
                errors.append(f"[{index}] hours are not in ascending order")

        if dtype == "solar_reduction":
            factor = adjustment.get("factor")
            if not isinstance(factor, (int, float)) or isinstance(factor, bool):
                errors.append(f"[{index}] factor must be numeric")
            elif not (0.0 <= float(factor) <= 1.0):
                errors.append(f"[{index}] factor {factor} outside [0, 1]")
        elif dtype == "minimum_battery_reserve":
            value = adjustment.get("minimum_energy_kwh")
            capacity = problem_input["battery"]["capacity_kwh"]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"[{index}] minimum_energy_kwh must be numeric")
            elif not math.isfinite(float(value)) or value < 0:
                errors.append(f"[{index}] minimum_energy_kwh must be finite and non-negative")
            elif float(value) > float(capacity) + TOL:
                errors.append(f"[{index}] minimum_energy_kwh {value} exceeds capacity {capacity}")
        elif dtype == "max_grid_window":
            value = adjustment.get("max_grid_kwh")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"[{index}] max_grid_kwh must be numeric")
            elif not math.isfinite(float(value)) or value < 0:
                errors.append(f"[{index}] max_grid_kwh must be finite and non-negative")

    # Ground-truth comparison (relevance, type, hours, numeric values).
    getr = expected["directive_interpretation"]
    for index, exp in enumerate(getr):
        if index >= len(interp) or not isinstance(interp[index], dict):
            continue
        got = interp[index]
        if bool(got.get("applies")) != bool(exp["applies"]):
            errors.append(f"[{index}] applies {got.get('applies')} != ground truth {exp['applies']}")
        if got.get("directive_type") != exp["directive_type"]:
            errors.append(
                f"[{index}] directive_type {got.get('directive_type')!r} != "
                f"ground truth {exp['directive_type']!r}"
            )
            continue
        if exp["directive_type"] == "no_op":
            continue
        exp_adj = exp["structured_adjustment"]
        got_adj = got.get("structured_adjustment") or {}
        if list(got_adj.get("hours", [])) != list(exp_adj["hours"]):
            errors.append(
                f"[{index}] hours {got_adj.get('hours')} != ground truth {exp_adj['hours']}"
            )
        for key, exp_value in exp_adj.items():
            if key == "hours":
                continue
            got_value = got_adj.get(key)
            if not isinstance(got_value, (int, float)) or isinstance(got_value, bool):
                errors.append(f"[{index}] {key} missing or non-numeric")
            elif abs(float(got_value) - float(exp_value)) > TOL:
                errors.append(
                    f"[{index}] {key} {got_value} != ground truth {exp_value} (tol {TOL})"
                )
    return errors


def check_plan(problem_input: dict, response: dict) -> list[str]:
    errors: list[str] = []
    interp = response.get("directive_interpretation") or []
    ctx = effective_constraints(problem_input, interp)
    hours, battery = ctx["hours"], ctx["battery"]

    capacity = float(battery["capacity_kwh"])
    initial = float(battery["initial_energy_kwh"])
    max_charge = float(battery["max_charge_kwh_per_hour"])
    max_discharge = float(battery["max_discharge_kwh_per_hour"])

    plan = response.get("hourly_plan")
    if not isinstance(plan, list):
        return ["hourly_plan is not a list"]
    if len(plan) != 24:
        errors.append(f"hourly_plan has {len(plan)} entries, expected 24")

    by_hour: dict[int, dict] = {}
    for entry in plan:
        if isinstance(entry, dict) and isinstance(entry.get("hour"), int) and not isinstance(entry.get("hour"), bool):
            by_hour[entry["hour"]] = entry
    if sorted(by_hour) != HOURS:
        errors.append(f"hourly_plan hours are {sorted(by_hour)}, expected 0..23 exactly once")

    def num(entry: dict, key: str, hour: int) -> float | None:
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"h{hour}: {key} is missing or non-numeric")
            return None
        if not math.isfinite(float(value)) or float(value) < -TOL:
            errors.append(f"h{hour}: {key}={value} must be finite and non-negative")
            return None
        return float(value)

    energy_before = initial
    recalc_grid = 0.0
    recalc_cost = 0.0
    recalc_peak = 0.0
    first_error_count = len(errors)

    for hour in HOURS:
        entry = by_hour.get(hour)
        if entry is None:
            continue
        grid = num(entry, "grid_kwh", hour)
        solar_used = num(entry, "solar_used_kwh", hour)
        battery_kwh = num(entry, "battery_kwh", hour)
        energy_after = num(entry, "battery_energy_after_kwh", hour)
        action = entry.get("battery_action")
        if grid is None or solar_used is None or battery_kwh is None or energy_after is None:
            continue
        if action not in ALLOWED_ACTIONS:
            errors.append(f"h{hour}: battery_action {action!r} must be one of {sorted(ALLOWED_ACTIONS)}")
            continue

        charge = battery_kwh if action == "charge" else 0.0
        discharge = battery_kwh if action == "discharge" else 0.0
        if action == "idle" and battery_kwh > TOL:
            errors.append(f"h{hour}: idle must report battery_kwh=0, got {battery_kwh}")

        demand = float(hours[hour]["demand_kwh"])
        if abs((grid + solar_used + discharge) - (demand + charge)) > TOL:
            errors.append(
                f"h{hour}: energy balance broken "
                f"({grid}+{solar_used}+{discharge} != {demand}+{charge})"
            )
        if solar_used - ctx["solar"][hour] > TOL:
            errors.append(
                f"h{hour}: solar_used {solar_used} exceeds effective solar {ctx['solar'][hour]}"
            )
        if charge > max_charge + TOL:
            errors.append(f"h{hour}: charge {charge} exceeds rate limit {max_charge}")
        if discharge > max_discharge + TOL:
            errors.append(f"h{hour}: discharge {discharge} exceeds rate limit {max_discharge}")
        if energy_after - capacity > TOL:
            errors.append(f"h{hour}: battery_energy_after {energy_after} exceeds capacity {capacity}")
        if ctx["reserve"][hour] - energy_after > TOL:
            errors.append(
                f"h{hour}: battery_energy_after {energy_after} below active reserve {ctx['reserve'][hour]}"
            )
        if abs(energy_after - (energy_before + charge - discharge)) > TOL:
            errors.append(
                f"h{hour}: transition broken "
                f"({energy_after} != {energy_before}+{charge}-{discharge})"
            )
        if hour in ctx["no_charge"] and charge > TOL:
            errors.append(f"h{hour}: no_charge_window violated (charge={charge})")
        if hour in ctx["no_discharge"] and discharge > TOL:
            errors.append(f"h{hour}: no_discharge_window violated (discharge={discharge})")
        if hour in ctx["grid_cap"] and grid - ctx["grid_cap"][hour] > TOL:
            errors.append(f"h{hour}: grid {grid} exceeds cap {ctx['grid_cap'][hour]}")

        energy_before = energy_after
        recalc_grid += grid
        recalc_cost += grid * float(hours[hour]["tariff_bdt_per_kwh"])
        recalc_peak = max(recalc_peak, grid)

    if abs(energy_before - initial) > TOL:
        errors.append(
            f"end-of-day neutrality broken: final {energy_before} != initial {initial}"
        )

    if len(errors) == first_error_count:
        for key, value in (
            ("total_grid_kwh", recalc_grid),
            ("total_cost_bdt", recalc_cost),
            ("peak_grid_kwh", recalc_peak),
        ):
            reported = response.get(key)
            if not isinstance(reported, (int, float)) or isinstance(reported, bool):
                errors.append(f"{key} missing or non-numeric")
            elif abs(float(reported) - value) > TOL:
                errors.append(f"{key} reported {reported} != recalculated {value:.2f} (tol {TOL})")

    summary = response.get("plan_summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("plan_summary missing/empty")
    if response.get("scenario_id") != problem_input["scenario_id"]:
        errors.append(
            f"scenario_id echo {response.get('scenario_id')!r} != {problem_input['scenario_id']!r}"
        )
    return errors


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def post_json(base_url: str, payload: dict, timeout: float) -> tuple[int, dict | str]:
    url = base_url.rstrip("/") + "/optimize-energy"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def get_health(base_url: str, timeout: float) -> tuple[int, dict | str]:
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("GRIDWISE_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--only", default=None, help="Comma-separated case ids, e.g. SAMPLE-01,SAMPLE-04")
    parser.add_argument("--offline", action="store_true", help="Call the app in-process (no server)")
    parser.add_argument("--json-out", default=None, help="Write a machine-readable report here")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help=(
            "Seconds to sleep between cases. Use this when running against a "
            "rate-limited provider: Groq's free tier allows only 8000 tokens/"
            "minute and each interpretation costs roughly 1500-2500 tokens, so a "
            "back-to-back run exhausts the bucket after ~5 cases and the rest "
            "fail with a controlled 422/500. --delay 15 keeps the full suite green."
        ),
    )
    args = parser.parse_args()

    pack = json.loads(Path(args.pack).read_text(encoding="utf-8"))
    cases = pack["cases"]
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if not cases:
        print("no cases selected")
        return 2

    if args.offline:
        sys.path.insert(0, str(REPO_ROOT))
        from app.main import optimize_energy_core  # type: ignore
        from app.models import OptimizationRequest  # type: ignore

        def call(payload: dict) -> tuple[int, dict]:
            try:
                result = optimize_energy_core(OptimizationRequest(**payload))
                return 200, result.model_dump()
            except Exception as exc:  # pragma: no cover - surfaced as a failure
                return 500, {"error": f"{type(exc).__name__}: {exc}"}

        print("mode: offline (in-process)\n")
    else:
        def call(payload: dict) -> tuple[int, dict]:
            return post_json(args.base_url, payload, args.timeout)  # type: ignore

        status, health = get_health(args.base_url, args.timeout)
        print(f"GET /health -> {status} {health}")
        if status != 200 or not isinstance(health, dict) or health.get("status") != "ok":
            print("FAIL: /health did not return {'status': 'ok'}")
            return 1
        print(f"mode: live ({args.base_url})\n")

    rows = []
    passed = 0
    latencies = []
    for index, case in enumerate(cases):
        # Pace requests when asked. Free-tier providers cap tokens per minute, so
        # a back-to-back run can exhaust the bucket mid-suite; without this the
        # later cases fail for reasons unrelated to the implementation.
        if index and args.delay > 0:
            time.sleep(args.delay)

        problem_input, expected = case["input"], case["expected_output"]
        started = time.perf_counter()
        status, response = call(problem_input)
        latency = time.perf_counter() - started
        latencies.append(latency)

        errors: list[str] = []
        if status != 200:
            errors.append(f"HTTP {status}: {str(response)[:200]}")
        elif not isinstance(response, dict):
            errors.append("response is not a JSON object")
        else:
            errors.extend(check_shape(problem_input, response, expected))
            errors.extend(check_plan(problem_input, response))

        reference_cost = float(expected["total_cost_bdt"])
        team_cost = None
        ratio = None
        if isinstance(response, dict) and not errors:
            team_cost = float(response["total_cost_bdt"])
            ratio = 1.0 if team_cost <= TOL else min(1.0, reference_cost / team_cost)

        ok = not errors and ratio is not None and ratio >= 1.0 - 1e-9
        passed += int(ok)
        rows.append(
            {
                "id": case["id"],
                "label": case.get("label"),
                "ok": ok,
                "latency_s": round(latency, 3),
                "reported_cost": team_cost,
                "reference_cost": reference_cost,
                "quality_ratio": ratio,
                "errors": errors,
            }
        )

        badge = PASS if ok else FAIL
        detail = f"cost={team_cost:.2f} ref={reference_cost:.2f}" if team_cost is not None else "no cost"
        q = f" ratio={ratio:.4f}" if ratio is not None else ""
        print(f"[{badge}] {case['id']:<10} {latency:5.2f}s  {detail}{q}  {case.get('label', '')}")
        for error in errors[:8]:
            print(f"        - {error}")
        if len(errors) > 8:
            print(f"        - ... {len(errors) - 8} more")

    total = len(rows)
    print("\n" + "=" * 78)
    print(f"public samples: {passed}/{total} passed")
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]
        print(f"latency: mean {statistics.mean(latencies):.2f}s  max {max(latencies):.2f}s  p95 {p95:.2f}s")
    ratios = [r["quality_ratio"] for r in rows if r["quality_ratio"] is not None]
    if ratios:
        print(f"optimization quality ratio: mean {statistics.mean(ratios):.4f}  min {min(ratios):.4f}")
    print("=" * 78)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"report written to {args.json_out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
