"""Zahlenwerte — Warmwasserprofile und Vorgaben der Modbus-Steuerung."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TripleSolarConfigEntry
from .coordinator import ModbusCoordinator
from .entity import TripleSolarEntity


@dataclass(frozen=True, kw_only=True)
class TsNumberDescription(NumberEntityDescription):
    """Bereich und Schritt kommen aus der Registertabelle, nicht von hier."""


def _dhw_profile_numbers() -> list[TsNumberDescription]:
    items: list[TsNumberDescription] = []
    for profile in ("eco", "normal", "comfort"):
        ph = {"profile": profile}
        items += [
            TsNumberDescription(
                key=f"dhw_{profile}_setpoint",
                translation_key="dhw_profile_setpoint",
                translation_placeholders=ph,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                device_class=NumberDeviceClass.TEMPERATURE,
                native_step=0.5,
                entity_category=EntityCategory.CONFIG,
            ),
            TsNumberDescription(
                key=f"dhw_{profile}_hysteresis",
                translation_key="dhw_profile_hysteresis",
                translation_placeholders=ph,
                native_unit_of_measurement=UnitOfTemperature.KELVIN,
                native_step=0.5,
                entity_category=EntityCategory.CONFIG,
            ),
            TsNumberDescription(
                key=f"dhw_{profile}_modulation",
                translation_key="dhw_profile_modulation",
                translation_placeholders=ph,
                native_unit_of_measurement=PERCENTAGE,
                native_step=1,
                entity_category=EntityCategory.CONFIG,
            ),
        ]
    return items


NUMBERS: tuple[TsNumberDescription, ...] = (
    *_dhw_profile_numbers(),
    # Externe Modbus-Steuerung (Register-Gruppe „Control"). Wirksam nur mit
    # Steuerungstyp „Modbus-Steuerung"; ob das Leistungsbudget auch in
    # anderen Steuerungstypen greift, sagt die Doku nicht → aus, bis an der
    # Anlage geprüft (docs/modbus-api.md, „Offene Punkte").
    TsNumberDescription(
        key="control_modulation",
        translation_key="control_modulation",
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    TsNumberDescription(
        key="control_power_budget",
        translation_key="control_power_budget",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=NumberDeviceClass.POWER,
        native_step=100,
        mode=NumberMode.BOX,
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
    async_add_entities(TsNumber(coordinator, desc) for desc in NUMBERS)


class TsNumber(TripleSolarEntity[ModbusCoordinator], NumberEntity):
    entity_description: TsNumberDescription

    def __init__(self, coordinator: ModbusCoordinator, description: TsNumberDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        reg = coordinator.registers[description.key]
        self._attr_native_min_value = reg.minimum
        self._attr_native_max_value = reg.maximum

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key not in self.coordinator.unsupported

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get(self.entity_description.key)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(self.entity_description.key, value)
