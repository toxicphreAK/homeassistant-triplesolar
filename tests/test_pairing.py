"""Kopplung: CSR → Zertifikat → damit Modbus TLS sprechen."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509

from custom_components.triplesolar.modbus import ModbusClient, build_ssl_context
from custom_components.triplesolar.pairing import (
    DiscoveredService,
    PairingError,
    generate_key_and_csr,
    normalize_hostname,
    pair,
    split_signing_response,
)

from .fakes import HOSTNAME, FakeModbusServer, FakePki

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def start_signer(
    pki: FakePki, handler=None, received: list[bytes] | None = None
) -> tuple[web.AppRunner, int]:
    received = received if received is not None else []

    async def sign(request: web.Request) -> web.Response:
        body = await request.read()
        received.append(body)
        return web.Response(body=pki.sign_csr(body), content_type="application/x-pem-file")

    app = web.Application()
    app.router.add_post("/csr", handler or sign)
    runner = web.AppRunner(app)
    await runner.setup()
    # Selbstsigniert wie auf der Anlage; der Client prüft hier bewusst nicht.
    site = web.TCPSite(
        runner, "127.0.0.1", 0, ssl_context=FakePki().server_context(require_client_cert=False)
    )
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def test_key_and_csr() -> None:
    kc = generate_key_and_csr("unit-test")
    csr = x509.load_pem_x509_csr(kc.csr_pem.encode())
    assert csr.is_signature_valid
    assert csr.subject.rfc4514_string() == "CN=unit-test"
    assert "PRIVATE KEY" in kc.key_pem


def test_split_signing_response() -> None:
    pki = FakePki()
    leaf, root = split_signing_response(pki.sign_csr(generate_key_and_csr().csr_pem.encode()))
    assert root == pki.ca_pem
    assert x509.load_pem_x509_certificate(leaf.encode()).issuer == pki.ca_cert.subject
    with pytest.raises(PairingError):
        split_signing_response(pki.ca_pem.encode())  # nur ein Zertifikat
    with pytest.raises(PairingError):
        split_signing_response(b"kein pem")


def test_normalize_hostname() -> None:
    assert normalize_hostname("HPCB-A1B2C3.local.") == "hpcb-a1b2c3.local"


async def test_pair_then_modbus_tls() -> None:
    """Der ganze Weg: koppeln, dann mit dem Ergebnis Modbus TLS lesen."""
    pki = FakePki()
    received: list[bytes] = []
    runner, port = await start_signer(pki, received=received)
    try:
        async with aiohttp.ClientSession() as session:
            creds = await pair(
                session, DiscoveredService(HOSTNAME, "127.0.0.1", port), generate_key_and_csr()
            )
    finally:
        await runner.cleanup()
    assert creds.device_hostname == HOSTNAME
    assert len(received) == 1  # genau EINE CSR

    srv = await FakeModbusServer({0: 1}, ssl_context=pki.server_context()).start()
    client = ModbusClient(
        "127.0.0.1", srv.port, ssl_context=build_ssl_context(creds), expected_hostname=creds.device_hostname
    )
    try:
        assert await client.read_input_registers(0, 1) == [1]
    finally:
        await client.close()
        await srv.stop()


async def test_pair_http_error() -> None:
    async def refuse(request: web.Request) -> web.Response:
        return web.Response(status=409, text="already used")

    runner, port = await start_signer(FakePki(), refuse)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(PairingError, match="409"):
                await pair(session, DiscoveredService(HOSTNAME, "127.0.0.1", port), generate_key_and_csr())
    finally:
        await runner.cleanup()


async def test_pair_unreachable() -> None:
    runner, port = await start_signer(FakePki())
    await runner.cleanup()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(PairingError):
            await pair(session, DiscoveredService(HOSTNAME, "127.0.0.1", port), generate_key_and_csr())
