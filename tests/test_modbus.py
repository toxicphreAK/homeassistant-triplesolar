"""Modbus-Client gegen den Fake-Server — TCP und TLS mit gegenseitiger Prüfung."""

from __future__ import annotations

import asyncio
import struct

import pytest

from custom_components.triplesolar.modbus import (
    ModbusClient,
    ModbusError,
    ModbusExceptionResponse,
    PeerVerificationError,
    TlsCredentials,
    build_ssl_context,
)

from .fakes import HOSTNAME, FakeModbusServer, FakePki

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def server():
    srv = await FakeModbusServer({0: 1, 1: 0xFFFF, 2: 7}, {0x1108: 1}).start()
    yield srv
    await srv.stop()


async def test_read_and_write_tcp(server: FakeModbusServer) -> None:
    client = ModbusClient("127.0.0.1", server.port, unit_id=7)
    try:
        assert await client.read_input_registers(0, 3) == [1, 0xFFFF, 7]
        await client.write_register(0x1108, 0xFFFF)
        assert await client.read_holding_registers(0x1108, 1) == [0xFFFF]
    finally:
        await client.close()
    assert server.writes == [(0x1108, 0xFFFF)]
    assert {unit for unit, _, _ in server.requests} == {7}
    # Eine Verbindung für alle Anfragen, keine Neuverbindung je Abruf.
    assert not client.connected


async def test_exception_response_keeps_connection(server: FakeModbusServer) -> None:
    client = ModbusClient("127.0.0.1", server.port)
    try:
        with pytest.raises(ModbusExceptionResponse) as exc:
            await client.read_input_registers(0x15, 1)
        assert exc.value.code == 2
        assert client.connected  # Protokollfehler ≠ Verbindungsfehler
        assert await client.read_input_registers(2, 1) == [7]
    finally:
        await client.close()


async def test_connection_refused() -> None:
    srv = await FakeModbusServer().start()
    port = srv.port
    await srv.stop()
    client = ModbusClient("127.0.0.1", port, timeout=2)
    with pytest.raises(ModbusError):
        await client.read_input_registers(0, 1)


async def test_mismatched_transaction_id_reconnects() -> None:
    async def handle(reader, writer):
        header = await reader.readexactly(7)
        tid, _, length, unit = struct.unpack(">HHHB", header)
        await reader.readexactly(length - 1)
        body = bytes([4, 2, 0, 1])
        writer.write(struct.pack(">HHHB", tid + 1, 0, len(body) + 1, unit) + body)
        await writer.drain()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    client = ModbusClient("127.0.0.1", port, timeout=2)
    try:
        with pytest.raises(ModbusError, match="Transaktions-ID"):
            await client.read_input_registers(0, 1)
        assert not client.connected
    finally:
        srv.close()
        await srv.wait_closed()


async def test_timeout_raises_modbus_error() -> None:
    release = asyncio.Event()

    async def handle(reader, writer):
        await reader.read(100)
        await release.wait()  # antwortet nie
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    client = ModbusClient("127.0.0.1", port, timeout=0.3)
    try:
        with pytest.raises(ModbusError):
            await client.read_input_registers(0, 1)
        assert not client.connected
    finally:
        release.set()
        srv.close()
        await srv.wait_closed()


async def test_argument_validation(server: FakeModbusServer) -> None:
    client = ModbusClient("127.0.0.1", server.port)
    with pytest.raises(ValueError):
        await client.read_input_registers(0, 0)
    with pytest.raises(ValueError):
        await client.read_input_registers(0, 126)
    with pytest.raises(ValueError):
        await client.write_register(0x1108, 70000)


def _creds(pki: FakePki, hostname: str = HOSTNAME) -> TlsCredentials:
    cert, key = pki.client_credentials()
    return TlsCredentials(pki.ca_pem, cert, key, hostname)


async def test_tls_mutual_auth() -> None:
    pki = FakePki()
    srv = await FakeModbusServer({0: 3}, ssl_context=pki.server_context()).start()
    client = ModbusClient(
        "127.0.0.1", srv.port, ssl_context=build_ssl_context(_creds(pki)), expected_hostname=HOSTNAME
    )
    try:
        assert await client.read_input_registers(0, 1) == [3]
    finally:
        await client.close()
        await srv.stop()


async def test_tls_rejects_other_heat_pump() -> None:
    """Gleiche Root-CA, anderer Hostname → nicht die gekoppelte Anlage."""
    pki = FakePki()
    srv = await FakeModbusServer({0: 3}, ssl_context=pki.server_context()).start()
    client = ModbusClient(
        "127.0.0.1",
        srv.port,
        ssl_context=build_ssl_context(_creds(pki, "hpcb-ffffff.local")),
        expected_hostname="hpcb-ffffff.local",
    )
    try:
        with pytest.raises(PeerVerificationError):
            await client.read_input_registers(0, 1)
        assert not client.connected
    finally:
        await srv.stop()


async def test_tls_rejects_foreign_ca() -> None:
    pki, other = FakePki(), FakePki()
    # Der abgebrochene Handshake landet serverseitig im Exception-Handler
    # der Schleife; das ist hier der erwartete Ausgang, kein Testfehler.
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _ctx: None)
    srv = await FakeModbusServer({0: 3}, ssl_context=pki.server_context()).start()
    client = ModbusClient(
        "127.0.0.1",
        srv.port,
        ssl_context=build_ssl_context(_creds(other)),
        expected_hostname=HOSTNAME,
        timeout=3,
    )
    try:
        with pytest.raises(ModbusError):
            await client.read_input_registers(0, 1)
    finally:
        await srv.stop()
        loop.set_exception_handler(previous)


async def test_reconnects_after_server_closed_idle_connection() -> None:
    """Gegenstelle schließt nach jeder Anfrage (Leerlauf-Timeout): kein Fehlversuch."""

    async def handle(reader, writer):
        header = await reader.readexactly(7)
        tid, _, length, unit = struct.unpack(">HHHB", header)
        await reader.readexactly(length - 1)
        body = bytes([4, 2, 0, 5])
        writer.write(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    client = ModbusClient("127.0.0.1", port, timeout=2)
    try:
        assert await client.read_input_registers(0, 1) == [5]
        await asyncio.sleep(0.05)  # FIN der Gegenstelle ankommen lassen
        assert not client.connected
        assert await client.read_input_registers(0, 1) == [5]
    finally:
        await client.close()
        srv.close()
        await srv.wait_closed()
