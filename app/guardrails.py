"""Deterministic guardrails: validate interpretation, fold it into constraints.

Everything here is pure and CPU-only. Per Problem Statement Section 08 the LLM
output is untrusted structured data until this module has accepted it, and the
optimizer only ever sees the ``EffectiveConstraints`` produced here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from app.models import DirectiveInterpretation, OptimizationRequest

logger = logging.getLogger(__name__)

HOURS = list(range(24))
ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


class GuardrailError(ValueError):
    """Raised when an interpretation cannot be trusted."""


# --------------------------------------------------------------------------- #
# Validation (Problem Statement s08)
# --------------------------------------------------------------------------- #
def validate_interpretation(
    notes: list[str], directives: list[DirectiveInterpretation]
) -> list[DirectiveInterpretation]:
    """Independently re-check the interpretation before it reaches the optimizer.

    Pydantic already enforces shape. This layer re-asserts the cross-entry rules
    that a single-field validator cannot see: note-to-directive coverage and
    exactly-once mapping.
    """
    if not directives:
        raise GuardrailError("directive_interpretation is empty")
    if len(directives) != len(notes):
        raise GuardrailError(
            f"expected exactly one interpretation per note ({len(notes)}), got {len(directives)}"
        )

    seen: set[int] = set()
    for position, directive in enumerate(directives):
        if directive.directive_type not in ALLOWED_DIRECTIVE_TYPES:
            raise GuardrailError(
                f"entry {position}: unsupported directive_type {directive.directive_type!r}"
            )
        if directive.note_index != position:
            raise GuardrailError(
                f"entry {position}: note_index is {directive.note_index}; entries must be "
                "returned in note_index order 0..N-1"
            )
        if directive.note_index in seen:
            raise GuardrailError(f"entry {position}: duplicate note_index {directive.note_index}")
        if not 0 <= directive.note_index < len(notes):
            raise GuardrailError(
                f"entry {position}: note_index {directive.note_index} does not identify an "
                "existing operator note"
            )
        seen.add(directive.note_index)

        if directive.directive_type == "no_op":
            if directive.applies is not False or directive.structured_adjustment is not None:
                raise GuardrailError(
                    f"entry {position}: no_op requires applies=false and null adjustment"
                )
        elif directive.applies is not True:
            raise GuardrailError(
                f"entry {position}: {directive.directive_type} requires applies=true"
            )

    if seen != set(range(len(notes))):
        missing = sorted(set(range(len(notes))) - seen)
        raise GuardrailError(f"notes without an interpretation entry: {missing}")

    return directives


# --------------------------------------------------------------------------- #
# Effective constraints (Problem Statement s05.3)
# --------------------------------------------------------------------------- #
@dataclass
class EffectiveConstraints:
    """Per-hour limits the LP must respect after every directive is applied."""

    solar: dict[int, float]
    reserve: dict[int, float]
    no_charge: set[int] = field(default_factory=set)
    no_discharge: set[int] = field(default_factory=set)
    grid_cap: dict[int, float] = field(default_factory=dict)
    applied: list[dict] = field(default_factory=list)


def build_effective_constraints(
    request: OptimizationRequest, directives: list[DirectiveInterpretation]
) -> EffectiveConstraints:
    """Fold directives into concrete per-hour numbers (solar fold, reserves, caps)."""
    base_solar = {entry.hour: float(entry.solar_kwh) for entry in request.hours}
    base_minimum = float(request.battery.minimum_energy_kwh)

    constraints = EffectiveConstraints(
        solar=dict(base_solar),
        reserve={hour: base_minimum for hour in HOURS},
    )

    for directive in directives:
        if not directive.applies:
            continue
        adjustment = directive.structured_adjustment or {}
        affected = list(adjustment.get("hours", [])) if isinstance(adjustment, dict) else []
        kind = directive.directive_type

        if kind == "solar_reduction":
            factor = float(adjustment["factor"])
            for hour in affected:
                constraints.solar[hour] = constraints.solar[hour] * factor
        elif kind == "minimum_battery_reserve":
            level = float(adjustment["minimum_energy_kwh"])
            if level > float(request.battery.capacity_kwh) + 1e-9:
                # Guardrail from s08: a reserve above capacity makes the hour
                # infeasible. Clamp rather than emit an unsolvable model.
                logger.warning(
                    "reserve %.3f exceeds capacity %.3f; clamping for hours %s",
                    level,
                    request.battery.capacity_kwh,
                    affected,
                )
                level = float(request.battery.capacity_kwh)
            for hour in affected:
                constraints.reserve[hour] = max(constraints.reserve[hour], level)
        elif kind == "no_charge_window":
            constraints.no_charge.update(affected)
        elif kind == "no_discharge_window":
            constraints.no_discharge.update(affected)
        elif kind == "max_grid_window":
            cap = float(adjustment["max_grid_kwh"])
            if not math.isfinite(cap) or cap < 0:
                raise GuardrailError(f"max_grid_kwh {cap} is not finite and non-negative")
            for hour in affected:
                current = constraints.grid_cap.get(hour)
                constraints.grid_cap[hour] = cap if current is None else min(current, cap)

        if kind != "no_op":
            constraints.applied.append(
                {
                    "note_index": directive.note_index,
                    "directive_type": kind,
                    "hours": affected,
                    "structured_adjustment": directive.structured_adjustment,
                }
            )

    # A no_charge and no_discharge window on the same hour only forbids battery
    # movement in that hour; it is not a contradiction (the battery idles).
    return constraints


def feasibility_warnings(
    request: OptimizationRequest, constraints: EffectiveConstraints
) -> list[str]:
    """Cheap, non-fatal diagnostics surfaced in logs (never in the API response)."""
    warnings: list[str] = []
    battery = request.battery
    for hour in HOURS:
        reserve = constraints.reserve[hour]
        if reserve > float(battery.capacity_kwh) + 1e-9:
            warnings.append(f"h{hour}: reserve {reserve} above capacity")
        demand = float(request.hours[hour].demand_kwh)
        reachable = (
            constraints.solar[hour]
            + float(battery.max_discharge_kwh_per_hour)
            + (constraints.grid_cap.get(hour, math.inf))
        )
        if demand > reachable + 1e-9:
            warnings.append(
                f"h{hour}: demand {demand} may exceed solar + discharge + grid cap "
                f"({constraints.grid_cap.get(hour, 'uncapped')})"
            )
    return warnings
