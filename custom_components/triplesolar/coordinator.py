"""Datenabruf: ein Coordinator je Verbindung (Modbus lokal oder Cloud)."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud import CloudAuthError, CloudClient, CloudError
from .const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CONNECTION,
    CONF_HEAT_PUMP_ID,
    CONF_HOSTNAME,
    CONF_MODULES,
    CONF_ROOT_CA,
    CONF_UNIT_ID,
    CONNECTION_MODBUS_TLS,
    DEFAULT_CLOUD_SCAN_S,
    DEFAULT_MODBUS_SCAN_S,
    DEFAULT_MODULES,
    DEFAULT_UNIT_ID,
    DOMAIN,
    MODBUS_DISCOVERY_TIMEOUT_S,
)
from .modbus import ModbusClient, ModbusError, ModbusExceptionResponse, TlsCredentials, build_ssl_context
from .pairing import MODBUS_TLS_SERVICE_TYPE, PairingError, discover_service
from .registers import ReadBlock, Register, RegisterType, plan_reads, registers_by_key

_LOGGER = logging.getLogger(__name__)

# Modbus-Exception 2: Adresse unbekannt. Eine Firmware vor 2026.09.01.0
# oder eine Anlage ohne optionales Zubehör kann einzelne Register nicht
# kennen; die werden einmal einzeln geprüft und danach ausgelassen.
ILLEGAL_DATA_ADDRESS = 2

# Frühestens so oft wird nach einem Verbindungsfehler per mDNS nach der
# (per DHCP vielleicht umgezogenen) Adresse der gekoppelten Anlage gesucht.
REDISCOVERY_INTERVAL_S = 300


def credentials_from_entry(data: dict[str, Any]) -> TlsCredentials:
    return TlsCredentials(
        root_ca_pem=data[CONF_ROOT_CA],
        client_cert_pem=data[CONF_CLIENT_CERT],
        client_key_pem=data[CONF_CLIENT_KEY],
        device_hostname=data[CONF_HOSTNAME],
    )


async def async_create_modbus_client(hass: HomeAssistant, data: dict[str, Any]) -> ModbusClient:
    """Client aus Entry-Daten; der TLS-Kontext wird im Executor gebaut."""
    ssl_context = None
    expected = None
    if data[CONF_CONNECTION] == CONNECTION_MODBUS_TLS:
        creds = credentials_from_entry(data)
        ssl_context = await hass.async_add_executor_job(build_ssl_context, creds)
        expected = creds.device_hostname
    return ModbusClient(
        data[CONF_HOST],
        data[CONF_PORT],
        unit_id=data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
        ssl_context=ssl_context,
        expected_hostname=expected,
    )


class ModbusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Liest alle dokumentierten Register, schreibt Holding-Register."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ModbusClient) -> None:
        scan = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_S)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} modbus {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=scan),
        )
        self.client = client
        self.options_snapshot = dict(entry.options)
        self.modules: int = entry.options.get(CONF_MODULES, entry.data.get(CONF_MODULES, DEFAULT_MODULES))
        self.registers: dict[str, Register] = registers_by_key(self.modules)
        self.unsupported: set[str] = set()
        self._blocks: list[ReadBlock] = plan_reads(list(self.registers.values()))
        self._last_rediscovery = 0.0

    async def _async_update_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            for block in self._blocks:
                data.update(await self._read_block(block))
        except ModbusError as exc:
            await self._async_rediscover()
            raise UpdateFailed(str(exc)) from exc
        return data

    async def _async_rediscover(self) -> None:
        """Neue IP der gekoppelten Anlage per mDNS suchen (nur Modbus TLS).

        Die Kopplung bindet an den Hostnamen, nicht an die IP. Ändert der
        Router die Adresse, findet die Anlage sich unter ``_mbap-s._tcp``
        mit demselben Hostnamen wieder; die neue Adresse wird im Entry
        gespeichert, damit sie den nächsten Neustart überlebt.
        """
        entry = self.config_entry
        if entry.data[CONF_CONNECTION] != CONNECTION_MODBUS_TLS:
            return
        now = time.monotonic()
        if now - self._last_rediscovery < REDISCOVERY_INTERVAL_S:
            return
        self._last_rediscovery = now
        try:
            aiozc = await zeroconf.async_get_async_instance(self.hass)
            found = await discover_service(
                aiozc,
                MODBUS_TLS_SERVICE_TYPE,
                timeout=MODBUS_DISCOVERY_TIMEOUT_S,
                expected_hostname=entry.data[CONF_HOSTNAME],
            )
        except PairingError as exc:
            _LOGGER.debug("Wärmepumpe per mDNS nicht gefunden: %s", exc)
            return
        if (found.address, found.port) == (self.client.host, self.client.port):
            return
        _LOGGER.info(
            "Wärmepumpe %s ist umgezogen: %s:%s → %s:%s",
            found.hostname,
            self.client.host,
            self.client.port,
            found.address,
            found.port,
        )
        await self.client.close()
        self.client.host, self.client.port = found.address, found.port
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: found.address, CONF_PORT: found.port}
        )

    async def _read_block(self, block: ReadBlock) -> dict[str, Any]:
        keys = [k for k in block.keys if k not in self.unsupported]
        if not keys:
            return {}
        if len(keys) != len(block.keys):
            # Ein Register des Blocks ist bekannt unbekannt → einzeln lesen.
            result: dict[str, Any] = {}
            for key in keys:
                result.update(await self._read_single(self.registers[key]))
            return result
        try:
            raw = await self._read(block.type, block.address, block.count)
        except ModbusExceptionResponse as exc:
            if exc.code != ILLEGAL_DATA_ADDRESS:
                raise
            result = {}
            for key in block.keys:
                result.update(await self._read_single(self.registers[key]))
            return result
        return {key: self.registers[key].decode(value) for key, value in zip(block.keys, raw, strict=True)}

    async def _read_single(self, reg: Register) -> dict[str, Any]:
        try:
            raw = await self._read(reg.type, reg.address, 1)
        except ModbusExceptionResponse as exc:
            if exc.code != ILLEGAL_DATA_ADDRESS:
                raise
            _LOGGER.info("Register %s (0x%04X) wird von der Anlage nicht unterstützt", reg.key, reg.address)
            self.unsupported.add(reg.key)
            return {reg.key: None}
        return {reg.key: reg.decode(raw[0])}

    async def _read(self, reg_type: RegisterType, address: int, count: int) -> list[int]:
        if reg_type is RegisterType.INPUT:
            return await self.client.read_input_registers(address, count)
        return await self.client.read_holding_registers(address, count)

    async def async_write(self, key: str, value: float | int) -> None:
        """Wert schreiben, sofort zurücklesen, Entitäten aktualisieren."""
        reg = self.registers[key]
        try:
            raw = reg.encode(value)
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc
        try:
            await self.client.write_register(reg.address, raw)
            readback = await self.client.read_holding_registers(reg.address, 1)
        except ModbusError as exc:
            raise HomeAssistantError(f"Schreiben von {key} fehlgeschlagen: {exc}") from exc
        if self.data is not None:
            self.async_set_updated_data({**self.data, key: reg.decode(readback[0])})

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.close()


class CloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Status und Energiezähler aus myTripleSolar."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: CloudClient) -> None:
        scan = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_CLOUD_SCAN_S)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            # Ohne die ID: sie „gewährt Zugriff auf die Wärmepumpe" und landet
            # sonst bei jedem Abruffehler im Log.
            name=f"{DOMAIN} cloud {entry.title}",
            update_interval=timedelta(seconds=scan),
        )
        self.client = client
        self.options_snapshot = dict(entry.options)
        self.heat_pump_id: str = entry.data[CONF_HEAT_PUMP_ID]

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.client.status(self.heat_pump_id)
            energy = await self.client.energy_totals(self.heat_pump_id)
        except CloudAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except CloudError as exc:
            raise UpdateFailed(str(exc)) from exc
        return {"status": status, "energy": energy}
