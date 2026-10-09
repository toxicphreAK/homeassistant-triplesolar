"""Inbetriebnahme: koppeln und alle dokumentierten Register einmal auslesen.

    .venv/bin/python -m tools.probe_modbus pair            # Kopplungsmodus vorher in myTripleSolar an
    .venv/bin/python -m tools.probe_modbus read [--host IP] [--modules 2]
    .venv/bin/python -m tools.probe_modbus read --tcp IP   # unverschlüsselt, Port 502

Die Zugangsdaten landen in ``certs/`` (gitignored — enthält den privaten
Schlüssel). Die Ausgabe ist der Abgleich für die offenen Punkte in
docs/modbus-api.md: Einheit der Verdichterfrequenz, Unit-ID, Verhalten
fehlender optionaler Fühler.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from enum import IntEnum
from pathlib import Path

import aiohttp
from zeroconf.asyncio import AsyncZeroconf

from custom_components.triplesolar.modbus import (
    ModbusClient,
    ModbusExceptionResponse,
    TlsCredentials,
    build_ssl_context,
)
from custom_components.triplesolar.pairing import (
    CSR_SERVICE_TYPE,
    MODBUS_TLS_SERVICE_TYPE,
    discover_service,
    generate_key_and_csr,
    pair,
)
from custom_components.triplesolar.registers import RegisterType, all_registers

CERTS = Path("certs")


def _show(value: object) -> str:
    return value.name if isinstance(value, IntEnum) else str(value)


async def cmd_pair() -> None:
    aiozc = AsyncZeroconf()
    try:
        print("Suche _ts-csr._tcp (Kopplungsmodus muss an sein) …")
        service = await discover_service(aiozc, CSR_SERVICE_TYPE, timeout=45)
        print(f"Gefunden: {service}")
        async with aiohttp.ClientSession() as session:
            creds = await pair(session, service, generate_key_and_csr())
    finally:
        await aiozc.async_close()
    CERTS.mkdir(exist_ok=True)
    target = CERTS / "credentials.json"
    target.touch(mode=0o600)
    target.write_text(json.dumps(dataclasses.asdict(creds), indent=2))
    print(f"Gekoppelt mit {creds.device_hostname}; gespeichert in {CERTS}/credentials.json")


async def cmd_read(host: str | None, tcp: str | None, modules: int, unit: int) -> None:
    if tcp:
        client = ModbusClient(tcp, 502, unit_id=unit)
    else:
        data = json.loads((CERTS / "credentials.json").read_text())
        creds = TlsCredentials(**data)
        if host is None:
            aiozc = AsyncZeroconf()
            try:
                found = await discover_service(
                    aiozc, MODBUS_TLS_SERVICE_TYPE, timeout=15, expected_hostname=creds.device_hostname
                )
            finally:
                await aiozc.async_close()
            host, port = found.address, found.port
        else:
            port = 802
        client = ModbusClient(
            host,
            port,
            unit_id=unit,
            ssl_context=build_ssl_context(creds),
            expected_hostname=creds.device_hostname,
        )
    try:
        for reg in all_registers(modules):
            read = (
                client.read_input_registers
                if reg.type is RegisterType.INPUT
                else client.read_holding_registers
            )
            try:
                raw = (await read(reg.address, 1))[0]
            except ModbusExceptionResponse as exc:
                print(f"{reg.type.value:7} 0x{reg.address:04X} {reg.key:28} — {exc}")
                continue
            value = _show(reg.decode(raw))
            print(f"{reg.type.value:7} 0x{reg.address:04X} {reg.key:28} raw={raw:5d} → {value}")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pair")
    read = sub.add_parser("read")
    read.add_argument("--host", help="IP der Anlage (sonst per mDNS)")
    read.add_argument("--tcp", help="Modbus TCP ohne TLS an dieser IP")
    read.add_argument("--modules", type=int, default=1)
    read.add_argument("--unit", type=int, default=1)
    args = parser.parse_args()
    if args.cmd == "pair":
        asyncio.run(cmd_pair())
    else:
        asyncio.run(cmd_read(args.host, args.tcp, args.modules, args.unit))


if __name__ == "__main__":
    main()
