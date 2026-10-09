"""Test-Doubles: Modbus-Server (TCP/TLS), PKI wie die Wärmepumpe, CSR-Signierer."""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOSTNAME = "hpcb-a1b2c3.local"


def _pem_cert(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _pem_key(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@dataclass
class FakePki:
    """Root-CA + Server-Zertifikat, so wie die Anlage sie hält."""

    hostname: str = HOSTNAME
    ca_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def __post_init__(self) -> None:
        now = dt.datetime.now(dt.UTC)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Triple Solar HP test root")])
        self.ca_cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.ca_key, hashes.SHA256())
        )
        self.server_key = ec.generate_private_key(ec.SECP256R1())
        self.server_cert = self._issue(self.server_key.public_key(), self.hostname, server=True)

    def _issue(self, public_key, common_name: str, *, server: bool) -> x509.Certificate:
        now = dt.datetime.now(dt.UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(self.ca_cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False
            )
        return builder.sign(self.ca_key, hashes.SHA256())

    @property
    def ca_pem(self) -> str:
        return _pem_cert(self.ca_cert)

    def sign_csr(self, csr_pem: bytes) -> bytes:
        """Was der Signierdienst der Anlage zurückgibt: Leaf + Root (PEM)."""
        csr = x509.load_pem_x509_csr(csr_pem)
        cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        leaf = self._issue(csr.public_key(), cn, server=False)
        return (_pem_cert(leaf) + self.ca_pem).encode()

    def client_credentials(self, cn: str = "test-client") -> tuple[str, str]:
        key = ec.generate_private_key(ec.SECP256R1())
        return _pem_cert(self._issue(key.public_key(), cn, server=False)), _pem_key(key)

    def server_context(self, *, require_client_cert: bool = True) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.pem"
            path.write_text(_pem_cert(self.server_cert) + _pem_key(self.server_key))
            ctx.load_cert_chain(path)
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=self.ca_pem)
        return ctx


class FakeModbusServer:
    """Minimaler Modbus/TCP-Server (FC 3, 4, 6) über asyncio.

    Adressen, die nicht in ``input``/``holding`` stehen, beantwortet er mit
    Exception 2 — wie ein echter Server für undokumentierte Register.
    """

    def __init__(
        self,
        input_regs: dict[int, int] | None = None,
        holding_regs: dict[int, int] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.input = dict(input_regs or {})
        self.holding = dict(holding_regs or {})
        self.ssl = ssl_context
        self.requests: list[tuple[int, int, int]] = []  # (unit, function, address)
        self.writes: list[tuple[int, int]] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> FakeModbusServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self.ssl)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            # Seit Python 3.12 wartet wait_closed() auf offene Verbindungen —
            # die des Clients unter Test bleibt sonst bis zum Timeout stehen.
            self._server.close_clients()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _proto, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                writer.write(self._respond(tid, unit, pdu))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def _respond(self, tid: int, unit: int, pdu: bytes) -> bytes:
        function = pdu[0]
        address, value = struct.unpack(">HH", pdu[1:5])
        self.requests.append((unit, function, address))
        if function in (3, 4):
            table = self.input if function == 4 else self.holding
            addrs = range(address, address + value)
            if any(a not in table for a in addrs):
                body = bytes([function | 0x80, 2])
            else:
                data = b"".join(struct.pack(">H", table[a] & 0xFFFF) for a in addrs)
                body = bytes([function, len(data)]) + data
        elif function == 6:
            if address not in self.holding:
                body = bytes([function | 0x80, 2])
            else:
                self.holding[address] = value
                self.writes.append((address, value))
                body = pdu[:5]
        else:
            body = bytes([function | 0x80, 1])
        return struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body


def sample_registers() -> tuple[dict[int, int], dict[int, int]]:
    """Plausibler Momentzustand: Warmwasser läuft, Modul 1 heizt."""
    raw = lambda v: v & 0xFFFF  # noqa: E731
    inp = {
        0x0: 1,  # dhw prio
        **{a: 0 for a in range(0x1, 0x15)},
        0x16: 6000,
        0x20: 1,
        0x21: 1,
        0x22: 360,
        0x25: 452,
        0x26: 500,
        0x27: 1,
        0x28: 1,
        0x40: 0,
        0x41: 0,
        0x42: 0,
        0x45: 285,
        0x46: 300,
        0x4A: 1,
        0x60: 0,
        0x61: 0,
        0x62: 0,
        0x65: 0,
        0x66: 0,
        0x69: 0,
        0x120: 211,
        0x121: raw(-35),
        0x122: 480,
        0x123: 210,
        0x124: 0,
        0x400: 4,
        0x422: 485,
        0x441: 650,
        0x461: 400,
        0x4A1: 0,
        0x4C0: 82,
        0x4C1: 41,
        0x500: 455,
        0x501: 512,
        0x680: 1250,
        0x681: 4100,
        0x683: 1234,
    }
    inp[0x3] = 4711  # eine aktive Meldung
    hold = {
        0x1101: 1,
        0x1108: 1,
        0x2101: 0,
        0x2107: 1,
        0x3101: 1,
        0x310A: 1,
        0x3601: 450,
        0x3602: 50,
        0x3603: 360,
        0x3701: 500,
        0x3702: 50,
        0x3703: 360,
        0x3801: 550,
        0x3802: 50,
        0x3803: 600,
        0xB101: 0,
        0xB102: 0,
        0xB103: 0,
    }
    return inp, hold
