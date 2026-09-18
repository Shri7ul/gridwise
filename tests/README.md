# Browser verification harnesses

These two scripts drive a real headless Chromium against a running server. They
are **not** part of the Python suite and are not run by `pytest`, because they
need `playwright`, which is deliberately not a runtime dependency of the service.
`tests/test_frontend.py` (the Python suite) covers everything that can be checked
from the HTML/CSS/JS source alone; these cover what only a renderer can.

## Why they exist

Static analysis cannot tell you that `getElementById("notes")` is returning an
`<h3>` because a heading and a textarea share an `id`. It did happen:

```
page.inputValue: Node is not an <input>, <textarea> or <select>
```

The same applies to the hand-rolled SVG chart — that it produces 35 bars and 24
points is a runtime fact. And the mobile-layout bug (30px of horizontal overflow
at 390px, caused by the six-column schedule table forcing its grid track wider
than the viewport) only surfaced after the result panel was rendered. None of
these were visible without executing the page.

## Setup

Playwright and its Chromium build are installed into the managed Node workspace,
outside the repo:

```bash
mkdir -p "C:/Users/USER/.workbuddy-ai/binaries/node/workspace"
cd "C:/Users/USER/.workbuddy-ai/binaries/node/workspace"
npm install playwright
npx playwright install chromium
```

Node resolves `require("playwright")` relative to the **script's** directory, not
the cwd, so `NODE_PATH` must point at that workspace when you run these.

## Start a server first

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 10000
```

Confirm you have the current revision before trusting the result — a stale
process on the port serves old code and produces confusing failures:

```bash
curl -s http://127.0.0.1:10000/api
# must include: "ui":"/"
```

## `verify_frontend.js` — the full check

```bash
export NODE_PATH="C:/Users/USER/.workbuddy-ai/binaries/node/workspace/node_modules"
node tests/verify_frontend.js http://127.0.0.1:10000 ./shots
```

33 assertions across load, theme toggle, the sample-notes button, a live
optimizer run (which calls the configured LLM, so it needs `GROQ_API_KEY`), the
chart geometry, the JSON invariants, and the 390px layout. Exit code is non-zero
if anything fails. Screenshots land in the out-dir.

If the responsive check fails it prints the offending elements with their
geometry, so the regression is actionable rather than a bare pixel count.

## `capture_request.js` — debugging a bad request

```bash
node tests/capture_request.js http://127.0.0.1:10000
```

Intercepts the outgoing `POST /optimize-energy` and prints the body the page
actually sent plus the response. Use this when the UI reports an error: it shows
the real payload rather than a reconstruction, so a bug in `buildRequest()`
cannot hide behind a stub.
