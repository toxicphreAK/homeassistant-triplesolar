"""Diagnosedaten — ohne Schlüssel, Zertifikate und Zugangsdaten."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import TripleSolarConfigEntry
from .const import CONF_CLIENT_CERT, CONF_CLIENT_KEY, CONF_HEAT_PUMP_ID, CONF_ROOT_CA
from .coordinator import ModbusCoordinator

# Die Wärmepumpen-ID „gewährt Zugriff auf die Wärmepumpe" (myTripleSolar) —
# im Entry als heat_pump_id, in den Cloud-Daten als status.id.
TO_REDACT = {
    CONF_CLIENT_KEY,
    CONF_CLIENT_CERT,
    CONF_ROOT_CA,
    CONF_PASSWORD,
    CONF_EMAIL,
    CONF_HEAT_PUMP_ID,
    "id",
}


def _plain(value: Any) -> Any:
    return value.name if isinstance(value, IntEnum) else value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TripleSolarConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    result: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
    }
    if isinstance(coordinator, ModbusCoordinator):
        result["data"] = {k: _plain(v) for k, v in (coordinator.data or {}).items()}
        result["unsupported_registers"] = sorted(coordinator.unsupported)
    else:
        result["data"] = async_redact_data(coordinator.data or {}, TO_REDACT)
    return result
