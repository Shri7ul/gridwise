# GridWise ⚡

**LLM-assisted operator-note interpretation and 24-hour cost-minimising energy
scheduling for the BUP CSE Fest 2026 Hackathon (Online Preliminary).**

Given a 24-hour campus scenario — demand, solar forecast, hourly grid tariff, battery
limits — plus 1–3 short natural-language operator notes, the service:

1. **Interprets** each note with a language model into one machine-checkable directive.
2. **Validates** that interpretation deterministically, before it is trusted.
3. **Optimizes** a 24-hour schedule with a linear program that minimises grid cost.
4. **Returns** the interpretation, the hourly plan, and the recomputed totals.

Human notes are never trusted as math. They become a fixed structured format, are
checked by deterministic code, and only then reach the optimizer.

| | |
| --- | --- |
| **Live endpoint** | `https://shriful.tech/gridwise` |
| **API** | `GET /health` · `POST /optimize-energy` |
| **Operator UI** | `GET /` (same origin, no build step) |
| **Interactive docs** | `GET /docs` |
| **Model / provider** | `openai/gpt-oss-120b` via Groq (any OpenAI-compatible endpoint works) |
| **Solver** | PuLP 3.2.2 with the bundled CBC binary |

---

## Table of contents

**Get it running**

- [Quickstart — clean environment](#quickstart--clean-environment)
- [Environment variables](#environment-variables)
- [Docker fallback](#docker-fallback)

**Verify it**

- [Verify the service](#verify-the-service)
- [Run the public sample cases](#run-the-public-sample-cases)
- [Run the test suites](#run-the-test-suites)

**Understand it**

- [Architecture](#architecture)
- [LLM role and guardrails](#llm-role-and-guardrails)
- [Optimization model](#optimization-model)
- [API reference](#api-reference)
- [Operator web UI](#operator-web-ui)

**Operate it**

- [Deployment](#deployment)
- [Serving a subpath via Cloudflare Worker](#serving-a-subpath-via-cloudflare-worker)
- [Known limitations](#known-limitations)
- [Security and secret handling](#security-and-secret-handling)
- [Dependencies and credits](#dependencies-and-credits)
- [Project layout](#project-layout)

---

## Quickstart — clean environment

No prior state required. Copy-paste the whole block.

```bash
# 1. Clone
git clone https://github.com/Shri7ul/gridwise.git && cd gridwise

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 3. Dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure ONE credential
cp .env.example .env
#    edit .env and set GROQ_API_KEY=...   (or OPENAI_API_KEY=... / LLM_API_KEY=...)

# 5. Start the service
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

That is the whole setup. Python 3.12 or 3.13; no system solver package, no build step,
no database.

### Confirm it started

In a second terminal:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

The startup log prints the resolved provider without ever printing the key:

```text
gridwise starting: version=1.0.0 provider=groq model=openai/gpt-oss-120b llm_configured=True fallback_enabled=False configured_port=8000
```

If `llm_configured=False`, the credential was not found — check that `.env` sits next to
`requirements.txt` and that the key name is one of `GROQ_API_KEY`, `OPENAI_API_KEY`, or
`LLM_API_KEY`.

> `.env` is loaded automatically at startup — no shell `export` needed. Precedence is
> **real environment variable > `.env` > built-in default**, so the same image runs
> locally from `.env` and on Render/Docker from platform-injected secrets with no code
> change.

### Run one request

```bash
curl -s -X POST http://127.0.0.1:8000/optimize-energy \
  -H "Content-Type: application/json" \
  --data-binary @demo_inputs/SAMPLE-01.json | python -m json.tool
```

`demo_inputs/SAMPLE-01.json` … `SAMPLE-10.json` are the published public cases, extracted
verbatim and ready to POST. There is a longer hand-written example in
[curl examples](#curl-examples).

### About the solver

CBC ships **inside the `pulp` wheel** (`pulp/solverdir/cbc/linux/i64/cbc`), so nothing
else is needed for the local quickstart. Inside a `python:3.12-slim` container that
binary needs one shared library the base image omits — `libstdc++6` — which is why the
Dockerfile installs it. See [Docker fallback](#docker-fallback).

---

## Environment variables

Only **one** credential is required: `GROQ_API_KEY` takes priority, then
`OPENAI_API_KEY`, then `LLM_API_KEY`.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GROQ_API_KEY` | one of these | — | Groq API key (default provider) |
| `OPENAI_API_KEY` | three | — | OpenAI API key |
| `LLM_API_KEY` | is required | — | Any OpenAI-compatible key (Together, OpenRouter, Ollama) |
| `LLM_BASE_URL` | no | `https://api.groq.com/openai/v1` | OpenAI-compatible base URL |
| `LLM_MODEL` | no | `openai/gpt-oss-120b` | Model identifier |
| `LLM_TIMEOUT_SECONDS` | no | `12` | Per-attempt provider timeout |
| `REQUEST_DEADLINE_SECONDS` | no | `25` | Total budget for the request path |
| `LLM_MAX_RETRIES` | no | `2` | Retries after the first attempt |
| `LLM_TEMPERATURE` | no | `0` | Sampling temperature |
| `LLM_MAX_TOKENS` | no | `1600` | Response cap |
| `ALLOW_DETERMINISTIC_FALLBACK` | no | `false` | Degraded-availability fallback (see [limitations](#known-limitations)) |
| `PORT` | no | `8000` | Listen port (injected by Render/Docker) |
| `LOG_LEVEL` | no | `INFO` | Log verbosity |

**Never commit a filled-in `.env`.** It is git-ignored; `.env.example` holds the names
and safe defaults only.

---

## Verify the service

Three checks, in increasing order of thoroughness. All three are required by the
submission checklist.

```bash
export BASE=http://127.0.0.1:8000        # local
# export BASE=https://gridwise-zntk.onrender.com   # deployed
```

**1. Health**

```bash
curl -s "$BASE/health"
# {"status":"ok"}
```

**2. The service index**

```bash
curl -s "$BASE/api"
# {"service":"gridwise-energy-optimization","health":"/health",
#  "optimize":"POST /optimize-energy","docs":"/docs","ui":"/"}
```

**3. One public sample, end to end**

```bash
python tests/run_public_samples.py --base-url "$BASE" --only SAMPLE-01
```

Expected: the case passes, and the cost-quality ratio is `1.0000`.

`$BASE/` in a browser is the operator UI; `$BASE/docs` is Swagger UI.

---

## Run the public sample cases

The end-to-end check that mirrors what the judge does.

**Against a running service (live HTTP):**

```bash
python tests/run_public_samples.py --base-url http://127.0.0.1:8000
```

Add `--delay 15` when using a free-tier provider key — see [limitations](#known-limitations)
for why.

**Without a server (in-process):**

```bash
# A configured LLM credential exercises the real interpretation path.
GROQ_API_KEY=gsk_... python tests/run_public_samples.py --offline

# With no credential, enable the labelled fallback interpreter instead.
ALLOW_DETERMINISTIC_FALLBACK=true python tests/run_public_samples.py --offline
```

`--offline` still runs the genuine interpretation stage. With neither a credential nor
the fallback flag, every case correctly fails with a controlled 422 — the service refuses
to invent an interpretation rather than silently degrading.

**A single case, or a machine-readable report:**

```bash
python tests/run_public_samples.py --only SAMPLE-01,SAMPLE-04
python tests/run_public_samples.py --json-out report_public.json
```

The runner replays every rule the judge uses — coverage, directive ground truth,
guardrail ranges, energy balance, effective solar, battery bounds and rate limits,
no-charge / no-discharge / grid-cap directives, end-of-day neutrality, and the reported
totals against the plan — then prints the cost-quality ratio
`min(1, reference_cost / our_cost)`.

**Expected result:**

```text
public samples: 10/10 passed
latency: mean 0.13s  max 0.14s  p95 0.14s
optimization quality ratio: mean 1.0000  min 1.0000
```

All ten public cases reach `ratio = 1.0000`, i.e. cost equal to the published reference
optimum. Because the battery must return to its initial level, total grid energy is fixed
at `sum(demand) − sum(solar_used)`; the optimizer's freedom is *which hours* to buy in,
and it matches the reference optimum on every case.

Output is also checked against the interpreter ground truth, so a passing run means both
the interpretation and the schedule would satisfy an independent judge.

---

## Run the test suites

```bash
python -m pytest tests/ -q        # 71 passed
```

Or individually:

| Suite | Tests | Covers |
| --- | --- | --- |
| `tests/test_docker_contract.py` | 10 | CBC exists in the wheel, every non-glibc library CBC needs is provided by an apt package the Dockerfile installs, COPY sources exist, **the frontend assets reach the image**, binds `0.0.0.0`, honours `$PORT`, non-root, no build-arg or COPY secrets |
| `tests/test_config.py` | 10 | `.env` is actually read, real env vars win over `.env`, credential priority, honest "unconfigured" reporting, repository-wide secret sweep, `.env.example` placeholder integrity |
| `tests/test_llm_stage.py` | 14 | defensive JSON extraction (fences, prose, trailing commas), retry recovery, repair pass, unsupported-type rejection, secret redaction |
| `tests/test_api_e2e.py` | 16 | `/health`, full pipeline over HTTP for all 10 public cases, response schema, totals, 400/422/500 taxonomy, non-finite input rejection, Swagger request-body contract, **OpenAPI `$ref` resolvability**, secret-leak sweep |
| `tests/test_frontend.py` | 21 | `GET /` is HTML and `/api` is still the JSON index, every asset URL resolves with the right MIME type, no duplicate element `id`s, labels/`aria` point at real ids, every id the JS looks up exists, every class the JS applies is styled, no external dependency, no credential or local path in the served bytes, the page's request body passes `OptimizationRequest`, every field the UI reads exists in the response model, JS directive labels match the backend enum, **the paste-a-JSON validator is executed in Node — every rejection must name the offending field, and everything it accepts must pass the real Pydantic model** |
| `tests/run_public_samples.py` | 10 cases | independent rule replay against a live or in-process service, plus the optimization-quality ratio |

All suites pass on Python 3.12 and 3.13.

### Browser and Worker harnesses

These run separately and are **not** service dependencies (Playwright is deliberately
absent from `requirements.txt`):

| Harness | Needs | Checks |
| --- | --- | --- |
| `tests/verify_frontend.js` | Playwright + a running server | 54 runtime assertions in headless Chromium |
| `tests/capture_request.js` | Playwright + a running server | prints the request the page actually sent |
| `tests/verify_worker.js` | Node only | 61 assertions on the Cloudflare Worker, no network |
| `tests/verify_worker_live.js` | Node + network | 33 assertions against the deployed Render origin |

See [`tests/README.md`](tests/README.md) for how to run them.

### Verified results

Python 3.13, `openai/gpt-oss-120b` via Groq, live provider:

| Run | Result |
| --- | --- |
| `test_docker_contract.py` | 10/10 |
| `test_config.py` | 10/10 |
| `test_llm_stage.py` | 14/14 |
| `test_api_e2e.py` | 16/16 |
| `test_frontend.py` | 21/21 |
| `run_public_samples.py` (live LLM, `--delay 15`) | 10/10, quality ratio 1.0000, p95 3.3 s |

Both guard suites are negative-controlled rather than decorative:

- `test_docker_contract.py` — reverting the Dockerfile to `libgomp1` fails 2 tests; `libstdc++6` passes.
- `test_config.py` — planting a realistic key in a committed file fails the sweep; removing it passes.
- `test_openapi_request_body_schema_refs_all_resolve` — inlining `model_json_schema()`
  directly makes it fail on `#/$defs/HourInput` and `#/$defs/BatteryInput`, the exact two
  refs the browser reported; `_inline_schema_for_swagger()` passes.

**Verified from a fresh clone** (only committed files, no `.env`, credential supplied as a
platform environment variable — the deployment path):

| Check | Result |
| --- | --- |
| `cp .env.example .env` with no key, then start | `/health` → `{"status":"ok"}`, `provider=unconfigured`, clear warning, no crash |
| All suites in the clone | config 10/10, llm stage 14/14, e2e 16/16 |
| Public samples, live provider from the clone | 10/10, quality ratio 1.0000, p95 3.3 s |
| Env-var precedence | platform `LLM_MODEL` beats a conflicting `.env` value; `.env` wins when no env var is set |

---

## Architecture

```
                       ┌──────────────────────────────────────────────┐
  operator_notes ─────▶│  1. LLM INTERPRETER  (app/llm_interpreter.py)│
  (natural language)   │     OpenAI-compatible chat completion        │
                       │     strict JSON prompt, no phrase tables     │
                       └───────────────────────┬──────────────────────┘
                                               │ untrusted JSON
                                               ▼
                       ┌──────────────────────────────────────────────┐
                       │  2. GUARDRAILS  (app/guardrails.py, models.py)│
                       │     schema + note coverage + hour ordering    │
                       │     factor/range checks, no_op semantics      │
                       │     folds directives into per-hour limits     │
                       └───────────────────────┬──────────────────────┘
                                               │ trusted EffectiveConstraints
                                               ▼
                       ┌──────────────────────────────────────────────┐
                       │  3. OPTIMIZER  (app/optimizer.py)            │
                       │     PuLP / CBC linear program                 │
                       │     minimise SUM(grid[h] * tariff[h])         │
                       └───────────────────────┬──────────────────────┘
                                               │ plan
                                               ▼
                       ┌──────────────────────────────────────────────┐
                       │  4. REPLAY GATE + RESPONSE                   │
                       │     re-checks every rule before returning     │
                       └──────────────────────────────────────────────┘
```

The core idea from the Problem Statement is preserved: **human notes are never trusted as
math**. They are converted to a fixed structured format, checked by deterministic code,
and only then applied to the optimization model.

| Module | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI routes, HTTP status taxonomy, secret-safe logging |
| `app/models.py` | Strict Pydantic request/response models and validation |
| `app/llm_interpreter.py` | Prompt, defensive JSON parsing, retry, LLM-side repair |
| `app/guardrails.py` | Independent deterministic validation + constraint folding |
| `app/optimizer.py` | PuLP LP, plan extraction, final replay gate |
| `app/summary.py` | Deterministic `plan_summary` text |
| `app/fallback.py` | Disabled-by-default degraded-availability interpreter |

---

## LLM role and guardrails

### Model and provider

- **Provider:** any OpenAI-compatible chat-completions endpoint. The default is **Groq**
  (`https://api.groq.com/openai/v1`), chosen for its free tier and low latency.
- **Model:** `openai/gpt-oss-120b` by default. `gpt-4o-mini` is a drop-in alternative via
  `LLM_BASE_URL` + `LLM_MODEL`. Models verified end-to-end against all ten public cases:
  `openai/gpt-oss-120b` (default), `qwen/qwen3.8-27b` (fastest), `openai/gpt-oss-20b`.
- **Groq retires models.** `llama-3.3-70b-versatile` is no longer served and returns
  `404 model_not_found`. Before deploying, list what your key can use:

  ```bash
  curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"
  ```

- **Role of the LLM:** it is the **only** component that converts free text into
  structured directives. Its output feeds the optimizer directly, after deterministic
  validation. It is *not* used for `plan_summary`, documentation, or any cosmetic purpose
  — the summary is generated deterministically in `app/summary.py`.
- **Reproducibility:** `LLM_TEMPERATURE=0` and `response_format={"type": "json_object"}`
  are sent on every call. Malformed output is retried with the validation error appended
  as feedback.
- **No hard-coded phrase matching:** interpretation is not driven by a keyword table. (The
  keyword logic in `app/fallback.py` belongs to the *disabled* fallback and is documented
  as such.)

### Guardrails applied to the interpretation

Per Problem Statement §5.1 and §8:

- exactly one entry per note, in `note_index` order;
- only supported directive types;
- `no_op` ⇒ `applies=false` and `structured_adjustment=null`; every other type ⇒
  `applies=true`;
- hours are unique integers 0–23 in ascending order;
- `factor ∈ [0,1]`; reserve and cap values finite and non-negative;
- the interpretation never alters demand, tariff, or battery limits.

A final replay in `app/optimizer.py` re-checks the whole plan against every rule before the
response is returned, so an invalid plan cannot be served.

---

## Optimization model

Objective (Problem Statement §5.2):

```
minimise  total_cost_bdt = Σ grid_kwh[h] × tariff_bdt_per_kwh[h]   for h = 0..23
```

Per hour `h`, with `E[h]` the battery energy after hour `h`:

| Rule | Constraint |
| --- | --- |
| Energy balance (§9.5) | `grid + solar_used + discharge = demand + charge` |
| Effective solar (§9.4) | `solar_used ≤ solar[h] × Π(factors)` |
| Battery transition (§9.1) | `E[h] = E[h-1] + charge − discharge` |
| Reserve / capacity (§9.2) | `active_reserve[h] ≤ E[h] ≤ capacity` |
| Rate limits (§9.3) | `charge ≤ max_charge`, `discharge ≤ max_discharge` |
| No-charge window (§5.3) | `charge = 0` |
| No-discharge window (§5.3) | `discharge = 0` |
| Grid cap (§5.3) | `grid ≤ max_grid_kwh` |
| End-of-day neutrality (§9.6) | `E[23] = initial_energy_kwh` |

`solar_reduction` multiplies the forecast for the affected hours; `minimum_battery_reserve`
raises the floor via `max(base_minimum, directive_minimum)`.

Because no objective term rewards buying extra energy, the LP optimum never charges and
discharges in the same hour: any simultaneous pair could be reduced by
`min(charge, discharge)` without changing the battery trajectory or the cost.

---

## API reference

### `GET /health`

Readiness probe. Returns HTTP 200.

```json
{ "status": "ok" }
```

### `POST /optimize-energy`

**Request**

| Field | Type | Notes |
| --- | --- | --- |
| `scenario_id` | string | echoed back in the response |
| `operator_notes` | array[1..3] of non-empty string | natural-language notes |
| `hours` | array[24] | one entry per hour 0–23, exactly once |
| `hours[].hour` | integer 0–23 | unique |
| `hours[].demand_kwh` | number ≥ 0 | campus demand |
| `hours[].solar_kwh` | number ≥ 0 | base solar before directives |
| `hours[].tariff_bdt_per_kwh` | number ≥ 0 | grid price |
| `battery.capacity_kwh` | number > 0 | usable capacity |
| `battery.initial_energy_kwh` | number ≥ 0 | energy at the start of hour 0 |
| `battery.minimum_energy_kwh` | number ≥ 0 | base reserve floor |
| `battery.max_charge_kwh_per_hour` | number ≥ 0 | charge rate limit |
| `battery.max_discharge_kwh_per_hour` | number ≥ 0 | discharge rate limit |

Unknown fields are rejected. Extra keys in `hours[]` or `battery` fail validation.

**Response (HTTP 200)**

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": { "hours": [12, 13], "factor": 0.25 },
      "explanation": "Solar availability is reduced to 25% during the panel-cleaning window."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 90.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 110.0
    }
    /* ... 23 more ... */
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "..."
}
```

**Directive types** (`directive_type` ∈): `solar_reduction`,
`minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`,
`no_op`.

`factor` is the **usable fraction remaining**: an 80% reduction → `0.2`. Time windows are
**start-inclusive, end-exclusive**: 1 PM–3 PM → `[13, 14]`. `battery_action` ∈ `charge` |
`discharge` | `idle`, and `battery_kwh` must be `0` when idle.

**Status codes**

| Code | Meaning |
| --- | --- |
| 200 | success |
| 400 | malformed JSON or structurally invalid request |
| 422 | well-formed but semantically invalid request, or the LLM could not produce a valid interpretation |
| 500 | controlled internal error (no stack trace, no secrets) |

`/docs` shows the request body pre-filled with a complete, executable 24-hour scenario, so
**Execute** works with no typing; you can also paste your own JSON over it. The example is
verified by `test_documented_example_round_trips_through_the_endpoint`.

> **Why the request body is declared via `openapi_extra`.** The handler reads the raw body
> itself in order to map malformed JSON to `400` rather than a framework `422`. That makes
> the body invisible to FastAPI's OpenAPI inference, so Swagger would otherwise show
> *"No parameters"* with no input box. The schema is built by
> `_inline_schema_for_swagger()`, which rewrites Pydantic's document-root-relative
> `#/$defs/X` pointers to `#/components/schemas/X` and registers the nested `HourInput` /
> `BatteryInput` models there — inlining `model_json_schema()` directly makes `/docs`
> report *"Invalid object key `$defs`"*. Declaring a real `Body(...)` parameter instead
> would fix the docs but break the error taxonomy, because FastAPI would validate before
> the handler runs and answer `422` where the spec requires `400`.
> `test_openapi_request_body_schema_refs_all_resolve` guards this by resolving every
> `$ref` in the served document.

### curl examples

**Minimal inline body** (abridged to five hours for readability — a real request needs all
24):

```bash
curl -s -X POST "$BASE/optimize-energy" \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "GRID-101",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "Do not charge the battery between 2 PM and 4 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour": 0,"demand_kwh": 180,"solar_kwh": 0,"tariff_bdt_per_kwh": 7},
      {"hour": 1,"demand_kwh": 175,"solar_kwh": 0,"tariff_bdt_per_kwh": 7},
      {"hour": 2,"demand_kwh": 170,"solar_kwh": 0,"tariff_bdt_per_kwh": 6},
      {"hour": 3,"demand_kwh": 165,"solar_kwh": 0,"tariff_bdt_per_kwh": 6},
      {"hour": 4,"demand_kwh": 165,"solar_kwh": 5,"tariff_bdt_per_kwh": 6}
    ],
    "battery": {
      "capacity_kwh": 500,
      "initial_energy_kwh": 200,
      "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100,
      "max_discharge_kwh_per_hour": 100
    }
  }' | python -m json.tool
```

**A complete, ready-to-POST scenario** — use the public cases instead of retyping 24 rows:

```bash
curl -s -X POST "$BASE/optimize-energy" \
  -H "Content-Type: application/json" \
  --data-binary @demo_inputs/SAMPLE-01.json | python -m json.tool
```

**Error behaviour**

```bash
# malformed JSON -> 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/optimize-energy" \
  -H "Content-Type: application/json" -d '{not json'

# structurally invalid (missing hours) -> 400/422
curl -s -X POST "$BASE/optimize-energy" \
  -H "Content-Type: application/json" -d '{"scenario_id":"X"}'
```

> **Windows note:** use `--data-binary @file` rather than `-d "$BODY"`. Git-Bash/CMD
> quoting can silently send an empty body, which the API correctly reports as
> `400 {"error":"request body is empty"}` — that looks like an API failure but is a shell
> quoting bug.

---

## Operator web UI

The service serves its own front end at **`/`**, from the same origin and port as the API.
There is nothing extra to configure: the Render service you deploy *is* the UI.

Open the root URL and you can:

- write the day's operator notes (one per line, max 3);
- edit the battery parameters and every cell of the 24-hour forecast — the table is
  pre-filled with a plausible campus day, so it runs without any typing;
- press **Optimize schedule** and read the result: total cost, grid energy, peak grid, one
  card per interpreted directive, a hand-rolled SVG chart of the hourly grid/solar/battery
  profile, the full 24-row schedule with the hours touched by a directive highlighted, and
  the raw JSON response;
- **or skip the form entirely** — see below.

### Paste a whole request JSON

The bottom panel takes a complete `POST /optimize-energy` body. A judge who wants to bring
their own scenario does not have to retype it into 72 fields.

| Button | Does |
| --- | --- |
| **Load JSON** | parses and validates, then enables the two buttons below |
| **Optimize this JSON** | POSTs exactly what you pasted — the form is not consulted |
| **Fill the form** | loads the pasted values into the fields above so every cell becomes editable |
| **Paste a sample request** | fills the box with a complete valid 24-hour scenario |
| **Clear** | empties the box and resets the panel state |

Validation happens on the client before anything is sent, so a mistake produces a specific
message rather than a bare `HTTP 400`:

- malformed JSON → the engine's own message, **plus a line/column when the engine reports an
  offset** (it does not for `Unexpected token`; the page says so honestly instead of
  pointing at the wrong character);
- an unknown key → the stray key is named, so a typo like `noets` is caught directly
  rather than being reported as the field it displaced;
- a wrong hour count → `"hours" must have exactly 24 entries (got 23)`;
- an impossible battery → `"battery.minimum_energy_kwh" (400) exceeds the starting energy (200)`.

> The client check is a **friendly front end, not a substitute for the server** — the
> service is authoritative and still validates everything. What the client must never do is
> *accept* something the server will reject, because a judge told "this looks fine" who then
> gets a `422` is worse off than one who got no client validation at all.
> `test_paste_validator_output_passes_the_real_model` asserts that direction: everything the
> client accepts is fed through `OptimizationRequest.model_validate`.

### Routes

| Route | Serves |
| --- | --- |
| `GET /` | the operator UI (`app/static/index.html`) |
| `GET /api` | the JSON service index |
| `GET /health` | readiness probe |
| `GET /docs` | Swagger UI |
| `POST /optimize-energy` | the optimizer |
| `GET /static/*` | the UI's CSS and JS |

> `GET /` used to return the JSON index. That moved to `GET /api` when the UI was added.
> Anything that parsed the old root response must switch to `/api`.

Design constraints behind it:

- **No external dependencies.** No CDN, no web font, no charting library, no build step.
  `app/static/` is three hand-written files (~1250 lines) shipped as-is. A judging
  environment with no network still renders the whole page, and a reviewer can read all of
  it.
- **Dependency-free chart.** The SVG chart is built with `createElementNS` and coloured from
  the live CSS custom properties, so it follows the light/dark toggle without any hard-coded
  hex. It plots 24 grid bars, the solar contribution stacked on each bar, and the battery
  energy level as a line with 24 points.
- **Same origin.** Serving the UI from the API origin means no CORS configuration and one
  service to deploy rather than two.
- **Errors are real.** A `400`/`422`/`500` is rendered with its taxonomy-appropriate
  explanation and any `details` the API returned — the page never claims success when the
  request failed.
- **Accessible.** Keyboard-operable, labelled controls, `aria-live` on the status pill and
  error region, a skip link, a visible focus ring, and `prefers-reduced-motion` respected.

`tests/test_frontend.py` enforces the delivery contract from the source, and
`tests/verify_frontend.js` verifies runtime behaviour (the chart, the 390px layout, the
paste round trip, a live run) in real headless Chromium.

---

## Deployment

### Render (free tier)

> **Step-by-step runbook:** [`DEPLOY_RENDER.md`](DEPLOY_RENDER.md) — 8 steps from a rotted
> key to a verified live URL, including the build-risk assessment, a troubleshooting table,
> and the Docker fallback path.

**Option A — Blueprint (`render.yaml` is included)**

1. Push this repository to GitHub.
2. Render dashboard → **New** → **Blueprint** → select the repository.
3. When prompted, set `GROQ_API_KEY` (the `sync: false` variable).
4. Apply. Render builds with `pip install -r requirements.txt` and starts
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
5. Wait for the health check on `/health` to go green.

**Option B — manual Web Service**

1. **New** → **Web Service** → connect the repository.
2. Runtime **Python 3**, region of your choice, plan **Free**.
3. Build command: `pip install --upgrade pip && pip install -r requirements.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Health check path: `/health`
6. Environment → add `GROQ_API_KEY` (plus `LLM_BASE_URL` / `LLM_MODEL` if not using Groq defaults).
7. Deploy, then confirm:

```bash
curl -s https://<your-service>.onrender.com/health
# {"status":"ok"}
```

**Verify the deployment from outside your environment** (required by the guide):

```bash
BASE=https://<your-service>.onrender.com
python tests/run_public_samples.py --base-url "$BASE" --delay 15 --json-out report_render.json
```

`--delay 15` matters on a free-tier key: each interpretation costs ~1500–2500 tokens against
an **8000 tokens/minute** bucket, so an un-paced run exhausts the quota after roughly five
cases and the rest fail with a provider 429 — a rate limit, not a bug in the service. Expect
**10/10 with quality ratio 1.0000**; keep `report_render.json` as per-case evidence.

**Free-tier specifics:** the instance sleeps after ~15 minutes of inactivity and takes tens
of seconds to wake, which can exceed the judge's 30 s per-request timeout. Either keep it
warm during judging or use the Docker fallback. `$PORT` is injected by Render — never
hard-code it. `DEPLOY_RENDER.md` has the full troubleshooting table.

---

## Serving a subpath via Cloudflare Worker

To expose the app at `https://yourdomain.com/gridwise` while the origin stays on Render,
use [`deploy/cloudflare-worker.js`](deploy/cloudflare-worker.js).

### Why a path-stripping proxy is not enough

The app generates **absolute** URLs and its JavaScript fetches at **runtime**:

```
=== absolute paths in index.html ===      === runtime calls in app.js ===
href="/static/app.css"                    fetch("/health")            (line 653)
src="/static/app.js"                      fetch("/optimize-energy")   (line 676)
href="/docs"
```

Stripping `/gridwise` on the way in and rewriting `href`/`src` on the way out fixes the
*document*, but not `app.js`. Those two `fetch()` calls run in the browser **after** the page
loads, so the browser resolves them against `yourdomain.com`, not Render — `/health` 404s and
the Optimize button fails. No amount of server-side HTML rewriting can fix a URL that does
not exist until after the page has loaded.

The Worker therefore does three things, and all three are needed:

| # | Mechanism | Fixes |
| - | --------- | ----- |
| 1 | Path routing — `/gridwise/X` is fetched from Render as `/X` | the proxy leg |
| 2 | HTML rewriting of root-absolute `href`/`src` | the initial document |
| 3 | An **injected runtime shim** redefining `window.fetch` and `XMLHttpRequest.prototype.open` | `app.js`'s runtime calls |

Plus three details that are easy to miss:

- **`Accept-Encoding: identity`** on the upstream request. If Render gzips the HTML,
  `response.text()` yields binary and the rewriting corrupts the page.
- **`redirect: "manual"`**, so the Worker can prefix `Location` itself — otherwise `/docs`
  sends the browser to `/docs/oauth2-redirect` at the wrong root.
- **`duplex: "half"`** whenever a streamed `body` is forwarded. Required by the spec;
  without it the POST fails with `RequestInit: duplex option is required when sending a body`.

`X-Forwarded-Host` / `X-Forwarded-Proto` are set so FastAPI's `url_for()` — used by Swagger UI
— builds correct URLs, and `Set-Cookie` is re-scoped from `Path=/` to `Path=/gridwise/` so
cookies are not broadcast across the whole domain.

### Deploy

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Worker**.
2. Paste `deploy/cloudflare-worker.js`, edit `ORIGIN` / `PREFIX`, **Deploy**.
3. Add a route: **yourdomain.com/gridwise*** → this Worker.
4. Verify:

```bash
curl -sI https://yourdomain.com/gridwise/health
curl -s  https://yourdomain.com/gridwise/api | head -c 200
```

5. Open `https://yourdomain.com/gridwise/` and press **Optimize**. The page loading is *not*
   proof — the Optimize button is what exercises the shim.

### Tests

```bash
node tests/verify_worker.js        # 61 checks, no network
node tests/verify_worker_live.js   # 33 checks against real Render
```

Both are needed. The offline harness uses a fake origin and cannot reach a real `fetch`, so
it could not see the `duplex` bug; the live harness drives the actual deployment.

---

## Docker fallback

The Docker image is a documented alternative execution path. The Dockerfile installs
`libstdc++6` (required by the bundled CBC binary inside `python:3.12-slim`) and `curl` (for
the healthcheck).

**Build and run locally:**

```bash
docker build -t gridwise-energy:1.0.0 .
docker run --rm -p 8000:8000 -e GROQ_API_KEY="your-key-here" gridwise-energy:1.0.0
```

**Or pull the published image** (replace with your exact tag or digest):

```bash
docker pull <registry>/gridwise-energy:1.0.0
docker run --rm -p 8000:8000 \
  -e GROQ_API_KEY="your-key-here" \
  <registry>/gridwise-energy:1.0.0
```

**Then verify:**

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

The image binds `0.0.0.0`, honours `$PORT`, exposes `8000`, contains **no baked-in
credentials**, and runs as a non-root user.

To re-verify CBC's library dependency after any `pulp` upgrade:

```bash
strings <pulp_path>/solverdir/cbc/linux/i64/cbc | grep -E '^lib(stdc\+\+|gomp)'
```

(`libgomp1` is *not* required — CBC has no OpenMP dependency, despite this being a common
assumption. Installing it instead of `libstdc++6` makes the solver fail at container runtime
only.)

---

## Known limitations

- **Provider dependency.** The LLM stage requires a reachable, credentialed provider. Teams own their keys, quota, and availability. If the provider is down and `ALLOW_DETERMINISTIC_FALLBACK=false` (the default), the endpoint returns a controlled 422 rather than a wrong answer.
- **Deterministic fallback is disabled by default.** The challenge requires the language model on the interpretation path, so the keyword fallback in `app/fallback.py` is opt-in only (`ALLOW_DETERMINISTIC_FALLBACK=true`) and every response it produces is labelled as a fallback in `plan_summary`. It handles the public note phrasings but is weaker than the LLM on unseen paraphrases; it exists for availability, not as a substitute.
- **Model choice affects extraction, not scheduling.** A weaker model may mis-extract hours or factors. Guardrails reject structurally invalid output and retry, but a confidently wrong numeric value that is still in range cannot be detected. `openai/gpt-oss-120b` and `gpt-4o-mini` both handle the sample phrasing well.
- **Free-tier tokens-per-minute is the real throughput limit.** Groq's free tier allows **8000 tokens/minute**, and each interpretation costs roughly 1500–2500 tokens because the system prompt documents all six directive types. Ten cases back to back therefore exhaust the bucket after ~5 requests, and the remainder fail with a controlled 422 (not a crash, and not a wrong answer). Run the suite with pacing to stay inside the budget:
  ```bash
  python tests/run_public_samples.py --offline --delay 15
  ```
  This is a quota limit of the provider account, not a property of the implementation. A paid tier, a higher `LLM_MODEL` quota, or the pacing flag all resolve it.
- **The optimizer is a linear program, not a mixed-integer one.** Actions are derived from the optimal flows. Because no objective term rewards simultaneous charge and discharge, the optimum is naturally action-consistent; a log warning fires if the solver ever reports both in one hour, and the plan is collapsed to its net effect so the returned schedule stays valid.
- **Degenerate optima exist.** Several distinct schedules can achieve the same minimum cost (the objective is indifferent to which hours carry the fixed total grid energy). Any of them is valid; the judge compares cost and validity, not the exact action sequence.
- **One-shot latency.** A request costs one to three provider calls. `p95` is dominated by provider latency; budget roughly 1–4 s with Groq.
- **No electricity export.** Grid export is out of scope, so surplus solar is curtailed and `grid_kwh` is never negative.
- **Render free tier cold starts.** The instance sleeps when idle; the first request after a sleep can exceed the ~30 s judging timeout. Warm it before evaluation or use the Docker fallback.
- **In-memory only.** No caching, database, or cross-request state. Every request is independent, which is intentional for judging determinism.

---

## Security and secret handling

- No credentials are committed. `.env` is git-ignored. Only `.env.example` (names and empty placeholders) is tracked — and note that `.env.example` **is** whitelisted for committing, so a real key pasted into the template would be published. `tests/test_config.py` sweeps the whole repository for credential-shaped literals and asserts the template ships only empty placeholders.
- `.env` is loaded at startup with `override=False`, so a platform-injected variable (Docker `-e`, Render env settings) always wins over the file. A committed `.env` can never shadow a deployment secret.
- Credentials are read from the environment at startup and never written to logs or responses.
- `app/config.py:redact()` scrubs credential-shaped substrings from any error text before it is logged or raised, including provider error bodies that echo request headers. This is covered by a test.
- Error responses never include a stack trace. The API returns one of a small set of fixed messages.
- `operator_notes` are screened before logging: a note containing response-reserved keys (`error`, `detail`, `message`) is withheld rather than echoed into a log line, so a caller cannot smuggle content into a response field.
- Request bodies are capped at 1 MB to bound memory use.
- The Docker image contains no secrets and runs as a non-root user.

---

## Dependencies and credits

| Package | Version | Purpose |
| --- | --- | --- |
| `fastapi` | 0.115.6 | HTTP API framework |
| `uvicorn[standard]` | 0.34.0 | ASGI server |
| `pydantic` | 2.10.4 | Request/response validation |
| `httpx` | 0.28.1 | LLM provider HTTP client |
| `pulp` | 3.2.2 | Linear programming model (ships the CBC solver) |
| `python-dotenv` | 1.0.1 | `.env` loading (optional convenience) |

Third-party APIs: an OpenAI-compatible chat-completions endpoint (Groq by default; OpenAI,
Together, OpenRouter, or a local vLLM/Ollama server all work).

The front end is dependency-free and CDN-free by design — no charting library, no framework,
no build step.

The problem statement, participant guide, and public sample pack were provided by the
BUP CSE Fest 2026 organizers and live in `pdf/`.

**Submission note:** the repository must be created after the question reveal, kept private
during the event, and made public after the submission deadline.

---

## Project layout

```text
.
├── app/
│   ├── config.py             # environment-driven settings, secret redaction
│   ├── fallback.py           # disabled-by-default degraded interpreter
│   ├── guardrails.py         # deterministic validation + constraint folding
│   ├── llm_interpreter.py    # prompt, JSON parsing, retry, repair
│   ├── main.py               # FastAPI app, routes, error taxonomy, static mount
│   ├── models.py             # strict Pydantic models
│   ├── optimizer.py          # PuLP LP + replay gate
│   ├── summary.py            # deterministic plan summary
│   └── static/               # the operator UI — served at /, no build step
│       ├── index.html        #   markup
│       ├── app.css           #   theme tokens, layout, light/dark
│       └── app.js            #   request building, SVG chart, rendering
├── demo_inputs/              # the 10 public sample inputs, extracted verbatim
├── deploy/
│   └── cloudflare-worker.js  # serve under a subpath via Cloudflare
├── pdf/                      # official documents and public sample cases
├── tests/
│   ├── README.md              # how to run the browser and Worker harnesses
│   ├── capture_request.js     # browser: print the real request/response
│   ├── verify_frontend.js     # browser: 54 runtime assertions
│   ├── verify_worker.js       # Node: 61 Worker assertions, offline
│   ├── verify_worker_live.js  # Node: 33 Worker assertions, live origin
│   ├── run_public_samples.py  # independent judge-parity harness
│   ├── test_api_e2e.py        # HTTP pipeline tests
│   ├── test_config.py         # settings tests
│   ├── test_docker_contract.py# packaging + static-asset tests
│   ├── test_frontend.py       # frontend delivery contract
│   └── test_llm_stage.py      # LLM-stage unit tests
├── Dockerfile
├── DEPLOY_RENDER.md
├── render.yaml
├── requirements.txt
├── .env.example
└── README.md
```
