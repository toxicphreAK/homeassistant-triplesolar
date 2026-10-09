"""Binärsensoren — Anforderungen, Ventile, Störung."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TripleSolarConfigEntry
from .coordinator import CloudCoordinator, ModbusCoordinator
from .entity import TripleSolarEntity
from .registers import HeatPumpState


@dataclass(frozen=True, kw_only=True)
class TsBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]


def _flag(key: str) -> Callable[[dict[str, Any]], bool | None]:
    return lambda data: None if data.get(key) is None else bool(data[key])


def _modbus_descriptions(modules: int) -> list[TsBinaryDescription]:
    items = [
        TsBinaryDescription(
            key="fault",
            translation_key="fault",
            device_class=BinarySensorDeviceClass.PROBLEM,
            value_fn=lambda d: (
                None if d.get("heat_pump_state") is None else d["heat_pump_state"] is HeatPumpState.FAULT
            ),
        ),
        TsBinaryDescription(key="dhw_demand", translation_key="dhw_demand", value_fn=_flag("dhw_demand")),
        TsBinaryDescription(key="dhw_valve", translation_key="dhw_valve", value_fn=_flag("dhw_valve")),
        TsBinaryDescription(key="sh_demand", translation_key="sh_demand", value_fn=_flag("sh_demand")),
        TsBinaryDescription(
            key="sc_demand",
            translation_key="sc_demand",
            value_fn=_flag("sc_demand"),
            entity_registry_enabled_default=False,
        ),
    ]
    for n in range(1, modules + 1):
        items.append(
            TsBinaryDescription(
                key=f"m{n}_cooling_valve",
                translation_key="cooling_valve",
                translation_placeholders={"module": str(n)},
                value_fn=_flag(f"m{n}_cooling_valve"),
                entity_registry_enabled_default=False,
            )
        )
    return items


def _status(*keys: str) -> Callable[[dict[str, Any]], bool | None]:
    def get(data: dict[str, Any]) -> bool | None:
        node: Any = data.get("status")
        for key in keys:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return None if node is None else bool(node)

    return get


CLOUD_BINARY: tuple[TsBinaryDescription, ...] = (
    TsBinaryDescription(
        key="heating_active",
        translation_key="heating_active",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_status("controller", "spaceConditioning", "isHeatingActive"),
    ),
    TsBinaryDescription(
        key="sh_demand",
        translation_key="sh_demand",
        value_fn=_status("controller", "spaceConditioning", "hasHeatingDemand"),
    ),
    TsBinaryDescription(
        key="dhw_active",
        translation_key="dhw_active",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_status("controller", "domesticHotWater", "isActive"),
    ),
    TsBinaryDescription(
        key="dhw_demand",
        translation_key="dhw_demand",
        value_fn=_status("controller", "domesticHotWater", "hasDemand"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: TripleSolarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    descriptions = (
        CLOUD_BINARY
        if isinstance(coordinator, CloudCoordinator)
        else _modbus_descriptions(coordinator.modules)
    )
    async_add_entities(TsBinarySensor(coordinator, desc) for desc in descriptions)


class TsBinarySensor(TripleSolarEntity, BinarySensorEntity):
    entity_description: TsBinaryDescription

    def __init__(
        self, coordinator: ModbusCoordinator | CloudCoordinator, description: TsBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)
