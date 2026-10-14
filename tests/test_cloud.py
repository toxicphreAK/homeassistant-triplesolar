"""Cloud-Client gegen einen nachgebauten myTripleSolar-Server."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import web

from custom_components.triplesolar.cloud import (
    BASE_URL,
    CloudAuthError,
    CloudClient,
    CloudError,
    CloudRejectedError,
    _iso,
    jwt_expiry,
    latest_energy_totals,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


def make_jwt(exp: float) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp, 'sub': 'u1'})}.sig"


class FakeCloud:
    def __init__(self, token_lifetime: float = 3600) -> None:
        self.lifetime = token_lifetime
        self.logins = 0
        self.refreshes = 0
        self.valid: set[str] = set()
        self.queries: list[dict] = []
        self.password = "geheim"
        self.origin = ""  # wie der echte Server: nur die eigene Herkunft

    @web.middleware
    async def check_origin(self, request: web.Request, handler):
        if request.headers.get("Origin") != self.origin:
            return web.Response(status=403, text="Unknown origin")
        return await handler(request)

    def _issue(self) -> dict:
        access = make_jwt(time.time() + self.lifetime) + str(self.logins + self.refreshes)
        self.valid.add(access)
        return {"accessToken": access, "refreshToken": f"r{self.logins}{self.refreshes}"}

    async def login(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body != {"email": "a@b.de", "password": self.password}:
            return web.Response(status=401, text="invalid credentials")
        self.logins += 1
        return web.json_response(self._issue())

    async def refresh(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not body.get("refreshToken"):
            return web.Response(status=400)
        self.refreshes += 1
        return web.json_response(self._issue())

    async def graphql(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid:
            return web.Response(status=401, text="no authorization header")
        body = await request.json()
        self.queries.append(body)
        q = body["query"]
        if "heatPumps" in q:
            return web.json_response(
                {"data": {"heatPumps": [{"id": "HP1", "name": "Dach", "isOnline": True}]}}
            )
        if "HeatPumpStatus" in q:
            if body["variables"]["id"] != "HP1":
                return web.json_response({"data": {"heatPump": None}, "errors": [{"message": "not found"}]})
            return web.json_response({"data": {"heatPump": {"id": "HP1", "softwareVersion": "2026.09.08.1"}}})
        if "timeSeries" in q:
            return web.json_response(
                {
                    "data": {
                        "timeSeries": {
                            "id": "HP1",
                            "data": [
                                {
                                    "timestamp": "2026-10-09T09:00:00Z",
                                    "statistics": {
                                        "spaceHeatingTotalElectricEnergyConsumedkWh": 100.0,
                                        "dhwTotalElectricEnergyConsumedkWh": 50.0,
                                    },
                                },
                                {
                                    "timestamp": "2026-10-09T10:00:00Z",
                                    "statistics": {
                                        "spaceHeatingTotalElectricEnergyConsumedkWh": 101.5,
                                        "dhwTotalElectricEnergyConsumedkWh": None,
                                    },
                                },
                                {"timestamp": "2026-10-09T11:00:00Z", "__typename": "Other"},
                            ],
                        }
                    }
                }
            )
        return web.json_response({"errors": [{"message": "unknown query"}]})


@pytest.fixture
async def cloud():
    fake = FakeCloud()
    app = web.Application(middlewares=[fake.check_origin])
    app.router.add_post("/auth/login", fake.login)
    app.router.add_post("/auth/refresh", fake.refresh)
    app.router.add_post("/graphql", fake.graphql)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    fake.origin = fake.url
    async with aiohttp.ClientSession() as session:
        fake.session = session
        yield fake
    await runner.cleanup()


def client_for(fake: FakeCloud, password: str = "geheim") -> CloudClient:
    return CloudClient(fake.session, "a@b.de", password, base_url=fake.url)


async def test_login_and_list(cloud: FakeCloud) -> None:
    client = client_for(cloud)
    pumps = await client.heat_pumps()
    assert [(p.id, p.name, p.is_online) for p in pumps] == [("HP1", "Dach", True)]
    assert cloud.logins == 1


async def test_wrong_password(cloud: FakeCloud) -> None:
    with pytest.raises(CloudAuthError):
        await client_for(cloud, "falsch").login()


async def test_foreign_origin_is_rejected_not_auth_error(cloud: FakeCloud) -> None:
    # Herkunftsprüfung schlägt vor den Zugangsdaten zu — kein Reauth auslösen.
    cloud.origin = "https://anders.example"
    with pytest.raises(CloudRejectedError) as exc_info:
        await client_for(cloud).login()
    assert not isinstance(exc_info.value, CloudAuthError)


async def test_origin_is_sent_on_every_request(cloud: FakeCloud) -> None:
    cloud.lifetime = 5  # erzwingt /auth/refresh
    client = CloudClient(cloud.session, "a@b.de", "geheim", base_url=cloud.url + "/")
    await client.status("HP1")
    await client.status("HP1")
    assert cloud.refreshes >= 1


def test_default_origin_is_the_web_frontend() -> None:
    assert BASE_URL == "https://my.triplesolar.eu"
    assert CloudClient(None, "a@b.de", "x")._origin == "https://my.triplesolar.eu"  # type: ignore[arg-type]


async def test_refresh_before_expiry(cloud: FakeCloud) -> None:
    cloud.lifetime = 5  # läuft innerhalb der 15-s-Marge ab
    client = client_for(cloud)
    await client.status("HP1")
    await client.status("HP1")
    assert cloud.logins == 1
    assert cloud.refreshes >= 1


async def test_relogin_after_server_side_logout(cloud: FakeCloud) -> None:
    client = client_for(cloud)
    await client.status("HP1")
    cloud.valid.clear()  # Token serverseitig verworfen
    status = await client.status("HP1")
    assert status["softwareVersion"] == "2026.09.08.1"
    assert cloud.logins == 2


async def test_graphql_error(cloud: FakeCloud) -> None:
    with pytest.raises(CloudError, match="not found"):
        await client_for(cloud).status("HP2")


async def test_energy_totals(cloud: FakeCloud) -> None:
    client = client_for(cloud)
    totals = await client.energy_totals("HP1", now=datetime(2026, 10, 9, 11, 20, tzinfo=UTC))
    # Je Feld der jüngste Eintrag MIT Wert — null überschreibt nicht.
    assert totals == {
        "spaceHeatingTotalElectricEnergyConsumedkWh": 101.5,
        "dhwTotalElectricEnergyConsumedkWh": 50.0,
    }
    variables = cloud.queries[-1]["variables"]
    assert variables["interval"] == "01:00"
    assert variables["until"] == "2026-10-09T12:00:00.000Z"
    assert variables["from"] == "2026-10-07T12:00:00.000Z"


def test_jwt_expiry() -> None:
    assert jwt_expiry(make_jwt(1234)) == 1234
    assert jwt_expiry("kaputt") is None


def test_latest_energy_totals_order_independent() -> None:
    entries = [
        {"timestamp": "2026-10-09T10:00:00Z", "statistics": {"dhwTotalThermalEnergyProducedkWh": 9}},
        {"timestamp": "2026-10-09T09:00:00Z", "statistics": {"dhwTotalThermalEnergyProducedkWh": 8}},
    ]
    assert latest_energy_totals(entries) == {"dhwTotalThermalEnergyProducedkWh": 9.0}


def test_iso() -> None:
    assert _iso(datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)) == "2026-01-02T03:04:05.678Z"
