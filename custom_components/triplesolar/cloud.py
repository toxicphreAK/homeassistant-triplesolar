"""Client für die myTripleSolar-Cloud (GraphQL) — INOFFIZIELL.

Die Schnittstelle ist nicht dokumentiert. Alles hier ist aus dem
Web-Frontend https://my.triplesolar.eu (SvelteKit + Houdini, Stand
09.10.2026) abgeleitet; Einzelheiten in ``docs/cloud-api.md``. Sie kann
sich mit jedem Frontend-Update ändern — die lokale Modbus-Schnittstelle
ist der dokumentierte Weg, die Cloud die Ergänzung für das, was Modbus
nicht liefert: die kumulierten Energiezähler je Betriebsart.

Authentifizierung: ``POST /auth/login`` mit ``{email, password}`` →
``{accessToken, refreshToken}`` (JWT mit ``exp``); ``POST /auth/refresh``
mit ``{refreshToken}`` und dem alten Access-Token als Bearer. Das Frontend
erneuert 15 s vor Ablauf; dasselbe hier.

Der Server nimmt nur Anfragen mit ``Origin: https://my.triplesolar.eu`` an
(sonst HTTP 403 „Unknown origin", noch vor der Prüfung der Zugangsdaten).
Der Browser setzt den Header selbst; hier wird er mitgeschickt.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from yarl import URL

BASE_URL = "https://my.triplesolar.eu"
REFRESH_MARGIN_S = 15

QUERY_HEAT_PUMPS = """
query HeatPumps {
  heatPumps { id name isOnline alerts { code severity message } }
}
"""

# Gekürzt auf die Felder, die die Integration auswertet — dieselbe Struktur
# wie die Abfrage des Frontends, ohne Cache-Felder (__typename).
QUERY_STATUS = """
query HeatPumpStatus($id: String!, $refresh: Boolean) {
  heatPump(id: $id, refresh: $refresh) {
    id
    alerts { code severity message }
    softwareVersion
    connectionType
    lastStatus
    controller {
      spaceConditioning {
        heatingStatus coolingStatus isHeatingActive isCoolingActive
        hasHeatingDemand hasCoolingDemand
        roomTemp roomSetpTemp sinkSupplySetpTemp ambientTemp
        heatingModulationLevels { heatPump resistanceHeater external }
        coolingModulationLevels { heatPump }
      }
      domesticHotWater {
        status isActive hasDemand tankTemp tankSetpTemp
        modulationLevels { heatPump resistanceHeater }
      }
    }
    heatPumpModule {
      sensors {
        sourceReturnTemp sourceSupplyTemp sinkReturnTemp sinkSupplyTemp
        sinkPumpFlow sourcePumpFlow
      }
    }
    ... on PikaHeatPump { pressures { sink source } }
  }
}
"""

QUERY_PERFORMANCE = """
query Performance($id: String!, $from: Time!, $until: Time, $interval: Duration) {
  timeSeries(id: $id, from: $from, until: $until, interval: $interval) {
    id
    data {
      timestamp
      ... on PikaTimeSeriesEntry {
        statistics {
          coolingTotalElectricEnergyConsumedkWh
          coolingTotalThermalEnergyProducedkWh
          dhwTotalElectricEnergyConsumedkWh
          dhwTotalThermalEnergyProducedkWh
          spaceHeatingTotalElectricEnergyConsumedkWh
          spaceHeatingTotalThermalEnergyProducedkWh
        }
      }
    }
  }
}
"""

ENERGY_FIELDS = (
    "spaceHeatingTotalElectricEnergyConsumedkWh",
    "spaceHeatingTotalThermalEnergyProducedkWh",
    "dhwTotalElectricEnergyConsumedkWh",
    "dhwTotalThermalEnergyProducedkWh",
    "coolingTotalElectricEnergyConsumedkWh",
    "coolingTotalThermalEnergyProducedkWh",
)


class CloudError(Exception):
    """Cloud nicht erreichbar oder Antwort unbrauchbar."""


class CloudAuthError(CloudError):
    """Anmeldung abgelehnt — Zugangsdaten prüfen (Reauth)."""


class CloudRejectedError(CloudError):
    """Server weist den Client ab, bevor er die Zugangsdaten prüft.

    Bisher nur die Herkunftsprüfung (HTTP 403 „Unknown origin"). Tritt das
    trotz ``Origin``-Header auf, hat sich die inoffizielle Schnittstelle
    geändert — ein Reauth hilft dann nicht.
    """


@dataclass(frozen=True, slots=True)
class CloudHeatPump:
    id: str
    name: str
    is_online: bool | None


def jwt_expiry(token: str) -> float | None:
    """``exp`` aus einem JWT, ohne Signaturprüfung (die prüft der Server)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def latest_energy_totals(entries: list[dict[str, Any]]) -> dict[str, float]:
    """Neueste Zählerstände aus ``timeSeries.data``.

    Die ``…TotalkWh``-Felder sind ZÄHLERSTÄNDE, keine Intervallwerte — das
    Frontend bildet die Differenz aufeinanderfolgender Einträge. Je Feld
    zählt der jüngste Eintrag, der einen Wert trägt (``null`` heißt
    „noch kein Messwert", nicht 0).
    """
    with_stats = [e for e in entries if isinstance(e.get("statistics"), dict) and e.get("timestamp")]
    with_stats.sort(key=lambda e: e["timestamp"])
    totals: dict[str, float] = {}
    for entry in with_stats:
        for field in ENERGY_FIELDS:
            value = entry["statistics"].get(field)
            if isinstance(value, (int, float)):
                totals[field] = float(value)
    return totals


class CloudClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._base = base_url.rstrip("/")
        self._origin = str(URL(self._base).origin())
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._access: str | None = None
        self._refresh: str | None = None

    async def login(self) -> None:
        data = await self._post_json(
            "/auth/login", {"email": self._email, "password": self._password}, auth=False
        )
        self._set_tokens(data)
        if not self._access:
            raise CloudAuthError("Login ohne Access-Token beantwortet")

    async def _ensure_token(self) -> None:
        if not self._access:
            await self.login()
            return
        exp = jwt_expiry(self._access)
        if exp is None or exp - REFRESH_MARGIN_S > time.time():
            return
        if self._refresh:
            try:
                data = await self._post_json("/auth/refresh", {"refreshToken": self._refresh}, auth=True)
            except CloudAuthError:
                data = None
            if data and data.get("accessToken"):
                self._set_tokens(data)
                return
        await self.login()

    def _set_tokens(self, data: dict[str, Any]) -> None:
        if data.get("accessToken"):
            self._access = data["accessToken"]
        if data.get("refreshToken"):
            self._refresh = data["refreshToken"]

    async def _post_json(self, path: str, body: dict[str, Any], *, auth: bool) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Origin": self._origin}
        if auth and self._access:
            headers["Authorization"] = f"Bearer {self._access}"
        try:
            async with self._session.post(
                self._base + path, json=body, headers=headers, timeout=self._timeout
            ) as resp:
                text = await resp.text()
                if resp.status == 403 and "unknown origin" in text.lower():
                    raise CloudRejectedError(f"{path}: HTTP 403 {text[:200]} (Herkunftsprüfung)")
                if resp.status in (400, 401, 403):
                    raise CloudAuthError(f"{path}: HTTP {resp.status} {text[:200]}")
                if resp.status >= 300:
                    raise CloudError(f"{path}: HTTP {resp.status} {text[:200]}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CloudError(f"{path}: {exc}") from exc
        try:
            return json.loads(text)
        except ValueError as exc:
            raise CloudError(f"{path}: keine JSON-Antwort") from exc

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._ensure_token()
        for attempt in (1, 2):
            try:
                result = await self._post_json(
                    "/graphql", {"query": query, "variables": variables or {}}, auth=True
                )
            except CloudAuthError:
                if attempt == 2:
                    raise
                # Token serverseitig verworfen (Logout an anderer Stelle):
                # einmal neu anmelden, dann aufgeben.
                self._access = None
                await self.login()
                continue
            errors = result.get("errors")
            if errors:
                raise CloudError("; ".join(str(e.get("message", e)) for e in errors))
            data = result.get("data")
            if not isinstance(data, dict):
                raise CloudError("GraphQL-Antwort ohne data")
            return data
        raise AssertionError("unreachable")

    async def heat_pumps(self) -> list[CloudHeatPump]:
        data = await self.graphql(QUERY_HEAT_PUMPS)
        return [
            CloudHeatPump(str(hp["id"]), hp.get("name") or str(hp["id"]), hp.get("isOnline"))
            for hp in data.get("heatPumps") or []
        ]

    async def status(self, heat_pump_id: str, *, refresh: bool = False) -> dict[str, Any]:
        """Momentaufnahme. ``refresh=True`` holt live von der Anlage (über
        SIM/VPN) — nur selten verwenden, nicht bei jedem Abruf."""
        data = await self.graphql(QUERY_STATUS, {"id": heat_pump_id, "refresh": refresh})
        hp = data.get("heatPump")
        if not isinstance(hp, dict):
            raise CloudError("Wärmepumpe nicht gefunden oder kein Zugriff")
        return hp

    async def energy_totals(self, heat_pump_id: str, *, now: datetime | None = None) -> dict[str, float]:
        """Aktuelle Zählerstände (kWh) je Betriebsart.

        Abgefragt werden die letzten 48 h im Stundenraster — wie das
        Frontend für kurze Zeiträume (``01:00``); so liegt auch nach einem
        längeren Cloud-Ausfall der Anlage noch ein Wert im Fenster.
        """
        now = now or datetime.now(UTC)
        until = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        start = until - timedelta(hours=48)
        data = await self.graphql(
            QUERY_PERFORMANCE,
            {
                "id": heat_pump_id,
                "from": _iso(start),
                "until": _iso(until),
                "interval": "01:00",
            },
        )
        series = data.get("timeSeries") or {}
        return latest_energy_totals(series.get("data") or [])


def _iso(value: datetime) -> str:
    """ISO 8601 in UTC mit Millisekunden — wie ``Date.toISOString()``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
