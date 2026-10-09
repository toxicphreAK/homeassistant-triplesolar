"""Einrichtung: Modbus TLS (mit Kopplung), Modbus TCP oder myTripleSolar-Cloud."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .cloud import CloudAuthError, CloudClient, CloudError, CloudHeatPump
from .const import (
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CONNECTION,
    CONF_HEAT_PUMP_ID,
    CONF_HOSTNAME,
    CONF_MODULES,
    CONF_ROOT_CA,
    CONF_UNIT_ID,
    CONNECTION_CLOUD,
    CONNECTION_MODBUS_TCP,
    CONNECTION_MODBUS_TLS,
    DEFAULT_CLOUD_SCAN_S,
    DEFAULT_MODBUS_SCAN_S,
    DEFAULT_MODULES,
    DEFAULT_TCP_PORT,
    DEFAULT_TLS_PORT,
    DEFAULT_UNIT_ID,
    DOMAIN,
    MIN_CLOUD_SCAN_S,
    MIN_MODBUS_SCAN_S,
    MODBUS_DISCOVERY_TIMEOUT_S,
    PAIRING_DISCOVERY_TIMEOUT_S,
)
from .coordinator import async_create_modbus_client
from .modbus import ModbusError, TlsCredentials
from .pairing import (
    CSR_SERVICE_TYPE,
    MODBUS_TLS_SERVICE_TYPE,
    DiscoveryTimeout,
    PairingError,
    discover_service,
    generate_key_and_csr,
    pair,
)

_LOGGER = logging.getLogger(__name__)

MODULES_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=["1", "2"], mode=SelectSelectorMode.LIST, translation_key="modules")
)


async def async_test_modbus(hass, data: dict[str, Any]) -> None:
    """Liest Register 0x0 (Gesamtzustand) — scheitert mit ModbusError."""
    client = await async_create_modbus_client(hass, data)
    try:
        await client.read_input_registers(0x0, 1)
    finally:
        await client.close()


class TripleSolarConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._modules = DEFAULT_MODULES
        self._creds: TlsCredentials | None = None
        self._pair_task: asyncio.Task[TlsCredentials] | None = None
        self._pair_error: str | None = None
        self._failed_form_shown = False
        self._tls_error: str | None = None
        self._cloud: CloudClient | None = None
        self._cloud_login: dict[str, str] = {}
        self._cloud_pumps: list[CloudHeatPump] = []

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return TripleSolarOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=[CONNECTION_MODBUS_TLS, CONNECTION_MODBUS_TCP, CONNECTION_CLOUD]
        )

    # --- Modbus TLS: koppeln ----------------------------------------------

    async def async_step_modbus_tls(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Anleitung; nach „Absenden" läuft die Kopplung als Fortschritt."""
        if user_input is None:
            return self.async_show_form(
                step_id=CONNECTION_MODBUS_TLS,
                data_schema=vol.Schema({vol.Required(CONF_MODULES, default="1"): MODULES_SELECTOR}),
            )
        self._modules = int(user_input[CONF_MODULES])
        self._pair_error = None
        return await self.async_step_pairing()

    async def async_step_pairing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._pair_task is None:
            self._pair_task = self.hass.async_create_task(self._async_pair())
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pairing", progress_action="pairing", progress_task=self._pair_task
            )
        task, self._pair_task = self._pair_task, None
        self._failed_form_shown = False
        try:
            self._creds = task.result()
        except DiscoveryTimeout:
            self._pair_error = "pairing_not_found"
            return self.async_show_progress_done(next_step_id="pairing_failed")
        except PairingError as exc:
            _LOGGER.warning("Kopplung fehlgeschlagen: %s", exc)
            self._pair_error = "pairing_failed"
            return self.async_show_progress_done(next_step_id="pairing_failed")
        return self.async_show_progress_done(next_step_id="pairing_done")

    async def _async_pair(self) -> TlsCredentials:
        aiozc = await zeroconf.async_get_async_instance(self.hass)
        service = await discover_service(aiozc, CSR_SERVICE_TYPE, timeout=PAIRING_DISCOVERY_TIMEOUT_S)
        key_and_csr = await self.hass.async_add_executor_job(generate_key_and_csr)
        return await pair(async_get_clientsession(self.hass), service, key_and_csr)

    async def async_step_pairing_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Fehler anzeigen; „Absenden" versucht es erneut.

        Nach ``progress_done`` reicht HA die Eingabe des VORIGEN Schritts an
        diesen weiter. Ohne die Sperre würde eine sofort scheiternde Kopplung
        (Eager Task) sich selbst als „erneut versuchen" lesen — endlos. Ein
        neuer Versuch zählt daher nur, wenn das Formular wirklich gezeigt
        wurde.
        """
        if user_input is not None and self._failed_form_shown:
            self._pair_error = None
            return await self.async_step_pairing()
        self._failed_form_shown = True
        return self.async_show_form(step_id="pairing_failed", errors={"base": self._pair_error or "unknown"})

    async def async_step_pairing_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Gekoppelt — jetzt den Modbus-TLS-Dienst derselben Anlage suchen."""
        assert self._creds is not None
        await self.async_set_unique_id(self._creds.device_hostname)
        self._abort_if_unique_id_configured()
        try:
            aiozc = await zeroconf.async_get_async_instance(self.hass)
            found = await discover_service(
                aiozc,
                MODBUS_TLS_SERVICE_TYPE,
                timeout=MODBUS_DISCOVERY_TIMEOUT_S,
                expected_hostname=self._creds.device_hostname,
            )
        except PairingError:
            return await self.async_step_tls_host()
        result = await self._async_finish_tls(found.address, found.port)
        if result is not None:
            return result
        # Gefunden, aber nicht erreichbar: die Kopplung ist verbraucht (der
        # Signierdienst nimmt nur eine CSR) — also nicht neu koppeln lassen,
        # sondern mit dem Zertifikat eine andere Adresse anbieten.
        self._tls_error = "cannot_connect"
        return await self.async_step_tls_host()

    async def async_step_tls_host(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Fallback ohne mDNS: Adresse von Hand (z. B. aus dem Router)."""
        assert self._creds is not None
        errors: dict[str, str] = {}
        if self._tls_error:
            errors["base"], self._tls_error = self._tls_error, None
        elif user_input is not None:
            result = await self._async_finish_tls(user_input[CONF_HOST], int(user_input[CONF_PORT]))
            if result is not None:
                return result
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="tls_host",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_TLS_PORT): int,
                }
            ),
            description_placeholders={"hostname": self._creds.device_hostname},
            errors=errors,
        )

    async def _async_finish_tls(self, host: str, port: int) -> ConfigFlowResult | None:
        assert self._creds is not None
        data = {
            CONF_CONNECTION: CONNECTION_MODBUS_TLS,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_UNIT_ID: DEFAULT_UNIT_ID,
            CONF_MODULES: self._modules,
            CONF_HOSTNAME: self._creds.device_hostname,
            CONF_ROOT_CA: self._creds.root_ca_pem,
            CONF_CLIENT_CERT: self._creds.client_cert_pem,
            CONF_CLIENT_KEY: self._creds.client_key_pem,
        }
        try:
            await async_test_modbus(self.hass, data)
        except ModbusError as exc:
            _LOGGER.warning("Modbus TLS zu %s:%s: %s", host, port, exc)
            return None
        return self.async_create_entry(title=self._creds.device_hostname, data=data)

    # --- Modbus TCP --------------------------------------------------------

    async def async_step_modbus_tcp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_CONNECTION: CONNECTION_MODBUS_TCP,
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_UNIT_ID: int(user_input[CONF_UNIT_ID]),
                CONF_MODULES: int(user_input[CONF_MODULES]),
            }
            await self.async_set_unique_id(f"{data[CONF_HOST]}:{data[CONF_PORT]}")
            self._abort_if_unique_id_configured()
            try:
                await async_test_modbus(self.hass, data)
            except ModbusError as exc:
                _LOGGER.warning("Modbus TCP zu %s: %s", data[CONF_HOST], exc)
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=data[CONF_HOST], data=data)
        return self.async_show_form(
            step_id=CONNECTION_MODBUS_TCP,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_TCP_PORT): int,
                    vol.Required(CONF_UNIT_ID, default=DEFAULT_UNIT_ID): vol.All(int, vol.Range(1, 247)),
                    vol.Required(CONF_MODULES, default="1"): MODULES_SELECTOR,
                }
            ),
            errors=errors,
        )

    # --- Cloud ---------------------------------------------------------------

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._cloud_login = {CONF_EMAIL: user_input[CONF_EMAIL], CONF_PASSWORD: user_input[CONF_PASSWORD]}
            self._cloud = CloudClient(
                async_get_clientsession(self.hass), user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            try:
                await self._cloud.login()
                self._cloud_pumps = await self._cloud.heat_pumps()
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudError as exc:
                _LOGGER.warning("myTripleSolar: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                if not self._cloud_pumps:
                    errors["base"] = "no_heat_pumps"
                elif len(self._cloud_pumps) == 1:
                    return await self._async_finish_cloud(self._cloud_pumps[0])
                else:
                    return await self.async_step_cloud_select()
        return self.async_show_form(
            step_id=CONNECTION_CLOUD,
            data_schema=vol.Schema({vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_cloud_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            pump = next(p for p in self._cloud_pumps if p.id == user_input[CONF_HEAT_PUMP_ID])
            return await self._async_finish_cloud(pump)
        return self.async_show_form(
            step_id="cloud_select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HEAT_PUMP_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[{"value": p.id, "label": p.name} for p in self._cloud_pumps]
                        )
                    )
                }
            ),
        )

    async def _async_finish_cloud(self, pump: CloudHeatPump) -> ConfigFlowResult:
        await self.async_set_unique_id(f"cloud-{pump.id}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"{pump.name} (Cloud)",
            data={CONF_CONNECTION: CONNECTION_CLOUD, CONF_HEAT_PUMP_ID: pump.id, **self._cloud_login},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = CloudClient(
                async_get_clientsession(self.hass), entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            try:
                await client.login()
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


class TripleSolarOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        is_cloud = entry.data[CONF_CONNECTION] == CONNECTION_CLOUD
        if user_input is not None:
            options = {CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            if not is_cloud:
                options[CONF_MODULES] = int(user_input[CONF_MODULES])
                options[CONF_UNIT_ID] = int(user_input[CONF_UNIT_ID])
            return self.async_create_entry(data=options)

        default_scan = DEFAULT_CLOUD_SCAN_S if is_cloud else DEFAULT_MODBUS_SCAN_S
        minimum = MIN_CLOUD_SCAN_S if is_cloud else MIN_MODBUS_SCAN_S
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_SCAN_INTERVAL, default=entry.options.get(CONF_SCAN_INTERVAL, default_scan)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=minimum, max=3600, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                )
            ),
        }
        if not is_cloud:
            modules = entry.options.get(CONF_MODULES, entry.data.get(CONF_MODULES, DEFAULT_MODULES))
            unit = entry.options.get(CONF_UNIT_ID, entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID))
            schema[vol.Required(CONF_MODULES, default=str(modules))] = MODULES_SELECTOR
            schema[vol.Required(CONF_UNIT_ID, default=unit)] = vol.All(vol.Coerce(int), vol.Range(1, 247))
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
