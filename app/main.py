"""FastAPI application: GET /health and POST /optimize-energy.

Error handling is deliberate and matches Problem Statement s6.1:

  400  malformed JSON or structurally invalid request
  422  well-formed but semantically invalid request
  500  controlled internal error, never a stack trace or secret

Reserved keys (``error``, ``detail``, ``message``) are filtered out of any
``operator_notes`` payload before it is logged or shown, so a caller cannot use a
note to smuggle instructions into a response field.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings, get_settings, redact
from app.guardrails import (
    GuardrailError,
    build_effective_constraints,
    feasibility_warnings,
    validate_interpretation,
)
from app.llm_interpreter import InterpretationError, LLMInterpreter
from app.models import (
    DirectiveInterpretation,
    HealthResponse,
    OptimizationRequest,
    OptimizationResponse,
)
from app.optimizer import OptimizationError, optimize
from app.summary import build_plan_summary

logger = logging.getLogger("gridwise")

MAX_BODY_BYTES = 1_000_000
RESERVED_KEYS = {"error", "detail", "message", "traceback", "status"}

settings: Settings = get_settings()
app = FastAPI(
    title="GridWise — Smart Campus Energy Optimization API",
    description=(
        "LLM-assisted operator-note interpretation plus 24-hour cost-minimising "
        "campus energy scheduling."
    ),
    version=settings.app_version,
)


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized bodies early; a 4-hour judging window should never see an OOM."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=400,
                    content={"error": "request body too large"},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid content-length header"})
    return await call_next(request)


@app.on_event("startup")
async def log_startup() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Log configuration shape, never values.
    logger.info(
        "gridwise starting: version=%s provider=%s model=%s llm_configured=%s "
        "fallback_enabled=%s configured_port=%s",
        settings.app_version,
        settings.provider_label,
        settings.llm_model,
        settings.llm_enabled,
        settings.allow_deterministic_fallback,
        settings.port,
    )
    if not settings.llm_enabled:
        logger.warning(
            "no LLM credential configured; /optimize-energy will return 500 unless "
            "ALLOW_DETERMINISTIC_FALLBACK=true"
        )


# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #
def _sanitise_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape validation errors without echoing request values back verbatim."""
    cleaned: list[dict[str, Any]] = []
    for error in errors[:25]:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        cleaned.append(
            {
                "field": location or "body",
                "type": str(error.get("type", "invalid")),
                "message": str(error.get("msg", "invalid value"))[:300],
            }
        )
    return cleaned


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Structurally valid JSON that violates the schema -> 422."""
    return JSONResponse(status_code=422, content={"errors": _sanitise_errors(exc.errors())})


@app.exception_handler(GuardrailError)
async def guardrail_exception_handler(request: Request, exc: GuardrailError):
    """Interpretation that cannot be trusted -> 422, controlled message only."""
    logger.warning("guardrail rejection: %s", redact(str(exc), settings))
    return JSONResponse(
        status_code=422,
        content={"error": "directive interpretation failed deterministic validation", "detail": str(exc)[:300]},
    )


class SemanticValidationError(ValueError):
    """Request was shaped correctly but contradicts itself."""


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler: a stable JSON error, never a stack trace."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal server error"})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Readiness probe for the judging harness (s6.2)."""
    return HealthResponse(status="ok")


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "gridwise-energy-optimization",
        "health": "/health",
        "optimize": "POST /optimize-energy",
        "docs": "/docs",
    }


def _sanitise_notes(notes: list[str]) -> list[str]:
    """Strip reserved response keys from note text before it is logged."""
    sanitised: list[str] = []
    for note in notes:
        lowered = note.lower()
        if any(f'"{key}"' in lowered or f"'{key}'" in lowered for key in RESERVED_KEYS):
            sanitised.append("[note withheld: contained response-reserved keys]")
        else:
            sanitised.append(note[:500])
    return sanitised


def optimize_energy_core(request: OptimizationRequest) -> OptimizationResponse:
    """Full pipeline: interpret -> guardrail -> optimize -> summarise.

    Split out from the route so tests can call it in-process and so the error
    taxonomy stays in one place.
    """
    started = time.perf_counter()
    constraints = None
    interpretation: list[DirectiveInterpretation] = []
    fallback_used = False

    interpreter = LLMInterpreter(settings)
    try:
        interpretation, report, meta = interpreter.interpret(
            request.operator_notes, float(request.battery.capacity_kwh)
        )
        logger.info(
            "interpretation ok: scenario=%s attempts=%s latency_ms=%s repairs=%s",
            request.scenario_id,
            meta.get("attempts"),
            meta.get("total_latency_ms"),
            meta.get("repairs") or [],
        )
    except InterpretationError as exc:
        safe_message = redact(str(exc), settings)
        if settings.allow_deterministic_fallback:
            logger.error("LLM unavailable (%s); using deterministic fallback", safe_message)
            from app.fallback import deterministic_interpretation

            interpretation = deterministic_interpretation(request)
            fallback_used = True
        else:
            logger.error("LLM interpretation failed: %s", safe_message)
            raise SemanticValidationError(
                "the language model could not produce a valid interpretation for the "
                "operator notes; retry the request"
            ) from exc

    # --- Deterministic guardrails (s08) -------------------------------------
    interpretation = validate_interpretation(request.operator_notes, interpretation)
    constraints = build_effective_constraints(request, interpretation)

    for warning in feasibility_warnings(request, constraints):
        logger.warning("feasibility: %s", warning)

    # --- Optimize (s05.2, s09) ----------------------------------------------
    try:
        result = optimize(request, constraints)
    except OptimizationError as exc:
        logger.error("optimization failed for %s: %s", request.scenario_id, exc)
        raise SemanticValidationError(
            "no valid schedule satisfies the scenario and its operator directives"
        ) from exc

    plan_summary = build_plan_summary(
        request=request,
        interpretation=interpretation,
        constraints=constraints,
        total_cost_bdt=result.total_cost_bdt,
        total_grid_kwh=result.total_grid_kwh,
        peak_grid_kwh=result.peak_grid_kwh,
        fallback_used=fallback_used,
    )

    response = OptimizationResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=interpretation,
        hourly_plan=result.plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary=plan_summary,
    )

    logger.info(
        "optimize ok: scenario=%s cost=%.2f grid=%.2f peak=%.2f elapsed_ms=%.1f",
        request.scenario_id,
        result.total_cost_bdt,
        result.total_grid_kwh,
        result.peak_grid_kwh,
        (time.perf_counter() - started) * 1000,
    )
    return response


def _sample_body() -> dict[str, Any]:
    """A complete, valid scenario used to pre-fill Swagger UI and the README curl.

    Doubles as living documentation of the exact request shape in Problem
    Statement s07, so the two cannot drift apart.
    """
    return {
        "scenario_id": "GRID-DEMO-001",
        "operator_notes": [
            "Solar output will drop to about 20% from 1 PM to 3 PM.",
            "Do not charge the battery between 2 PM and 4 PM.",
            "The cafeteria menu changes tomorrow.",
        ],
        "hours": [
            {
                "hour": hour,
                "demand_kwh": 180 + 20 * (hour % 5),
                "solar_kwh": 0 if hour < 6 or hour > 18 else 60 + 10 * ((hour - 6) % 6),
                "tariff_bdt_per_kwh": 7 + (hour % 4),
            }
            for hour in range(24)
        ],
        "battery": {
            "capacity_kwh": 500,
            "initial_energy_kwh": 200,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        },
    }


@app.post(
    "/optimize-energy",
    response_model=OptimizationResponse,
    openapi_extra={
        # The handler reads the raw body itself (so malformed JSON maps to 400
        # instead of a framework 422 traceback). That makes the body invisible to
        # FastAPI's automatic schema inference, which is why Swagger UI would
        # otherwise show "No parameters" with no input box. Declaring the body
        # here documents it for Swagger and any generated client while leaving
        # the runtime parsing path untouched.
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": OptimizationRequest.model_json_schema(),
                    "example": _sample_body(),
                }
            },
        }
    },
)
async def optimize_energy(request: Request) -> JSONResponse:
    """Accept one scenario and return the interpretation plus the 24-hour plan."""
    # Read the body ourselves so malformed JSON maps to 400 rather than a 422
    # traceback from the framework's own parser.
    raw = await request.body()
    if not raw:
        return JSONResponse(status_code=400, content={"error": "request body is empty"})

    try:
        import json

        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "request body is not valid JSON"})

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "request body must be a single JSON object"},
        )

    try:
        parsed = OptimizationRequest(**payload)
    except ValidationError as exc:
        # Semantically invalid but well-formed -> 422; a missing field or a bad
        # type is structural -> 400.
        errors = exc.errors()
        structural = any(
            str(error.get("type", "")).startswith(("missing", "model_attributes", "json_invalid", "string_type", "int_type", "float_type", "list_type", "dict_type"))
            for error in errors
        )
        status = 400 if structural else 422
        return JSONResponse(status_code=status, content={"errors": _sanitise_errors(errors)})

    logger.info(
        "optimize request: scenario=%s notes=%s",
        parsed.scenario_id,
        _sanitise_notes(parsed.operator_notes),
    )

    try:
        response = optimize_energy_core(parsed)
    except SemanticValidationError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)[:300]})
    except GuardrailError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "directive interpretation failed deterministic validation",
                "detail": str(exc)[:300],
            },
        )
    except Exception:
        logger.exception("optimize-energy failed for scenario=%s", parsed.scenario_id)
        return JSONResponse(status_code=500, content={"error": "internal server error"})

    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(settings.port),
        log_level=settings.log_level.lower(),
    )
