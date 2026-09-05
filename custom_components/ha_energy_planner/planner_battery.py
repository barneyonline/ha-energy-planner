"""Pure battery headroom and Enphase arbitrage value policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC_PERCENT,
    CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT,
    CONF_BATTERY_USABLE_CAPACITY_KWH,
    CONF_PLANNING_INTERVAL_MINUTES,
)
from .models import (
    DecisionContext,
)
from .planner_values import finite_float as _float_or_none
from .planner_values import nonnegative_float_or_none as _positive_or_none


def _arbitrage_value(
    context: DecisionContext,
    interval_minutes: int,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    forecast_export = _forecast_solar_export_value(context, interval_minutes, options)
    if forecast_export is not None:
        return {
            "value": forecast_export["value"],
            "source": "forecast_solar_export_value",
            "direction": "consume",
            "details": forecast_export,
        }
    return {
        "value": 0.0,
        "source": "insufficient_arbitrage_evidence",
        "direction": "consume",
        "details": _marginal_budget_summary(context, options or {}),
    }


def _enphase_profile_for_arbitrage(context: DecisionContext) -> str | None:
    return context.enphase_self_consumption_profile or context.enphase_arbitrage_profile


def _forecast_solar_export_value(
    context: DecisionContext,
    interval_minutes: int,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return estimated value of forecast solar surplus that could be self-consumed."""
    total = 0.0
    has_surplus = False
    interval_hours = interval_minutes / 60
    battery = _battery_model(context, options or {})
    remaining_charge_kwh = battery["charge_headroom_kwh"]
    accepted_surplus_kwh = 0.0
    forecast_surplus_kwh = 0.0
    for slot in context.slots:
        pv_kw = _positive_or_none(
            slot.pv_forecast_lower_kw if slot.pv_forecast_lower_kw is not None else slot.pv_forecast_kw
        )
        load_kw = _positive_or_none(
            slot.baseline_load_forecast_upper_kw
            if slot.baseline_load_forecast_upper_kw is not None
            else slot.baseline_load_forecast_kw
        )
        export_price = _float_or_none(slot.export_price)
        if pv_kw is None or load_kw is None or export_price is None:
            continue
        flexible_load_kw = max(float(slot.projected_ev_load_kw or 0.0), 0.0) + max(
            float(slot.projected_hvac_load_kw or 0.0),
            0.0,
        )
        surplus_kw = max(pv_kw - load_kw - flexible_load_kw, 0.0)
        if surplus_kw <= 0:
            continue
        forecast_surplus_kwh += surplus_kw * interval_hours
        has_surplus = True
        charge_kw = min(surplus_kw, battery["max_charge_kw"])
        charge_input_kwh = charge_kw * interval_hours
        stored_kwh = min(charge_input_kwh * battery["round_trip_efficiency"], remaining_charge_kwh)
        if stored_kwh <= 0:
            continue
        remaining_charge_kwh -= stored_kwh
        accepted_input_kwh = stored_kwh / battery["round_trip_efficiency"]
        accepted_surplus_kwh += accepted_input_kwh
        total += accepted_input_kwh * export_price * battery["round_trip_efficiency"]
    if not has_surplus:
        return None
    return {
        "value": round(total, 4),
        "forecast_surplus_kwh": round(forecast_surplus_kwh, 4),
        "accepted_surplus_kwh": round(accepted_surplus_kwh, 4),
        "battery_charge_headroom_kwh": battery["charge_headroom_kwh"],
        "remaining_charge_headroom_kwh": round(remaining_charge_kwh, 4),
        "battery_max_charge_kw": battery["max_charge_kw"],
        "battery_round_trip_efficiency": battery["round_trip_efficiency"],
    }


def _forecast_surplus_kwh(context: DecisionContext, interval_minutes: int) -> float:
    """Return forecast solar surplus after projected flexible loads."""
    interval_hours = interval_minutes / 60
    total = 0.0
    for slot in context.slots:
        pv_kw = _positive_or_none(
            slot.pv_forecast_lower_kw if slot.pv_forecast_lower_kw is not None else slot.pv_forecast_kw
        )
        load_kw = _positive_or_none(
            slot.baseline_load_forecast_upper_kw
            if slot.baseline_load_forecast_upper_kw is not None
            else slot.baseline_load_forecast_kw
        )
        if pv_kw is None or load_kw is None:
            continue
        flexible_kw = max(float(slot.projected_ev_load_kw or 0.0), 0.0) + max(
            float(slot.projected_hvac_load_kw or 0.0),
            0.0,
        )
        total += max(pv_kw - load_kw - flexible_kw, 0.0) * interval_hours
    return round(total, 4)


def _battery_model(context: DecisionContext, options: Mapping[str, Any]) -> dict[str, float]:
    """Return bounded battery physics used by planning estimates."""
    capacity_kwh = max(_float_or_none(options.get(CONF_BATTERY_USABLE_CAPACITY_KWH)) or 0.0, 0.0)
    soc = _float_or_none(context.current_battery_soc_percent)
    reserve_soc = max(_float_or_none(options.get(CONF_BATTERY_MIN_SOC_PERCENT)) or 0.0, 0.0)
    efficiency_percent = _float_or_none(options.get(CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT))
    efficiency = min(max((efficiency_percent or 90.0) / 100.0, 0.01), 1.0)
    max_charge_kw = max(_float_or_none(options.get(CONF_BATTERY_MAX_CHARGE_KW)) or 0.0, 0.0)
    max_discharge_kw = max(_float_or_none(options.get(CONF_BATTERY_MAX_DISCHARGE_KW)) or 0.0, 0.0)
    if soc is None or capacity_kwh <= 0:
        charge_headroom_kwh = capacity_kwh
        discharge_available_kwh = 0.0
    else:
        charge_headroom_kwh = max(capacity_kwh * ((100.0 - soc) / 100.0), 0.0)
        discharge_available_kwh = max(capacity_kwh * ((soc - reserve_soc) / 100.0), 0.0)
    return {
        "capacity_kwh": round(capacity_kwh, 4),
        "soc_percent": -1.0 if soc is None else round(soc, 4),
        "reserve_soc_percent": round(reserve_soc, 4),
        "charge_headroom_kwh": round(charge_headroom_kwh, 4),
        "discharge_available_kwh": round(discharge_available_kwh, 4),
        "round_trip_efficiency": round(efficiency, 4),
        "max_charge_kw": round(max_charge_kw, 4),
        "max_discharge_kw": round(max_discharge_kw, 4),
    }


def _arbitrage_spread(context: DecisionContext) -> float:
    import_prices = [
        price for price in (_float_or_none(slot.import_price) for slot in context.slots) if price is not None
    ]
    export_prices = [
        price for price in (_float_or_none(slot.export_price) for slot in context.slots) if price is not None
    ]
    if not import_prices or not export_prices:
        return 0.0
    return max(export_prices) - min(import_prices)


def _marginal_budget_summary(context: DecisionContext, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return shared energy budget used by marginal device decisions."""
    interval_minutes = int(options.get(CONF_PLANNING_INTERVAL_MINUTES, 5) or 5)
    surplus_kwh = _forecast_surplus_kwh(context, interval_minutes)
    battery = _battery_model(context, options)
    return {
        "forecast_surplus_kwh": surplus_kwh,
        "battery_charge_headroom_kwh": battery["charge_headroom_kwh"],
        "battery_discharge_available_kwh": battery["discharge_available_kwh"],
        "battery_max_charge_kw": battery["max_charge_kw"],
        "battery_max_discharge_kw": battery["max_discharge_kw"],
        "battery_round_trip_efficiency": battery["round_trip_efficiency"],
    }
