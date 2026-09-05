"""Upgrade contract for retired planner entities without loading empty platforms."""

from types import SimpleNamespace

from custom_components.ha_energy_planner import entity_registry_migration as migration
from custom_components.ha_energy_planner.const import DOMAIN, PLATFORMS


def test_retired_registry_migration_preserves_unrelated_entities(monkeypatch: object) -> None:
    original = {
        (platform, DOMAIN, f"entry-1_{key}"): f"{platform}.renamed_{key}"
        for platform, keys in migration.RETIRED_ENTITY_KEYS.items()
        for key in keys
    }
    original.update(
        {
            ("sensor", DOMAIN, "entry-1_mode"): "sensor.my_mode",
            ("number", DOMAIN, "entry-2_ev_target_soc"): "number.other_entry",
            ("time", "another_integration", "entry-1_ev_ready_by"): "time.unrelated",
        }
    )
    entities = dict(original)

    class Registry:
        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str | None:
            return entities.get((platform, domain, unique_id))

        def async_remove(self, entity_id: str) -> None:
            for key, value in list(entities.items()):
                if value == entity_id:
                    del entities[key]

    monkeypatch.setattr(migration.er, "async_get", lambda hass: Registry())
    migration.async_migrate_entity_registry(None, SimpleNamespace(entry_id="entry-1"))
    assert set(entities.values()) == {"sensor.my_mode", "number.other_entry", "time.unrelated"}
    migration.async_migrate_entity_registry(None, SimpleNamespace(entry_id="entry-1"))
    assert len(entities) == 3
    assert {"number", "time"}.isdisjoint(PLATFORMS)
