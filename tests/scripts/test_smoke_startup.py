"""The smoke plan must observe inputs that finish loading after the coordinator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script

ROOT = Path(__file__).resolve().parents[2]
INPUTS = {
    "sensor.smoke_import_price_forecast": ("0.1", {}),
    "sensor.smoke_export_price_forecast": ("0.05", {}),
    "sensor.smoke_pv_forecast_series": ("2500", {}),
    "weather.smoke_weather_forecast": ("sunny", {}),
    "climate.fake_daikin": ("off", {"current_temperature": 21}),
    "automation.fake_climate_conflict": ("on", {}),
}
ACTIVE = "binary_sensor.energy_planner_climate_control_active"


def seeded_ownership():
    source = (ROOT / "scripts/docker-ha-smoke.sh").read_text()
    payload = source.split('cat > "$TMP_DIR/.storage/ha_energy_planner_state" <<\'JSON\'\n', 1)[1]
    return json.loads(payload.split("\nJSON\n", 1)[0])["data"]["ownership"]


def startup_sequence():
    source = (ROOT / "scripts/docker-ha-smoke.sh").read_text()
    config = source.split('cat > "$TMP_DIR/configuration.yaml" <<\'YAML\'\n', 1)[1].split("\nYAML\n", 1)[0]
    actions = next(
        item["actions"] for item in yaml.safe_load(config)["automation"]
        if item["id"] == "ha_energy_planner_service_smoke"
    )
    assertion = next(index for index, step in enumerate(actions) if (
        step.get("action") == "fake_planner_test.assert_startup_climate_restored"
    ))
    return cv.SCRIPT_SCHEMA(actions[:assertion + 1])


@pytest.mark.parametrize("delayed", INPUTS)
@pytest.mark.parametrize("initial", [None, "unknown", "unavailable"])
def test_replan_waits_for_each_late_input(tmp_path, delayed, initial):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        calls = []
        task = None
        ownership = seeded_ownership()
        restore_requested = False

        async def replan(call):
            calls.append(call)

        async def restore(call):
            nonlocal restore_requested
            calls.append(call)
            restore_requested = True

        async def drain(call):
            calls.append(call)
            if restore_requested:
                ownership.clear()
                hass.states.async_set(ACTIVE, "off")

        async def assert_restored(call):
            calls.append(call)
            assert restore_requested and not ownership
            assert hass.states.get(ACTIVE).state == "off"

        async def precheck(call):
            pass

        hass.services.async_register("ha_energy_planner", "replan", replan)
        hass.services.async_register("ha_energy_planner", "restore_safe_state", restore)
        hass.services.async_register("fake_planner_test", "wait_for_planner_idle", drain)
        hass.services.async_register("fake_planner_test", "assert_startup_climate_restored", assert_restored)
        for service in ("assert_export_limit_control", "assert_ev_power_control", "assert_unsafe_arm_rejected"):
            hass.services.async_register("fake_planner_test", service, precheck)
        try:
            assert ownership == {"climate_automations": {"automation.fake_climate_conflict": "on"}}
            hass.states.async_set("binary_sensor.energy_planner_armed", "off")
            hass.states.async_set(ACTIVE, "on")
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
            assert hass.states.get(ACTIVE).state == "on"
            assert ownership
            hass.states.async_set(delayed, *INPUTS[delayed])
            await asyncio.wait_for(task, 3)
            assert [(call.domain, call.service) for call in calls] == [
                ("ha_energy_planner", "replan"), ("fake_planner_test", "wait_for_planner_idle"),
                ("ha_energy_planner", "restore_safe_state"), ("fake_planner_test", "wait_for_planner_idle"),
                ("fake_planner_test", "assert_startup_climate_restored"),
            ]
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_failed_startup_restore_aborts_before_off_assertion(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        ownership = seeded_ownership()
        calls = []

        async def record(call):
            calls.append((call.domain, call.service))

        async def fail_restore(call):
            await record(call)
            raise HomeAssistantError("Synthetic startup restore failed")

        for service in (
            "assert_export_limit_control", "assert_ev_power_control", "assert_unsafe_arm_rejected",
            "wait_for_planner_idle", "assert_startup_climate_restored",
        ):
            hass.services.async_register("fake_planner_test", service, record)
        hass.services.async_register("ha_energy_planner", "replan", record)
        hass.services.async_register("ha_energy_planner", "restore_safe_state", fail_restore)
        try:
            hass.states.async_set("binary_sensor.energy_planner_armed", "off")
            hass.states.async_set(ACTIVE, "on")
            for entity, (state, attributes) in INPUTS.items():
                hass.states.async_set(entity, state, attributes)
            script = Script(hass, startup_sequence(), "Smoke startup", "test")
            with pytest.raises(HomeAssistantError, match="Synthetic startup restore failed"):
                await script.async_run()
            assert calls[-1] == ("ha_energy_planner", "restore_safe_state")
            assert ("fake_planner_test", "assert_startup_climate_restored") not in calls
            assert hass.states.get(ACTIVE).state == "on"
            assert ownership == seeded_ownership()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())
