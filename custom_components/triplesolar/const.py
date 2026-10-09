"""Konstanten der Triple-Solar-Integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "triplesolar"
MANUFACTURER: Final = "Triple Solar"
MODEL: Final = "PVT-Wärmepumpe 5.0"

# Art der Verbindung — ein Config-Entry ist genau eine davon. Lokal und
# Cloud dürfen für dieselbe Anlage nebeneinander eingerichtet sein: Modbus
# liefert die Momentanwerte, die Cloud die kumulierten Energiezähler.
CONF_CONNECTION: Final = "connection"
CONNECTION_MODBUS_TLS: Final = "modbus_tls"
CONNECTION_MODBUS_TCP: Final = "modbus_tcp"
CONNECTION_CLOUD: Final = "cloud"

CONF_UNIT_ID: Final = "unit_id"
CONF_MODULES: Final = "modules"
CONF_HOSTNAME: Final = "device_hostname"
CONF_ROOT_CA: Final = "root_ca_pem"
CONF_CLIENT_CERT: Final = "client_cert_pem"
CONF_CLIENT_KEY: Final = "client_key_pem"
CONF_HEAT_PUMP_ID: Final = "heat_pump_id"

DEFAULT_TLS_PORT: Final = 802
DEFAULT_TCP_PORT: Final = 502
# Die Doku nennt für TCP/TLS keine Unit-ID; das Hersteller-Demo sendet den
# pymodbus-Standard 1. (43 ist die RTU-Adresse und gilt nur am RS485.)
DEFAULT_UNIT_ID: Final = 1
DEFAULT_MODULES: Final = 1

DEFAULT_MODBUS_SCAN_S: Final = 30
DEFAULT_CLOUD_SCAN_S: Final = 300
MIN_MODBUS_SCAN_S: Final = 10
MIN_CLOUD_SCAN_S: Final = 120

# Der Kopplungsmodus ist eine Minute offen; die Suche lässt Luft für den
# Klick zwischen „in myTripleSolar einschalten" und „Weiter".
PAIRING_DISCOVERY_TIMEOUT_S: Final = 45
MODBUS_DISCOVERY_TIMEOUT_S: Final = 15

# Längste Pause zwischen zwei Leistungswerten, über die noch integriert wird.
# Danach fängt die Energiesumme neu an zu zählen statt eine Lücke von
# Stunden mit dem letzten Wert zu füllen.
MAX_INTEGRATION_GAP_S: Final = 600
