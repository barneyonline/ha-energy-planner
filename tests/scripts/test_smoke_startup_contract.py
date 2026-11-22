"""HA-independent smoke scenario ordering and failure diagnostics.

Run directly with ``python3 -I tests/scripts/test_smoke_startup_contract.py -v``
to avoid the Home Assistant import in tests/conftest.py. Only YAML parsing and
the extracted helpers run; the smoke shell script is never executed.
"""

from __future__ import annotations

import ast
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = "binary_sensor.energy_planner_climate_control_active"
CONFLICT = "automation.fake_climate_conflict"


def heredoc(target, delimiter):
    source = (ROOT / "scripts/docker-ha-smoke.sh").read_text()
    return source.split(f'cat > "$TMP_DIR/{target}" <<\'{delimiter}\'\n', 1)[1].split(
        f"\n{delimiter}\n", 1,
    )[0]


def smoke_actions():
    config = yaml.safe_load(heredoc("configuration.yaml", "YAML"))
    return next(
        item["actions"] for item in config["automation"]
        if item["id"] == "ha_energy_planner_service_smoke"
    )


def seeded_ownership():
    return json.loads(heredoc(".storage/ha_energy_planner_state", "JSON"))["data"]["ownership"]


def smoke_helper(name, hass):
    # Compile only the requested helper. No helper-module imports or setup run.
    tree = ast.parse(heredoc("custom_components/fake_planner_test/__init__.py", "PY"))
    setup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef))
    helper = next(node for node in setup.body if (
        isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ))
    namespace = {"hass": hass, "asyncio": asyncio, "json": json}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "smoke helper", "exec"), namespace)
    return namespace[name]


class AIExplanationContractTests(unittest.TestCase):
    def explanation_wait(self, writer):
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: [
            SimpleNamespace(runtime_data=SimpleNamespace(_ai_advice_task=writer)),
        ]))
        return smoke_helper("wait_for_ai_explanation", hass)(None)

    def test_explain_waits_before_any_further_refresh(self):
        actions = smoke_actions()
        explain = actions.index({
            "action": "button.press", "data": {"entity_id": "button.energy_planner_explain"},
        })
        self.assertEqual([step["action"] for step in actions[explain + 1:explain + 3]], [
            "fake_planner_test.wait_for_ai_explanation",
            "ha_energy_planner.replan",
        ])

    def test_explanation_waits_for_background_write(self):
        async def scenario():
            writer = asyncio.get_running_loop().create_future()
            async with asyncio.TaskGroup() as tasks:
                waiter = tasks.create_task(self.explanation_wait(writer))
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
                writer.set_result(None)
                await asyncio.wait_for(waiter, timeout=1)

        asyncio.run(scenario())

    def test_explanation_wait_times_out_with_write_still_pending(self):
        async def scenario():
            writer = asyncio.get_running_loop().create_future()
            with patch.object(asyncio, "wait", return_value=(set(), {writer})) as wait:
                with self.assertRaisesRegex(TimeoutError, "did not complete within 30 seconds"):
                    await self.explanation_wait(writer)
            wait.assert_awaited_once_with({writer}, timeout=30)
            self.assertFalse(writer.done())

        asyncio.run(scenario())

    def test_cancelled_explanation_fails_the_wait(self):
        async def scenario():
            writer = asyncio.get_running_loop().create_future()
            writer.cancel()
            with self.assertRaisesRegex(RuntimeError, "cancelled before completion"):
                await self.explanation_wait(writer)

        asyncio.run(scenario())


class StartupContractTests(unittest.TestCase):
    def test_first_off_assertion_follows_input_readiness_restore_and_drain(self):
        actions = smoke_actions()
        assertion = next(
            i for i, step in enumerate(actions)
            if step.get("action") == "fake_planner_test.assert_startup_climate_restored"
        )
        prefix = actions[:assertion + 1]
        readiness = next(
            i for i, step in enumerate(prefix)
            if "sensor.smoke_import_price_forecast" in step.get("wait_template", "")
        )
        self.assertFalse(any(step.get("entity_id") == ACTIVE for step in prefix))
        self.assertIn("automation.fake_climate_conflict", prefix[readiness]["wait_template"])
        self.assertFalse(prefix[readiness]["continue_on_timeout"])
        self.assertEqual([step["action"] for step in prefix[readiness + 1:]], [
            "ha_energy_planner.replan",
            "fake_planner_test.wait_for_planner_idle",
            "ha_energy_planner.restore_safe_state",
            "fake_planner_test.wait_for_planner_idle",
            "fake_planner_test.assert_startup_climate_restored",
        ])
        self.assertEqual(prefix[readiness - 1], {
            "condition": "state", "entity_id": "binary_sensor.energy_planner_armed", "state": "off",
        })

    def assertion(self, active, conflict="on", *, ownership=None, loaded=True):
        states = {ACTIVE: active, CONFLICT: conflict, "binary_sensor.energy_planner_armed": "off"}
        entries = [SimpleNamespace(
            entry_id="smoke", runtime_data=SimpleNamespace(store=SimpleNamespace(
                data={"ownership": ownership if ownership is not None else {}},
            )),
        )] if loaded else []
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda entity: (
                SimpleNamespace(state=states[entity]) if states.get(entity) is not None else None
            )),
            config_entries=SimpleNamespace(async_entries=lambda domain: entries),
        )
        return smoke_helper("assert_startup_climate_restored", hass)(None)

    def test_seeded_legacy_ownership_is_reported_when_sensor_stays_on(self):
        ownership = seeded_ownership()
        self.assertEqual(ownership, {"climate_automations": {CONFLICT: "on"}})
        with self.assertRaisesRegex(RuntimeError, "Startup climate restoration failed") as failure:
            asyncio.run(self.assertion("on", ownership=ownership))
        report = json.loads(str(failure.exception).split(": ", 1)[1])
        self.assertEqual(report["states"][ACTIVE], "on")
        self.assertEqual(report["states"]["binary_sensor.energy_planner_armed"], "off")
        self.assertEqual(report["ownership"]["smoke"], ownership)

    def test_restored_baseline_passes(self):
        asyncio.run(self.assertion("off"))

    def test_unrestored_conflict_automation_fails(self):
        with self.assertRaisesRegex(RuntimeError, '"automation.fake_climate_conflict": "off"'):
            asyncio.run(self.assertion("off", conflict="off"))

    def test_missing_or_unavailable_sensor_fails_with_actual_state(self):
        for state in (None, "unknown", "unavailable"):
            with self.subTest(state=state), self.assertRaises(RuntimeError) as failure:
                asyncio.run(self.assertion(state))
            report = json.loads(str(failure.exception).split(": ", 1)[1])
            self.assertEqual(report["states"][ACTIVE], state)

    def test_missing_coordinator_fails(self):
        with self.assertRaisesRegex(RuntimeError, '"ownership": {}'):
            asyncio.run(self.assertion("off", loaded=False))


if __name__ == "__main__":
    unittest.main()
