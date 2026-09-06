"""Safe actionable availability details."""

from custom_components.ha_energy_planner.availability import availability_details


def test_known_input_and_discovery_issues_resolve_bounded_entity_mappings() -> None:
    assert sorted(availability_details([
        "household_load_entity_unavailable", "main_climate_target_unavailable",
        "climate_scheduler_guard_unavailable", "pv_forecast_entity_stale",
    ], {
        "household_load_entity": " sensor.house ",
        "daikin_climate_entity": "climate.main",
        "climate_change_from_scheduler_entity": "input_boolean.guard",
        "climate_scheduler_guard_timer_entity": "timer.guard",
        "pv_forecast_entity": "sensor.pv",
    }).values()) == [
        "issue=climate_scheduler_guard_unavailable entities=input_boolean.guard,timer.guard",
        "issue=household_load_entity_unavailable entities=sensor.house",
        "issue=main_climate_target_unavailable entities=climate.main",
        "issue=pv_forecast_entity_stale entities=sensor.pv",
    ]


def test_log_details_exclude_arbitrary_issues_values_and_advisories() -> None:
    details = sorted(availability_details([
        "private_entity_unavailable_token=secret", "input_health_unsafe",
        "advisory_household_load_entity_forecast_stale", "household_load_model_fallback_active",
        "household_load_entity_unavailable", "main_climate_target_unavailable",
        "climate_zone_unavailable", "climate_automation_unavailable",
    ], {
        "household_load_entity": "https://secret@example.com, token=secret",
        "daikin_climate_entity": 42,
        "climate_zone_entities": [None, "bad value", *[f"switch.zone_{i}" for i in range(8)]],
    }).values())
    assert details == [
        "issue=climate_automation_unavailable entities=not_configured",
        "issue=climate_zone_unavailable entities=switch.zone_0,switch.zone_1,switch.zone_2,switch.zone_3",
        "issue=household_load_entity_unavailable entities=not_configured",
        "issue=main_climate_target_unavailable entities=not_configured",
        "issue=required_evidence_missing entities=unidentified",
    ]
    assert "secret" not in str(details)


def test_availability_identity_groups_reasons_and_discovery_aliases() -> None:
    entries = {"daikin_climate_entity": "climate.main"}
    first = availability_details(["daikin_climate_unavailable"], entries)
    multiple = availability_details(["main_climate_target_unavailable", "daikin_climate_entity_stale"], entries)
    assert first.keys() == multiple.keys()
    assert len(multiple) == 1
    assert "main_climate_target_unavailable" in next(iter(multiple.values()))
    assert "daikin_climate_entity_stale" in next(iter(multiple.values()))
