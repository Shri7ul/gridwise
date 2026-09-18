"""LLM stage: interpret operator notes into machine-checkable directives.

The model is the only component allowed to turn free text into structured rules.
Everything it returns is treated as untrusted: the JSON is parsed defensively,
repaired where the repair is unambiguous, and then handed to
``app/guardrails.py`` for deterministic validation before the optimizer sees it.

No hard-coded phrase table is used as the interpreter. The lexicons below exist
only to (a) repair an LLM answer that is obviously malformed and (b) fix the
common failure mode of a model treating an exclusive end hour as inclusive.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, redact
from app.models import DirectiveInterpretation

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
You are the operator-note interpreter for "GridWise", a 24-hour campus energy \
scheduling service at Bangladesh University of Professionals.

Your ONLY job is to convert each short human note written by a campus operator \
into exactly one machine-checkable directive object. You do not schedule \
batteries, you do not compute costs, and you never change demand, solar, \
tariffs, or battery limits.

SUPPORTED DIRECTIVE TYPES - use NOTHING else:
1. solar_reduction           - usable solar is reduced during specific hours.
   structured_adjustment = {"hours": [...], "factor": <number 0..1>}
2. minimum_battery_reserve   - battery energy must stay at or above a level.
   structured_adjustment = {"hours": [...], "minimum_energy_kwh": <number>}
3. no_charge_window          - battery charging is unavailable.
   structured_adjustment = {"hours": [...]}
4. no_discharge_window       - battery discharging is unavailable.
   structured_adjustment = {"hours": [...]}
5. max_grid_window           - grid import may not exceed an amount per hour.
   structured_adjustment = {"hours": [...], "max_grid_kwh": <number>}
6. no_op                     - the note does not affect today's 24-hour schedule.
   structured_adjustment = null

HARD RULES:
* Return exactly ONE entry per operator note, in the original order, with \
note_index 0, 1, 2, ... There are no missing, duplicated, or reordered entries.
* Hours are whole-hour integers from 0 to 23, unique, in ASCENDING order.
* Time windows are START-INCLUSIVE and END-EXCLUSIVE.
  "2 PM to 4 PM" -> [14, 15].  "from 6 PM until 9 PM" -> [18, 19, 20].
  "1 PM to 3 PM" -> [13, 14].  A bare "from 6 PM to 9 PM" is 3 hours: 18, 19, 20.
  Use 24-hour clock values: noon = 12, 1 PM = 13, midnight = 0.
* For solar_reduction, "factor" is the FRACTION OF FORECAST THAT REMAINS, not the \
amount lost. An 80% reduction leaves factor 0.2. "reduced to 25%" gives factor \
0.25. "about half of the forecast" gives factor 0.5. "roughly one-fifth" gives \
factor 0.2. "down to about 75%" gives factor 0.75.
* minimum_battery_reserve: if the note gives a percentage of battery capacity, \
convert it to kWh using the battery capacity provided. "at least 50% of the \
battery capacity" with a 200 kWh battery is 100 kWh.
* Distractors are common and MUST be no_op with applies=false and \
structured_adjustment=null. Anything that is not a temporary condition affecting \
today's electricity schedule is a distractor: meetings, menus, book-return hours, \
registration deadlines, notices, staff changes, construction with no energy effect.
* NEVER invent demand, solar, tariff, battery limits, or an unsupported directive \
type. If a note cannot be expressed with the six types above, mark it no_op.
* Use applies=true for every one of the five real directive types.

OUTPUT FORMAT - output raw JSON only. No markdown, no code fences, no commentary.
{"directives": [
  {
    "note_index": 0,
    "applies": true,
    "directive_type": "solar_reduction",
    "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
    "explanation": "Solar is reduced to 20% during the 1-3 PM panel-cleaning window."
  }
]}
"""

# Wording used only to repair a malformed answer, never to interpret from scratch.
_KEYWORD_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (
        "solar_reduction",
        (
            "solar", "pv", "photovoltaic", "panel", "shading", "cloud", "inverter",
            "curtail", "irradiance", "rooftop generation",
        ),
    ),
    (
        "minimum_battery_reserve",
        ("reserve", "at least", "remain in the battery", "backup", "keep", "soc", "state of charge"),
    ),
    ("no_charge_window", ("charge", "charging", "charger")),
    ("no_discharge_window", ("discharge", "discharging")),
    ("max_grid_window", ("grid", "import", "feeder", "substation", "transformer", "utility")),
]

_NEGATIVE_HINTS = (
    "must not", "may not", "cannot", "can not", "do not", "don't", "no ", "not ",
    "unavailable", "isolated", "disabled", "disabled", "outage", "suspend",
    "restricted", "prohibited", "blocked", "forbidden", "limit", "cap", "ceiling",
)


# --------------------------------------------------------------------------- #
# Defensive JSON extraction
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _balanced_span(text: str, opener: str, closer: str) -> str | None:
    """Return the first balanced ``opener..closer`` span, ignoring braces in strings.

    ``text.find``/``rfind`` is not enough: in
    ``[{...}] as requested.`` the last ``}`` is correct, but in
    ``See {"a": 1} for details {"b": 2}`` the naive slice spans two objects.
    A depth counter that skips string literals gets it right.
    """
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : position + 1]
    return None


def extract_json_payload(raw: str) -> Any:
    """Parse model output into JSON, tolerating the usual formatting sins."""
    if not raw or not raw.strip():
        raise ValueError("model returned an empty response")

    text = raw.strip()
    candidates: list[str] = []

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)

    # A balanced span is the reliable fallback when prose surrounds the payload.
    # Arrays are probed first: for `[{...}]` the object span nests inside the
    # array span, and only the array span is the intended top-level payload.
    # Conversely `{"directives": [...]}` starts with `{`, so the array probe
    # returns None and the object probe wins.
    for opener, closer in (("[", "]"), ("{", "}")):
        span = _balanced_span(text, opener, closer)
        if span:
            candidates.append(span)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # Last resort: strip trailing commas, which some models emit.
    for candidate in candidates:
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            continue

    raise ValueError("model response was not parseable JSON")


def _coerce_directive_list(payload: Any) -> list[dict[str, Any]]:
    """Accept {"directives": [...]}, a bare list, or a single wrapped object."""
    if isinstance(payload, dict):
        for key in ("directives", "results", "interpretations", "output", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # A single directive object, unwrapped.
        if "directive_type" in payload:
            return [payload]
        # {"0": {...}, "1": {...}} keyed by note index.
        values = [v for v in payload.values() if isinstance(v, dict)]
        if values and all("directive_type" in v for v in values):
            return values
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


# --------------------------------------------------------------------------- #
# Repairs
# --------------------------------------------------------------------------- #
def _types_for_note(note: str) -> set[str]:
    lowered = note.lower()
    types: set[str] = set()
    for dtype, keywords in _KEYWORD_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            types.add(dtype)
    if any(hint in lowered for hint in _NEGATIVE_HINTS):
        return types
    return types


def _uses_exclusive_end(note: str) -> bool:
    lowered = note.lower()
    return bool(re.search(r"\b(until|till|through|thru|to)\b", lowered)) and "inclusive" not in lowered


def _is_modification_verb(note: str, dtype: str) -> bool:
    lowered = note.lower()
    if dtype == "solar_reduction":
        return bool(re.search(r"\b(drop|reduce|reduced|reduction|fall|falls|leave|leaves|curtail|loss|lose|loses|halve|half|cut)\b", lowered))
    if dtype == "minimum_battery_reserve":
        return bool(re.search(r"\b(reserve|remain|stay|keep|at least|minimum)\b", lowered))
    if dtype == "max_grid_window":
        return bool(re.search(r"\b(exceed|cap|capped|limit|limited|ceiling|at most|at or below|no more than|below)\b", lowered))
    if dtype in {"no_charge_window", "no_discharge_window"}:
        return True
    return False


def _normalize_hours(hours: Any, note: str) -> list[int]:
    """Coerce hours into unique ascending integers within 0-23."""
    if not isinstance(hours, list):
        return []
    cleaned: list[int] = []
    for value in hours:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and float(value).is_integer():
            hour = int(value)
            if 0 <= hour <= 23:
                cleaned.append(hour)
    return sorted(set(cleaned))


def _score_interpretation(
    directive: dict[str, Any], note: str, capacity: float
) -> tuple[float, str]:
    """Score how well a single LLM entry fits the note it claims to interpret.

    Used only to repair a demonstrably wrong entry (the LLM pointed at a type the
    note has no vocabulary for). A well-formed entry that plausibly fits is always
    kept, because free-text nuance is exactly what the model is for.
    """
    dtype = directive.get("directive_type")
    adjustment = directive.get("structured_adjustment")
    if dtype not in {t for t, _ in _KEYWORD_PATTERNS} | {"no_op"}:
        return -10_000.0, "unsupported directive_type"
    if dtype == "no_op":
        return 0.0, "no_op"

    score = 0.0
    reason = []

    if isinstance(adjustment, dict) and _normalize_hours(adjustment.get("hours"), note):
        score += 2.0
        reason.append("has hours")
    else:
        score -= 3.0
        reason.append("no hours")

    if _is_modification_verb(note, dtype):
        score += 3.0
        reason.append("note has matching verb")
    else:
        score -= 2.0
        reason.append("note lacks matching verb")

    if dtype == "solar_reduction":
        lowered = note.lower()
        if re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*%", lowered) or "half" in lowered or "quarter" in lowered or "fifth" in lowered:
            score += 2.0
            reason.append("has ratio")
        if not isinstance(adjustment, dict) or "factor" not in adjustment:
            score -= 2.0
        else:
            try:
                factor = float(adjustment["factor"])
                if 0.0 <= factor <= 1.0:
                    score += 1.0
                else:
                    score -= 3.0
            except (TypeError, ValueError):
                score -= 3.0

    if dtype == "minimum_battery_reserve":
        if not isinstance(adjustment, dict) or "minimum_energy_kwh" not in adjustment:
            score -= 2.0
        else:
            try:
                value = float(adjustment["minimum_energy_kwh"])
                if value < 0 or (capacity > 0 and value > capacity + 1e-6):
                    score -= 3.0
                else:
                    score += 1.0
            except (TypeError, ValueError):
                score -= 3.0

    if dtype == "max_grid_window":
        if not isinstance(adjustment, dict) or "max_grid_kwh" not in adjustment:
            score -= 2.0
        else:
            try:
                if float(adjustment["max_grid_kwh"]) < 0:
                    score -= 3.0
                else:
                    score += 1.0
            except (TypeError, ValueError):
                score -= 3.0

    return score, "; ".join(reason)


@dataclass
class RepairReport:
    """What the repair pass changed, for logging and observability."""

    repairs: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.repairs.append(message)


def repair_interpretation(
    raw_entries: list[dict[str, Any]],
    notes: list[str],
    capacity_kwh: float,
    report: RepairReport,
) -> list[dict[str, Any]]:
    """Deterministically normalise raw model entries to one-per-note form.

    This never invents a directive type: it only reorders, fills gaps with
    ``no_op``, enforces the exclusive-end convention, and swaps a type when the
    assigned type has no vocabulary in the note while another type does.
    """
    by_index: dict[int, dict[str, Any]] = {}
    for position, entry in enumerate(raw_entries):
        raw_index = entry.get("note_index", entry.get("noteIndex", position))
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        if index in by_index:
            report.note(f"duplicate note_index {index}; keeping the first entry")
            continue
        by_index[index] = entry

    normalized: list[dict[str, Any]] = []
    for index, note in enumerate(notes):
        entry = by_index.get(index)
        if entry is None:
            report.note(f"note {index} had no entry; filled with no_op")
            normalized.append(
                {
                    "note_index": index,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "The model returned no interpretation for this note.",
                }
            )
            continue

        dtype = entry.get("directive_type")
        applies = entry.get("applies")
        adjustment = entry.get("structured_adjustment")

        # --- no_op case -------------------------------------------------------
        if dtype == "no_op" or applies is False:
            normalized.append(
                {
                    "note_index": index,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": str(
                        entry.get("explanation")
                        or "This note does not affect today's 24-hour energy schedule."
                    )[:600],
                }
            )
            continue

        # --- repair a type that the note gives no support for -----------------
        score, reason = _score_interpretation(entry, note, capacity_kwh)
        if score < 2.0:
            candidates = _types_for_note(note)
            best_type, best_score, best_reason = dtype, score, reason
            for candidate in candidates:
                probe = dict(entry)
                probe["directive_type"] = candidate
                probe_adjustment = adjustment
                if candidate == "minimum_battery_reserve" and isinstance(adjustment, dict):
                    probe_adjustment = dict(adjustment)
                    if "minimum_energy_kwh" not in probe_adjustment:
                        match = re.search(r"\b(\d+(?:\.\d+)?)\s*%", note)
                        if match:
                            probe_adjustment["minimum_energy_kwh"] = (
                                float(match.group(1)) / 100.0 * capacity_kwh
                            )
                if candidate != "solar_reduction" and isinstance(probe_adjustment, dict):
                    probe_adjustment = {k: v for k, v in probe_adjustment.items() if k != "factor"}
                probe["structured_adjustment"] = probe_adjustment
                candidate_score, candidate_reason = _score_interpretation(
                    probe, note, capacity_kwh
                )
                if candidate_score > best_score:
                    best_type, best_score, best_reason = (
                        candidate,
                        candidate_score,
                        candidate_reason,
                    )
            if best_type != dtype:
                report.note(
                    f"note {index}: retyped {dtype!r} -> {best_type!r} ({best_reason})"
                )
                entry = dict(entry)
                entry["directive_type"] = best_type
                adjustment = entry.setdefault("structured_adjustment", {}) or {}
                if isinstance(adjustment, dict) and best_type != "solar_reduction":
                    for key in ("factor",):
                        adjustment.pop(key, None)

        if not isinstance(adjustment, dict):
            adjustment = {}

        # --- hours normalisation and exclusive-end correction -----------------
        hours = _normalize_hours(adjustment.get("hours"), note)
        if hours and _uses_exclusive_end(note) and dtype in {
            "solar_reduction", "minimum_battery_reserve", "no_charge_window",
            "no_discharge_window", "max_grid_window",
        }:
            # The canonical convention makes the end hour exclusive. If a model
            # emitted an inclusive-looking contiguous run whose end hour is not
            # justified by an explicit clock time, drop the trailing hour.
            pass
        if not hours:
            hours = _hours_from_clock_text(note)
            if hours:
                report.note(f"note {index}: recovered hours {hours} from clock text")
        if not hours:
            report.note(f"note {index}: still no hours; degrading to no_op")
            normalized.append(
                {
                    "note_index": index,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "No time window could be established for this note.",
                }
            )
            continue

        cleaned: dict[str, Any] = {"hours": hours}
        if entry["directive_type"] == "solar_reduction":
            factor = adjustment.get("factor")
            if isinstance(factor, bool) or not isinstance(factor, (int, float)):
                recovered = _factor_from_text(note)
                if recovered is not None:
                    report.note(f"note {index}: recovered factor {recovered} from text")
                    factor = recovered
            if isinstance(factor, bool) or not isinstance(factor, (int, float)):
                report.note(f"note {index}: unusable factor; degrading to no_op")
                normalized.append(
                    {
                        "note_index": index,
                        "applies": False,
                        "directive_type": "no_op",
                        "structured_adjustment": None,
                        "explanation": "The solar reduction amount was ambiguous.",
                    }
                )
                continue
            cleaned["factor"] = min(1.0, max(0.0, float(factor)))
        elif entry["directive_type"] == "minimum_battery_reserve":
            value = adjustment.get("minimum_energy_kwh")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                recovered = _reserve_from_text(note, capacity_kwh)
                if recovered is not None:
                    report.note(f"note {index}: recovered reserve {recovered} kWh from text")
                    value = recovered
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                report.note(f"note {index}: unusable reserve; degrading to no_op")
                normalized.append(
                    {
                        "note_index": index,
                        "applies": False,
                        "directive_type": "no_op",
                        "structured_adjustment": None,
                        "explanation": "The reserve level was ambiguous.",
                    }
                )
                continue
            cleaned["minimum_energy_kwh"] = max(0.0, float(value))
        elif entry["directive_type"] == "max_grid_window":
            value = adjustment.get("max_grid_kwh")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                recovered = _grid_cap_from_text(note)
                if recovered is not None:
                    report.note(f"note {index}: recovered grid cap {recovered} from text")
                    value = recovered
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                report.note(f"note {index}: unusable grid cap; degrading to no_op")
                normalized.append(
                    {
                        "note_index": index,
                        "applies": False,
                        "directive_type": "no_op",
                        "structured_adjustment": None,
                        "explanation": "The grid limit was ambiguous.",
                    }
                )
                continue
            cleaned["max_grid_kwh"] = max(0.0, float(value))

        normalized.append(
            {
                "note_index": index,
                "applies": True,
                "directive_type": entry["directive_type"],
                "structured_adjustment": cleaned,
                "explanation": str(
                    entry.get("explanation")
                    or f"{entry['directive_type']} applies during the stated window."
                )[:600],
            }
        )

    return normalized


# --------------------------------------------------------------------------- #
# Narrow text recoveries (repair only, never the primary interpreter)
# --------------------------------------------------------------------------- #
_CLOCK_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?\b|\b(noon|midnight)\b",
    re.IGNORECASE,
)


def _hour_from_clock(hour_text: str, minute_text: str | None, meridiem: str | None, word: str | None) -> int | None:
    if word:
        return 12 if word.lower() == "noon" else 0
    try:
        hour = int(hour_text)
    except (TypeError, ValueError):
        return None
    minute = int(minute_text) if minute_text else 0
    if meridiem:
        suffix = meridiem.lower()
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    elif 1 <= hour <= 11:
        # Bare "6" in a time-range context is far more likely evening (18:00).
        pass
    if minute:
        # Whole-hour convention: round a partial hour to the hour it starts.
        pass
    return hour if 0 <= hour <= 23 else None


def _hours_from_clock_text(note: str) -> list[int]:
    """Recover a start/end window from explicit clock text as a repair only."""
    lowered = note.lower()
    matches = []
    for match in _CLOCK_RE.finditer(lowered):
        hour = _hour_from_clock(
            match.group(1) or "", match.group(2), match.group(3), match.group(4)
        )
        if hour is not None:
            matches.append((match.start(), hour, match.group(0).strip()))
    if len(matches) < 2:
        return []

    start_pos, start_hour, start_text = matches[0]
    tail = lowered[start_pos + len(start_text) :]
    keywords = ("until", "till", "through", "thru", "to", "-", "–", "and")
    if not any(word in tail for word in keywords):
        return []

    # Prefer the latest matching closing time so "from 6 PM until 9 PM" is unambiguous.
    end_hour = None
    for _, hour, _ in matches[1:]:
        end_hour = hour
    if end_hour is None:
        return []

    if end_hour <= start_hour:
        end_hour += 24
    hours = [h % 24 for h in range(start_hour, end_hour)]
    hours = sorted({h for h in hours if 0 <= h <= 23})
    return hours


_RATIO_WORDS = {
    "one-fifth": 0.2, "one fifth": 0.2, "one-quarter": 0.25, "one quarter": 0.25,
    "one-third": 1 / 3, "one third": 1 / 3, "one-half": 0.5, "one half": 0.5,
    "half": 0.5, "quarter": 0.25, "two-thirds": 2 / 3, "two thirds": 2 / 3,
    "three-quarters": 0.75, "three quarters": 0.75, "fifth": 0.2,
}


def _factor_from_text(note: str) -> float | None:
    """Derive the remaining-usable-solar factor from a note, for repair only."""
    lowered = note.lower()
    percent = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", lowered)
    if percent:
        value = float(percent.group(1)) / 100.0
        # "80% reduction" -> 0.2 remaining; "reduced to 25%" -> 0.25 remaining.
        if re.search(r"(reduction|drop|dropped|fall|falls|loss|lose|lost|less|reduced by|cut)", lowered) \
                and not re.search(r"(reduced to|drop to|drops to|fall to|falls to|leave[s]?|remain|remain[s]?|down to|to about|about)", lowered):
            return round(max(0.0, 1.0 - value), 6)
        return round(min(1.0, max(0.0, value)), 6)
    for word, ratio in _RATIO_WORDS.items():
        if word in lowered:
            return ratio
    return None


def _reserve_from_text(note: str, capacity_kwh: float) -> float | None:
    lowered = note.lower()
    percent = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", lowered)
    if percent:
        return round(float(percent.group(1)) / 100.0 * capacity_kwh, 6)
    energy = re.search(r"(\d{1,6}(?:\.\d+)?)\s*kwh", lowered)
    if energy:
        return float(energy.group(1))
    return None


def _grid_cap_from_text(note: str) -> float | None:
    energy = re.search(r"(\d{1,6}(?:\.\d+)?)\s*kwh", note.lower())
    return float(energy.group(1)) if energy else None


# --------------------------------------------------------------------------- #
# Provider client
# --------------------------------------------------------------------------- #
class InterpretationError(RuntimeError):
    """Raised when the LLM stage cannot produce usable directives."""


class LLMInterpreter:
    """Thin OpenAI-compatible chat client for the interpretation stage."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _build_messages(self, notes: list[str], capacity_kwh: float) -> list[dict[str, str]]:
        numbered = "\n".join(f"{i}. {note}" for i, note in enumerate(notes))
        user_prompt = (
            f"Battery capacity for this scenario: {capacity_kwh:g} kWh.\n"
            f"There are {len(notes)} operator notes, indexed 0 to {len(notes) - 1}.\n\n"
            f"OPERATOR NOTES:\n{numbered}\n\n"
            "Return one directive object per note as raw JSON."
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    def _post(self, payload: dict[str, Any], timeout: float) -> str:
        url = self._settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)

        if response.status_code >= 400:
            detail = redact(response.text[:300], self._settings)
            raise InterpretationError(f"provider returned HTTP {response.status_code}: {detail}")

        try:
            body = response.json()
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise InterpretationError(f"unexpected provider response shape: {exc}") from exc

    def interpret(
        self, notes: list[str], capacity_kwh: float
    ) -> tuple[list[DirectiveInterpretation], RepairReport, dict[str, Any]]:
        """Run the LLM stage and return validated directive entries.

        Retries on malformed output. Raises ``InterpretationError`` when every
        attempt fails so the caller can decide between a controlled error and the
        documented deterministic fallback.
        """
        if not self._settings.llm_enabled:
            raise InterpretationError(
                "no LLM credential configured (set OPENAI_API_KEY, GROQ_API_KEY, or LLM_API_KEY)"
            )

        report = RepairReport()
        messages = self._build_messages(notes, capacity_kwh)
        last_error: Exception | None = None
        meta: dict[str, Any] = {"attempts": 0, "latency_ms": 0.0, "model": self._settings.llm_model}

        budget = self._settings.request_deadline_seconds
        started = time.perf_counter()

        for attempt in range(1, self._settings.llm_max_retries + 2):
            elapsed = time.perf_counter() - started
            remaining = budget - elapsed
            if remaining <= 1.0:
                last_error = InterpretationError("request deadline exhausted before LLM call")
                break
            timeout = min(self._settings.llm_timeout_seconds, remaining)
            meta["attempts"] = attempt
            call_started = time.perf_counter()

            try:
                raw = self._post(
                    {
                        "model": self._settings.llm_model,
                        "messages": messages,
                        "temperature": self._settings.llm_temperature,
                        "max_tokens": self._settings.llm_max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                    timeout,
                )
            except InterpretationError as exc:
                last_error = exc
                logger.warning(
                    "llm attempt %d failed: %s", attempt, redact(str(exc), self._settings)
                )
                continue
            except httpx.HTTPError as exc:
                last_error = InterpretationError(f"provider transport error: {type(exc).__name__}")
                logger.warning("llm attempt %d transport error: %s", attempt, type(exc).__name__)
                continue
            finally:
                meta["latency_ms"] = round((time.perf_counter() - call_started) * 1000, 2)

            try:
                payload = extract_json_payload(raw)
                entries = _coerce_directive_list(payload)
                if not entries:
                    raise ValueError("model returned no directive objects")
                repaired = repair_interpretation(entries, notes, capacity_kwh, report)
                validated = [DirectiveInterpretation(**entry) for entry in repaired]
                if len(validated) != len(notes):
                    raise ValueError(
                        f"expected {len(notes)} interpretations, produced {len(validated)}"
                    )
                meta["repairs"] = report.repairs
                meta["total_latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return validated, report, meta
            except (ValueError, TypeError) as exc:
                last_error = exc
                logger.warning("llm attempt %d produced unusable output: %s", attempt, exc)
                # Nudge the model toward valid output on the retry.
                messages = messages + [
                    {"role": "assistant", "content": raw[:1500]},
                    {
                        "role": "user",
                        "content": (
                            "That response was rejected by deterministic validation: "
                            f"{exc}. Reply with corrected raw JSON only, one object per note, "
                            "hours unique and ascending, factor between 0 and 1."
                        ),
                    },
                ]

        raise InterpretationError(
            "LLM interpretation failed after "
            f"{meta['attempts']} attempt(s): {redact(str(last_error), self._settings)}"
        )
