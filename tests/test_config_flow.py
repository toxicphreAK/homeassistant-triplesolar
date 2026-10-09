"""Einrichtungsdialoge: Kopplung (TLS), TCP, Cloud, Reauth, Optionen."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.triplesolar.cloud import CloudAuthError, CloudHeatPump
from custom_components.triplesolar.const import (
    CONF_CLIENT_KEY,
    CONF_CONNECTION,
    CONF_HEAT_PUMP_ID,
    CONF_HOSTNAME,
    CONF_MODULES,
    CONF_UNIT_ID,
    CONNECTION_CLOUD,
    CONNECTION_MODBUS_TCP,
    CONNECTION_MODBUS_TLS,
    DOMAIN,
)
from custom_components.triplesolar.modbus import ModbusError, TlsCredentials
from custom_components.triplesolar.pairing import DiscoveredService, DiscoveryTimeout

from .fakes import HOSTNAME, FakeModbusServer, FakePki, sample_registers

pytestmark = pytest.mark.usefixtures("socket_enabled")
CF = "custom_components.triplesolar.config_flow"


async def start(hass: HomeAssistant, option: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": option})


async def finish_progress(hass: HomeAssistant, result):
    """Fortschrittsschritt abwarten und weiterschalten.

    Ist die (gemockte) Kopplung schon fertig, überspringt HA den Fortschritt.
    """
    if result["type"] is not FlowResultType.SHOW_PROGRESS:
        return result
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


@pytest.fixture
async def tls_server():
    pki = FakePki()
    inp, hold = sample_registers()
    srv = await FakeModbusServer(inp, hold, ssl_context=pki.server_context()).start()
    srv.pki = pki
    yield srv
    await srv.stop()


def creds_for(pki: FakePki) -> TlsCredentials:
    cert, key = pki.client_credentials()
    return TlsCredentials(pki.ca_pem, cert, key, HOSTNAME)


async def test_tls_pairing_with_discovery(hass: HomeAssistant, tls_server) -> None:
    creds = creds_for(tls_server.pki)

    async def discover(_aiozc, service_type, **kwargs):
        if service_type.startswith("_ts-csr"):
            return DiscoveredService(HOSTNAME, "127.0.0.1", 4443)
        assert kwargs["expected_hostname"] == HOSTNAME  # nur die gekoppelte Anlage
        return DiscoveredService(HOSTNAME, "127.0.0.1", tls_server.port)

    with (
        patch(f"{CF}.discover_service", side_effect=discover),
        patch(f"{CF}.pair", AsyncMock(return_value=creds)) as pair_mock,
    ):
        result = await start(hass, CONNECTION_MODBUS_TLS)
        assert result["step_id"] == CONNECTION_MODBUS_TLS
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODULES: "1"})
        result = await finish_progress(hass, result)

    assert pair_mock.await_count == 1
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == HOSTNAME
    data = result["data"]
    assert data[CONF_CONNECTION] == CONNECTION_MODBUS_TLS
    assert data[CONF_HOSTNAME] == HOSTNAME
    assert data[CONF_PORT] == tls_server.port
    assert "PRIVATE KEY" in data[CONF_CLIENT_KEY]
    assert result["result"].unique_id == HOSTNAME


async def test_tls_pairing_not_found_then_retry(hass: HomeAssistant) -> None:
    with patch(f"{CF}.discover_service", side_effect=DiscoveryTimeout("nichts")):
        result = await start(hass, CONNECTION_MODBUS_TLS)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODULES: "1"})
        result = await finish_progress(hass, result)
        assert result["step_id"] == "pairing_failed"
        assert result["errors"] == {"base": "pairing_not_found"}
        # Erneut versuchen startet die Kopplung neu — und scheitert hier wieder.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await finish_progress(hass, result)
        assert result["step_id"] == "pairing_failed"


async def test_tls_manual_host_when_mdns_fails(hass: HomeAssistant, tls_server) -> None:
    creds = creds_for(tls_server.pki)

    async def discover(_aiozc, service_type, **kwargs):
        if service_type.startswith("_ts-csr"):
            return DiscoveredService(HOSTNAME, "127.0.0.1", 4443)
        raise DiscoveryTimeout("kein mbap-s")

    with (
        patch(f"{CF}.discover_service", side_effect=discover),
        patch(f"{CF}.pair", AsyncMock(return_value=creds)),
    ):
        result = await start(hass, CONNECTION_MODBUS_TLS)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODULES: "2"})
        result = await finish_progress(hass, result)
        assert result["step_id"] == "tls_host"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: tls_server.port + 1}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: tls_server.port}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODULES] == 2


async def test_tcp_flow(hass: HomeAssistant) -> None:
    inp, hold = sample_registers()
    srv = await FakeModbusServer(inp, hold).start()
    try:
        result = await start(hass, CONNECTION_MODBUS_TCP)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "127.0.0.1", CONF_PORT: srv.port, CONF_UNIT_ID: 1, CONF_MODULES: "1"},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_CONNECTION] == CONNECTION_MODBUS_TCP
        await hass.async_block_till_done()
        await hass.config_entries.async_unload(result["result"].entry_id)
    finally:
        await srv.stop()


async def test_tcp_flow_cannot_connect(hass: HomeAssistant) -> None:
    with patch(f"{CF}.async_test_modbus", AsyncMock(side_effect=ModbusError("weg"))):
        result = await start(hass, CONNECTION_MODBUS_TCP)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "10.0.0.9", CONF_PORT: 502, CONF_UNIT_ID: 1, CONF_MODULES: "1"}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_cloud_flow_single_pump(hass: HomeAssistant) -> None:
    with (
        patch(f"{CF}.CloudClient.login", AsyncMock()),
        patch(f"{CF}.CloudClient.heat_pumps", AsyncMock(return_value=[CloudHeatPump("HP1", "Dach", True)])),
        patch("custom_components.triplesolar.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await start(hass, CONNECTION_CLOUD)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "a@b.de", "password": "x"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HEAT_PUMP_ID] == "HP1"
    assert result["result"].unique_id == "cloud-HP1"


async def test_cloud_flow_choose_pump(hass: HomeAssistant) -> None:
    pumps = [CloudHeatPump("HP1", "Dach", True), CloudHeatPump("HP2", "Garage", False)]
    with (
        patch(f"{CF}.CloudClient.login", AsyncMock()),
        patch(f"{CF}.CloudClient.heat_pumps", AsyncMock(return_value=pumps)),
        patch("custom_components.triplesolar.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await start(hass, CONNECTION_CLOUD)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "a@b.de", "password": "x"}
        )
        assert result["step_id"] == "cloud_select"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HEAT_PUMP_ID: "HP2"})
    assert result["data"][CONF_HEAT_PUMP_ID] == "HP2"


async def test_cloud_flow_invalid_auth(hass: HomeAssistant) -> None:
    with patch(f"{CF}.CloudClient.login", AsyncMock(side_effect=CloudAuthError("401"))):
        result = await start(hass, CONNECTION_CLOUD)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "a@b.de", "password": "x"}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="cloud-HP1",
        data={
            CONF_CONNECTION: CONNECTION_CLOUD,
            CONF_HEAT_PUMP_ID: "HP1",
            "email": "a@b.de",
            "password": "alt",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(f"{CF}.CloudClient.login", AsyncMock()),
        patch("custom_components.triplesolar.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "neu"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "neu"


async def test_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CONNECTION: CONNECTION_MODBUS_TCP,
            CONF_HOST: "h",
            CONF_PORT: 502,
            CONF_UNIT_ID: 1,
            CONF_MODULES: 1,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_MODULES: "2", CONF_UNIT_ID: 43}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 60, CONF_MODULES: 2, CONF_UNIT_ID: 43}


async def test_tls_found_but_unreachable_offers_manual_host(hass: HomeAssistant, tls_server) -> None:
    """Gekoppelt, per mDNS gefunden, aber der Testabruf scheitert.

    Die Kopplung ist dann verbraucht — der Dialog darf nicht abstürzen,
    sondern bietet mit demselben Zertifikat eine andere Adresse an.
    """
    creds = creds_for(tls_server.pki)

    async def discover(_aiozc, service_type, **kwargs):
        if service_type.startswith("_ts-csr"):
            return DiscoveredService(HOSTNAME, "127.0.0.1", 4443)
        return DiscoveredService(HOSTNAME, "127.0.0.1", tls_server.port + 1)  # nichts lauscht

    with (
        patch(f"{CF}.discover_service", side_effect=discover),
        patch(f"{CF}.pair", AsyncMock(return_value=creds)) as pair_mock,
    ):
        result = await start(hass, CONNECTION_MODBUS_TLS)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODULES: "1"})
        result = await finish_progress(hass, result)
        assert result["step_id"] == "tls_host"
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: tls_server.port}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert pair_mock.await_count == 1  # nicht ein zweites Mal gekoppelt
