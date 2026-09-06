"""Bounded availability log details from known issue codes and entity mappings."""

from __future__ import annotations

import re
from typing import Any

from . import const

_ENTITY_KEYS = frozenset(
    value for name, value in vars(const).items()
    if name.startswith("CONF_") and isinstance(value, str) and value.endswith(("_entity", "_entities"))
)
_ISSUE_SUFFIXES = ("unavailable", "not_found", "missing", "stale", "non_numeric", "not_configured")
_DISCOVERY_KEYS = {
    "daikin_climate_unavailable": ("daikin_climate_entity",),
    "climate_automation_unavailable": ("climate_automation_entities",),
    "climate_zone_unavailable": ("climate_zone_entities",),
    "main_climate_target_unavailable": ("daikin_climate_entity",),
    "climate_zone_target_unavailable": ("climate_zone_entities",),
    "climate_manual_override_unavailable": ("climate_manual_override_entity",),
    "climate_scheduler_guard_unavailable": (
        "climate_change_from_scheduler_entity", "climate_scheduler_guard_timer_entity",
    ),
    "ev_start_control_unavailable": ("ev_charger_start_entity", "ev_charger_entity"),
    "ev_stop_control_unavailable": ("ev_charger_stop_entity", "ev_charger_entity"),
}


def availability_details(issues: list[str], entry_data: dict[str, Any]) -> list[str]:
    """Never put arbitrary issue text, configuration values or payloads in logs."""
    details: set[str] = set()
    for issue in issues:
        if issue.startswith("advisory_") or not any(part in issue for part in _ISSUE_SUFFIXES):
            continue
        keys = _DISCOVERY_KEYS.get(issue)
        if keys is None:
            keys = tuple(key for key in sorted(_ENTITY_KEYS) if issue in {
                f"{key}_{suffix}" for suffix in _ISSUE_SUFFIXES
            })
        if not keys:
            details.add("issue=required_evidence_missing entities=unidentified")
            continue
        entities: set[str] = set()
        for key in keys:
            value = entry_data.get(key, [])
            values = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
            for entity in values:
                if isinstance(entity, str) and re.fullmatch(r"[a-z_]+\.[a-z0-9_]{1,100}", entity.strip()):
                    entities.add(entity.strip())
        details.add(f"issue={issue} entities={','.join(sorted(entities)[:4]) or 'not_configured'}")
    return sorted(details)[:20]
