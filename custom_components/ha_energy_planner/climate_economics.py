"""Pure paired site-energy simulation; no battery commands or vendor assumptions."""

from __future__ import annotations

from math import sqrt
from typing import Any

from .climate_inputs import finite
from .climate_models import SiteCost
from .const import CONF_GRID_EXPORT_LIMIT_KW, CONF_GRID_IMPORT_LIMIT_KW, CONF_PLANNING_INTERVAL_MINUTES
from .models import DecisionContext
from .planner_battery import _battery_model


def site_cost(
    context: DecisionContext, powers: tuple[float, ...], options: dict[str, Any], *, conservative: bool = False
) -> SiteCost | None:
    """Value the same external loads and physical battery policy for each candidate."""
    if len(powers) != len(context.slots) or not context.climate_inputs.get("load_excludes_hvac"):
        return None
    if context.climate_inputs.get("battery_configured") and context.current_battery_soc_percent is None:
        return None
    battery = _battery_model(context, options)
    has_battery = context.current_battery_soc_percent is not None
    profile = context.current_enphase_profile
    if has_battery and profile not in {context.enphase_self_consumption_profile, context.enphase_full_backup_profile}:
        return None
    if has_battery and (not profile or battery["capacity_kwh"] <= 0):
        return None
    stored = battery["capacity_kwh"] * float(context.current_battery_soc_percent or 0) / 100
    floor = battery["capacity_kwh"] * battery["reserve_soc_percent"] / 100
    efficiency = sqrt(battery["round_trip_efficiency"])
    total = imported = exported = hvac = 0.0
    for index, (slot, power) in enumerate(zip(context.slots, powers, strict=True)):
        duration = (
            (context.slots[index + 1].valid_at - slot.valid_at).total_seconds() / 3600
            if index + 1 < len(context.slots)
            else float(options[CONF_PLANNING_INTERVAL_MINUTES]) / 60
        )
        if duration <= 0 or duration - float(options[CONF_PLANNING_INTERVAL_MINUTES]) / 60 > 1e-6:
            return None
        pv = (
            slot.pv_forecast_lower_kw if conservative and slot.pv_forecast_lower_kw is not None else slot.pv_forecast_kw
        )
        load = (
            slot.baseline_load_forecast_upper_kw
            if conservative and slot.baseline_load_forecast_upper_kw is not None
            else slot.baseline_load_forecast_kw
        )
        if any(finite(value) is None for value in (pv, load, slot.import_price, slot.export_price, power)):
            return None
        assert pv is not None and load is not None and slot.import_price is not None and slot.export_price is not None
        if min(pv, load, power) < 0:
            return None
        net = (load + slot.projected_ev_load_kw + power - pv) * duration
        if has_battery:
            if net < 0:
                charge = min(
                    -net, battery["max_charge_kw"] * duration, max(battery["capacity_kwh"] - stored, 0) / efficiency
                )
                stored += charge * efficiency
                net += charge
            elif profile == context.enphase_self_consumption_profile:
                discharge = min(net, battery["max_discharge_kw"] * duration, max(stored - floor, 0) * efficiency)
                stored -= discharge / efficiency
                net -= discharge
        if net / duration > float(options[CONF_GRID_IMPORT_LIMIT_KW]) or -net / duration > float(
            options[CONF_GRID_EXPORT_LIMIT_KW]
        ):
            return None
        imported += max(net, 0)
        exported += max(-net, 0)
        hvac += power * duration
        total += max(net, 0) * slot.import_price - max(-net, 0) * slot.export_price
    return SiteCost(total, imported, exported, hvac, stored)
