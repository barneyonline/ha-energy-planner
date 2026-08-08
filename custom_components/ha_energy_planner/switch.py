"""Switch platform for Energy Planner."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    DOMAIN,
)
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .type_defs import EnergyPlannerConfigEntry


@dataclass(frozen=True, kw_only=True)
class PlannerSwitchDescription(SwitchEntityDescription):
    """Switch description."""

    option_key: str | None
    default: bool
    active_control: bool = False
    reload_required: bool = False


SWITCHES: tuple[PlannerSwitchDescription, ...] = (
    PlannerSwitchDescription(
        key="active_control",
        translation_key="active_control",
        icon="mdi:home-automation",
        option_key=None,
        default=False,
        active_control=True,
    ),
    PlannerSwitchDescription(
        key="climate_control",
        translation_key="climate_control",
        icon="mdi:thermostat-auto",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_CLIMATE_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="ev_control",
        translation_key="ev_control",
        icon="mdi:ev-station",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_EV_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="enphase_control",
        translation_key="enphase_control",
        icon="mdi:home-battery-outline",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_ENPHASE_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="ev_keep_charger_on",
        translation_key="ev_keep_charger_on",
        icon="mdi:car-defrost-front",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_EV_KEEP_CHARGER_ON,
        default=False,
    ),
    PlannerSwitchDescription(
        key="ev_opportunistic_charging",
        translation_key="ev_opportunistic_charging",
        icon="mdi:cash-clock",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_EV_LOW_PRICE_CHARGING_ENABLED,
        default=False,
    ),
)

_RETIRED_CONTROL_SWITCH_KEYS = (
    "enabled",
    "dry_run",
    "ai_enabled",
    "ev_control_enabled",
    "climate_control_enabled",
    "enphase_control_enabled",
    "ev_connected_helper",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    _remove_retired_control_switches(hass, entry)
    async_add_planner_entities(
        entry, async_add_entities, (PlannerSwitch(coordinator, description) for description in SWITCHES)
    )


def _remove_retired_control_switches(hass: HomeAssistant, entry: EnergyPlannerConfigEntry) -> None:
    """Remove obsolete activation switches from the entity registry on upgrade."""
    registry = er.async_get(hass)
    for key in _RETIRED_CONTROL_SWITCH_KEYS:
        entity_id = registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{key}")
        if entity_id is not None:
            registry.async_remove(entity_id)


class PlannerSwitch(EnergyPlannerEntity, SwitchEntity):
    """Planner option switch."""

    entity_description: PlannerSwitchDescription

    def __init__(
        self,
        coordinator: EnergyPlannerCoordinator,
        description: PlannerSwitchDescription,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return switch state."""
        if self.entity_description.active_control:
            return bool(self.coordinator.active_control)
        assert self.entity_description.option_key is not None
        return bool(self.coordinator.options.get(self.entity_description.option_key, self.entity_description.default))

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn switch on."""
        if self.entity_description.active_control:
            await self.coordinator.async_set_active_control(True)
            self.async_write_ha_state()
            return
        await self._async_set_option(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn switch off."""
        if self.entity_description.active_control:
            await self.coordinator.async_set_active_control(False)
            self.async_write_ha_state()
            return
        await self._async_set_option(False)

    async def _async_set_option(self, value: bool) -> None:
        option_key = self.entity_description.option_key
        assert option_key is not None
        if option_key == CONF_EV_KEEP_CHARGER_ON:
            await self.coordinator.async_set_ev_keep_charger_on(value)
            self.async_write_ha_state()
            return
        if option_key in {
            CONF_CLIMATE_CONTROL_ENABLED,
            CONF_EV_CONTROL_ENABLED,
            CONF_ENPHASE_CONTROL_ENABLED,
        }:
            await self.coordinator.async_set_device_control(option_key, value)
            self.async_write_ha_state()
            return
        options = self.coordinator.options
        options[option_key] = value
        self.coordinator.hass.config_entries.async_update_entry(self.coordinator.entry, options=options)
        self.async_write_ha_state()
        await self.coordinator.async_handle_options_update()
