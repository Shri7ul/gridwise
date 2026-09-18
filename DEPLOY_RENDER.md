# Deploying GridWise on Render (Free Tier)

Everything in this guide has been verified against the repository. Where a step  
could not be executed here (no Render account, no Docker daemon), that is stated  
explicitly rather than assumed.

The service is single-port: the operator UI is served at `/` and the API alongside
it, so one Render web service covers both. There is nothing to configure for the
front end.

## Before you start — two blocking items

### 1. Rotate the Groq key

The key currently in `.env` was, at one point, written into `.env.example`.  
That file **is** committed to the repository (`.gitignore` whitelists it with  
`!.env.example`). Even though it has since been removed, treat the key as  
exposed:

1. Go to <https://console.groq.com/keys>
2. Delete the existing key
3. Create a new one — you will paste it into Render in step 4

### 2. Do not raise the Uvicorn worker count

Render's free tier gives **512 MB RAM**. One worker is correct — leave the start  
command alone. See "Free-tier behaviour" below.

---

## Step 1 — Create the Git repository

Render deploys from a Git host. From the project root:

```bash
cd D:\Hackathon\BUP

git init
git add .
git status              # <-- READ THIS BEFORE COMMITTING

# Confirm .env is NOT staged:
#   You should NOT see ".env" in the list.
#   You SHOULD see ".env.example".
```

If `.env` appears in `git status`, stop and fix `.gitignore` before continuing.

```bash
git commit -m "GridWise: LLM-interpreted energy optimizer for BUP CSE Fest 2026"
git branch -M main
```

Create an empty repository on GitHub, then:

```bash
git remote add origin https://github.com/<your-username>/gridwise.git
git push -u origin main
```

> The challenge asks for the repository to be **private during the event** and made  
> public after the submission deadline. GitHub private repos work with Render.

---

## Step 2 — Verify the build will work

Two things could break Render's build. Both are already handled:

| Risk                                 | Status                                                                                                                                                |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pulp` needs a C compiler to install | **Safe.** `pulp 3.2.2` publishes `pulp-3.2.2-py3-none-any.whl` (pure Python, any platform). No build step, and the CBC solver ships inside the wheel. |
| CBC solver missing at runtime        | **Safe.** `pulp/solverdir/cbc/linux/i64/cbc` is inside the wheel. On Render's native Python runtime this is the same binary verified locally.         |

There is **no** `apt-get` step possible on Render's native Python runtime, which is  
why the fallback Docker image exists (step 7).

---

## Step 3 — Create the Render service

### Option A — Blueprint (uses the committed `render.yaml`)

1. Render Dashboard → **New** → **Blueprint**
2. Connect your GitHub account and pick the `gridwise` repository
3. Render reads `render.yaml` and shows the service it will create
4. It will prompt for `GROQ_API_KEY` (declared with `sync: false`) — paste the  
   **new** key from step 1
5. Click **Apply**

### Option B — Manual web service

If you prefer to configure by hand:

1. **New** → **Web Service** → connect the repository
2. Settings:
   | Field             | Value                                                          |
   | ----------------- | -------------------------------------------------------------- |
   | Name              | `gridwise-energy-optimization`                                 |
   | Runtime           | **Python 3**                                                   |
   | Region            | Singapore (closest to Bangladesh)                              |
   | Branch            | `main`                                                         |
   | Build Command     | `pip install --upgrade pip && pip install -r requirements.txt` |
   | Start Command     | `uvicorn app.main:app --host 0.0.0.0 --port $PORT`             |
   | Instance Type     | **Free**                                                       |
   | Health Check Path | `/health`                                                      |
3. Add the environment variables exactly as in step 4.

> `--host 0.0.0.0` is mandatory. Render routes traffic to the container's external  
> interface; binding `127.0.0.1` produces a service that starts but never responds.

---


## Step 4 — Environment variables

Add these under **Environment** in the dashboard.

**Required — the credential (set exactly one):**

| Key            | Value                                                |
| -------------- | ---------------------------------------------------- |
| `GROQ_API_KEY` | your **new** `gsk_...` key — mark it as a **secret** |

**Required — settings (`render.yaml` already supplies these in Option A):**

| Key                            | Value                            | Why                                                   |
| ------------------------------ | -------------------------------- | ----------------------------------------------------- |
| `LLM_BASE_URL`                 | `https://api.groq.com/openai/v1` | Groq's OpenAI-compatible endpoint                     |
| `LLM_MODEL`                    | `openai/gpt-oss-120b`            | verified working; the previous Llama default now 404s |
| `PYTHON_VERSION`               | `3.12.7`                         | matches the version tested locally                    |
| `ALLOW_DETERMINISTIC_FALLBACK` | `false`                          | **leave this on `false`** — see the warning below     |
| `LLM_TIMEOUT_SECONDS`          | `12`                             | per-attempt provider timeout                          |
| `REQUEST_DEADLINE_SECONDS`     | `25`                             | keeps the request inside Render's limits              |
| `LLM_MAX_RETRIES`              | `2`                              | retries after a malformed model response              |
| `LOG_LEVEL`                    | `INFO`                           |                                                       |

**Do not set `PORT` yourself.** Render injects it; the app reads `$PORT`.

### ⚠️ Keep `ALLOW_DETERMINISTIC_FALLBACK=false`

The Participant Guide (§09) states that if the required LLM is absent from the  
operator-note interpretation path, the submission is **not eligible for the final  
preliminary shortlist**. With this flag `true`, a provider outage would route  
requests through `app/fallback.py`, a keyword interpreter — exactly the situation  
the rule targets. On `false`, the endpoint returns a controlled 422 instead, which  
is the safe and spec-sanctioned behaviour.

---


## Step 5 — Deploy and verify

Render builds and deploys automatically. Watch the log for:

```text
gridwise starting: version=1.0.0 provider=groq model=openai/gpt-oss-120b llm_configured=True fallback_enabled=False configured_port=<port>
```

`llm_configured=True` is the line that matters. If it says `False`, the credential  
did not reach the service — recheck the variable name and that you saved it.

Then verify from outside:

```bash
export BASE=https://<your-service-name>.onrender.com

# 0. The UI must be served — status 200 and content-type text/html
curl -s -o /dev/null -w "UI %{http_code} %{content_type}\n" "$BASE/"
# UI 200 text/html; charset=utf-8

#    …and the JSON index must have moved to /api
curl -s "$BASE/api"
# {"service":"gridwise-energy-optimization",…,"ui":"/"}

# 1. Readiness — must be exactly {"status":"ok"}
curl -s "$BASE/health"

# 2. Real optimization call
curl -s -X POST "$BASE/optimize-energy" \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "GRID-DEPLOY-1",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "Do not charge the battery between 2 PM and 4 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour":0,"demand_kwh":180,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":1,"demand_kwh":175,"solar_kwh":0,"tariff_bdt_per_kwh":7},
      {"hour":2,"demand_kwh":170,"solar_kwh":0,"tariff_bdt_per_kwh":6},
      {"hour":3,"demand_kwh":165,"solar_kwh":0,"tariff_bdt_per_kwh":6},
      {"hour":4,"demand_kwh":165,"solar_kwh":5,"tariff_bdt_per_kwh":6},
      {"hour":5,"demand_kwh":170,"solar_kwh":15,"tariff_bdt_per_kwh":7},
      {"hour":6,"demand_kwh":185,"solar_kwh":30,"tariff_bdt_per_kwh":9},
      {"hour":7,"demand_kwh":200,"solar_kwh":55,"tariff_bdt_per_kwh":11},
      {"hour":8,"demand_kwh":210,"solar_kwh":85,"tariff_bdt_per_kwh":13},
      {"hour":9,"demand_kwh":220,"solar_kwh":120,"tariff_bdt_per_kwh":15},
      {"hour":10,"demand_kwh":230,"solar_kwh":150,"tariff_bdt_per_kwh":16},
      {"hour":11,"demand_kwh":235,"solar_kwh":170,"tariff_bdt_per_kwh":17},
      {"hour":12,"demand_kwh":240,"solar_kwh":185,"tariff_bdt_per_kwh":16},
      {"hour":13,"demand_kwh":235,"solar_kwh":175,"tariff_bdt_per_kwh":15},
      {"hour":14,"demand_kwh":225,"solar_kwh":145,"tariff_bdt_per_kwh":14},
      {"hour":15,"demand_kwh":220,"solar_kwh":100,"tariff_bdt_per_kwh":15},
      {"hour":16,"demand_kwh":225,"solar_kwh":55,"tariff_bdt_per_kwh":19},
      {"hour":17,"demand_kwh":240,"solar_kwh":15,"tariff_bdt_per_kwh":23},
      {"hour":18,"demand_kwh":260,"solar_kwh":0,"tariff_bdt_per_kwh":29},
      {"hour":19,"demand_kwh":270,"solar_kwh":0,"tariff_bdt_per_kwh":31},
      {"hour":20,"demand_kwh":260,"solar_kwh":0,"tariff_bdt_per_kwh":27},
      {"hour":21,"demand_kwh":230,"solar_kwh":0,"tariff_bdt_per_kwh":19},
      {"hour":22,"demand_kwh":205,"solar_kwh":0,"tariff_bdt_per_kwh":11},
      {"hour":23,"demand_kwh":205,"solar_kwh":0,"tariff_bdt_per_kwh":9}
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

> On **Windows**, curl quoting can silently strip the payload and you will get  
> `400 {"error":"request body is empty"}`. If that happens, save the JSON to a  
> file and post it with `--data-binary` instead — that path is verified:
>
> ```bash
> curl -s -X POST "$BASE/optimize-energy" \
>   -H "Content-Type: application/json" --data-binary @body.json
> ```

You should get HTTP 200 with a 24-entry `hourly_plan`. The three directives are  
keyed by `directive_type` (not `type`), and each carries an `applies` flag:

| `directive_type`   | `applies` | `structured_adjustment`          |
| ------------------ | --------- | -------------------------------- |
| `solar_reduction`  | `true`    | `{"hours":[13,14],"factor":0.2}` |
| `no_charge_window` | `true`    | `{"hours":[14,15]}`              |
| `no_op`            | `false`   | `null`                           |

Measured on this configuration: HTTP 200, `total_cost_bdt` = 54404.0,  
`total_grid_kwh` = 4071.0, `peak_grid_kwh` = 305.0. The top-level keys are  
`scenario_id`, `directive_interpretation`, `hourly_plan`, `plan_summary`,  
`total_cost_bdt`, `total_grid_kwh`, `peak_grid_kwh` — note **`plan_summary`**,  
which is generated deterministically in `app/summary.py` and never by the model.

> These totals are specific to this example payload. Do not treat them as golden  
> values to assert against — if you change any hourly demand or solar figure the  
> cost moves. The invariant worth checking is that `total_grid_kwh` equals  
> `sum(hourly_plan[*].grid_kwh)` and the final `battery_energy_after_kwh` returns  
> to the initial 200.0.

### Run the full public suite against the deployed URL

This is the strongest end-to-end check — it replays all ten published cases and  
compares costs and every constraint against the official expectations:

```bash
python tests/run_public_samples.py --base-url "$BASE" --delay 15
```

`--delay 15` matters: Groq's free tier allows **8000 tokens/minute** and each  
interpretation costs roughly 1500–2500, so an unpaced run exhausts the bucket  
after about five cases and the remainder fail for quota reasons, not correctness.

---

## Step 6 — Free-tier behaviour you must plan for

| Behaviour             | Detail                                                                                                                                                                      | What to do                                                                    |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| **Cold starts**       | A free instance sleeps after ~15 minutes idle. The next request can take 30–60 s to wake, which exceeds the judge's 30 s per-request timeout and scores zero for that case. | Keep it warm with a periodic ping, or rely on the Docker fallback. See below. |
| **512 MB RAM**        | Ample for one Uvicorn worker plus CBC.                                                                                                                                      | Do not add `--workers`.                                                       |
| **Monthly hours cap** | Free instances get 750 instance-hours/month, which covers one always-on service.                                                                                            | Keep it to a single service.                                                  |
| **No `apt-get`**      | Native Python runtime cannot install system packages.                                                                                                                       | Not needed — CBC ships in the wheel.                                          |

### Keeping it warm

A free uptime pinger that hits `/health` every 10 minutes prevents the sleep.  
Alternatively, ping it yourself shortly before the judging window:

```bash
curl -s "$BASE/health" && echo " awake"
```

---


## Step 7 — The Docker fallback image (required deliverable)

The Guide requires a **pullable container image** as a fallback execution path,  
because the hosted endpoint may be unavailable during judging. This is worth  
rubric points on its own, not just insurance.

Docker is not installed on this machine, so the build and push have **not** been  
executed. Everything else has been verified: the Dockerfile installs the correct  
solver library, copies only the files it needs, and binds `0.0.0.0`.

On any machine with Docker Desktop:

```bash
cd D:\Hackathon\BUP

# 1. Build (multi-stage; the final image ships no build tooling and no secrets)
docker build -t <your-dockerhub-user>/gridwise-energy:1.0.0 .

# 2. Smoke-test it exactly as the organisers will
docker run --rm -p 8000:8000 \
  -e GROQ_API_KEY="<your-new-key>" \
  <your-dockerhub-user>/gridwise-energy:1.0.0

# In another terminal:
curl -s http://127.0.0.1:8000/health     # {"status":"ok"}
```

Once that works, push it:

```bash
docker login
docker push <your-dockerhub-user>/gridwise-energy:1.0.0

# Record the digest — the Guide says an exact tag OR digest is required.
docker inspect --format='{{index .RepoDigests 0}}' \
  <your-dockerhub-user>/gridwise-energy:1.0.0
```

> Registry images on Docker Hub free accounts can be removed after prolonged  
> inactivity. The Guide says the image "must remain pullable during evaluation",  
> so push it close to the deadline and confirm the repo is public.

Update the README's Docker section with your real registry path, tag, and digest —  
it currently contains placeholder text.

---


## Step 8 — Troubleshooting

| Symptom                                               | Cause                                                        | Fix                                                                                                                 |
| ----------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Deploy fails, log mentions `No matching distribution` | `PYTHON_VERSION` not set or invalid                          | Set `PYTHON_VERSION=3.12.7`                                                                                         |
| Log says `llm_configured=False`                       | Credential missing or misnamed                               | Must be one of `GROQ_API_KEY`, `OPENAI_API_KEY`, `LLM_API_KEY`                                                      |
| Every request returns 422                             | Provider unreachable, **or** free-tier token quota exhausted | Check the Render log for `HTTP 429`/quota messages; wait ~60 s and retry                                            |
| Every request returns 404 `model_not_found`           | Groq retired the model                                       | Run `curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"` and update `LLM_MODEL` |
| Service starts but curls hang                         | Bound to `127.0.0.1`                                         | Start command must be `--host 0.0.0.0 --port $PORT`                                                                 |
| First request times out, then works                   | Free-tier cold start                                         | Warm it with a `/health` ping before judging                                                                        |
| 500 on every call                                     | Provider error                                               | Log is redacted; the response deliberately hides details. Check `llm_configured` and quota first.                   |

---

## Deployment checklist

- [ ] Groq key **rotated** at console.groq.com/keys
- [ ] `git status` showed **no `.env`** before the first commit
- [ ] Repository pushed to GitHub (private during the event)
- [ ] Render service created (blueprint or manual)
- [ ] `GROQ_API_KEY` set as a secret environment variable
- [ ] `LLM_MODEL=openai/gpt-oss-120b`, `PYTHON_VERSION=3.12.7`
- [ ] `ALLOW_DETERMINISTIC_FALLBACK=false`
- [ ] Log shows `llm_configured=True`
- [ ] `curl "$BASE/"` returns **200 `text/html`** — the UI is served
- [ ] `curl "$BASE/api"` returns the JSON index with `"ui":"/"` (it moved off `/`)
- [ ] The UI loads in a browser, **Optimize schedule** returns a plan, and the page
      is usable at phone width (no horizontal scroll)
- [ ] The **Paste a whole request** panel loads a scenario and **Optimize this
      JSON** returns a plan (the judge-facing path)
- [ ] Remote `curl "$BASE/health"` returns `{"status":"ok"}`
- [ ] Remote `POST /optimize-energy` returns 200 with a 24-hour plan
- [ ] `python tests/run_public_samples.py --base-url "$BASE" --delay 15` → 10/10
- [ ] Docker image built, smoke-tested, pushed, digest recorded
- [ ] README updated with the real base URL, image tag, and digest
- [ ] Repository made public after the deadline
- [ ] 3-minute video recorded
