"""Switch platform for Energy Planner."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_AI_ENABLED,
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_DRY_RUN,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONNECTED_HELPER,
    CONF_EV_CONTROL_ENABLED,
    CONF_EV_KEEP_CHARGER_ON,
    CONF_EV_LOW_PRICE_CHARGING_ENABLED,
    CONF_PLANNER_ENABLED,
)
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity, async_add_planner_entities
from .type_defs import EnergyPlannerConfigEntry


@dataclass(frozen=True, kw_only=True)
class PlannerSwitchDescription(SwitchEntityDescription):
    """Switch description."""

    option_key: str
    default: bool
    reload_required: bool = False


SWITCHES: tuple[PlannerSwitchDescription, ...] = (
    PlannerSwitchDescription(
        key="enabled",
        translation_key="enabled",
        icon="mdi:power",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_PLANNER_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="dry_run",
        translation_key="dry_run",
        icon="mdi:test-tube",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_DRY_RUN,
        default=True,
    ),
    PlannerSwitchDescription(
        key="ai_enabled",
        translation_key="ai_enabled",
        icon="mdi:robot",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_AI_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="ev_control_enabled",
        translation_key="ev_control_enabled",
        icon="mdi:ev-station",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_EV_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="ev_connected_helper",
        translation_key="ev_connected_helper",
        icon="mdi:ev-plug-type2",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_EV_CONNECTED_HELPER,
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
    PlannerSwitchDescription(
        key="climate_control_enabled",
        translation_key="climate_control_enabled",
        icon="mdi:thermostat-auto",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_CLIMATE_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="enphase_control_enabled",
        translation_key="enphase_control_enabled",
        icon="mdi:home-battery-outline",
        entity_category=EntityCategory.CONFIG,
        option_key=CONF_ENPHASE_CONTROL_ENABLED,
        default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyPlannerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator: EnergyPlannerCoordinator = entry.runtime_data
    async_add_planner_entities(
        entry, async_add_entities, (PlannerSwitch(coordinator, description) for description in SWITCHES)
    )


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
        return bool(self.coordinator.options.get(self.entity_description.option_key, self.entity_description.default))

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn switch on."""
        await self._async_set_option(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn switch off."""
        await self._async_set_option(False)

    async def _async_set_option(self, value: bool) -> None:
        option_key = self.entity_description.option_key
        if option_key == CONF_EV_CONNECTED_HELPER:
            await self.coordinator.async_set_ev_connected_helper(value)
            self.async_write_ha_state()
            return
        if option_key == CONF_EV_KEEP_CHARGER_ON:
            await self.coordinator.async_set_ev_keep_charger_on(value)
            self.async_write_ha_state()
            return
        options = self.coordinator.options
        options[option_key] = value
        self.coordinator.hass.config_entries.async_update_entry(self.coordinator.entry, options=options)
        self.async_write_ha_state()
        await self.coordinator.async_handle_options_update()
