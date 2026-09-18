"""Application settings.

Everything is environment-driven so the same image runs locally, in Docker, and
on Render. No secret is ever defaulted to a real value or logged.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _load_dotenv_once() -> None:
    """Load a project-local ``.env`` into ``os.environ`` if one exists.

    Uses ``override=False`` semantics on purpose: a real environment variable
    always wins over the file. That is what makes the same image behave
    correctly locally (values come from ``.env``) and in Docker/Render (values
    are injected by the platform and the file is absent or ignored).

    ``python-dotenv`` is a declared dependency, but this import is guarded so a
    missing optional install degrades to "no .env support" rather than a crash.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared, defensive only
        return
    # override=False -> existing os.environ entries (Docker/Render) take priority.
    load_dotenv(env_path, override=False)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_settings() -> "Settings":
    return Settings()


# Populate os.environ from .env when Settings first reads configuration, so the
# documented "copy .env.example to .env" quickstart actually works. Real
# platform environment variables always take precedence (override=False).
_load_dotenv_once()


class Settings:
    """Runtime configuration.

    LLM provider selection is deliberately small and explicit: OpenAI-compatible
    first (which also covers Groq, Together, OpenRouter, and local vLLM/Ollama
    endpoints), then an optional deterministic fallback for degraded operation.
    """

    def __init__(self) -> None:
        # --- Provider credentials -------------------------------------------------
        self.openai_api_key = _env("OPENAI_API_KEY")
        self.groq_api_key = _env("GROQ_API_KEY")
        self.llm_api_key = _env("LLM_API_KEY")

        # --- Endpoint / model ------------------------------------------------------
        # Defaults point at Groq's OpenAI-compatible API because it is fast and free
        # tier friendly; any OpenAI-compatible base URL works.
        self.llm_base_url = _env("LLM_BASE_URL") or _env("OPENAI_BASE_URL") or "https://api.groq.com/openai/v1"
        # NOTE: Groq retires models regularly. `llama-3.3-70b-versatile` is no
        # longer served (the endpoint returns 404 model_not_found), so the Groq
        # default is gpt-oss-120b. Always confirm with:
        #   curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"
        self.llm_model = (
            _env("LLM_MODEL")
            or _env("OPENAI_MODEL")
            or ("openai/gpt-oss-120b" if self.groq_api_key else "gpt-4o-mini")
        )

        # --- Behaviour -------------------------------------------------------------
        self.llm_timeout_seconds = _env_float("LLM_TIMEOUT_SECONDS", 12.0)
        self.llm_max_retries = _env_int("LLM_MAX_RETRIES", 2)
        self.request_deadline_seconds = _env_float("REQUEST_DEADLINE_SECONDS", 25.0)
        self.llm_temperature = _env_float("LLM_TEMPERATURE", 0.0)
        self.llm_max_tokens = _env_int("LLM_MAX_TOKENS", 1600)
        self.log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()

        # The mandatory LLM stage must never be silently replaced by regex, so the
        # deterministic fallback is opt-in and documented as a degraded mode.
        self.allow_deterministic_fallback = (
            _env("ALLOW_DETERMINISTIC_FALLBACK", "false").lower() in {"1", "true", "yes", "on"}
        )

        self.app_version = _env("APP_VERSION", "1.0.0")
        self.port = _env_int("PORT", 8000)

    # --- Derived helpers ---------------------------------------------------------
    @property
    def api_key(self) -> str:
        """Resolve the credential in priority order; empty string means unset."""
        return self.groq_api_key or self.openai_api_key or self.llm_api_key

    @property
    def provider_label(self) -> str:
        """Provider name for /health and logs. Never includes the key itself."""
        if self.groq_api_key:
            return "groq"
        if self.openai_api_key:
            return "openai"
        if self.llm_api_key:
            return "openai-compatible"
        return "unconfigured"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.api_key)


def redact(text: str, settings: "Settings") -> str:
    """Remove any credential-shaped substrings from a string before logging.

    Belt-and-braces: a provider error message can echo the request headers back.
    """
    if not text:
        return text
    out = text
    for secret in {settings.api_key, settings.openai_api_key, settings.groq_api_key, settings.llm_api_key}:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    return out
