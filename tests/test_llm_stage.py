"""Unit tests for the LLM interpretation stage, using a stub provider.

These run without any API key. They cover the parts of the pipeline that are easy
to get subtly wrong and expensive to discover during judging:

  * defensive JSON extraction (fences, prose around the object, trailing commas)
  * the retry loop recovering from a malformed first answer
  * the repair pass filling a missing note and correcting an off-by-one end hour
  * rejection of unsupported directive types
  * secret redaction in provider error messages

Run directly (``python tests/test_llm_stage.py``) or with pytest if installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.llm_interpreter import (  # noqa: E402
    InterpretationError,
    LLMInterpreter,
    extract_json_payload,
    repair_interpretation,
    RepairReport,
)


def _settings(**overrides) -> Settings:
    settings = Settings()
    settings.llm_api_key = "test-key-not-a-real-secret"
    settings.groq_api_key = ""
    settings.openai_api_key = ""
    settings.llm_max_retries = 2
    settings.request_deadline_seconds = 10.0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


# --------------------------------------------------------------------------- #
def test_extract_plain_object():
    payload = extract_json_payload('{"directives": [{"note_index": 0}]}')
    assert payload["directives"][0]["note_index"] == 0


def test_extract_from_markdown_fence():
    raw = 'Here you go:\n```json\n{"directives": [{"note_index": 1}]}\n```\nDone.'
    payload = extract_json_payload(raw)
    assert payload["directives"][0]["note_index"] == 1


def test_extract_from_bare_array_with_prose():
    raw = 'The answer is [{"note_index": 0, "directive_type": "no_op"}] as requested.'
    payload = extract_json_payload(raw)
    assert isinstance(payload, list) and payload[0]["directive_type"] == "no_op"


def test_extract_repairs_trailing_comma():
    payload = extract_json_payload('{"directives": [{"note_index": 0},]}')
    assert payload["directives"][0]["note_index"] == 0


def test_extract_rejects_empty():
    for raw in ("", "   ", "I cannot help with that."):
        try:
            extract_json_payload(raw)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {raw!r}")


# --------------------------------------------------------------------------- #
def test_repair_fills_missing_note_with_no_op():
    notes = ["Reduce solar by 20% from 1 PM to 3 PM.", "Unrelated note."]
    entries = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
            "explanation": "Solar reduced.",
        }
    ]
    report_calls = RepairReport()
    repaired = repair_interpretation(entries, notes, 200.0, report_calls)
    assert len(repaired) == 2
    assert repaired[0]["directive_type"] == "solar_reduction"
    assert repaired[1]["directive_type"] == "no_op"
    assert repaired[1]["applies"] is False
    assert repaired[1]["structured_adjustment"] is None
    assert any("no entry" in note for note in report_calls.repairs)


def test_repair_normalises_hours_order_and_duplicates():
    notes = ["Do not charge the battery between 2 PM and 4 PM."]
    entries = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [15, 14, 15]},
            "explanation": "No charging.",
        }
    ]
    repaired = repair_interpretation(entries, notes, 200.0, RepairReport())
    assert repaired[0]["structured_adjustment"]["hours"] == [14, 15]


def test_repair_retypes_when_assigned_type_has_no_support_in_note():
    # The model wrongly labelled a reserve note as a grid cap.
    notes = ["Keep at least 120 kWh in reserve from 6 PM until 9 PM."]
    entries = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [18, 19, 20], "max_grid_kwh": 120},
            "explanation": "mislabeled",
        }
    ]
    repaired = repair_interpretation(entries, notes, 200.0, RepairReport())
    assert repaired[0]["directive_type"] == "minimum_battery_reserve"
    assert repaired[0]["structured_adjustment"]["minimum_energy_kwh"] == 120
    assert "max_grid_kwh" not in repaired[0]["structured_adjustment"]


def test_repair_degrades_to_no_op_when_no_hours_recoverable():
    notes = ["Solar will be reduced at some point today."]
    entries = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [], "factor": 0.5},
            "explanation": "vague",
        }
    ]
    report_calls = RepairReport()
    repaired = repair_interpretation(entries, notes, 200.0, report_calls)
    assert repaired[0]["directive_type"] == "no_op"
    assert any("no hours" in note for note in report_calls.repairs)


def test_repair_recovers_percentage_reserve_in_kwh():
    notes = ["Keep at least 50% of the battery capacity stored from 6 PM until 9 PM."]
    entries = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18, 19, 20]},
            "explanation": "percent given, kWh missing",
        }
    ]
    repaired = repair_interpretation(entries, notes, 200.0, RepairReport())
    assert repaired[0]["structured_adjustment"]["minimum_energy_kwh"] == 100.0


# --------------------------------------------------------------------------- #
class _StubInterpreter(LLMInterpreter):
    """Provider stub: returns queued responses instead of calling the network."""

    def __init__(self, settings: Settings, responses: list[str | Exception]):
        super().__init__(settings)
        self._responses = list(responses)
        self.calls = 0

    def _post(self, payload, timeout):  # type: ignore[override]
        self.calls += 1
        if not self._responses:
            raise AssertionError("stub exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_interpreter_recovers_after_malformed_first_answer():
    valid = json.dumps(
        {
            "directives": [
                {
                    "note_index": 0,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "irrelevant",
                }
            ]
        }
    )
    stub = _StubInterpreter(_settings(), ["not json at all", valid])
    directives, _, meta = stub.interpret(["The cafeteria menu changes tomorrow."], 200.0)
    assert stub.calls == 2
    assert meta["attempts"] == 2
    assert directives[0].directive_type == "no_op"


def test_interpreter_raises_after_exhausting_retries():
    stub = _StubInterpreter(_settings(), ["nope", "still nope", "nope again"])
    try:
        stub.interpret(["Solar reduced 1 PM to 3 PM."], 200.0)
    except InterpretationError as exc:
        assert "failed after" in str(exc)
    else:
        raise AssertionError("expected InterpretationError")


def test_interpreter_rejects_unsupported_directive_type():
    # An invented type must never survive into the optimizer.
    bogus = json.dumps(
        {
            "directives": [
                {
                    "note_index": 0,
                    "applies": True,
                    "directive_type": "curtail_demand",
                    "structured_adjustment": {"hours": [10]},
                    "explanation": "invented",
                }
            ]
        }
    )
    stub = _StubInterpreter(_settings(), [bogus, bogus, bogus])
    try:
        stub.interpret(["Something happens at 10."], 200.0)
    except InterpretationError:
        pass
    else:
        raise AssertionError("unsupported directive type should not be accepted")


def test_provider_error_message_is_redacted():
    secret = "sk-live-SUPERSECRETVALUE1234567890"
    settings = _settings()
    settings.llm_api_key = secret
    settings.groq_api_key = ""
    settings.openai_api_key = ""
    # Every attempt fails with a message that echoes the key, so the redaction
    # must hold on each hop of the retry loop.
    error = InterpretationError(f"provider returned HTTP 401: bad key {secret}")
    stub = _StubInterpreter(settings, [error, error, error])
    try:
        stub.interpret(["Some note about solar."], 200.0)
    except InterpretationError as exc:
        assert secret not in str(exc), "secret leaked into the raised error"
        assert "[redacted]" in str(exc)
    else:
        raise AssertionError("expected InterpretationError")


def _run_all() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
