# Project Memory — GridWise (BUP CSE Fest 2026, Preliminary)

## What this project is

A judging-ready FastAPI service for the *GridWise — Smart Campus Energy Optimizer* hackathon.
Pipeline: **operator notes → LLM (structured directives) → deterministic guardrails → PuLP LP
cost-minimizing battery/grid schedule → deterministic summary**. Rules live in `pdf/`.

## Non-negotiable conventions

| Thing | Rule |
|---|---|
| `solar_reduction.factor` | The **usable fraction remaining**, NOT the reduction. 80% reduction → `0.2` |
| Hour windows | Whole hours, **start-inclusive / end-exclusive**. 1 PM→3 PM = `[13, 14]` |
| Energy balance | `grid + solar_used + battery_discharge = demand + battery_charge` |
| End of day | `E[23] == initial_energy_kwh` — this pins total grid energy to `sum(demand) − sum(solar_used)` |
| Unsupported directive types | Never invented; must map to a known type or `no_op` |
| LLM output | Always untrusted. Validated by pure code before it reaches the optimizer |
| Plan summary | **Deterministic** (`app/summary.py`), never LLM-generated |
| Secrets in errors/logs | Passed through `redact()` before being surfaced |
| Default model | `openai/gpt-oss-120b`. Groq **retires models**; `llama-3.3-70b-versatile` now 404s |
| `.env` | Loaded with `override=False` — platform env vars always beat the file |
| `.env.example` | **IS committed** (`!.env.example`). Never put a real key in it |

## Architecture invariants

- **`optimizer._assert_plan_valid()` is a hard gate.** It replays the returned plan through
  every constraint (balance, solar, rate limits, reserve, capacity, transition, directives,
  neutrality, totals) and raises if anything disagrees. Never bypass it — it is the only
  thing standing between an LP bug and a silently invalid submission.
- **The LP is degenerate by construction.** Total grid energy is fixed, so multiple
  equal-cost schedules exist. Do not chase an exact action sequence; score validity + cost.
- **Post-solve netting** removes simultaneous charge/discharge, then tops up the reserve.
  Anything added here must still survive `_assert_plan_valid()`.
- **Error taxonomy:** malformed JSON → 400, structural → 400, semantic → 422, internal → 500.
  No stack traces or credentials ever reach the client.
- **A route that reads `await request.body()` must declare `openapi_extra.requestBody`,**
  or Swagger UI shows "No parameters" with no input box (FastAPI cannot infer a body it
  never receives as a parameter).

## Testing

- `tests/run_public_samples.py --offline` is the judge-parity harness (10 published cases);
  `--base-url` runs the same checks over live HTTP. **`--delay 15` is required on a free Groq
  key** (8000 tokens/minute, ~1500–2500 per request → ~5 cases max back-to-back).
- Suite: `test_docker_contract.py` (9), `test_config.py` (10), `test_llm_stage.py` (14),
  `test_api_e2e.py` (15) = 48, plus the 10-case harness. `pytest` is not preinstalled in the
  isolated venv — install it first.
- The stub suites override `_post` to return a queued **string** (matching `_post`'s contract;
  returning an object is a real bug that was hit once).
- **Stub-only suites hide credential-path bugs.** The `.env`-not-loaded and retired-model bugs
  were invisible to 41 green tests and only surfaced with a real key. Any "copy this file and
  fill it in" instruction needs a test proving the file is read.

## Docker / solver packaging

- CBC in the pulp wheel (`solverdir/cbc/linux/i64/cbc`) links **`libstdc++.so.6` +
  `libgcc_s.so.1`** — **not** `libgomp`. `python:3.12-slim` ships neither, so the Dockerfile
  must `apt-get install libstdc++6` (pulls libgcc-s1). The original `libgomp1` was wrong.
- Re-verify after any pulp upgrade:
  `strings <pulp>/solverdir/cbc/linux/i64/cbc | grep -E '^lib(stdc\+\+|gomp)'`
- Explicit `COPY` paths, never `COPY . .`; `.dockerignore` is defence in depth.

## API response schema (verified live)

- Directives are keyed **`directive_type`** (not `type`), each with an `applies` boolean and a
  `structured_adjustment` (`null` for `no_op`). Top-level keys: `scenario_id`,
  `directive_interpretation`, `hourly_plan`, `plan_summary`, `total_cost_bdt`,
  `total_grid_kwh`, `peak_grid_kwh`.
- `plan_summary` is deterministic (`app/summary.py`) — the model never writes it. There is no
  `summary` key.
- Totals are **payload-specific**. Assert invariants instead:
  `total_grid_kwh == sum(hourly_plan[*].grid_kwh)`, and the final `battery_energy_after_kwh`
  returns to `battery.initial_energy_kwh`.

## Deployment (Render free tier)

- `DEPLOY_RENDER.md` is the runbook; README links it from the TOC and its Deploy section.
- Every command in the guide has been **executed**: Render-shaped env simulation, startup log
  line, `/openapi.json` body declaration, the example curl, and the README verification command
  (10/10, ratio 1.0000, p95 2.43 s locally).
- `pulp-3.2.2-py3-none-any.whl` is pure Python → no compile on Render, CBC is in the wheel, so
  Render's "no apt-get" limit is irrelevant.
- **Windows curl trap:** `-d "$BODY"` can silently send an empty body →
  `400 {"error":"request body is empty"}`, which looks exactly like an API failure. Use
  `--data-binary @file.json`.
- Free tier: 512 MB (keep one Uvicorn worker), sleeps after ~15 min idle with a 30–60 s cold
  start that blows the 30 s per-request budget, 750 instance-hours/month.

## Verification discipline

Four bugs were each hidden behind a confident comment asserting behaviour that was not real:
`.env` claimed to load, the default model claimed to exist, `libgomp1` claimed to be CBC's
dependency, and my own first read of the response schema used `type`/`summary` when the real
keys are `directive_type`/`plan_summary`. **Check the claim, don't read the comment.**
Binary deps: `strings ... | grep '^lib'`. Config: instantiate `Settings()` and print what
resolved. Schema: call the endpoint and print `sorted(d.keys())`. Numbers in docs: measure
them — a remembered `total_grid_kwh` (4055) versus the real 4071 is a future bug report.

## Environment notes (Windows)

- Proxy env vars break localhost calls → use `--noproxy '*'` (or `no_proxy='*'`).
- Background servers die with their shell. Server + tests must run in one Bash call.
- CBC ships inside the pulp wheel; no system solver needed. Docker is unavailable locally.
- `pulp.__version__` lies (reports 3.0.2 on 3.2.2). Ignore it.
