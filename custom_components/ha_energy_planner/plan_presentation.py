"""Pure plan presentation shared by sensors and calendars."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .models import ActionAsset, ActionKind, InputHealth, PlanAction, to_jsonable

if TYPE_CHECKING:
    from .coordinator import EnergyPlannerCoordinator


def plain_action(action: PlanAction) -> dict[str, Any]:
    """Return action metadata in a user-readable shape."""
    attrs = {
        "action": action_label(action),
        "decision": action_sentence(action),
        "when": action_window(action),
        "why": reason_summary(action.reason_codes),
        "constraints": [plain_reason(item) for item in action.hard_constraints[:8]],
        "desired_state": plain_state_details(action.desired_state),
        "estimated_value": action.expected_cost_delta,
    }
    return {key: value for key, value in attrs.items() if value not in (None, [], {})}


def plain_state_details(state: dict[str, Any]) -> dict[str, Any]:
    """Return readable details for a current, planned, or desired state."""
    details: dict[str, Any] = {}
    for key, value in state.items():
        if value is None:
            continue
        if key in {"reason_codes", "issues"} and isinstance(value, list):
            details[plain_key(key)] = [plain_reason(item) for item in value]
        elif key in {"state", "action", "hvac_mode", "arbitrage_direction", "arbitrage_source"}:
            details[plain_key(key)] = display_state(value)
        elif key in {
            "start",
            "end",
            "execute_not_before",
            "execute_not_after",
            "daylight_window_start_utc",
            "daylight_window_end_utc",
        }:
            details[plain_key(key)] = date_time_label(value) or value
        elif key == "daylight_lowest_cost" and isinstance(value, dict):
            details[plain_key(key)] = plain_daylight_evidence(value)
        elif key == "allocation_source_now":
            details[plain_key(key)] = plain_allocation_source(value)
        elif key == "allocated_slots" and isinstance(value, list):
            details["Charging windows"] = len(value)
            allocation_sources = list(
                dict.fromkeys(
                    plain_allocation_source(item.get("allocation_source"))
                    for item in value
                    if isinstance(item, dict) and item.get("allocation_source")
                )
            )
            if allocation_sources:
                details["Charging allocation sources"] = allocation_sources
        else:
            details[plain_key(key)] = bounded_json(value)
    return details


def plain_daylight_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Return daylight scheduling evidence with readable labels and values."""
    details: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if key in {"window_start_utc", "window_end_utc"}:
            label = "Sunrise" if key == "window_start_utc" else "Sunset"
            details[label] = date_time_label(item) or item
        elif key == "reason":
            details["Status"] = plain_reason(item)
        else:
            details[plain_key(key)] = bounded_json(item)
    return details


def plain_allocation_source(value: Any) -> str:
    """Return a readable EV charging allocation source."""
    labels = {
        "daylight": "Daylight preference",
        "ready_by": "Ready-by schedule",
        "ready_by_fallback": "Ready-by fallback",
    }
    text = str(value or "ready_by")
    return labels.get(text, display_state(text))


def action_sentence(action: PlanAction) -> str:
    """Return a one-sentence explanation of a planned action."""
    desired = action.desired_state
    if action.kind == ActionKind.SET_PROFILE:
        return f"Switch Enphase profile to {desired.get('profile', 'the selected profile')}."
    if action.kind == ActionKind.RESTORE_AI:
        return f"Restore Enphase to {desired.get('profile', 'the AI profile')}."
    if action.kind == ActionKind.SET_HVAC:
        mode = display_state(desired.get("hvac_mode", "climate"))
        target = desired.get("target_temperature")
        if target is not None:
            return f"Set climate to {mode} at {target} C."
        return f"Set climate to {mode}."
    if action.kind == ActionKind.RELEASE_HVAC:
        return "Release climate control to the configured automations."
    if action.kind == ActionKind.EV_SCHEDULE:
        target = desired.get("target_soc_percent")
        ready_by = desired.get("ready_by")
        target_text = f" to {target}%" if target is not None else ""
        ready_text = f" by {ready_by}" if ready_by else ""
        return f"Schedule EV charging{target_text}{ready_text}."
    return action_label(action)


def action_label(action: PlanAction) -> str:
    """Return a short user-facing action label."""
    labels = {
        ActionKind.SET_PROFILE: "Switch Enphase profile",
        ActionKind.RESTORE_AI: "Restore AI profile",
        ActionKind.SET_HVAC: "Change climate state",
        ActionKind.RELEASE_HVAC: "Release climate control",
        ActionKind.EV_START: "Start EV charging",
        ActionKind.EV_STOP: "Stop EV charging",
        ActionKind.EV_SCHEDULE: "Schedule EV charging",
    }
    return labels.get(action.kind, display_state(action.kind))


def action_window(action: PlanAction) -> str:
    """Return a concise action execution window."""
    start = local_datetime(action.execute_not_before)
    end = local_datetime(action.execute_not_after)
    if start is None or end is None:
        return "Next planning window"
    start_label = date_time_label(start)
    if start.date() == end.date():
        return f"{start_label}-{end:%H:%M}"
    return f"{start_label}-{date_time_label(end)}"


def decision_data_quality_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Explain material input limitations without exposing internal safety weights."""
    if coordinator.data is None:
        return {}
    issue_codes = [issue for issue in coordinator.data.input_issues if not str(issue).startswith("advisory_")][:12]
    sources = confidence_sources(coordinator)
    numeric_sources = [source for source in sources if isinstance(source.get("confidence"), int | float)]
    weakest_score = min((float(source["confidence"]) for source in numeric_sources), default=1.0)
    limiting_sources = [
        limiting_source_evidence(coordinator, source)
        for source in numeric_sources
        if weakest_score < 1.0 and float(source["confidence"]) == weakest_score
    ][:8]

    if str(coordinator.data.health) == InputHealth.UNSAFE or coordinator.data.confidence <= 0:
        status = "Unsafe inputs"
        summary = "Inputs are not safe enough for automatic control."
    elif issue_codes:
        status = "Input issue"
        summary = f"{len(issue_codes)} input issue{'s are' if len(issue_codes) != 1 else ' is'} limiting this plan."
    elif limiting_sources:
        status = "Fallback data"
        names = ", ".join(str(source["input"]) for source in limiting_sources[:3])
        suffix = " and other inputs" if len(limiting_sources) > 3 else ""
        verb = "uses" if len(limiting_sources) == 1 else "use"
        summary = f"{names}{suffix} {verb} the weakest data source in this plan."
    elif coordinator.data.confidence < 1.0:
        status = "Limited data"
        summary = "Plan data quality is reduced, but no specific limiting source was recorded."
    else:
        status = "Good"
        summary = "No material input-quality limitation affected this plan."

    return {
        "plan_id": coordinator.data.plan_id,
        "status": status,
        "summary": summary,
        "limiting_inputs": limiting_sources,
        "input_issues": [{"code": str(issue), "description": plain_reason(str(issue))} for issue in issue_codes],
        "improvement_actions": confidence_improvement_actions(
            coordinator.data.confidence,
            confidence_health_score(coordinator.data.health),
            forecast_source_confidence(coordinator),
            sources,
            confidence_issue_groups(issue_codes),
        ),
    }


def limiting_source_evidence(
    coordinator: EnergyPlannerCoordinator,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Return useful evidence for an input that limits the plan."""
    coverage = next(
        (item for item in forecast_coverage_sources(coordinator) if item.get("entity_id") == source.get("entity_id")),
        {},
    )
    evidence = {
        "input": source.get("input"),
        "entity_id": source.get("entity_id"),
        "source": source.get("source"),
        "reason": source.get("reason"),
        "coverage": coverage_summary(
            coverage,
            coordinator.data.horizon_hours if coordinator.data is not None else 0.0,
        ),
    }
    return {key: value for key, value in evidence.items() if value not in (None, "", {})}


def coverage_summary(coverage: dict[str, Any], horizon_hours: float) -> str | None:
    """Return a short forecast coverage explanation."""
    covered = coverage.get("covered_hours")
    if not isinstance(covered, int | float):
        return None
    return f"{float(covered):g} of {float(horizon_hours):g} planning hours available"


def confidence_issue_groups(issues: list[str]) -> dict[str, Any]:
    """Group input issues for existing improvement guidance."""
    groups = {
        "price": ("amber_import_price_", "amber_export_price_"),
        "pv": ("pv_forecast_",),
        "load": ("baseline_load_forecast_",),
        "battery": ("battery_soc_",),
        "ev": ("ev_",),
        "climate": ("daikin_", "climate_", "weather_"),
        "occupancy": ("person_", "occupancy_"),
    }
    return {
        name: {"issues": [issue for issue in issues if any(issue.startswith(prefix) for prefix in prefixes)][:8]}
        for name, prefixes in groups.items()
    }


def confidence_health_score(health: InputHealth | str) -> float:
    """Return confidence score contributed by input health."""
    if str(health) == InputHealth.HEALTHY:
        return 1.0
    if str(health) == InputHealth.DEGRADED:
        return 0.65
    return 0.0


def forecast_source_confidence(coordinator: EnergyPlannerCoordinator) -> float | None:
    """Return the forecast-source confidence used by the current plan when known."""
    latest = latest_forecast_snapshot(coordinator)
    confidence = latest.get("confidence") if isinstance(latest, dict) else None
    if isinstance(confidence, dict):
        value = confidence.get("forecast_source_confidence")
        if isinstance(value, int | float):
            return round(float(value), 4)
    if coordinator.data is None:
        return None
    health_score = confidence_health_score(coordinator.data.health)
    if coordinator.data.confidence < health_score:
        return coordinator.data.confidence
    return None


def confidence_sources(coordinator: EnergyPlannerCoordinator) -> list[dict[str, Any]]:
    """Return bounded source confidence evidence from the latest forecast snapshot."""
    latest = latest_forecast_snapshot(coordinator)
    confidence = latest.get("confidence") if isinstance(latest, dict) else None
    sources = confidence.get("sources") if isinstance(confidence, dict) else []
    if not isinstance(sources, list):
        return []
    return [
        {
            "input": confidence_source_label(source),
            "entity_id": source.get("entity_id"),
            "source": display_state(source.get("source", "unknown")),
            "confidence": source.get("confidence"),
            "confidence_percent": round(float(source.get("confidence", 0.0) or 0.0) * 100, 1),
            "reason": confidence_source_reason(source),
        }
        for source in sources[:12]
        if isinstance(source, dict)
    ]


def forecast_coverage_sources(coordinator: EnergyPlannerCoordinator) -> list[dict[str, Any]]:
    """Return bounded per-input temporal coverage from the current snapshot."""
    latest = latest_forecast_snapshot(coordinator)
    sources = latest.get("forecast_coverage") if isinstance(latest, dict) else []
    if not isinstance(sources, list):
        return []
    keys = (
        "config_key",
        "entity_id",
        "classification",
        "first_timestamp",
        "last_timestamp",
        "covered_hours",
        "continuous_hours",
        "longest_continuous_hours",
        "leading_missing_slots",
        "trailing_missing_slots",
        "internal_missing_slots",
        "leading_gap_filled_slots",
        "leading_gap_filled_hours",
    )
    return [
        {key: source.get(key) for key in keys if key in source} for source in sources[:12] if isinstance(source, dict)
    ]


def built_in_load_forecast_attrs(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return bounded built-in model evidence without exposing stored profiles."""
    latest = latest_forecast_snapshot(coordinator)
    value = latest.get("built_in_load_forecast") if isinstance(latest, dict) else None
    if not isinstance(value, dict):
        value = {}
    keys = (
        "source",
        "source_entity_id",
        "status",
        "model_version",
        "contract_version",
        "trained_at",
        "last_attempt_at",
        "last_attempt_source_entity_id",
        "last_training_status",
        "last_training_quality_failures",
        "last_training_validation",
        "unusable_since",
        "model_age_hours",
        "history_started_on",
        "history_ended_on",
        "history_days",
        "training_days",
        "complete_days",
        "fully_observed_days",
        "minimum_training_day_coverage",
        "history_coverage",
        "forecast_coverage",
        "recent_correction_factor",
        "first_expected_kw",
        "first_upper_kw",
        "quality_failures",
        "safety_gates_bypassed",
        "validation",
        "cleaning",
        "update_reason",
        "live_source_status",
        "live_source_outage_seconds",
        "outage_grace_minutes",
        "current_correction_applied",
        "fallback_applied",
    )
    return {key: bounded_json(value.get(key)) for key in keys if value.get(key) is not None}


def action_load_forecast_attrs(
    coordinator: EnergyPlannerCoordinator,
    action_id: str,
) -> dict[str, Any]:
    """Return model health and load evidence aligned to one action."""
    latest = latest_forecast_snapshot(coordinator)
    rows = latest.get("action_load_forecasts") if isinstance(latest, dict) else None
    if not isinstance(rows, list):
        return {}
    row = next(
        (item for item in rows if isinstance(item, dict) and str(item.get("action_id", "")) == action_id),
        None,
    )
    if row is None:
        return {}
    model = built_in_load_forecast_attrs(coordinator)
    model.pop("first_expected_kw", None)
    model.pop("first_upper_kw", None)
    return {
        **model,
        "valid_at": bounded_json(row.get("valid_at")),
        "expected_kw": bounded_json(row.get("expected_kw")),
        "conservative_kw": bounded_json(row.get("conservative_kw")),
    }


def latest_forecast_snapshot(coordinator: EnergyPlannerCoordinator) -> dict[str, Any]:
    """Return the most recent forecast snapshot for the current plan where possible."""
    snapshots = coordinator.store.data.get("forecast_snapshots", [])
    if not isinstance(snapshots, list):
        return {}
    plan_id = None if coordinator.data is None else coordinator.data.plan_id
    for item in reversed(snapshots):
        if isinstance(item, dict) and (plan_id is None or item.get("plan_id") == plan_id):
            return item
    return {}


def confidence_improvement_actions(
    overall: float,
    health_score: float,
    forecast_confidence: float | None,
    sources: list[dict[str, Any]],
    breakdown: dict[str, Any],
) -> list[str]:
    """Return prioritized actions to improve plan confidence."""
    actions: list[str] = []
    if forecast_confidence is not None and forecast_confidence <= health_score and forecast_confidence < 1.0:
        limiting_sources = [source for source in sources if source.get("confidence") == forecast_confidence]
        for source in limiting_sources[:4]:
            if source.get("source") == "Point Value Repeated":
                actions.append(
                    f"Replace {source['input']} ({source.get('entity_id')}) with an entity that exposes forecast data "
                    "for the planning horizon, or add source confidence metadata."
                )
            elif source.get("source") == "Invalid State":
                actions.append(f"Fix {source['input']} ({source.get('entity_id')}) so it has a numeric usable state.")
            else:
                actions.append(
                    f"Improve {source['input']} ({source.get('entity_id')}) source confidence or data quality."
                )
    if overall == health_score and health_score < 1.0:
        for name, details in breakdown.items():
            issues = details.get("issues", []) if isinstance(details, dict) else []
            if issues:
                actions.append(f"Resolve {name} input issue(s): {', '.join(str(issue) for issue in issues[:3])}.")
    if not actions and overall < 1.0:
        actions.append(
            "Use forecast-capable entities with confidence metadata for price, PV, load, and weather inputs."
        )
    if not actions:
        actions.append("Confidence is already at 100%; no action is needed.")
    return actions[:8]


def confidence_source_label(source: dict[str, Any]) -> str:
    """Return a readable configured input label."""
    labels = {
        "amber_import_price_entity": "Amber import price",
        "amber_export_price_entity": "Amber export price",
        "pv_forecast_entity": "PV forecast",
        "pv_forecast_secondary_entity": "Second PV forecast",
        "household_load_entity": "Built-in household load forecast",
        "weather_entity": "Weather forecast",
    }
    config_key = str(source.get("config_key", "unknown"))
    return labels.get(config_key, config_key.replace("_", " ").capitalize())


def confidence_source_reason(source: dict[str, Any]) -> str:
    """Return a readable reason for one confidence source score."""
    source_kind = source.get("source")
    if source_kind == "forecast_series":
        return "Forecast series found; confidence comes from entity metadata when present, otherwise 100%."
    if source_kind == "forecast_series_stitched":
        return "Timestamped forecast series were stitched, with the primary source taking precedence on overlap."
    if source_kind == "forecast_series_partial":
        return (
            "Forecast series coverage is shorter than the displayed planning horizon; coverage thresholds limit health."
        )
    if source_kind == "built_in_recorder_history":
        return "A local deterministic forecast learned from measured household load in Home Assistant Recorder."
    if source_kind == "point_value_repeated":
        return (
            "Only a current point value was found, so it is repeated across the planning horizon with a "
            "conservative fallback weight."
        )
    if source_kind == "point_value_only":
        return (
            "Only a current point value was found; required forecast coverage is unavailable and planning fails closed."
        )
    if source_kind == "invalid_state":
        return "The entity state could not be converted into usable forecast data."
    return "Confidence source was not classified."


def asset_name(asset: ActionAsset) -> str:
    """Return a readable asset name."""
    names = {
        ActionAsset.DAIKIN: "Climate",
        ActionAsset.ENPHASE: "Enphase",
        ActionAsset.EV: "EV",
    }
    return names.get(asset, display_state(asset))


def reason_summary(reasons: Any) -> str:
    """Return a readable reason summary from one or more reason codes."""
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        return ""
    readable = [plain_reason(reason) for reason in reasons[:4]]
    return "; ".join(reason for reason in readable if reason)


def plain_reason(value: Any) -> str:
    """Return a plain-English explanation for an internal reason or issue code."""
    text = str(value or "").strip()
    labels = {
        "away_hvac_policy": "Nobody is home, so climate control can be reduced.",
        "occupied_comfort_within_bounds": "The home is occupied and temperature is already within the comfort range.",
        "manual_hvac_override_inactive": "No manual climate override is active.",
        "hvac_min_cycle": "The climate minimum cycle time is being respected.",
        "hvac_precondition_before_expensive_period": "Preconditioning before a more expensive electricity period.",
        "hvac_thermal_shift_before_expensive_period": (
            "Heating or cooling now because electricity is cheap and the home can coast through a later "
            "expensive period."
        ),
        "enphase_price_spread_above_threshold": "The forecast price spread is above the Enphase savings threshold.",
        "enphase_forecast_solar_export_value_above_threshold": (
            "Forecast solar surplus value is above the Enphase savings threshold."
        ),
        "enphase_insufficient_arbitrage_evidence_below_threshold": (
            "There is not enough forecast battery or solar value to justify Enphase profile ownership."
        ),
        "enphase_arbitrage_below_threshold": "The expected Enphase value is below the configured savings threshold.",
        "ev_soc_below_target": "The EV battery is below the target state of charge.",
        "least_cost_slots_before_ready_by": "Charging was placed in the cheapest slots before the ready-by time.",
        "least_cost_solar_aware_slots_before_ready_by": (
            "Charging was placed in the lowest effective-cost slots, including forecast solar surplus."
        ),
        "daylight_lowest_effective_cost_slots": (
            "Charging was placed in the lowest effective-cost complete daylight window."
        ),
        "daylight_lowest_effective_cost_with_ready_by_fallback": (
            "Daylight charging was preferred first, with remaining charge placed before ready-by."
        ),
        "ev_daylight_lowest_cost_selected": "A complete lowest-cost daylight charging window was selected.",
        "ev_daylight_lowest_cost_charge_now": "The EV is in its selected lowest-cost daylight window.",
        "ev_daylight_lowest_cost_with_ready_by_fallback": (
            "Daylight slots were selected first and ready-by slots cover the remaining charge."
        ),
        "ev_daylight_forecast_incomplete": (
            "The complete remaining sunrise-to-sunset forecast is not available, so ready-by scheduling is used."
        ),
        "ev_daylight_window_not_before_ready_by": (
            "No complete daylight window ends before the next ready-by deadline."
        ),
        "ev_daylight_continuous_capacity_insufficient": (
            "Daylight cannot fit one complete continuous session, so ready-by scheduling is used."
        ),
        "ev_daylight_no_eligible_charge": ("No daylight slot is eligible under the configured charging constraints."),
        "ev_daylight_deferred_active_session": (
            "The confirmed continuous charging session takes precedence over daylight replanning."
        ),
        "ev_daylight_deferred_opportunistic_charge": (
            "The current opportunistic-price charging request takes precedence over daylight scheduling."
        ),
        "configured_target": "The configured EV target state of charge is being used.",
        "history_max_daily_consumption": "Trip history raised the EV target to cover recent driving.",
        "battery_floor": "The battery reserve limit must be respected.",
        "enphase_min_savings": "The Enphase savings threshold must be met.",
        "enphase_profile_hold": "The Enphase profile hold period must be respected.",
        "ready_by": "The EV ready-by time must be respected.",
        "comfort": "The climate comfort range must be respected.",
    }
    if text in labels:
        return labels[text]
    return display_state(text)


def plain_key(value: Any) -> str:
    """Return a readable attribute key."""
    labels = {
        "reason_codes": "Reasons",
        "hvac_mode": "Climate mode",
        "target_temperature": "Target temperature C",
        "current_temperature": "Current temperature C",
        "current_power_kw": "Current power kW",
        "outdoor_temperature": "Outdoor temperature C",
        "occupied_temperature_low": "Comfort low C",
        "occupied_temperature_high": "Comfort high C",
        "target_soc_percent": "Target SOC percent",
        "ready_by": "Ready by",
        "daylight_lowest_cost_enabled": "Daylight lowest-cost enabled",
        "daylight_lowest_cost_applicable": "Daylight window applicable",
        "daylight_forecast_complete": "Daylight forecast complete",
        "daylight_lowest_cost_selected": "Daylight schedule selected",
        "daylight_window_start_utc": "Sunrise",
        "daylight_window_end_utc": "Sunset",
        "daylight_lowest_cost_reason": "Daylight scheduling status",
        "daylight_lowest_cost": "Daylight lowest-cost charging",
        "allocation_source": "Allocation source",
        "allocation_source_now": "Current allocation source",
        "arbitrage_value": "Estimated value",
        "arbitrage_source": "Value source",
        "arbitrage_direction": "Battery strategy",
        "execute_not_before": "Start",
        "execute_not_after": "End",
    }
    text = str(value)
    return labels.get(text, display_state(text))


def date_time_label(value: Any) -> str | None:
    """Return an explicit local date and time for a planned action."""
    parsed = local_datetime(value)
    if parsed is None:
        return value if isinstance(value, str) and value else None
    return f"{parsed:%a} {parsed.day} {parsed:%b}, {parsed:%H:%M}"


def local_datetime(value: Any) -> datetime | None:
    """Return a datetime converted to Home Assistant's configured timezone."""
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed
    return dt_util.as_local(parsed)


def display_state(value: Any) -> str:
    """Return a short user-facing state string."""
    text = str(value or "unknown").strip().replace("_", " ")
    if not text:
        return "Unknown"
    words = []
    for word in text.split():
        upper = word.upper()
        words.append(upper if upper in {"AI", "EV", "HVAC", "PV", "SOC"} else word.capitalize())
    return " ".join(words)


def bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Convert values to bounded JSON-friendly attributes."""
    if depth >= 4:
        return "<truncated>"
    value = to_jsonable(value)
    if isinstance(value, dict):
        return {str(key): bounded_json(item, depth=depth + 1) for key, item in list(value.items())[:16]}
    if isinstance(value, list):
        items = [bounded_json(item, depth=depth + 1) for item in value[:12]]
        if len(value) > 12:
            items.append({"truncated_count": len(value) - 12})
        return items
    return value
