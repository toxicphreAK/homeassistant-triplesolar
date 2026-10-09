"""Triple Solar PVT-Wärmepumpe 5.0 — lokal per Modbus TLS/TCP oder über myTripleSolar."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import CloudClient
from .const import CONF_CONNECTION, CONNECTION_CLOUD
from .coordinator import CloudCoordinator, ModbusCoordinator, async_create_modbus_client

MODBUS_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
# Die Cloud wird nur gelesen: Schreibzugriffe über eine undokumentierte
# Schnittstelle sind ein Risiko, das der lokale Weg nicht hat.
CLOUD_PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

type TripleSolarConfigEntry = ConfigEntry[ModbusCoordinator | CloudCoordinator]


def platforms_for(entry: ConfigEntry) -> list[Platform]:
    return CLOUD_PLATFORMS if entry.data[CONF_CONNECTION] == CONNECTION_CLOUD else MODBUS_PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: TripleSolarConfigEntry) -> bool:
    coordinator: ModbusCoordinator | CloudCoordinator
    if entry.data[CONF_CONNECTION] == CONNECTION_CLOUD:
        client = CloudClient(async_get_clientsession(hass), entry.data["email"], entry.data["password"])
        coordinator = CloudCoordinator(hass, entry, client)
    else:
        # Optionen (Unit-ID) haben Vorrang vor den Daten der Ersteinrichtung.
        modbus = await async_create_modbus_client(hass, {**entry.data, **entry.options})
        coordinator = ModbusCoordinator(hass, entry, modbus)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    await hass.config_entries.async_forward_entry_setups(entry, platforms_for(entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TripleSolarConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms_for(entry))
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_reload_on_options(hass: HomeAssistant, entry: TripleSolarConfigEntry) -> None:
    # Nur bei geänderten Optionen neu laden — die Adress-Aktualisierung
    # nach einem DHCP-Umzug ändert die Daten und ist bereits wirksam.
    if dict(entry.options) != entry.runtime_data.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)
