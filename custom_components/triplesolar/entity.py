"""Gemeinsame Basis der Entitäten."""

from __future__ import annotations

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION,
    CONF_HEAT_PUMP_ID,
    CONF_HOSTNAME,
    CONNECTION_CLOUD,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import CloudCoordinator, ModbusCoordinator


def device_identifier(coordinator: ModbusCoordinator | CloudCoordinator) -> str:
    """Stabile Kennung der Anlage je Verbindung.

    Modbus TLS: der gekoppelte Hostname (``hpcb-xxxxxx.local``) — er bleibt,
    wenn die IP wechselt. Modbus TCP: Host. Cloud: die Wärmepumpen-ID.
    Lokal und Cloud werden bewusst NICHT zu einem Gerät verschmolzen: es
    gibt keine dokumentierte Zuordnung zwischen Cloud-ID und Hostname.
    """
    data = coordinator.config_entry.data
    if data[CONF_CONNECTION] == CONNECTION_CLOUD:
        return f"cloud-{data[CONF_HEAT_PUMP_ID]}"
    return data.get(CONF_HOSTNAME) or data[CONF_HOST]


class TripleSolarEntity[T: (ModbusCoordinator, CloudCoordinator)](CoordinatorEntity[T]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: T, key: str) -> None:
        super().__init__(coordinator)
        ident = device_identifier(coordinator)
        self._attr_unique_id = f"{ident}-{key}"
        is_cloud = isinstance(coordinator, CloudCoordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ident)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"{MODEL} (Cloud)" if is_cloud else MODEL,
            configuration_url="https://my.triplesolar.eu",
        )
