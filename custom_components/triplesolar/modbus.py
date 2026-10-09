"""Schlanker asynchroner Modbus-TCP/TLS-Client.

Warum kein pymodbus: Home Assistant bringt pymodbus für seine eigene
Modbus-Integration in einer festen Version mit, und dessen API hat sich in
3.x mehrfach geändert (``slave`` → ``device_id``, TLS-Kontext). Eine
Custom-Integration, die eine andere Version verlangt, kollidiert damit.
Gebraucht werden hier genau drei Funktionscodes (0x03, 0x04, 0x06) über
EINE dauerhafte Verbindung — das sind ein paar Dutzend Zeilen MBAP, die
sich vollständig testen lassen.

Modbus/TCP Security (Modbus TLS) ist dasselbe Protokoll in einem
TLS-Tunnel mit gegenseitiger Zertifikatsprüfung, Port 802
(Modbus.org, „MB-TCP-Security-v21").
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
import struct
import tempfile
from dataclasses import dataclass

from cryptography import x509

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06

EXCEPTION_TEXT = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Server Device Failure",
    6: "Server Device Busy",
}

# Modbus begrenzt eine Leseantwort auf 125 Register.
MAX_READ_COUNT = 125


class ModbusError(Exception):
    """Verbindung oder Protokoll gestört — ein erneuter Versuch kann helfen."""


class ModbusExceptionResponse(ModbusError):
    """Der Server hat mit einer Modbus-Exception geantwortet."""

    def __init__(self, function: int, code: int) -> None:
        self.function = function
        self.code = code
        text = EXCEPTION_TEXT.get(code, "unbekannt")
        super().__init__(f"Modbus-Exception {code} ({text}) auf Funktion 0x{function:02X}")


class PeerVerificationError(ModbusError):
    """Das Zertifikat der Gegenstelle gehört nicht zur gekoppelten Anlage."""


@dataclass(frozen=True, slots=True)
class TlsCredentials:
    """Ergebnis der Kopplung — alles, was Modbus TLS braucht (PEM)."""

    root_ca_pem: str
    client_cert_pem: str
    client_key_pem: str
    device_hostname: str


def build_ssl_context(creds: TlsCredentials) -> ssl.SSLContext:
    """TLS-Kontext mit gegenseitiger Authentifizierung.

    ``check_hostname`` ist aus, weil per IP verbunden wird — die Anlage
    heißt ``hpcb-xxxxxx.local``, und mDNS-Namensauflösung ist in einem
    HA-Container nicht selbstverständlich. Die Identität wird stattdessen
    nach dem Handshake gegen den gespeicherten Hostnamen geprüft
    (:func:`verify_peer_hostname`), wie im Demo-Repository des Herstellers.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=creds.root_ca_pem)
    # load_cert_chain nimmt nur Dateipfade; über eine temporäre Datei, die
    # sofort wieder gelöscht wird, damit der Schlüssel nicht liegen bleibt.
    # Blockierende Datei-I/O — in Home Assistant im Executor aufrufen.
    fd, path = tempfile.mkstemp(prefix="triplesolar-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(creds.client_cert_pem)
            fh.write("\n")
            fh.write(creds.client_key_pem)
        ctx.load_cert_chain(certfile=path)
    finally:
        os.unlink(path)
    return ctx


def verify_peer_hostname(der_cert: bytes | None, expected_hostname: str) -> None:
    """Prüft, dass das Server-Zertifikat den gekoppelten Hostnamen trägt."""
    if not der_cert:
        raise PeerVerificationError("Die Wärmepumpe hat kein Zertifikat vorgelegt")
    cert = x509.load_der_x509_certificate(der_cert)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise PeerVerificationError("Zertifikat der Wärmepumpe ohne Subject Alternative Name") from None
    names = {name.lower().rstrip(".") for name in san.get_values_for_type(x509.DNSName)}
    if expected_hostname.lower().rstrip(".") not in names:
        raise PeerVerificationError(
            f"Zertifikat gehört zu {sorted(names)}, gekoppelt ist {expected_hostname!r}"
        )


class ModbusClient:
    """Eine Verbindung, nacheinander abgearbeitete Anfragen.

    Die Anfragen werden über ein Lock serialisiert: die Transaktions-ID
    würde parallele Anfragen zwar unterscheiden, aber nicht jede
    Modbus-Implementierung beantwortet sie auch parallel, und die Anlage
    ist hier kein Engpass.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        unit_id: int = 1,
        ssl_context: ssl.SSLContext | None = None,
        expected_hostname: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._ssl = ssl_context
        self._expected_hostname = expected_hostname
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._tid = 0

    @property
    def connected(self) -> bool:
        # Bei reinem TCP bleibt eine vom Server geschlossene Verbindung
        # halboffen (is_closing() bleibt False); erst at_eof() verrät sie.
        # Ohne die Prüfung scheiterte nach jedem Leerlauf-Timeout der Gegenstelle
        # die erste Anfrage.
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader is not None
            and not self._reader.at_eof()
        )

    async def connect(self) -> None:
        if self.connected:
            return
        await self.close()  # halboffene Reste aufräumen
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self._ssl),
                self._timeout,
            )
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            raise ModbusError(f"Verbindung zu {self.host}:{self.port} fehlgeschlagen: {exc}") from exc
        if self._ssl is not None and self._expected_hostname:
            sslobj = self._writer.get_extra_info("ssl_object")
            der = sslobj.getpeercert(binary_form=True) if sslobj else None
            try:
                verify_peer_hostname(der, self._expected_hostname)
            except PeerVerificationError:
                await self.close()
                raise

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 2)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        return await self._read(FC_READ_INPUT, address, count)

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        return await self._read(FC_READ_HOLDING, address, count)

    async def write_register(self, address: int, value: int) -> None:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"Registerwert {value} außerhalb 0…65535")
        payload = struct.pack(">HH", address, value)
        response = await self._request(FC_WRITE_SINGLE, payload)
        if response != payload:
            raise ModbusError(f"Schreibbestätigung passt nicht: {response.hex()} ≠ {payload.hex()}")

    async def _read(self, function: int, address: int, count: int) -> list[int]:
        if not 1 <= count <= MAX_READ_COUNT:
            raise ValueError(f"Anzahl {count} außerhalb 1…{MAX_READ_COUNT}")
        response = await self._request(function, struct.pack(">HH", address, count))
        if not response or response[0] != 2 * count or len(response) != 1 + 2 * count:
            raise ModbusError(f"Antwort mit falscher Länge auf 0x{function:02X} @0x{address:04X}")
        return list(struct.unpack(f">{count}H", response[1:]))

    async def _request(self, function: int, payload: bytes) -> bytes:
        async with self._lock:
            await self.connect()
            assert self._reader is not None and self._writer is not None
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            pdu = bytes([function]) + payload
            # MBAP: Transaktion, Protokoll 0, Länge (Unit + PDU), Unit
            frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit_id) + pdu
            try:
                self._writer.write(frame)
                await self._writer.drain()
                header = await asyncio.wait_for(self._reader.readexactly(7), self._timeout)
                rtid, proto, length, _unit = struct.unpack(">HHHB", header)
                if proto != 0 or length < 2 or length > 256:
                    raise ModbusError(f"Ungültiger MBAP-Kopf {header.hex()}")
                body = await asyncio.wait_for(self._reader.readexactly(length - 1), self._timeout)
            except (OSError, TimeoutError, asyncio.IncompleteReadError, ssl.SSLError) as exc:
                await self.close()
                raise ModbusError(f"Übertragung zu {self.host}:{self.port} gestört: {exc}") from exc
            except ModbusError:
                await self.close()
                raise
            if rtid != tid:
                # Eine verspätete Antwort auf eine abgebrochene Anfrage: der
                # Datenstrom ist nicht mehr synchron, also neu verbinden.
                await self.close()
                raise ModbusError(f"Transaktions-ID {rtid} statt {tid}")
            if body[0] == function | 0x80:
                raise ModbusExceptionResponse(function, body[1] if len(body) > 1 else 0)
            if body[0] != function:
                await self.close()
                raise ModbusError(f"Antwort auf Funktion 0x{body[0]:02X} statt 0x{function:02X}")
            return body[1:]
