"""Rohantworten der myTripleSolar-Cloud ausgeben — zum Abgleich der Einheiten.

    TS_EMAIL=… TS_PASSWORD=… .venv/bin/python -m tools.probe_cloud [HEAT_PUMP_ID]

Ohne ID werden die Anlagen des Kontos gelistet. Mit ID: Status-Abfrage und
die Zählerstände der letzten 48 h, jeweils als JSON. Die Ausgabe enthält
keine Zugangsdaten, wohl aber die Wärmepumpen-ID (laut myTripleSolar
„gewährt Zugriff auf die Wärmepumpe") — nicht weitergeben.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

from custom_components.triplesolar.cloud import CloudClient


async def run(heat_pump_id: str | None) -> None:
    async with aiohttp.ClientSession() as session:
        client = CloudClient(session, os.environ["TS_EMAIL"], os.environ["TS_PASSWORD"])
        if heat_pump_id is None:
            for hp in await client.heat_pumps():
                print(f"{hp.id}  {hp.name}  online={hp.is_online}")
            return
        print(json.dumps(await client.status(heat_pump_id), indent=2, ensure_ascii=False))
        print(json.dumps(await client.energy_totals(heat_pump_id), indent=2))


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else None))
