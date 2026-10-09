"""Auswahl — Leistungsprofile und Betriebsart der Modbus-Steuerung.

Profil „blocked" (-1) ist die Modbus-Entsprechung des SG-Ready-Sperrbetriebs,
„comfort" (2) die des bevorzugten Betriebs (Regelungslogik 4.4). Damit lässt
sich PV-Überschuss in Warmwasser schieben, ohne SG-Ready-Kontakte zu verdrahten.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TripleSolarConfigEntry
from .coordinator import ModbusCoordinator
from .entity import TripleSolarEntity
from .registers import OperatingMode, Profile


@dataclass(frozen=True, kw_only=True)
class TsSelectDescription(SelectEntityDescription):
    enum: type[IntEnum]


SELECTS: tuple[TsSelectDescription, ...] = (
    TsSelectDescription(key="dhw_profile_set", translation_key="dhw_profile_set", enum=Profile),
    TsSelectDescription(key="sh_profile_set", translation_key="sh_profile_set", enum=Profile),
    TsSelectDescription(
        key="sc_profile_set",
        translation_key="sc_profile_set",
        enum=Profile,
        entity_registry_enabled_default=False,
    ),
    # Wirkt nur mit Steuerungstyp „Modbus-Steuerung" in myTripleSolar und
    # übersteuert dann Heizkurve/Thermostat → bewusst aus.
    TsSelectDescription(
        key="control_operating_mode",
        translation_key="control_operating_mode",
        enum=OperatingMode,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: TripleSolarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if not isinstance(coordinator, ModbusCoordinator):
        return
    async_add_entities(TsSelect(coordinator, desc) for desc in SELECTS)


class TsSelect(TripleSolarEntity[ModbusCoordinator], SelectEntity):
    entity_description: TsSelectDescription

    def __init__(self, coordinator: ModbusCoordinator, description: TsSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = [m.name.lower() for m in description.enum]

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key not in self.coordinator.unsupported

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value.name.lower() if isinstance(value, IntEnum) else None

    async def async_select_option(self, option: str) -> None:
        member = self.entity_description.enum[option.upper()]
        await self.coordinator.async_write(self.entity_description.key, member)
