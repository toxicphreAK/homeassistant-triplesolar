"""Kopplung (Bootstrapping) für Modbus TLS und mDNS-Suche.

Ablauf laut Hersteller (Modbus API Documentation, Abschnitt „Modbus TLS
Bootstrapping"; Referenz-Implementierung
https://gitlab.com/triple-solar/public/modbus-demo, Stand 22.09.2026):

1. In myTripleSolar den Kopplungsmodus einschalten. Die Anlage startet
   einen EINMALIGEN CSR-Signierdienst, angekündigt per mDNS-SD als
   ``_ts-csr._tcp``. Er läuft höchstens eine Minute oder bis eine CSR
   eintrifft.
2. Der Client erzeugt Schlüssel + CSR und schickt die CSR per
   ``POST https://<host>:<port>/csr`` (selbstsigniert — es gibt noch
   keinen Vertrauensanker, daher ohne Prüfung).
3. Antwort: signiertes Client-Zertifikat + Root-CA der Anlage (PEM).
4. Schlüssel, Zertifikat, Root-CA und der Hostname (``hpcb-xxxxxx.local``)
   werden gespeichert; der Hostname bindet spätere Verbindungen an genau
   diese Anlage.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .modbus import TlsCredentials

CSR_SERVICE_TYPE = "_ts-csr._tcp.local."
MODBUS_TLS_SERVICE_TYPE = "_mbap-s._tcp.local."
MODBUS_TCP_SERVICE_TYPE = "_mbap._tcp.local."

DEFAULT_COMMON_NAME = "home-assistant-triplesolar"


class PairingError(Exception):
    """Kopplung fehlgeschlagen."""


class DiscoveryTimeout(PairingError):
    """Kein passender Dienst innerhalb der Wartezeit gefunden."""


@dataclass(frozen=True, slots=True)
class DiscoveredService:
    hostname: str  # ohne abschließenden Punkt, z. B. "hpcb-a1b2c3.local"
    address: str
    port: int


def normalize_hostname(hostname: str) -> str:
    return hostname.rstrip(".").lower()


async def discover_service(
    aiozc: AsyncZeroconf,
    service_type: str,
    *,
    timeout: float = 10.0,
    expected_hostname: str | None = None,
) -> DiscoveredService:
    """Erste Instanz von ``service_type`` — bei ``expected_hostname`` nur diese.

    Andere Anlagen im Netz (Duo-Kaskade, Nachbar im selben Segment) werden
    übergangen statt angenommen: wer den ersten Antwortenden nimmt, koppelt
    unter Umständen die falsche Wärmepumpe.
    """
    expected = normalize_hostname(expected_hostname) if expected_hostname else None
    loop = asyncio.get_running_loop()
    found: asyncio.Future[DiscoveredService] = loop.create_future()
    pending: set[asyncio.Task] = set()

    async def resolve(name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(aiozc.zeroconf, 3000):
            return
        if not info.server:
            return
        hostname = normalize_hostname(info.server)
        if expected is not None and hostname != expected:
            return
        addresses = info.parsed_addresses()
        if not addresses or info.port is None:
            return
        # IPv4 bevorzugen: link-local IPv6 braucht eine Scope-ID, die
        # asyncio.open_connection aus dem Adress-String nicht immer bekommt.
        address = next((a for a in addresses if ":" not in a), addresses[0])
        if not found.done():
            found.set_result(DiscoveredService(hostname, address, info.port))

    def on_change(zeroconf, service_type, name, state_change) -> None:  # noqa: ARG001
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            task = loop.create_task(resolve(name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(aiozc.zeroconf, [service_type], handlers=[on_change])
    try:
        return await asyncio.wait_for(found, timeout)
    except TimeoutError:
        what = f"{service_type} von {expected_hostname}" if expected_hostname else service_type
        raise DiscoveryTimeout(f"Kein Dienst {what} im Netz gefunden") from None
    finally:
        await browser.async_cancel()
        for task in pending:
            task.cancel()


@dataclass(frozen=True, slots=True)
class KeyAndCsr:
    key_pem: str
    csr_pem: str


def generate_key_and_csr(common_name: str = DEFAULT_COMMON_NAME) -> KeyAndCsr:
    """EC P-256 wie im Demo des Herstellers (CPU-lastig → Executor)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(key, hashes.SHA256())
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return KeyAndCsr(key_pem, csr.public_bytes(serialization.Encoding.PEM).decode())


def split_signing_response(pem_bundle: bytes) -> tuple[str, str]:
    """Antwort des Signierdienstes → (Client-Zertifikat, Root-CA)."""
    try:
        certs = x509.load_pem_x509_certificates(pem_bundle)
    except ValueError as exc:
        raise PairingError(f"Antwort enthält kein gültiges PEM: {exc}") from exc
    if len(certs) != 2:
        raise PairingError(f"Erwartet: Client-Zertifikat + Root-CA, erhalten: {len(certs)} Zertifikat(e)")
    leaf, root = certs
    return (
        leaf.public_bytes(serialization.Encoding.PEM).decode(),
        root.public_bytes(serialization.Encoding.PEM).decode(),
    )


async def request_signing(
    session: aiohttp.ClientSession, host: str, port: int, csr_pem: str, *, timeout: float = 10.0
) -> bytes:
    """Schickt die CSR an den Signierdienst.

    ``ssl=False`` ist hier Absicht und nur hier: dieser Aufruf BESORGT erst
    den Vertrauensanker. Alles danach läuft mit voller gegenseitiger Prüfung.
    """
    host_part = f"[{host}]" if ":" in host else host
    url = f"https://{host_part}:{port}/csr"
    try:
        async with session.post(
            url,
            data=csr_pem.encode(),
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.read()
            if resp.status != 200:
                raise PairingError(f"Signierdienst antwortet {resp.status}: {body[:200]!r}")
            return body
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise PairingError(f"CSR an {url} fehlgeschlagen: {exc}") from exc


async def pair(
    session: aiohttp.ClientSession,
    service: DiscoveredService,
    key_and_csr: KeyAndCsr,
) -> TlsCredentials:
    """CSR gegen Zertifikat tauschen — der Dienst nimmt genau EINE CSR an."""
    bundle = await request_signing(session, service.address, service.port, key_and_csr.csr_pem)
    client_cert, root_ca = split_signing_response(bundle)
    return TlsCredentials(
        root_ca_pem=root_ca,
        client_cert_pem=client_cert,
        client_key_pem=key_and_csr.key_pem,
        device_hostname=service.hostname,
    )
