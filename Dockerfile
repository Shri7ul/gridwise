# GridWise — Smart Campus Energy Optimization API
#
# Multi-stage so the final image ships no build tooling and no credentials.
# Binds 0.0.0.0 and honours $PORT so it runs unchanged on Render, Fly, Cloud Run,
# or a local `docker run`.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

# Runtime shared libraries for the PuLP/CBC solver binary and the healthcheck.
#
# The `cbc` executable shipped inside the pulp wheel (pulp/solverdir/cbc/linux/
# i64/cbc) declares these DT_NEEDED entries: libc, libm, libpthread, librt,
# libdl (all glibc, always present) plus libstdc++.so.6 and libgcc_s.so.1.
# Neither of the last two ships in python:3.12-slim -- that image installs only
# ca-certificates, netbase and tzdata, then purges its build toolchain.
#
# So libstdc++6 is the package that actually matters here; it pulls in
# libgcc-s1 as a dependency. (libgomp1 is NOT needed: CBC has no OpenMP
# dependency. Verify with: strings <cbc> | grep -E '^lib(stdc\+\+|gomp)'.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libstdc++6 curl \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY tests ./tests
COPY pdf ./pdf
COPY requirements.txt README.md ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 gridwise \
 && chown -R gridwise:gridwise /app
USER gridwise

EXPOSE 8000

# No secrets are baked in: GROQ_API_KEY / OPENAI_API_KEY / LLM_API_KEY are
# provided at run time via `-e` or the platform's environment settings.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Shell form so ${PORT} expands at container start.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
