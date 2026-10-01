"""Pure, timestamped tariff policy for independent zero-export control."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .const import CONF_MIN_TARIFF_CONFIDENCE, CONF_PRICE_FRESHNESS_MINUTES
from .forecasts import (
    _explicit_interval_minutes,
    _flatten_item,
    _forecast_item_unit,
    _forecast_items,
    _parse_datetime_or_none,
    _with_canonical_keys,
    normalize_scalar_value,
)
from .models import ActionAsset, ActionKind, DecisionContext, DecisionSlot, PlanAction

EXPORT_ASSET = "enphase_export_limit"
PRICE_KEYS = ("export_price", "feed_in_price", "per_kwh", "price", "value")
START_KEYS = ("start_time", "period_start", "valid_at", "datetime", "from", "time", "date", "nem_time")
END_KEYS = ("end_time", "period_end", "valid_until", "to")
BLOCK_DURATION = timedelta(minutes=30)


READBACK_MAX_AGE = timedelta(minutes=10)


def confirmed_feedback(state: Any, now: datetime) -> tuple[dict[str, Any], str | None]:
    """Validate the public Enphase sensor contract without importing its internals."""
    if state is None or state.state in {"unknown", "unavailable"}:
        return {}, "export_limit_entity_unavailable"
    attributes = state.attributes
    readback: Any = attributes.get("last_successful_readback")
    if type(readback) not in (int, float) or not isfinite(readback):
        return {}, "export_limit_readback_invalid"
    age = now.timestamp() - readback
    watts = attributes.get("confirmed_watts")
    slew: Any = attributes.get("slew_rate")
    if (
        type(slew) not in (int, float)
        or not isfinite(slew)
        or slew <= 0
        or round(slew, 2) != slew
        or type(attributes.get("pending")) is not bool
        or (watts is not None and (type(watts) is not int or not 0 <= watts <= 100000))
        or state.state not in {"disabled", "zero_export", "limited", "pending", "unconfirmed"}
        or (state.state == "zero_export" and watts != 0)
        or (state.state == "limited" and (watts is None or watts <= 0))
        or (state.state == "disabled" and watts is not None)
        or (state.state == "pending" and attributes["pending"] is not True)
        or (state.state == "unconfirmed" and attributes["pending"] is not True)
    ):
        return {}, "export_limit_unsupported"
    result = {
        "watts": watts,
        "slew_rate": slew,
        "state": state.state,
        "pending": attributes["pending"],
        "requested_watts": attributes.get("requested_watts"),
        "requested_action": attributes.get("requested_action"),
        "requested_at": attributes.get("pending_requested_at"),
        "request_status": attributes.get("request_status"),
        "last_readback": readback,
    }
    if not 0 <= age <= READBACK_MAX_AGE.total_seconds():
        return result, "export_limit_readback_stale"
    return result, None


def aware_time(value: Any) -> datetime | None:
    """Reject ambiguous local timestamps, retaining absolute UTC instants."""
    result = value if isinstance(value, datetime) else _parse_datetime_or_none(value)
    return result.astimezone(UTC) if result is not None and result.tzinfo is not None else None


def block_time(value: Any) -> datetime:
    """Read an already validated interval timestamp."""
    result = aware_time(value)
    assert result is not None
    return result


def _tariff_confidence(attributes: dict[str, Any]) -> float | None:
    """Accept the input layer's fraction/percentage fields, failing closed on invalid evidence."""
    for key in ("confidence", "confidence_percent", "forecast_confidence", "forecast_confidence_percent"):
        if key not in attributes:
            continue
        value = attributes[key]
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(number) or not 0 <= number <= 100:
            return None
        return number / 100 if number > 1 else number
    return 1.0


def tariff_blocks(state: Any, now: datetime, options: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Preserve real half-hour source intervals without extrapolating scalar prices."""
    if state is None or state.state in {"unknown", "unavailable"}:
        return [], "export_tariff_unavailable"
    attributes = _with_canonical_keys(state.attributes or {})
    updated = aware_time(attributes.get("issued_at") or attributes.get("last_updated") or state.last_updated)
    freshness = timedelta(minutes=float(options.get(CONF_PRICE_FRESHNESS_MINUTES, 30)))
    if updated is None or not timedelta(0) <= now - updated <= freshness:
        return [], "export_tariff_stale"
    confidence = _tariff_confidence(attributes)
    if (
        confidence is None
        or confidence < float(options.get(CONF_MIN_TARIFF_CONFIDENCE, 50)) / 100
    ):
        return [], "export_tariff_confidence_low"
    items = _forecast_items(attributes, PRICE_KEYS)
    parsed = []
    for raw in items:
        if not isinstance(raw, dict):
            return [], "export_tariff_invalid"
        item = _flatten_item(raw)
        start = next((aware_time(item[key]) for key in START_KEYS if key in item), None)
        key = next((key for key in PRICE_KEYS if key in item), None)
        value: Any = item.get(key) if key is not None else None
        if start is None or type(value) not in (int, float) or not isfinite(value):
            return [], "export_tariff_invalid"
        unit = _forecast_item_unit(item, key, str(attributes.get("unit_of_measurement", attributes.get("unit", ""))))
        price = normalize_scalar_value(value, value_kind="price", unit=unit)
        end_key = next((key for key in END_KEYS if key in item), None)
        end = aware_time(item[end_key]) if end_key else None
        if end_key and end is None:
            return [], "export_tariff_invalid"
        parsed.append((start, end, price))
    parsed.sort(key=lambda value: value[0])
    blocks: list[dict[str, Any]] = []
    cadence = _explicit_interval_minutes(attributes)
    if cadence is not None and cadence != 30:
        return [], "export_tariff_ambiguous_duration"
    for index, (start, end, price) in enumerate(parsed):
        if end is None:
            if cadence == 30 or (index + 1 < len(parsed) and parsed[index + 1][0] - start == BLOCK_DURATION):
                end = start + BLOCK_DURATION
            elif index > 0 and start - parsed[index - 1][0] == BLOCK_DURATION:
                end = start + BLOCK_DURATION
            else:
                return [], "export_tariff_ambiguous_duration"
        if end - start != BLOCK_DURATION or (blocks and start < block_time(blocks[-1]["end"])):
            return [], "export_tariff_overlap_or_duration"
        if blocks and start > block_time(blocks[-1]["end"]):
            return [], "export_tariff_gap"
        blocks.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "price": price,
                "watts": 0 if price < 0 else None,
                "confidence": confidence,
            }
        )
    current = next((block for block in blocks if block_time(block["start"]) <= now < block_time(block["end"])), None)
    if current is None:
        return [], "export_tariff_block_missing"
    following = next((block for block in blocks if block_time(block["start"]) == block_time(current["end"])), None)
    return [current, *([following] if following else [])], None


def build_actions(context: DecisionContext) -> list[PlanAction]:
    """Plan transitions, retaining a preview even when no change is necessary."""
    evidence = context.export_limit
    if evidence.get("ready") is not True:
        return []
    expected = evidence["feedback"]["watts"]
    actions = []
    for block in evidence["blocks"]:
        desired = block["watts"]
        if desired != expected:
            actions.append(
                PlanAction(
                    action_id=f"{context.plan_id}-export-limit-{block['start']}",
                    plan_id=context.plan_id,
                    execute_not_before=block_time(block["start"]),
                    execute_not_after=block_time(block["end"]),
                    asset=ActionAsset.ENPHASE_EXPORT_LIMIT,
                    kind=ActionKind.SET_EXPORT_LIMIT if desired == 0 else ActionKind.DISABLE_EXPORT_LIMIT,
                    desired_state={"watts": desired, "tariff_block": block},
                    hard_constraints=["export_tariff_fresh", "export_limit_readback"],
                    reason_codes=["negative_export_price" if desired == 0 else "nonnegative_export_price"],
                    expected_cost_delta=None,
                    confidence=block["confidence"],
                )
            )
        expected = desired
    return actions


def assumed_zero_export(context: DecisionContext, at: datetime) -> bool:
    """Apply planned curtailment only to covered, eligible tariff intervals."""
    return context.export_limit.get("ready") is True and any(
        block_time(block["start"]) <= at < block_time(block["end"]) and block["watts"] == 0
        for block in context.export_limit.get("blocks", [])
    )


def area_safe(plan: Any) -> bool:
    """Read additive area evidence without making legacy plan health optimistic."""
    return getattr(plan, "control_area_health", {}).get(EXPORT_ASSET, {}).get("ready") is True


def curtailment_preview(context: DecisionContext, slot: DecisionSlot) -> dict[str, Any]:
    """Keep production intact while distinguishing assumed grid curtailment."""
    zero_export = assumed_zero_export(context, slot.valid_at)
    net_export = (None if slot.pv_forecast_kw is None or slot.baseline_load_forecast_kw is None else
                  max(slot.pv_forecast_kw - slot.baseline_load_forecast_kw
                      - slot.projected_ev_load_kw - slot.projected_hvac_load_kw, 0.0))
    return {"assumed_zero_export": zero_export,
            "projected_grid_export_kw": None if net_export is None else 0.0 if zero_export else net_export,
            "export_limit_confirmation": context.export_limit.get("feedback", {}).get("state", "unknown")}
