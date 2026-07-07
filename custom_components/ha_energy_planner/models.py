"""Typed data contracts for Energy Planner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OccupancyState(StrEnum):
    """Known occupancy states."""

    OCCUPIED = "occupied"
    AWAY = "away"
    UNKNOWN = "unknown"


class HAEOStatus(StrEnum):
    """HAEO health in the current context."""

    READY = "ready"
    STALE = "stale"
    FAILED = "failed"


class InputHealth(StrEnum):
    """Input health classification."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNSAFE = "unsafe"


class PlannerMode(StrEnum):
    """Global execution mode."""

    DISABLED = "DISABLED"
    DRY_RUN = "DRY_RUN"
    ACTIVE_HEALTHY = "ACTIVE_HEALTHY"
    ACTIVE_DEGRADED = "ACTIVE_DEGRADED"
    MANUAL_HVAC_OVERRIDE = "MANUAL_HVAC_OVERRIDE"
    FAILSAFE_RESTORE = "FAILSAFE_RESTORE"


class ActionAsset(StrEnum):
    """Controllable asset classes."""

    ENPHASE = "enphase"
    DAIKIN = "daikin"
    EV = "ev"


class ActionKind(StrEnum):
    """Supported plan action kinds."""

    SET_PROFILE = "set_profile"
    RESTORE_AI = "restore_ai"
    SET_HVAC = "set_hvac"
    EV_START = "ev_start"
    EV_STOP = "ev_stop"
    EV_SCHEDULE = "ev_schedule"


class OutcomeResult(StrEnum):
    """Execution result values."""

    APPLIED = "applied"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
    RESTORED = "restored"


class HAEOSolvePhase(StrEnum):
    """HAEO solve phases."""

    BASELINE = "baseline"
    FLEXIBLE_LOAD = "flexible_load"


@dataclass(slots=True)
class ForecastPoint:
    """Normalized forecast point."""

    issued_at: datetime
    valid_at: datetime
    source: str
    value: float
    unit: str
    confidence: float | None
    fresh_until: datetime


@dataclass(slots=True)
class Override:
    """Planner override state."""

    kind: str
    source: str
    expires_at: datetime | None
    reason: str


@dataclass(slots=True)
class DecisionSlot:
    """Five-minute decision slot."""

    valid_at: datetime
    import_price: float | None
    export_price: float | None
    pv_forecast_kw: float | None
    baseline_load_forecast_kw: float | None
    projected_ev_load_kw: float = 0.0
    projected_hvac_load_kw: float = 0.0
    outdoor_temperature_forecast_c: float | None = None
    occupied: bool | None = None
    haeo_battery_soc_forecast_percent: float | None = None
    haeo_grid_import_forecast_kw: float | None = None
    haeo_grid_export_forecast_kw: float | None = None
    haeo_battery_charge_forecast_kw: float | None = None
    haeo_battery_discharge_forecast_kw: float | None = None


@dataclass(slots=True)
class DecisionContext:
    """Planner input context for a rolling horizon."""

    created_at: datetime
    plan_id: str
    slots: list[DecisionSlot]
    current_battery_soc_percent: float | None
    current_ev_soc_percent: float | None
    occupancy_state: OccupancyState
    haeo_status: HAEOStatus
    input_health: InputHealth
    current_enphase_profile: str | None = None
    enphase_ai_profile: str | None = None
    enphase_arbitrage_profile: str | None = None
    enphase_self_consumption_profile: str | None = None
    enphase_full_backup_profile: str | None = None
    current_hvac_mode: str | None = None
    current_hvac_temperature_c: float | None = None
    current_hvac_power_kw: float | None = None
    current_outdoor_temperature_c: float | None = None
    ev_connected: bool | None = None
    ev_target_soc_percent: float | None = None
    ev_ready_by: str | None = None
    ev_trip_observed_days: int = 0
    ev_trip_max_daily_soc_percent: float = 0.0
    ev_trip_average_daily_soc_percent: float = 0.0
    ev_trip_history_sufficient: bool = False
    occupied_temperature_low_c: float | None = None
    occupied_temperature_high_c: float | None = None
    active_overrides: list[Override] = field(default_factory=list)
    input_issues: list[str] = field(default_factory=list)
    forecast_confidence: float = 1.0


@dataclass(slots=True)
class PlanAction:
    """A planned action eligible for future execution."""

    action_id: str
    plan_id: str
    execute_not_before: datetime
    execute_not_after: datetime
    asset: ActionAsset
    kind: ActionKind
    desired_state: dict[str, Any]
    hard_constraints: list[str]
    reason_codes: list[str]
    expected_cost_delta: float | None
    confidence: float
    requires_haeo_plan_id: str | None


@dataclass(slots=True)
class ActionOutcome:
    """Compact execution/audit result."""

    action_id: str
    attempted_at: datetime
    result: OutcomeResult
    reason: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    plan_id: str
    asset: str | None = None
    kind: str | None = None
    service_target: str | None = None


@dataclass(slots=True)
class ConstraintViolation:
    """A hard-constraint violation detected during planning or execution."""

    code: str
    message: str
    asset: ActionAsset | None = None
    action_id: str | None = None
    blocking: bool = True


@dataclass(slots=True)
class FlexibleLoadProjection:
    """Projected flexible-load contribution for one decision slot."""

    valid_at: datetime
    ev_load_kw: float = 0.0
    hvac_load_kw: float = 0.0


@dataclass(slots=True)
class HAEOSolveResult:
    """Result of invoking or checking HAEO."""

    phase: HAEOSolvePhase
    status: HAEOStatus
    reason: str
    plan_id: str
    service_called: str | None = None
    response: dict[str, Any] | None = None


@dataclass(slots=True)
class EnergyPlan:
    """Current deterministic energy plan."""

    plan_id: str
    created_at: datetime
    horizon_hours: int
    interval_minutes: int
    status: str
    health: InputHealth
    mode: PlannerMode
    summary: str
    confidence: float
    estimated_daily_cost: float | None
    actions: list[PlanAction]
    preview: list[dict[str, Any]]
    input_issues: list[str] = field(default_factory=list)
    device_plans: dict[str, Any] = field(default_factory=dict)
    decision_audit: dict[str, Any] = field(default_factory=dict)
    rejected_actions: list[dict[str, Any]] = field(default_factory=list)
    timeline_card: list[dict[str, Any]] = field(default_factory=list)
    confidence_breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def next_action(self) -> PlanAction | None:
        """Return the earliest planned action."""
        if not self.actions:
            return None
        return min(self.actions, key=lambda action: action.execute_not_before)


def to_jsonable(value: Any) -> Any:
    """Convert dataclass and datetime values to Store-friendly structures."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    return value
