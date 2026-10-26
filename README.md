# Triple Solar PVT-Wärmepumpe 5.0 für Home Assistant

Custom-Integration für die **Triple Solar PVT-Wärmepumpe 5.0** (und 5.0 Duo), zwei Wege:

| Weg | Quelle | Liefert | Schreibt |
|---|---|---|---|
| **Lokal – Modbus TLS** (empfohlen) | [Modbus-API des Herstellers](docs/modbus-api.md), ab Firmware 2026.09.01.0 | Zustände, Temperaturen, Modulation, **elektrische und thermische Leistung in W**, Verdichterstarts | Profile, Freigaben, Warmwasser-Sollwerte, Modbus-Steuerung |
| Lokal – Modbus TCP | dieselbe API, unverschlüsselt | dasselbe | dasselbe |
| **Cloud – myTripleSolar** | [inoffizielle GraphQL-Schnittstelle](docs/cloud-api.md) der Web-App | Status, Temperaturen, **kWh-Zähler je Betriebsart** (Heizen/Warmwasser/Kühlen, elektrisch und thermisch) | nichts |

Beide dürfen nebeneinander laufen: Modbus für die Momentanwerte und die Steuerung, die
Cloud für die Energiezähler, die Modbus nicht hat. Sie erscheinen als zwei Geräte — es gibt
keine dokumentierte Zuordnung von Cloud-ID und Hostname.

> **Stand:** gebaut gegen die Herstellerdoku (Stand 25.09.2026), das Demo-Repository des
> Herstellers und das myTripleSolar-Frontend (09.10.2026); getestet gegen einen
> nachgebauten Modbus-/TLS-Server und eine nachgebaute Cloud (`tests/`). **Noch nicht an
> einer echten Anlage gelaufen** — die offenen Punkte stehen in
> [docs/modbus-api.md](docs/modbus-api.md#offene-punkte-am-laufenden-system-zu-prüfen) und
> [docs/cloud-api.md](docs/cloud-api.md#offene-punkte); `tools/probe_*.py` klären sie.

## Voraussetzungen

- Home Assistant **≥ 2025.3** (die CI testet 2025.3.4 und die aktuelle Version).
- Für Modbus: die Wärmepumpe **per Ethernet** im Heimnetz (die eingebaute SIM reicht nur
  für die Cloud), Firmware **≥ 2026.09.01.0** (Updates kommen automatisch per OTA).
- Für die Kopplung: Home Assistant und Wärmepumpe im **selben Netzsegment** — mDNS
  (UDP 5353) geht nicht über Router/VLAN-Grenzen. Danach reicht Unicast auf Port 802.
- Keine zusätzlichen Python-Pakete: Modbus ist selbst implementiert (`modbus.py`, ~200
  Zeilen), weil Home Assistants eigenes pymodbus in einer festen Version mitkommt, deren
  API sich in 3.x mehrfach geändert hat.

## Installation

HACS → Benutzerdefinierte Repositories → dieses Repository (Kategorie „Integration"), oder
`custom_components/triplesolar/` nach `<config>/custom_components/` kopieren. Neustart,
dann *Einstellungen → Geräte & Dienste → Integration hinzufügen → Triple Solar*.

Icon und Logo bringt die Integration in `brand/` selbst mit. Home Assistant zeigt sie ab
2026.3, ältere Versionen zeigen den Platzhalter. Logo und Bildmarke gehören
Triple Solar und kennzeichnen hier nur das unterstützte Gerät.

## Einrichtung

### Modbus TLS (Kopplung)

1. Ethernet anschließen.
2. In myTripleSolar den **Kopplungsmodus** einschalten — er ist **eine Minute** offen.
3. In Home Assistant „Lokal – Modbus TLS" wählen und **sofort** absenden. Die Integration
   sucht `_ts-csr._tcp`, erzeugt einen Schlüssel (EC P-256), lässt ihn von der Anlage
   signieren und sucht danach `_mbap-s._tcp` **mit demselben Hostnamen** — eine zweite
   Anlage im Netz (Duo, Nachbar) wird nicht versehentlich übernommen.
4. Findet mDNS den Modbus-Dienst nicht, fragt der Dialog nach der IP (Port 802).

Schlüssel und Zertifikate liegen im Config-Entry (`.storage/core.config_entries`) und sind
in den Diagnosedaten geschwärzt. Ändert sich die IP, sucht die Integration die Anlage
höchstens alle 5 min per mDNS unter ihrem Hostnamen und speichert die neue Adresse.

### Modbus TCP

Erst in myTripleSolar *I/O → Modbus-TCP-Modus → Unsicher* einschalten. Nur in einem
abgeschotteten Netz — jeder im Segment kann dann Profile und Sollwerte schreiben.

### Cloud

E-Mail und Passwort von myTripleSolar. Bei mehreren Anlagen im Konto wird gewählt. Abgelehnte
Anmeldung → Home Assistant fragt nach dem neuen Passwort (Reauth).

### Optionen

Abfrageintervall (Modbus Standard 30 s, min. 10 s; Cloud 300 s, min. 120 s),
Anzahl Module (2 = Duo), Unit-ID (Standard 1).

## Entitäten (Modbus)

| Plattform | Entitäten | standardmäßig aus |
|---|---|---|
| Sensor | Betriebszustand, Warmwasser/Heizen: Status, Modulation, Temperatur, Soll, Profil · Außen-/Raumtemperatur · je Modul: Zustand, Verdichterfrequenz, Pumpen, Quelle/Senke Vor-/Rücklauf, **elektrische Leistung**, **Wärmeleistung**, **Leistungszahl**, Verdichterstarts · **integrierte Energiezähler** (Strom, Wärme, Kälte) · aktive Meldungen (Codes als Attribut) | alles zum Kühlen, Raum-Soll, Feuchte, angeforderte Modulation |
| Binärsensor | Störung, Warmwasser-/Heizanforderung, Warmwasserventil | Kühlanforderung, Kühlventil |
| Select | **Warmwasser-Profil**, **Heizen-Profil** (gesperrt / eco / normal / comfort) | Kühlen-Profil, Modbus-Steuerung Betriebsart |
| Number | Warmwasser Soll / Hysterese / Modulation je Profil | Modbus-Steuerung Modulation, **Leistungsbudget (W)** |
| Switch | Heizen, Warmwasser | Kühlen |

Die integrierten Energiezähler (Trapezregel über die Leistung, Lücken > 10 min werden nicht
überbrückt, Summe übersteht Neustarts) taugen für das Energie-Dashboard, wenn keine Cloud
eingebunden ist. Mit Cloud sind deren Zähler die bessere Quelle: sie laufen auch, während
Home Assistant steht.

## Automationen

### PV-Überschuss in den Warmwasserspeicher

Profil „comfort" ist die Modbus-Entsprechung von SG-Ready „bevorzugter Betrieb"
(Regelungslogik 4.4). Voraussetzung: kein Zeitplan in myTripleSolar, der das Profil
zurückschaltet.

```yaml
alias: "WP – Warmwasser bei PV-Überschuss auf Komfort"
mode: single
triggers:
  - trigger: numeric_state
    entity_id: sensor.fronius_power_grid   # negativ = Einspeisung
    below: -2500
    for: "00:10:00"
    id: ueberschuss
  - trigger: numeric_state
    entity_id: sensor.fronius_power_grid
    above: 0
    for: "00:10:00"
    id: bezug
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: ueberschuss
          - condition: numeric_state
            entity_id: sensor.fronius_battery_state_of_charge
            above: 80
        sequence:
          - action: select.select_option
            target: { entity_id: select.pvt_warmepumpe_5_0_hot_water_profile }
            data: { option: comfort }
      - conditions:
          - condition: trigger
            id: bezug
        sequence:
          - action: select.select_option
            target: { entity_id: select.pvt_warmepumpe_5_0_hot_water_profile }
            data: { option: normal }
```

Geschrieben wird nur beim Wechsel, nicht zyklisch (ob die Anlage Schreibzugriffe im Flash
ablegt, ist offen). Die Entity-IDs hängen von der Sprache bei der Einrichtung ab.

### § 14a gehört NICHT in Home Assistant

Die netzdienliche Steuerung nach § 14a EnWG läuft über den **potentialfreien Eingang
„Leistungsbegrenzung"** (Kontakt zu → max. 3,5 kW, Heizelement 1 kW; siehe
[docs/regelung-und-eingaenge.md](docs/regelung-und-eingaenge.md)). Ein Steuerbefehl des
Netzbetreibers darf nicht davon abhängen, dass Home Assistant, WLAN oder ein Switch läuft.
Home Assistant liest den Zustand mit (Status „gesperrt", Leistung), steuert ihn aber nicht.

## Entwicklung

```sh
uv sync                             # .venv aus uv.lock (Python ≥ 3.14.2, wie Home Assistant)
.venv/bin/python -m pytest          # Register, Modbus/TLS, Kopplung, Cloud, HA-Setup, Flows
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python tools/gen_translations.py   # strings.json + translations/{en,de}.json
```

Die Tests laufen gegen einen nachgebauten Modbus-Server (`tests/fakes.py`) mit eigener PKI
— dieselbe gegenseitige TLS-Prüfung wie an der Anlage, inklusive Ablehnung eines fremden
Hostnamens und einer fremden CA.

### Inbetriebnahme an der echten Anlage

```sh
.venv/bin/python -m tools.probe_modbus pair      # Kopplungsmodus vorher an; legt certs/ an (gitignored)
.venv/bin/python -m tools.probe_modbus read      # jedes dokumentierte Register einmal, roh + dekodiert
TS_EMAIL=… TS_PASSWORD=… .venv/bin/python -m tools.probe_cloud [ID]
```

Eine Kopplung ist einmalig: Home Assistant braucht danach seine **eigene** (Kopplungsmodus
erneut einschalten), das Probe-Zertifikat ist nicht übertragbar.

## Aufbau

```
custom_components/triplesolar/
  registers.py    Registertabelle (einzige Stelle mit Adressen/Skalierung) + Lesepläne
  modbus.py       Modbus TCP/TLS-Client (FC 3/4/6), Peer-Prüfung über den SAN
  pairing.py      mDNS-Suche, CSR erzeugen, Zertifikat holen
  cloud.py        myTripleSolar: Login/Refresh, Status, Zählerstände
  coordinator.py  Abruf (Modbus/Cloud), Schreiben mit Rücklesen, mDNS-Neusuche bei IP-Wechsel
  config_flow.py  Dialoge: Kopplung (Fortschrittsschritt), TCP, Cloud, Reauth, Optionen
  sensor.py · binary_sensor.py · select.py · number.py · switch.py · diagnostics.py
  brand/          Icon und Logo (hell/dunkel) aus dem Webauftritt von Triple Solar
docs/             Modbus-API (Abschrift), Cloud-API, Regelung/Eingänge/§ 14a/SG Ready
tools/            Übersetzungsgenerator, Probe-Werkzeuge für die Inbetriebnahme
tests/            Fakes (Modbus, PKI, Cloud) und Tests
```

## Lizenz

Der Code steht unter der [MIT-Lizenz](LICENSE). Ausgenommen sind Inhalte von Triple Solar:
Icon und Logo in `custom_components/triplesolar/brand/` und die abgeschriebene
Herstellerdokumentation in `docs/` bleiben bei Triple Solar.
