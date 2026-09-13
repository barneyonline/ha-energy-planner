"""EV policy defaults and validated physical charger capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import floor, isfinite
from typing import Any

EV_INPUTS = ("ev_power_entity", "ev_energy_entity", "ev_power_limit_entity")
EV_DEFAULTS = {
    "ev_charging_strategy": "legacy",
    "ev_readiness_buffer_minutes": 30,
    "ev_schedule_min_saving": 0.5,
    "ev_schedule_min_saving_percent": 5,
    "ev_min_dwell_minutes": 15,
    "ev_price_policy": "hard_ceiling",
    "ev_emergency_price": 0.0,
    "ev_emergency_budget": 0.0,
    "ev_limit_min": 0.0,
    "ev_limit_max": 0.0,
    "ev_voltage": 250.0,
    "ev_phases": 1,
}


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if isfinite(number) else None


def strategy(options: Mapping[str, Any]) -> str:
    value = options.get("ev_charging_strategy", "legacy")
    if value in {"continuous", "split", "adaptive"}:
        return str(value)
    return "continuous" if options.get("ev_continuous_charging", True) else "split"


@dataclass(frozen=True, slots=True)
class PowerCapability:
    """Number entity limits, expressed in native units and conservative kW."""

    entity_id: str
    unit: str
    minimum: float
    maximum: float
    step: float
    origin: float
    kw_per_unit: float
    current: float

    def setpoint(self, headroom_kw: float) -> float | None:
        limit = min(self.maximum, headroom_kw / self.kw_per_unit)
        value = self.origin + floor((limit - self.origin + 1e-9) / self.step) * self.step
        return round(value, 6) if value >= self.minimum - 1e-9 and value > 0 else None

    def power(self, headroom_kw: float) -> float:
        value = self.setpoint(headroom_kw)
        return 0.0 if value is None else value * self.kw_per_unit


def power_capability(state: Any, options: Mapping[str, Any]) -> PowerCapability | None:
    """Reject unsupported/ambiguous controls rather than guessing their limits."""
    if state is None or not str(state.entity_id).startswith("number."):
        return None
    attrs = state.attributes
    unit = attrs.get("unit_of_measurement")
    low, high, step, current = (
        finite(v)
        for v in (
            attrs.get("min"),
            attrs.get("max"),
            attrs.get("step"),
            state.state,
        )
    )
    configured_low = finite(options.get("ev_limit_min"))
    configured_high = finite(options.get("ev_limit_max"))
    if any(v is None for v in (low, high, step, current, configured_low, configured_high)):
        return None
    assert low is not None and high is not None and step is not None and current is not None
    assert configured_low is not None and configured_high is not None
    minimum, maximum = max(low, configured_low, step), min(high, configured_high)
    if step <= 0 or minimum > maximum or configured_high <= 0:
        return None
    factor = {"W": 0.001, "kW": 1.0}.get(unit)
    if unit == "A":
        voltage, phases = finite(options.get("ev_voltage")), finite(options.get("ev_phases"))
        if voltage is None or not 100 <= voltage <= 300 or phases not in {1, 3}:
            return None
        factor = voltage * phases / 1000
    if factor is None:
        return None
    return PowerCapability(state.entity_id, unit, minimum, maximum, step, low, factor, current)
