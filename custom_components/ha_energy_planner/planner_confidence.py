"""Pure per-asset confidence eligibility, independent of action generation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_AMBER_EXPORT_PRICE,
    CONF_AMBER_IMPORT_PRICE,
    CONF_HOUSEHOLD_LOAD,
    CONF_MIN_CLIMATE_CONFIDENCE,
    CONF_MIN_ENPHASE_CONFIDENCE,
    CONF_MIN_EV_CONFIDENCE,
    CONF_MIN_LOAD_CONFIDENCE,
    CONF_MIN_SOLAR_CONFIDENCE,
    CONF_MIN_TARIFF_CONFIDENCE,
    CONF_PV_FORECAST,
    CONF_WEATHER,
    DEFAULT_OPTIONS,
)
from .models import (
    ActionAsset,
    ActionKind,
    DecisionContext,
    EnergyPlan,
    InputHealth,
    PlanAction,
)
from .planner_values import finite_float as _finite_number


def confidence_from_health(input_health: InputHealth) -> float:
    """Return confidence scalar for input health."""
    if input_health == InputHealth.HEALTHY:
        return 1.0
    if input_health == InputHealth.DEGRADED:
        return 0.65
    return 0.0


def confidence_from_context(context: DecisionContext) -> float:
    """Return confidence capped by health and required forecast sources."""
    return _forecast_source_confidence(
        context,
        CONF_AMBER_IMPORT_PRICE,
        CONF_AMBER_EXPORT_PRICE,
        CONF_PV_FORECAST,
        CONF_HOUSEHOLD_LOAD,
    )


def _hvac_rollback_capability_unavailable(context: DecisionContext) -> bool:
    """Return whether HVAC takeover lacks a required rollback target."""
    return any(
        issue
        in {
            "main_climate_target_unavailable",
            "climate_zone_target_unavailable",
        }
        for issue in context.input_issues
    )


def _confidence_breakdown(context: DecisionContext, actions: list[PlanAction]) -> dict[str, Any]:
    """Return confidence by planning subsystem."""
    base = confidence_from_context(context)
    health = confidence_from_health(context.input_health)
    issue_text = " ".join(
        issue
        for issue in context.input_issues
        if not issue.startswith("advisory_") and issue != "household_load_model_fallback_active"
    )
    breakdown = {
        "overall": base,
        "tariff": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_AMBER_IMPORT_PRICE, CONF_AMBER_EXPORT_PRICE),
            issue_text,
            ("amber_", "price_"),
        ),
        "solar": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_PV_FORECAST),
            issue_text,
            ("pv_forecast", "solar"),
        ),
        "load": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_HOUSEHOLD_LOAD),
            issue_text,
            ("baseline_load", "household_load", "load_forecast"),
        ),
        "climate": _subsystem_confidence(
            _forecast_source_confidence(context, CONF_WEATHER),
            issue_text,
            ("daikin_", "climate_", "weather_"),
        ),
        "ev": _subsystem_confidence(health, issue_text, ("ev_",)),
        "enphase": _subsystem_confidence(health, issue_text, ("enphase_", "battery_soc")),
    }
    assets_with_actions = {str(action.asset) for action in actions}
    subsystem_breakdown = {key: value for key, value in breakdown.items() if key != "overall"}
    return {
        **breakdown,
        "action_assets": sorted(assets_with_actions),
        "limited_by": min(subsystem_breakdown, key=lambda key: subsystem_breakdown[key]),
    }


def _forecast_source_confidence(context: DecisionContext, *config_keys: str) -> float:
    """Return health-capped confidence for relevant configured forecast sources."""
    health = confidence_from_health(context.input_health)
    by_source = context.forecast_confidence_by_source
    if not by_source:
        return round(min(health, context.forecast_confidence), 4)
    relevant = [float(by_source[key]) for key in config_keys if key in by_source]
    return round(min([health, *relevant]), 4)


def _is_hvac_away_off_action(action: PlanAction) -> bool:
    """Return whether an action conservatively turns unoccupied HVAC off."""
    return bool(
        action.asset == ActionAsset.DAIKIN
        and action.kind == ActionKind.SET_HVAC
        and action.desired_state.get("hvac_mode") == "off"
        and "away_hvac_policy" in action.reason_codes
    )


def _subsystem_confidence(base: float, issue_text: str, issue_markers: tuple[str, ...]) -> float:
    """Return confidence reduced when a subsystem has matching input issues."""
    if any(marker in issue_text for marker in issue_markers):
        return round(min(base, 0.4), 4)
    return base


def _action_meets_confidence_threshold(
    action: PlanAction,
    context: DecisionContext,
    options: Mapping[str, Any],
) -> bool:
    """Return whether an action clears tariff and device confidence thresholds."""
    return asset_meets_confidence_threshold(action.asset, context, options)


def asset_meets_confidence_threshold(
    asset: ActionAsset,
    context: DecisionContext,
    options: Mapping[str, Any],
) -> bool:
    """Return whether an asset clears its relevant confidence thresholds."""
    breakdown = _confidence_breakdown(context, [])
    return _confidence_values_meet_threshold(asset, breakdown, options)


def plan_asset_meets_confidence_threshold(
    asset: ActionAsset,
    plan: EnergyPlan | Any,
    options: Mapping[str, Any],
) -> bool:
    """Return whether a current plan proves confidence eligibility for an asset."""
    breakdown = getattr(plan, "confidence_breakdown", None)
    if not isinstance(breakdown, Mapping):
        return False
    return _confidence_values_meet_threshold(asset, breakdown, options)


def confidence_eligible_control_areas(
    plan: EnergyPlan | Any,
    control_areas: list[str],
    options: Mapping[str, Any],
) -> list[str]:
    """Return control areas whose current plan clears asset confidence gates."""
    asset_by_area = {
        "ev": ActionAsset.EV,
        "hvac": ActionAsset.DAIKIN,
        "enphase": ActionAsset.ENPHASE,
    }
    return [
        area
        for area in control_areas
        if area in asset_by_area and plan_asset_meets_confidence_threshold(asset_by_area[area], plan, options)
    ]


def _confidence_values_meet_threshold(
    asset: ActionAsset,
    breakdown: Mapping[str, Any],
    options: Mapping[str, Any],
) -> bool:
    """Return whether confidence evidence clears every gate for an asset."""
    for key, option in _confidence_checks(asset):
        threshold = float(options.get(option, DEFAULT_OPTIONS.get(option, 0.0)) or 0.0) / 100.0
        actual = _finite_number(breakdown.get(key))
        if actual is None or actual < threshold:
            return False
    return True


def _confidence_checks(asset: ActionAsset) -> list[tuple[str, str]]:
    """Return confidence components and threshold settings for an asset."""
    checks = [("tariff", CONF_MIN_TARIFF_CONFIDENCE)]
    if asset == ActionAsset.DAIKIN:
        checks.extend([("climate", CONF_MIN_CLIMATE_CONFIDENCE), ("load", CONF_MIN_LOAD_CONFIDENCE)])
    elif asset == ActionAsset.EV:
        checks.extend([("ev", CONF_MIN_EV_CONFIDENCE), ("solar", CONF_MIN_SOLAR_CONFIDENCE)])
    elif asset == ActionAsset.ENPHASE:
        checks.extend([("enphase", CONF_MIN_ENPHASE_CONFIDENCE), ("solar", CONF_MIN_SOLAR_CONFIDENCE)])
    return checks


def _confidence_rejection_reason(
    asset: ActionAsset,
    context: DecisionContext,
    options: Mapping[str, Any],
) -> str | None:
    """Return a plain-English confidence rejection reason for an asset."""
    fake_action = PlanAction(
        action_id="confidence-check",
        plan_id=context.plan_id,
        execute_not_before=context.created_at,
        execute_not_after=context.created_at,
        asset=asset,
        kind=ActionKind.SET_HVAC if asset == ActionAsset.DAIKIN else ActionKind.EV_SCHEDULE,
        desired_state={},
        hard_constraints=[],
        reason_codes=[],
        expected_cost_delta=None,
        confidence=confidence_from_context(context),
    )
    if _action_meets_confidence_threshold(fake_action, context, options):
        return None
    breakdown = _confidence_breakdown(context, [])
    failures = []
    for key, option in _confidence_checks(asset):
        actual = float(breakdown.get(key, 0.0) or 0.0)
        threshold = float(options.get(option, 0.0) or 0.0) / 100.0
        if actual < threshold:
            failures.append(f"{key} {round(actual * 100, 1)}% (requires {round(threshold * 100, 1)}%)")
    return "Skipped because confidence is below the configured threshold: " + ", ".join(failures) + "."
