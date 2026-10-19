"""The smoke plan must observe inputs that finish loading after the coordinator."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script

ROOT = Path(__file__).resolve().parents[2]
INPUTS = {
    "sensor.smoke_import_price_forecast": ("0.1", {}),
    "sensor.smoke_export_price_forecast": ("0.05", {}),
    "sensor.smoke_pv_forecast_series": ("2500", {}),
    "climate.fake_daikin": ("off", {"current_temperature": 21}),
    "automation.fake_climate_conflict": ("on", {}),
}


def startup_sequence():
    source = (ROOT / "scripts/docker-ha-smoke.sh").read_text()
    config = source.split('cat > "$TMP_DIR/configuration.yaml" <<\'YAML\'\n', 1)[1].split("\nYAML\n", 1)[0]
    actions = next(
        item["actions"] for item in yaml.safe_load(config)["automation"]
        if item["id"] == "ha_energy_planner_service_smoke"
    )
    replan = next(index for index, step in enumerate(actions) if step.get("action") == "ha_energy_planner.replan")
    return cv.SCRIPT_SCHEMA(actions[replan - 1:replan + 2])


@pytest.mark.parametrize("delayed", INPUTS)
@pytest.mark.parametrize("initial", [None, "unknown", "unavailable"])
def test_replan_waits_for_each_late_input(tmp_path, delayed, initial):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = []
        task = None

        async def replan(call):
            calls.append(call)

        hass.services.async_register("ha_energy_planner", "replan", replan)
        hass.services.async_register("fake_planner_test", "wait_for_planner_idle", replan)
        try:
            for entity, (state, attributes) in INPUTS.items():
                if entity != delayed:
                    hass.states.async_set(entity, state, attributes)
                elif initial is not None:
                    hass.states.async_set(entity, initial)
            script = Script(hass, startup_sequence(), "Smoke startup", "test")
            task = asyncio.create_task(script.async_run())
            await hass.async_block_till_done()
            assert calls == []
            assert not task.done()
            hass.states.async_set(delayed, *INPUTS[delayed])
            await asyncio.wait_for(task, 3)
            assert [(call.domain, call.service) for call in calls] == [
                ("ha_energy_planner", "replan"), ("fake_planner_test", "wait_for_planner_idle"),
            ]
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await hass.async_stop(force=True)

    asyncio.run(run())
