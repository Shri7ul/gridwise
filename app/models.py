"""Strict Pydantic models for the GridWise API contract.

Field names, types, and constraints are taken directly from Section 07 (request
schema) and Section 10 (response schema) of the Preliminary Problem Statement.
Anything the statement does not allow is rejected here rather than silently
coerced, so a malformed request fails as 400/422 and never reaches the optimizer.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Shared scalars
# --------------------------------------------------------------------------- #
# ``allow_inf_nan=False`` makes non-finite JSON (Infinity/NaN) a validation error
# instead of a value that quietly poisons the LP.
FiniteNonNegative = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
FinitePositive = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
HourIndex = Annotated[int, Field(ge=0, le=23)]

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]
BatteryAction = Literal["charge", "discharge", "idle"]


class _StrictModel(BaseModel):
    """Reject unknown keys so schema drift surfaces immediately."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Request (Problem Statement s07)
# --------------------------------------------------------------------------- #
class HourInput(_StrictModel):
    hour: HourIndex
    demand_kwh: FiniteNonNegative
    solar_kwh: FiniteNonNegative
    tariff_bdt_per_kwh: FiniteNonNegative


class BatteryInput(_StrictModel):
    capacity_kwh: FinitePositive
    initial_energy_kwh: FiniteNonNegative
    minimum_energy_kwh: FiniteNonNegative
    max_charge_kwh_per_hour: FiniteNonNegative
    max_discharge_kwh_per_hour: FiniteNonNegative

    @model_validator(mode="after")
    def _consistent(self) -> "BatteryInput":
        if self.initial_energy_kwh > self.capacity_kwh + 1e-9:
            raise ValueError("initial_energy_kwh must not exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh + 1e-9:
            raise ValueError("minimum_energy_kwh must not exceed capacity_kwh")
        if self.minimum_energy_kwh > self.initial_energy_kwh + 1e-9:
            # Not stated as illegal, but it makes hour 0 infeasible; the organizer
            # promises feasible scenarios, so flag it rather than return a 500.
            raise ValueError("minimum_energy_kwh must not exceed initial_energy_kwh")
        return self


class OptimizationRequest(_StrictModel):
    scenario_id: str = Field(min_length=1, max_length=200)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput] = Field(min_length=24, max_length=24)
    battery: BatteryInput

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, notes: list[str]) -> list[str]:
        cleaned = [note.strip() for note in notes]
        if any(not note for note in cleaned):
            raise ValueError("operator_notes entries must be non-empty strings")
        if any(len(note) > 2000 for note in cleaned):
            raise ValueError("each operator note must be at most 2000 characters")
        return cleaned

    @model_validator(mode="after")
    def _hours_are_exactly_0_to_23(self) -> "OptimizationRequest":
        seen = [entry.hour for entry in self.hours]
        if sorted(seen) != list(range(24)):
            missing = sorted(set(range(24)) - set(seen))
            duplicates = sorted({h for h in seen if seen.count(h) > 1})
            raise ValueError(
                "hours must contain each integer from 0 through 23 exactly once "
                f"(missing={missing[:6]}, duplicated={duplicates[:6]})"
            )
        return self


# --------------------------------------------------------------------------- #
# Directive interpretation (Problem Statement s04, s10.2)
# --------------------------------------------------------------------------- #
class SolarReductionAdjustment(_StrictModel):
    hours: list[HourIndex] = Field(min_length=1, max_length=24)
    factor: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class MinimumBatteryReserveAdjustment(_StrictModel):
    hours: list[HourIndex] = Field(min_length=1, max_length=24)
    minimum_energy_kwh: FiniteNonNegative


class HoursOnlyAdjustment(_StrictModel):
    hours: list[HourIndex] = Field(min_length=1, max_length=24)


class MaxGridAdjustment(_StrictModel):
    hours: list[HourIndex] = Field(min_length=1, max_length=24)
    max_grid_kwh: FiniteNonNegative


StructuredAdjustment = Union[
    SolarReductionAdjustment,
    MinimumBatteryReserveAdjustment,
    HoursOnlyAdjustment,
    MaxGridAdjustment,
]


def _validate_hours(hours: list[int]) -> list[int]:
    """Guardrail: unique integers 0-23 returned in ascending order (s05.1, s08)."""
    if not hours:
        raise ValueError("hours must be a non-empty list")
    if len(set(hours)) != len(hours):
        raise ValueError("hours must be unique")
    if list(hours) != sorted(hours):
        raise ValueError("hours must be sorted in ascending order")
    return hours


class DirectiveInterpretation(_StrictModel):
    """One machine-checkable interpretation entry per operator note.

    ``structured_adjustment`` is typed as ``Any`` on purpose: the shape is
    validated against the selected ``directive_type`` by the model validator
    below, which is stricter than a plain union (a union would accept e.g.
    ``{"hours": [...], "factor": 0.5}`` for ``no_charge_window``).
    """

    note_index: Annotated[int, Field(ge=0)]
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: object | None = None
    explanation: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def _shape_matches_type(self) -> "DirectiveInterpretation":
        adjustment = self.structured_adjustment

        if self.directive_type == "no_op":
            if self.applies:
                raise ValueError("no_op must use applies=false")
            if adjustment is not None:
                raise ValueError("no_op must use structured_adjustment=null")
            return self

        if not self.applies:
            raise ValueError(f"{self.directive_type} must use applies=true")
        if adjustment is None:
            raise ValueError(f"{self.directive_type} requires a structured_adjustment object")
        if not isinstance(adjustment, dict):
            raise ValueError("structured_adjustment must be a JSON object")

        expected_keys = {
            "solar_reduction": {"hours", "factor"},
            "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
            "no_charge_window": {"hours"},
            "no_discharge_window": {"hours"},
            "max_grid_window": {"hours", "max_grid_kwh"},
        }[self.directive_type]

        extra = set(adjustment) - expected_keys
        missing = expected_keys - set(adjustment)
        if extra or missing:
            raise ValueError(
                f"{self.directive_type} adjustment keys must be exactly "
                f"{sorted(expected_keys)} (missing={sorted(missing)}, extra={sorted(extra)})"
            )

        hours = adjustment["hours"]
        if not isinstance(hours, list) or any(
            isinstance(h, bool) or not isinstance(h, int) for h in hours
        ):
            raise ValueError("hours must be a list of integers")
        _validate_hours(hours)

        if self.directive_type == "solar_reduction":
            factor = adjustment["factor"]
            if isinstance(factor, bool) or not isinstance(factor, (int, float)):
                raise ValueError("factor must be numeric")
            if not 0.0 <= float(factor) <= 1.0:
                raise ValueError("factor must be between 0 and 1 inclusive")

        elif self.directive_type == "minimum_battery_reserve":
            value = adjustment["minimum_energy_kwh"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("minimum_energy_kwh must be numeric")
            if float(value) < 0:
                raise ValueError("minimum_energy_kwh must be non-negative")

        elif self.directive_type == "max_grid_window":
            value = adjustment["max_grid_kwh"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("max_grid_kwh must be numeric")
            if float(value) < 0:
                raise ValueError("max_grid_kwh must be non-negative")

        return self


# --------------------------------------------------------------------------- #
# Response (Problem Statement s10)
# --------------------------------------------------------------------------- #
class HourlyPlanEntry(_StrictModel):
    hour: HourIndex
    grid_kwh: FiniteNonNegative
    solar_used_kwh: FiniteNonNegative
    battery_action: BatteryAction
    battery_kwh: FiniteNonNegative
    battery_energy_after_kwh: FiniteNonNegative

    @model_validator(mode="after")
    def _idle_is_zero(self) -> "HourlyPlanEntry":
        if self.battery_action == "idle" and self.battery_kwh > 1e-9:
            raise ValueError("battery_kwh must be 0 when battery_action is idle")
        return self


class OptimizationResponse(_StrictModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry] = Field(min_length=24, max_length=24)
    total_grid_kwh: FiniteNonNegative
    total_cost_bdt: FiniteNonNegative
    peak_grid_kwh: FiniteNonNegative
    plan_summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _plan_covers_all_hours(self) -> "OptimizationResponse":
        if sorted(entry.hour for entry in self.hourly_plan) != list(range(24)):
            raise ValueError("hourly_plan must cover hours 0 through 23 exactly once")
        return self


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
