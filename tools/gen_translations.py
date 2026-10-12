"""Erzeugt strings.json + translations/{en,de}.json aus EINER Tabelle.

Aufruf: python tools/gen_translations.py — danach prüft der Test
``tests/test_misc.py``, dass jede verwendete translation_key existiert.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "triplesolar"

# Schlüssel → (English, Deutsch)
STATE_PROFILE = {
    "blocked": ("Blocked", "Gesperrt"),
    "eco": ("Eco", "Eco"),
    "normal": ("Normal", "Normal"),
    "comfort": ("Comfort", "Komfort"),
}
STATE_CIRCUIT = {
    "fault": ("Fault", "Störung"),
    "off": ("Off", "Aus"),
    "running": ("Running", "Läuft"),
    "blocked": ("Blocked", "Gesperrt"),
    "await_heat_sources": ("Awaiting heat sources", "Wartet auf Wärmeerzeuger"),
    "prepare_enable_dhw_valve": ("Opening DHW valve", "Warmwasserventil öffnet"),
    "prepare_disable_dhw_valve": ("Closing DHW valve", "Warmwasserventil schließt"),
}
STATE_HP = {
    "fault": ("Fault", "Störung"),
    "off": ("Off", "Aus"),
    "dhw_prio": ("Hot water (priority)", "Warmwasser (Vorrang)"),
    "dhw_no_prio": ("Hot water", "Warmwasser"),
    "space_heating_prio": ("Heating (priority)", "Heizen (Vorrang)"),
    "space_heating_no_prio": ("Heating", "Heizen"),
    "space_cooling_prio": ("Cooling (priority)", "Kühlen (Vorrang)"),
    "space_cooling_no_prio": ("Cooling", "Kühlen"),
    "program_active": ("Program active", "Programm aktiv"),
    "dhw_handover": ("Handover to hot water", "Übergabe Warmwasser"),
    "space_heating_handover": ("Handover to heating", "Übergabe Heizen"),
    "space_cooling_handover": ("Handover to cooling", "Übergabe Kühlen"),
}
STATE_MODULE = {
    "fault": ("Fault", "Störung"),
    "off": ("Off", "Aus"),
    "heating_standby": ("Heating standby", "Heizen Bereitschaft"),
    "heating_pump_startup": ("Heating pump start-up", "Heizen Pumpenanlauf"),
    "heating_compressor_startup": ("Heating compressor start-up", "Heizen Verdichteranlauf"),
    "heating_active": ("Heating", "Heizen aktiv"),
    "heating_wind_down": ("Heating wind-down", "Heizen Nachlauf"),
    "cooling_standby": ("Cooling standby", "Kühlen Bereitschaft"),
    "cooling_pump_startup": ("Cooling pump start-up", "Kühlen Pumpenanlauf"),
    "cooling_compressor_startup": ("Cooling compressor start-up", "Kühlen Verdichteranlauf"),
    "cooling_active": ("Cooling", "Kühlen aktiv"),
    "cooling_wind_down": ("Cooling wind-down", "Kühlen Nachlauf"),
}
STATE_OPMODE = {"off": ("Off", "Aus"), "heating": ("Heating", "Heizen"), "cooling": ("Cooling", "Kühlen")}

M = " {module}"
SENSORS = {
    "heat_pump_state": (("Operating state", "Betriebszustand"), STATE_HP),
    "rated_power": (("Rated power", "Nennleistung"), None),
    "active_messages": (("Active messages", "Aktive Meldungen"), None),
    "dhw_status": (("Hot water status", "Warmwasser Status"), STATE_CIRCUIT),
    "dhw_modulation": (("Hot water modulation", "Warmwasser Modulation"), None),
    "dhw_tank_temp": (("Hot water tank temperature", "Warmwasser Speichertemperatur"), None),
    "dhw_tank_setpoint": (("Hot water tank setpoint", "Warmwasser Solltemperatur"), None),
    "dhw_profile": (("Hot water profile", "Warmwasser Profil"), STATE_PROFILE),
    "sh_status": (("Heating status", "Heizen Status"), STATE_CIRCUIT),
    "sh_modulation": (("Heating modulation", "Heizen Modulation"), None),
    "sh_supply_temp": (("Heating supply temperature", "Heizen Vorlauftemperatur"), None),
    "sh_supply_setpoint": (("Heating supply setpoint", "Heizen Vorlauf-Soll"), None),
    "sh_profile": (("Heating profile", "Heizen Profil"), STATE_PROFILE),
    "sc_status": (("Cooling status", "Kühlen Status"), STATE_CIRCUIT),
    "sc_modulation": (("Cooling modulation", "Kühlen Modulation"), None),
    "sc_supply_temp": (("Cooling supply temperature", "Kühlen Vorlauftemperatur"), None),
    "sc_supply_setpoint": (("Cooling supply setpoint", "Kühlen Vorlauf-Soll"), None),
    "sc_modulation_setpoint": (
        ("Cooling modulation setpoint (OpenTherm)", "Kühlen Modulations-Soll (OpenTherm)"),
        None,
    ),
    "ambient_temp": (("Outdoor temperature", "Außentemperatur"), None),
    "room_temp": (("Room temperature", "Raumtemperatur"), None),
    "room_setpoint": (("Room setpoint", "Raum-Solltemperatur"), None),
    "relative_humidity": (("Relative humidity", "Relative Luftfeuchte"), None),
    "input_modulation": (("Requested modulation", "Angeforderte Modulation"), None),
    "module_state": (("Module" + M + " state", "Modul" + M + " Zustand"), STATE_MODULE),
    "compressor_frequency": (
        ("Module" + M + " compressor frequency", "Modul" + M + " Verdichterfrequenz"),
        None,
    ),
    "source_pump": (("Module" + M + " source pump", "Modul" + M + " Quellenpumpe"), None),
    "sink_pump": (("Module" + M + " circulation pump", "Modul" + M + " Umwälzpumpe"), None),
    "source_return_temp": (
        ("Module" + M + " source return (from PVT)", "Modul" + M + " Quelle Rücklauf (vom PVT)"),
        None,
    ),
    "source_supply_temp": (
        ("Module" + M + " source supply (to PVT)", "Modul" + M + " Quelle Vorlauf (zum PVT)"),
        None,
    ),
    "sink_return_temp": (("Module" + M + " heating return", "Modul" + M + " Heizung Rücklauf"), None),
    "sink_supply_temp": (("Module" + M + " heating supply", "Modul" + M + " Heizung Vorlauf"), None),
    "electric_power": (("Module" + M + " electric power", "Modul" + M + " elektrische Leistung"), None),
    "sink_power": (("Module" + M + " thermal power", "Modul" + M + " Wärmeleistung"), None),
    "compressor_starts": (("Module" + M + " compressor starts", "Modul" + M + " Verdichterstarts"), None),
    "cop": (("Module" + M + " COP", "Modul" + M + " Leistungszahl"), None),
    "electric_energy": (
        ("Module" + M + " electric energy (integrated)", "Modul" + M + " Stromverbrauch (integriert)"),
        None,
    ),
    "heat_energy_heating": (
        ("Module" + M + " heat produced (integrated)", "Modul" + M + " Wärme erzeugt (integriert)"),
        None,
    ),
    "heat_energy_cooling": (
        ("Module" + M + " cooling produced (integrated)", "Modul" + M + " Kälte erzeugt (integriert)"),
        None,
    ),
    # Cloud
    "dhw_status_cloud": (("Hot water status", "Warmwasser Status"), None),
    "heating_status_cloud": (("Heating status", "Heizen Status"), None),
    "dhw_heater_modulation": (("Hot water heating element", "Warmwasser Heizstab"), None),
    "sh_heater_modulation": (("Heating element", "Heizen Heizstab"), None),
    "source_return_temp_cloud": (("Source return (from PVT)", "Quelle Rücklauf (vom PVT)"), None),
    "source_supply_temp_cloud": (("Source supply (to PVT)", "Quelle Vorlauf (zum PVT)"), None),
    "sink_return_temp_cloud": (("Heating return", "Heizung Rücklauf"), None),
    "sink_supply_temp_cloud": (("Heating supply", "Heizung Vorlauf"), None),
    "sink_pump_flow": (("Heating flow", "Durchfluss Heizung"), None),
    "source_pump_flow": (("Source flow", "Durchfluss Quelle"), None),
    "sink_pressure": (("Heating pressure", "Druck Heizung"), None),
    "source_pressure": (("Source pressure", "Druck Quelle"), None),
    "alerts": (("Alerts", "Meldungen"), None),
    "software_version": (("Firmware", "Firmware"), None),
    "connection_type": (("Cloud connection", "Cloud-Verbindung"), None),
    "sh_electric_energy": (("Heating electric energy", "Heizen Stromverbrauch"), None),
    "sh_thermal_energy": (("Heating heat produced", "Heizen Wärme erzeugt"), None),
    "dhw_electric_energy": (("Hot water electric energy", "Warmwasser Stromverbrauch"), None),
    "dhw_thermal_energy": (("Hot water heat produced", "Warmwasser Wärme erzeugt"), None),
    "sc_electric_energy": (("Cooling electric energy", "Kühlen Stromverbrauch"), None),
    "sc_thermal_energy": (("Cooling produced", "Kühlen Kälte erzeugt"), None),
    "total_electric_energy": (("Total electric energy", "Stromverbrauch gesamt"), None),
    "total_thermal_energy": (("Total heat produced", "Wärme erzeugt gesamt"), None),
}
BINARY = {
    "fault": ("Fault", "Störung"),
    "dhw_demand": ("Hot water demand", "Warmwasser Anforderung"),
    "dhw_valve": ("Hot water valve", "Warmwasserventil"),
    "sh_demand": ("Heating demand", "Heizen Anforderung"),
    "sc_demand": ("Cooling demand", "Kühlen Anforderung"),
    "cooling_valve": ("Module" + M + " cooling valve", "Modul" + M + " Kühlventil"),
    "heating_active": ("Heating active", "Heizen aktiv"),
    "dhw_active": ("Hot water active", "Warmwasser aktiv"),
}
SELECTS = {
    "dhw_profile_set": (("Hot water profile", "Warmwasser Profil"), STATE_PROFILE),
    "sh_profile_set": (("Heating profile", "Heizen Profil"), STATE_PROFILE),
    "sc_profile_set": (("Cooling profile", "Kühlen Profil"), STATE_PROFILE),
    "control_operating_mode": (
        ("Modbus control: operating mode", "Modbus-Steuerung: Betriebsart"),
        STATE_OPMODE,
    ),
}
P = " {profile}"
NUMBERS = {
    "dhw_profile_setpoint": ("Hot water setpoint" + P, "Warmwasser Soll" + P),
    "dhw_profile_hysteresis": ("Hot water hysteresis" + P, "Warmwasser Hysterese" + P),
    "dhw_profile_modulation": ("Hot water modulation" + P, "Warmwasser Modulation" + P),
    "control_modulation": ("Modbus control: modulation", "Modbus-Steuerung: Modulation"),
    "control_power_budget": ("Modbus control: power budget", "Modbus-Steuerung: Leistungsbudget"),
}
SWITCHES = {
    "sh_enable": ("Heating", "Heizen"),
    "dhw_enable": ("Hot water", "Warmwasser"),
    "sc_enable": ("Cooling", "Kühlen"),
}

CONFIG = {
    "step": {
        "user": {
            "title": ("Connect Triple Solar heat pump", "Triple-Solar-Wärmepumpe verbinden"),
            "menu_options": {
                "modbus_tls": ("Local – Modbus TLS (recommended)", "Lokal – Modbus TLS (empfohlen)"),
                "modbus_tcp": ("Local – Modbus TCP (unencrypted)", "Lokal – Modbus TCP (unverschlüsselt)"),
                "cloud": ("myTripleSolar cloud (energy counters)", "myTripleSolar-Cloud (Energiezähler)"),
            },
        },
        "modbus_tls": {
            "title": ("Pair via Modbus TLS", "Koppeln per Modbus TLS"),
            "description": (
                "1. Connect the heat pump to your LAN by cable.\n2. In myTripleSolar enable **pairing mode** (it stays open for one minute).\n3. Press Submit right away – Home Assistant looks for the heat pump via mDNS and pairs.",
                "1. Die Wärmepumpe per Kabel ins Heimnetz bringen.\n2. In myTripleSolar den **Kopplungsmodus** einschalten (er ist eine Minute offen).\n3. Sofort auf Absenden klicken – Home Assistant sucht die Anlage per mDNS und koppelt.",
            ),
            "data": {"modules": ("Heat pump modules", "Wärmepumpenmodule")},
            "data_description": {
                "modules": (
                    "2 for a PVT heat pump 5.0 Duo (cascade).",
                    "2 bei einer PVT-Wärmepumpe 5.0 Duo (Kaskade).",
                )
            },
        },
        "pairing_failed": {
            "title": ("Pairing failed", "Kopplung fehlgeschlagen"),
            "description": (
                "Re-enable pairing mode in myTripleSolar, then press Submit to try again.",
                "Kopplungsmodus in myTripleSolar erneut einschalten, dann Absenden für einen neuen Versuch.",
            ),
        },
        "tls_host": {
            "title": ("Heat pump address", "Adresse der Wärmepumpe"),
            "description": (
                "Paired with {hostname}, but its Modbus TLS service was not found via mDNS. Enter its IP address (e.g. from your router).",
                "Gekoppelt mit {hostname}, aber deren Modbus-TLS-Dienst war per mDNS nicht zu finden. IP-Adresse eingeben (z. B. aus dem Router).",
            ),
            "data": {"host": ("Host", "Host"), "port": ("Port", "Port")},
        },
        "modbus_tcp": {
            "title": ("Modbus TCP", "Modbus TCP"),
            "description": (
                "Unencrypted Modbus TCP must first be enabled in myTripleSolar (I/O → Modbus TCP mode: Insecure). Use only in an isolated network.",
                "Unverschlüsseltes Modbus TCP muss erst in myTripleSolar eingeschaltet werden (I/O → Modbus-TCP-Modus: Unsicher). Nur in einem abgeschotteten Netz verwenden.",
            ),
            "data": {
                "host": ("Host", "Host"),
                "port": ("Port", "Port"),
                "unit_id": ("Unit ID", "Unit-ID"),
                "modules": ("Heat pump modules", "Wärmepumpenmodule"),
            },
        },
        "cloud": {
            "title": ("myTripleSolar", "myTripleSolar"),
            "description": (
                "Unofficial interface of the myTripleSolar web app. Read-only.",
                "Inoffizielle Schnittstelle der myTripleSolar-Web-App. Nur lesend.",
            ),
            "data": {"email": ("E-mail", "E-Mail"), "password": ("Password", "Passwort")},
        },
        "cloud_select": {
            "title": ("Choose heat pump", "Wärmepumpe wählen"),
            "data": {"heat_pump_id": ("Heat pump", "Wärmepumpe")},
        },
        "reauth_confirm": {
            "title": ("Sign in again", "Erneut anmelden"),
            "description": ("Password for {email}", "Passwort für {email}"),
            "data": {"password": ("Password", "Passwort")},
        },
    },
    "progress": {
        "pairing": (
            "Looking for the heat pump in pairing mode (up to 45 s)…",
            "Suche die Wärmepumpe im Kopplungsmodus (bis 45 s)…",
        ),
    },
    "error": {
        "cannot_connect": ("Cannot connect", "Verbindung fehlgeschlagen"),
        "invalid_auth": ("Invalid credentials", "Anmeldedaten ungültig"),
        "no_heat_pumps": ("No heat pump in this account", "Keine Wärmepumpe in diesem Konto"),
        "pairing_not_found": (
            "No heat pump in pairing mode found. Is pairing mode enabled, and is Home Assistant in the same network (mDNS, UDP 5353)?",
            "Keine Wärmepumpe im Kopplungsmodus gefunden. Ist der Kopplungsmodus an, und ist Home Assistant im selben Netz (mDNS, UDP 5353)?",
        ),
        "pairing_failed": ("Pairing failed – see the log", "Kopplung fehlgeschlagen – Details im Log"),
        "unknown": ("Unexpected error", "Unerwarteter Fehler"),
    },
    "abort": {
        "already_configured": ("Already configured", "Bereits eingerichtet"),
        "reauth_successful": ("Signed in again", "Erneut angemeldet"),
    },
}
OPTIONS = {
    "step": {
        "init": {
            "title": ("Options", "Optionen"),
            "data": {
                "scan_interval": ("Polling interval", "Abfrageintervall"),
                "modules": ("Heat pump modules", "Wärmepumpenmodule"),
                "unit_id": ("Unit ID", "Unit-ID"),
            },
        }
    }
}
SELECTOR = {
    "modules": {
        "options": {
            "1": ("1 (PVT heat pump 5.0)", "1 (PVT-Wärmepumpe 5.0)"),
            "2": ("2 (Duo / cascade)", "2 (Duo / Kaskade)"),
        }
    }
}


def pick(node, lang: int):
    if isinstance(node, tuple):
        return node[lang]
    if isinstance(node, dict):
        return {k: pick(v, lang) for k, v in node.items()}
    raise TypeError(node)


def entity_block(lang: int) -> dict:
    def with_states(table):
        out = {}
        for key, (name, states) in table.items():
            entry = {"name": name[lang]}
            if states:
                entry["state"] = {s: v[lang] for s, v in states.items()}
            out[key] = entry
        return out

    return {
        "sensor": with_states(SENSORS),
        "binary_sensor": {k: {"name": v[lang]} for k, v in BINARY.items()},
        "select": with_states(SELECTS),
        "number": {k: {"name": v[lang]} for k, v in NUMBERS.items()},
        "switch": {k: {"name": v[lang]} for k, v in SWITCHES.items()},
    }


def build(lang: int) -> dict:
    return {
        "config": pick(CONFIG, lang),
        "options": pick(OPTIONS, lang),
        "selector": pick(SELECTOR, lang),
        "entity": entity_block(lang),
    }


def main() -> None:
    en, de = build(0), build(1)
    dump = lambda d: json.dumps(d, ensure_ascii=False, indent=2) + "\n"  # noqa: E731
    (ROOT / "strings.json").write_text(dump(en))
    (ROOT / "translations").mkdir(exist_ok=True)
    (ROOT / "translations" / "en.json").write_text(dump(en))
    (ROOT / "translations" / "de.json").write_text(dump(de))


if __name__ == "__main__":
    main()
