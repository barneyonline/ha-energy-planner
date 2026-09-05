"""Switch platform for Energy Planner."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CLIMATE_CONTROL_ENABLED,
    CONF_ENPHASE_CONTROL_ENABLED,
    CONF_EV_CONTROL_ENABLED,
)
from .coordinator import EnergyPlannerCoordinator
from .entity import EnergyPlannerEntity
from .type_defs import EnergyPlannerConfigEntry

# Coordinator locks serialize commands; allow stop controls to dispatch immediately.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PlannerSwitchDescription(SwitchEntityDescription):
    """Switch description."""

    option_key: str | None
    default: bool
    active_control: bool = False


SWITCHES: tuple[PlannerSwitchDescription, ...] = (
    PlannerSwitchDescription(
        key="active_control",
        translation_key="active_control",
        option_key=None,
        default=False,
        active_control=True,
    ),
    PlannerSwitchDescription(
        key="climate_control",
        translation_key="climate_control",
        option_key=CONF_CLIMATE_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="ev_control",
        translation_key="ev_control",
        option_key=CONF_EV_CONTROL_ENABLED,
        default=False,
    ),
    PlannerSwitchDescription(
        key="enphase_control",
        translation_key="enphase_control",
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
    async_add_entities(PlannerSwitch(coordinator, description) for description in SWITCHES)


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
            return bool(
                getattr(
                    self.coordinator,
                    "automatic_control_requested",
                    self.coordinator.active_control,
                )
            )
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
        assert option_key in {
            CONF_CLIMATE_CONTROL_ENABLED,
            CONF_EV_CONTROL_ENABLED,
            CONF_ENPHASE_CONTROL_ENABLED,
        }
        await self.coordinator.async_set_device_control(option_key, value)
        self.async_write_ha_state()
