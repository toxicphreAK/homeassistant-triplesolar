"""Schalter — Heizen, Kühlen und Warmwasser freigeben."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TripleSolarConfigEntry
from .coordinator import ModbusCoordinator
from .entity import TripleSolarEntity

SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(key="sh_enable", translation_key="sh_enable"),
    SwitchEntityDescription(key="dhw_enable", translation_key="dhw_enable"),
    SwitchEntityDescription(
        key="sc_enable", translation_key="sc_enable", entity_registry_enabled_default=False
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: TripleSolarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if not isinstance(coordinator, ModbusCoordinator):
        return
    async_add_entities(TsSwitch(coordinator, desc) for desc in SWITCHES)


class TsSwitch(TripleSolarEntity[ModbusCoordinator], SwitchEntity):
    def __init__(self, coordinator: ModbusCoordinator, description: SwitchEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key not in self.coordinator.unsupported

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(self.entity_description.key, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(self.entity_description.key, 0)
