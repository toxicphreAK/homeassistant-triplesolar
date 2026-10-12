"""Einrichtung in Home Assistant gegen den Fake-Modbus-Server und eine Fake-Cloud."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.triplesolar.const import (
    CONF_CONNECTION,
    CONF_HEAT_PUMP_ID,
    CONF_MODULES,
    CONF_UNIT_ID,
    CONNECTION_CLOUD,
    CONNECTION_MODBUS_TCP,
    DOMAIN,
)

from .fakes import FakeModbusServer, sample_registers

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def server():
    inp, hold = sample_registers()
    srv = await FakeModbusServer(inp, hold).start()
    yield srv
    await srv.stop()


async def setup_modbus(hass: HomeAssistant, server: FakeModbusServer, **extra) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"127.0.0.1:{server.port}",
        data={
            CONF_CONNECTION: CONNECTION_MODBUS_TCP,
            CONF_HOST: "127.0.0.1",
            CONF_PORT: server.port,
            CONF_UNIT_ID: 1,
            CONF_MODULES: 1,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def eid(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"127.0.0.1-{key}")
    assert entity_id, key
    return entity_id


async def enable(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str:
    """Standardmäßig deaktivierte Entität einschalten und neu laden."""
    registry = er.async_get(hass)
    entity_id = eid(hass, platform, key)
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entity_id


async def test_modbus_setup_and_states(hass: HomeAssistant, server: FakeModbusServer) -> None:
    entry = await setup_modbus(hass, server)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get(eid(hass, "sensor", "heat_pump_state")).state == "dhw_prio"
    assert hass.states.get(eid(hass, "sensor", "dhw_tank_temp")).state == "45.2"
    assert hass.states.get(eid(hass, "sensor", "ambient_temp")).state == "-3.5"
    assert hass.states.get(eid(hass, "sensor", "m1_electric_power")).state == "1250"
    assert hass.states.get(eid(hass, "sensor", "m1_cop")).state == "3.28"
    assert hass.states.get(eid(hass, "sensor", "m1_compressor_frequency")).state == "48.5"
    messages = hass.states.get(eid(hass, "sensor", "active_messages"))
    assert messages.state == "1"
    assert messages.attributes["codes"] == [4711]
    assert hass.states.get(eid(hass, "binary_sensor", "fault")).state == "off"
    assert hass.states.get(eid(hass, "binary_sensor", "dhw_demand")).state == "on"
    assert hass.states.get(eid(hass, "select", "dhw_profile_set")).state == "normal"
    assert hass.states.get(eid(hass, "switch", "sh_enable")).state == "on"
    assert hass.states.get(eid(hass, "number", "dhw_normal_setpoint")).state == "50.0"

    # Kühlen und Modbus-Steuerung sind standardmäßig aus.
    registry = er.async_get(hass)
    assert registry.async_get(eid(hass, "select", "control_operating_mode")).disabled
    assert registry.async_get(eid(hass, "number", "control_power_budget")).disabled
    assert registry.async_get(eid(hass, "sensor", "sc_status")).disabled

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_writes_reach_the_heat_pump(hass: HomeAssistant, server: FakeModbusServer) -> None:
    entry = await setup_modbus(hass, server)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "dhw_profile_set"), "option": "comfort"},
        blocking=True,
    )
    assert server.holding[0x310A] == 2
    assert hass.states.get(eid(hass, "select", "dhw_profile_set")).state == "comfort"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "sh_profile_set"), "option": "blocked"},
        blocking=True,
    )
    assert server.holding[0x1108] == 0xFFFF  # -1 als int16

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": eid(hass, "number", "dhw_eco_setpoint"), "value": 47.5},
        blocking=True,
    )
    assert server.holding[0x3601] == 475

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eid(hass, "switch", "dhw_enable")}, blocking=True
    )
    assert server.holding[0x3101] == 0

    budget = await enable(hass, entry, "number", "control_power_budget")
    await hass.services.async_call("number", "set_value", {"entity_id": budget, "value": 3500}, blocking=True)
    assert server.holding[0xB103] == 3500


async def test_write_out_of_range_is_rejected(hass: HomeAssistant, server: FakeModbusServer) -> None:
    await setup_modbus(hass, server)
    with pytest.raises((HomeAssistantError, ValueError)):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": eid(hass, "number", "dhw_eco_setpoint"), "value": 80},
            blocking=True,
        )
    assert server.holding[0x3601] == 450
    assert not server.writes


async def test_unsupported_register_is_skipped(hass: HomeAssistant, server: FakeModbusServer) -> None:
    """Ältere Firmware ohne 0x124: der Rest des Blocks liest weiter."""
    del server.input[0x124]
    entry = await setup_modbus(hass, server)
    coordinator = entry.runtime_data
    assert coordinator.unsupported == {"input_modulation"}
    assert hass.states.get(eid(hass, "sensor", "room_temp")).state == "21.1"

    # Zweiter Abruf: das bekannte Loch wird nicht mehr als Block angefragt.
    server.requests.clear()
    await coordinator.async_refresh()
    assert (1, 4, 0x124) not in server.requests


async def test_unsupported_register_makes_entity_unavailable(
    hass: HomeAssistant, server: FakeModbusServer
) -> None:
    """Fehlt ein Register, ist die Entität dazu nicht verfügbar — auf jeder Plattform."""
    del server.input[0x25]  # dhw_tank_temp
    del server.holding[0x310A]  # dhw_profile_set
    del server.holding[0x3601]  # dhw_eco_setpoint
    del server.holding[0x3101]  # dhw_enable
    entry = await setup_modbus(hass, server)
    assert entry.runtime_data.unsupported >= {
        "dhw_tank_temp",
        "dhw_profile_set",
        "dhw_eco_setpoint",
        "dhw_enable",
    }

    assert hass.states.get(eid(hass, "sensor", "dhw_tank_temp")).state == STATE_UNAVAILABLE
    assert hass.states.get(eid(hass, "select", "dhw_profile_set")).state == STATE_UNAVAILABLE
    assert hass.states.get(eid(hass, "number", "dhw_eco_setpoint")).state == STATE_UNAVAILABLE
    assert hass.states.get(eid(hass, "switch", "dhw_enable")).state == STATE_UNAVAILABLE
    # Nachbarn derselben Plattform bleiben verfügbar.
    assert hass.states.get(eid(hass, "sensor", "ambient_temp")).state == "-3.5"
    assert hass.states.get(eid(hass, "select", "sh_profile_set")).state != STATE_UNAVAILABLE
    assert hass.states.get(eid(hass, "number", "dhw_normal_setpoint")).state == "50.0"
    assert hass.states.get(eid(hass, "switch", "sh_enable")).state == "on"


async def test_connection_loss_makes_entities_unavailable(
    hass: HomeAssistant, server: FakeModbusServer
) -> None:
    entry = await setup_modbus(hass, server)
    await server.stop()  # schließt auch die offene Verbindung
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(eid(hass, "sensor", "dhw_tank_temp")).state == STATE_UNAVAILABLE


async def test_integrated_energy(hass: HomeAssistant, server: FakeModbusServer) -> None:
    entry = await setup_modbus(hass, server)
    coordinator = entry.runtime_data
    energy = eid(hass, "sensor", "m1_electric_energy")
    assert float(hass.states.get(energy).state) == 0.0
    start = dt_util.utcnow()
    times = [start + timedelta(seconds=s) for s in (0, 300, 600)]
    for t in times:
        with patch("custom_components.triplesolar.sensor.dt_util.utcnow", return_value=t):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
    # 1250 W × 600 s = 0,2083 kWh
    assert float(hass.states.get(energy).state) == pytest.approx(0.2083, abs=1e-4)


async def test_cloud_setup(hass: HomeAssistant) -> None:
    status = {
        "id": "HP1",
        "softwareVersion": "2026.09.08.1",
        "alerts": [{"code": 12, "severity": "WARNING", "message": "Druck niedrig"}],
        "controller": {
            "spaceConditioning": {"heatingStatus": "ACTIVE", "isHeatingActive": True, "ambientTemp": 7.5},
            "domesticHotWater": {"tankTemp": 48.1, "isActive": False, "hasDemand": False},
        },
    }
    energy = {
        "spaceHeatingTotalElectricEnergyConsumedkWh": 100.0,
        "dhwTotalElectricEnergyConsumedkWh": 40.0,
        "spaceHeatingTotalThermalEnergyProducedkWh": 450.0,
        "dhwTotalThermalEnergyProducedkWh": 120.0,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="cloud-HP1",
        data={
            CONF_CONNECTION: CONNECTION_CLOUD,
            CONF_HEAT_PUMP_ID: "HP1",
            "email": "a@b.de",
            "password": "x",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.triplesolar.CloudClient.status", AsyncMock(return_value=status)),
        patch("custom_components.triplesolar.CloudClient.energy_totals", AsyncMock(return_value=energy)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)

    def cloud(platform: str, key: str) -> str:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"cloud-HP1-{key}")
        assert entity_id, key
        return entity_id

    assert hass.states.get(cloud("sensor", "dhw_tank_temp")).state == "48.1"
    assert hass.states.get(cloud("sensor", "heating_status_cloud")).state == "active"
    assert hass.states.get(cloud("sensor", "total_electric_energy")).state == "140.0"  # Kühlen fehlt → 0
    assert hass.states.get(cloud("sensor", "total_thermal_energy")).state == "570.0"
    assert hass.states.get(cloud("sensor", "alerts")).state == "1"
    assert hass.states.get(cloud("binary_sensor", "heating_active")).state == "on"
    # Die Cloud ist nur lesend eingebunden.
    assert not [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain in ("select", "number", "switch")
    ]


async def test_cloud_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    from custom_components.triplesolar.cloud import CloudAuthError

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="cloud-HP1",
        data={
            CONF_CONNECTION: CONNECTION_CLOUD,
            CONF_HEAT_PUMP_ID: "HP1",
            "email": "a@b.de",
            "password": "x",
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.triplesolar.CloudClient.status", AsyncMock(side_effect=CloudAuthError("401"))
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_cloud_diagnostics_hide_heat_pump_id(hass: HomeAssistant) -> None:
    from custom_components.triplesolar.diagnostics import async_get_config_entry_diagnostics

    secret = "HP-SECRET-4711"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dach (Cloud)",
        unique_id=f"cloud-{secret}",
        data={
            CONF_CONNECTION: CONNECTION_CLOUD,
            CONF_HEAT_PUMP_ID: secret,
            "email": "a@b.de",
            "password": "x",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.triplesolar.CloudClient.status", AsyncMock(return_value={"id": secret})),
        patch("custom_components.triplesolar.CloudClient.energy_totals", AsyncMock(return_value={})),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert secret not in repr(diag)
    assert secret not in entry.runtime_data.name
