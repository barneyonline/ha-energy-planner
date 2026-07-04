"""Tests for entity translation and icon metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.ha_energy_planner.binary_sensor import BINARY_SENSORS
from custom_components.ha_energy_planner.button import BUTTONS
from custom_components.ha_energy_planner.sensor import SENSORS
from custom_components.ha_energy_planner.switch import SWITCHES

ENTITY_DESCRIPTIONS = {
    "binary_sensor": BINARY_SENSORS,
    "button": BUTTONS,
    "sensor": SENSORS,
    "switch": SWITCHES,
}


def test_all_entity_descriptions_have_icons() -> None:
    """Every integration-created entity should have a visible icon."""
    for platform, descriptions in ENTITY_DESCRIPTIONS.items():
        for description in descriptions:
            assert isinstance(description.icon, str), f"{platform}.{description.key} is missing an icon"
            assert description.icon.startswith("mdi:"), f"{platform}.{description.key} icon should be an mdi icon"


def test_all_entity_descriptions_have_translated_names() -> None:
    """Every integration-created entity should have a non-empty translated name."""
    for translations_path in (
        _integration_path("strings.json"),
        *_integration_path("translations").glob("en*.json"),
    ):
        translations = _load_json(translations_path)
        entity_translations = translations.get("entity", {})

        for platform, descriptions in ENTITY_DESCRIPTIONS.items():
            platform_translations = entity_translations.get(platform, {})
            for description in descriptions:
                translation_key = description.translation_key
                assert isinstance(translation_key, str), f"{platform}.{description.key} is missing a translation key"
                translated = platform_translations.get(translation_key)
                assert isinstance(translated, dict), (
                    f"{platform}.{translation_key} is missing from {translations_path.name}"
                )
                name = translated.get("name")
                assert isinstance(name, str), f"{platform}.{translation_key} name should be a string"
                assert name.strip(), f"{platform}.{translation_key} name should not be empty"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _integration_path(*parts: str) -> Path:
    return Path(__file__).parents[1] / "custom_components" / "ha_energy_planner" / Path(*parts)
