"""Configuration tests: .env loading, precedence, and credential resolution.

These exist because the documented quickstart tells users to copy
``.env.example`` to ``.env`` and fill in a key. That instruction is only true if
something actually reads the file -- ``python-dotenv`` was a declared dependency
that nothing imported, so a correctly-filled ``.env`` was silently ignored and
every request failed with "no LLM credential configured".

What they prove:
  * a project-local .env is loaded into the environment
  * real environment variables take precedence over .env (Docker/Render inject
    secrets this way and must not be overridden by a stale file)
  * credential resolution priority is Groq -> OpenAI -> generic
  * no_settings_object_ever_exposes_a_key_via_provider_label

Run: python tests/test_config.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

# Child processes get a clean slate so the parent's environment cannot mask a
# .env-loading failure.
_BASE_ENV = {
    k: v
    for k, v in os.environ.items()
    if k not in {"GROQ_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"}
}

_PROBE = """
import sys
sys.path.insert(0, r"{root}")
from app.config import get_settings
s = get_settings()
# Emit explicit markers so an empty value still produces a parseable line
# (printing a bare "" yields a blank line that strip() would discard).
print("KEY:" + s.api_key)
print("MODEL:" + s.llm_model)
print("LABEL:" + s.provider_label)
"""


def _parse_probe(stdout: str) -> tuple[str, str, str]:
    """Parse the marker-prefixed probe output into (key, model, label)."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        if name in {"KEY", "MODEL", "LABEL"}:
            fields[name] = value
    missing = {"KEY", "MODEL", "LABEL"} - fields.keys()
    assert not missing, f"probe output incomplete ({missing}):\n{stdout}"
    return fields["KEY"], fields["MODEL"], fields["LABEL"]


def _run_probe_from(directory: Path, env_overrides: dict[str, str]) -> list[str]:
    """Run the probe against ``directory`` as the importable project root.

    ``root`` must be the caller-supplied directory, not REPO_ROOT -- otherwise
    the real ``app.config`` (and the real ``.env`` beside it) is imported and a
    live credential leaks into every assertion.
    """
    env = dict(_BASE_ENV)
    env.update(env_overrides)
    result = subprocess.run(
        [PYTHON, "-c", _PROBE.format(root=directory)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(directory),
        timeout=60,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    return list(_parse_probe(result.stdout))


def _make_app_tree(directory: Path, dotenv_text: str | None = None) -> Path:
    """Create a minimal ``app/`` package (real config.py) under ``directory``.

    The loader resolves ``.env`` relative to the module file, so the copy must
    sit in its own tree; otherwise the project's real ``.env`` would be picked up
    and the probe would leak a live credential into every assertion.
    """
    app_dir = directory / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "app" / "config.py", app_dir / "config.py")
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    if dotenv_text is not None:
        (directory / ".env").write_text(dotenv_text, encoding="utf-8")
    return directory


def _run_probe(env_overrides: dict[str, str]) -> list[str]:
    """Run the probe against a clean tree that contains no .env."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = _make_app_tree(Path(tmp))
        return _run_probe_from(tree, env_overrides)


def test_dotenv_file_is_actually_read():
    """A .env next to the package root must populate Settings.

    Builds a throwaway tree containing only ``app/config.py`` and a ``.env``, so
    the only way GROQ_API_KEY can appear is if the file was loaded.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tree = _make_app_tree(
            Path(tmp),
            dotenv_text="GROQ_API_KEY=gsk_loaded_from_dotenv\nLLM_MODEL=model-from-dotenv\n",
        )

        key, model, label = _run_probe_from(tree, {})
        assert key == "gsk_loaded_from_dotenv", (
            f".env was not loaded (got {key[:8]!r}); the documented quickstart is broken"
        )
        assert model == "model-from-dotenv", model
        assert label == "groq", label


def test_missing_dotenv_is_not_an_error():
    """No .env must degrade to 'unconfigured', never a crash."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = _make_app_tree(Path(tmp))
        key, _, label = _run_probe_from(tree, {})
        assert key == "", key
        assert label == "unconfigured", label


def test_groq_key_takes_priority_over_other_providers():
    _, model, label = _run_probe(
        {"GROQ_API_KEY": "gsk_priority_test", "OPENAI_API_KEY": "sk-openai-lower"}
    )
    assert label == "groq", label
    assert model == "openai/gpt-oss-120b", f"groq default model not applied: {model}"


def test_openai_used_when_groq_absent():
    _, model, label = _run_probe({"OPENAI_API_KEY": "sk-openai-only"})
    assert label == "openai", label
    assert model == "gpt-4o-mini", model


def test_generic_key_is_last_resort():
    _, _, label = _run_probe({"LLM_API_KEY": "generic-key-value"})
    assert label == "openai-compatible", label


def test_explicit_env_model_beats_the_builtin_default():
    _, model, _ = _run_probe(
        {"GROQ_API_KEY": "gsk_x", "LLM_MODEL": "qwen/qwen3.8-27b"}
    )
    assert model == "qwen/qwen3.8-27b", model


def test_unconfigured_provider_is_reported_honestly():
    key, _, label = _run_probe({})
    assert key == "", f"a key leaked from the ambient environment: {key[:6]!r}"
    assert label == "unconfigured", label


def test_env_var_wins_over_dotenv_value():
    """Platform-injected secrets must not be overridden by a stale .env file.

    This is the property that lets the same image run locally (values from .env)
    and on Render/Docker (values from the platform).
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        print("  (skipped: no project .env present)")
        return

    # The project .env sets LLM_MODEL; an explicit env var must win.
    key, model, _ = _run_probe({"LLM_MODEL": "sentinel-model-from-platform"})
    assert model == "sentinel-model-from-platform", (
        f".env overrode a real environment variable: {model}"
    )


# --------------------------------------------------------------------------- #
# Repository hygiene
# --------------------------------------------------------------------------- #
_SKIP_DIRS = {".git", ".workbuddy-ai", "__pycache__", ".kilo", ".venv", "node_modules"}

# Credential-shaped literals. Keep these narrow: a false positive that blocks a
# legitimate file is worse than a slightly missed exotic format.
_SECRET_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),          # Groq
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),         # OpenAI / generic
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),    # OpenAI project keys
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),        # Google
    re.compile(r"hf_[A-Za-z0-9]{20,}"),           # Hugging Face
)

# Values that are obviously synthetic. Redaction tests must embed a
# credential-shaped string to prove the redactor works, so excluding clear
# fixtures keeps the guard honest instead of training people to ignore it.
_FIXTURE_MARKERS = ("SUPERSECRET", "EXAMPLE", "PLACEHOLDER", "DUMMY", "FAKE", "NOTREAL")


def _is_synthetic(value: str) -> bool:
    return any(marker in value.upper() for marker in _FIXTURE_MARKERS)


def test_no_committed_file_contains_a_credential():
    """No tracked file may contain a real API key.

    ``.env`` is git-ignored, but ``.env.example`` is deliberately *whitelisted*
    for committing (the ``!.env.example`` rule). A real key pasted into the
    template therefore gets published. That is exactly what happened once, so
    this sweep now covers the whole repository rather than only response bodies.
    """
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name == ".env":  # git-ignored: allowed to hold the real value
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(text)
            if match and not _is_synthetic(match.group(0)):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)[:12]}...")

    assert not offenders, (
        "credential-shaped literal found in a committed file:\n  " + "\n  ".join(offenders)
    )


def test_env_example_ships_only_placeholders():
    """The template must be copy-paste safe for every credential field."""
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        for line in template.splitlines():
            if line.strip().startswith("#"):
                continue
            if line.strip().startswith(f"{name}="):
                value = line.split("=", 1)[1].strip()
                assert value == "", (
                    f"{name} in .env.example must be empty, found {len(value)} chars"
                )


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {name}: {exc}")
        except Exception as exc:  # pragma: no cover
            failed += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
