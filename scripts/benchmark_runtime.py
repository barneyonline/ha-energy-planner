#!/usr/bin/env python3
"""Repeatable synthetic CPU probes; run inside the Home Assistant Docker image."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from statistics import median
from time import perf_counter
from types import SimpleNamespace

from custom_components.ha_energy_planner.const import CONF_PV_FORECAST, CONF_PV_FORECAST_SECONDARY
from custom_components.ha_energy_planner.coordinator import _decision_input_fingerprint
from custom_components.ha_energy_planner.ev import build_ev_charge_calibration
from custom_components.ha_energy_planner.forecasts import _value_for_slot


def main() -> None:
    """Report measurements without a hardware-dependent pass/fail threshold."""
    base = datetime(2026, 8, 1, tzinfo=UTC)
    results = []
    for size in (1000, 5000, 20000):
        charging = [
            SimpleNamespace(last_changed=base + timedelta(seconds=i * 120), state="on" if i % 2 == 0 else "off")
            for i in range(size)
        ]
        soc = [SimpleNamespace(last_changed=point.last_changed, state="50") for point in charging]
        durations = []
        for _ in range(3):
            started = perf_counter()
            build_ev_charge_calibration(
                charging, soc, charge_rate_kw=7, trained_at=base + timedelta(days=30),
                charging_entity_id="sensor.charging", soc_entity_id="sensor.soc",
            )
            durations.append(perf_counter() - started)
        results.append({"operation": "ev_calibration", "rows_per_source": size, "median_seconds": median(durations)})
    cadence = timedelta(minutes=5)
    for size in (288, 10000):
        points = [(base + (i - size + 288) * cadence, float(i)) for i in range(size)]
        durations = []
        for _ in range(3):
            started = perf_counter()
            values = [_value_for_slot(base + i * cadence, points, final_cadence=cadence) for i in range(288)]
            durations.append(perf_counter() - started)
        assert values == [float(i) for i in range(size - 288, size)]
        results.append({"operation": "forecast_lookup", "source_rows": size, "median_seconds": median(durations)})
    for size in (288, 10000):
        attributes = {"forecast": [
            {"datetime": (base + i * cadence).isoformat(), "value": float(i % 20)} for i in range(size)
        ]}
        states = {
            entity_id: SimpleNamespace(state="1.0", attributes=attributes)
            for entity_id in ("sensor.forecast_a", "sensor.forecast_b")
        }
        hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
        entry_data = {CONF_PV_FORECAST: "sensor.forecast_a", CONF_PV_FORECAST_SECONDARY: "sensor.forecast_b"}
        durations = []
        for _ in range(3):
            started = perf_counter()
            _decision_input_fingerprint(hass, entry_data, {}, [], now=base, weather_forecast={})
            durations.append(perf_counter() - started)
        results.append({
            "operation": "decision_fingerprint", "forecast_entities": 2, "rows_per_source": size,
            "attribute_json_bytes_per_entity": len(json.dumps(attributes).encode()),
            "median_seconds": median(durations),
        })
    print(json.dumps({"synthetic": True, "repetitions": 3, "results": results}, indent=2))


if __name__ == "__main__":
    main()
