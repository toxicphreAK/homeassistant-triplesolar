# Modbus-API der Triple Solar PVT-Wärmepumpe 5.0

Abschrift von **Triple Solar, „Modbus API Documentation"**
(<https://triplesolar.tettra.site/zusatzliche-dokumente/modbus-api-documentation>),
erstellt 02.09.2026, zuletzt geändert 25.09.2026, **gültig ab Firmware 2026.09.01.0**,
abgerufen 09.10.2026. Maßgeblich ist das Original; Abweichungen hier sind Abschreibfehler.
Die Tabelle in `custom_components/triplesolar/registers.py` folgt dieser Abschrift, und
`tests/test_registers.py` prüft beide Listen gegeneinander.

Referenz-Implementierung des Herstellers für die TLS-Kopplung:
<https://gitlab.com/triple-solar/public/modbus-demo> (Python, pymodbus, Stand 22.09.2026).

## Schnittstellen

| Weg | Transport | Voraussetzung |
|---|---|---|
| **Modbus TLS** | Port **802**, gegenseitige X.509-Authentifizierung | Ethernet-Anschluss; **ab Werk aktiv** |
| Modbus TCP | Port **502**, unverschlüsselt | Ethernet; muss der Betreiber in myTripleSolar **ausdrücklich einschalten** (I/O → Modbus-TCP-Modus „Unsicher") |
| Modbus RTU | RS485, **19200 8N1, Adresse 43** | über Register 0x520F–0x5211 bzw. myTripleSolar änderbar |

mDNS-SD: `_mbap-s._tcp` (TLS) und `_mbap._tcp` (TCP), Host `hpcb-xxxxxx.local`. Bei einer
**Duo-Kaskade** melden sich zwei Anlagen.

## Kopplung (Modbus TLS Bootstrapping)

1. Kopplungsmodus in myTripleSolar einschalten → die Anlage startet einen
   **einmaligen** CSR-Signierdienst, angekündigt als `_ts-csr._tcp`; er läuft höchstens
   **eine Minute** oder bis eine CSR eintrifft.
2. Client erzeugt Schlüssel (Demo: EC P-256) + CSR und schickt sie per
   `POST https://<host>:<port>/csr` (Body = CSR als PEM). Der Dienst ist selbstsigniert —
   es gibt noch keinen Vertrauensanker, also ohne Zertifikatsprüfung.
3. Antwort 200: **Client-Zertifikat + Root-CA** der Anlage, PEM hintereinander.
4. Gespeichert werden Schlüssel, Zertifikat, Root-CA und der Hostname; der Hostname bindet
   spätere Verbindungen an genau diese Anlage (Prüfung über den SAN des Server-Zertifikats,
   weil per IP verbunden wird).

## Registerkarte

Alle Register sind 16 Bit (**int16**). „value/10" heißt: Rohwert durch 10.

### Holding-Register (lesen/schreiben)

| Adresse | Funktion | Format |
|---|---|---|
| **Heizen** | | |
| 0x1101 | Freigabe | 0 = aus, 1 = an |
| 0x1108 | Profil | -1 = gesperrt, 0 = eco, 1 = normal, 2 = comfort |
| **Kühlen** | | |
| 0x2101 | Freigabe | 0 / 1 |
| 0x2107 | Profil | -1 … 2 wie oben |
| **Warmwasser** | | |
| 0x3101 | Freigabe | 0 / 1 |
| 0x310A | Profil | -1 … 2 wie oben |
| **Warmwasserprofil eco** | | |
| 0x3601 | Speicher-Soll | (10 … 70) × 10 |
| 0x3602 | untere Hysterese | (0 … 50) × 10 |
| 0x3603 | Modulation | (0 … 100) × 10 |
| **Warmwasserprofil normal** | 0x3701 / 0x3702 / 0x3703 | wie eco |
| **Warmwasserprofil comfort** | 0x3801 / 0x3802 / 0x3803 | wie eco |
| **Hardware** | | |
| 0x520F | RTU-Adresse | 1 … 247 (Standard 43) |
| 0x5210 | RTU-Baudrate | Standard 19200 |
| 0x5211 | RTU-Parität | 0 = keine (Standard), 1 = gerade, 2 = ungerade |
| **Steuerung** | | |
| 0xB101 | Betriebsart | 0 = aus, 1 = heizen, 2 = kühlen |
| 0xB102 | Modulationsgrad | 0 … 100 |
| 0xB103 | **Leistungsbudget (W)** | 0 … 65535 (**0 = keine Grenze**) |

### Input-Register (nur lesen)

| Adresse | Funktion | Format |
|---|---|---|
| **Allgemein** | | |
| 0x0 | Zustand der Wärmepumpe | -1 Störung · 0 aus · 1 WW Vorrang · 2 WW · 3 Heizen Vorrang · 4 Heizen · 5 Kühlen Vorrang · 6 Kühlen · 7 Programm aktiv · 8 Übergabe WW · 9 Übergabe Heizen · 10 Übergabe Kühlen |
| 0x1 … 0x14 | die ersten 20 aktiven Fehler-/Warn-/Info-Meldungen | (Format nicht beschrieben) |
| 0x16 | Nennleistung (max.) aller angeschlossenen Wärmeerzeuger (W) | Wert |
| **Warmwasser** | | |
| 0x20 | Status | -1 Störung · 0 aus · 1 läuft · 2 gesperrt · 3 wartet auf Wärmeerzeuger · 4 WW-Ventil öffnet · 5 WW-Ventil schließt |
| 0x21 | Anforderung | 0 / 1 |
| 0x22 | Modulation | value/10 |
| 0x25 | Speichertemperatur | value/10 |
| 0x26 | Speicher-Soll (aktuelles Profil) | value/10 |
| 0x27 | WW-Ventil offen | 0 / 1 |
| 0x28 | aktuelles Profil | -1 … 2 |
| **Heizen** | | |
| 0x40 | Status | -1 Störung · 0 aus · 1 läuft · 2 gesperrt · 3 wartet auf Wärmeerzeuger |
| 0x41 | Anforderung | 0 / 1 |
| 0x42 | Modulation | value/10 |
| 0x45 | Vorlauftemperatur | value/10 |
| 0x46 | Vorlauf-Soll (aktuelle Regelart) | value/10 |
| 0x4A | aktuelles Profil | -1 … 2 |
| **Kühlen** | | |
| 0x60 | Status | wie 0x40 |
| 0x61 | Anforderung | 0 / 1 |
| 0x62 | Modulation | value/10 |
| 0x65 | Vorlauftemperatur | value/10 |
| 0x66 | Vorlauf-Soll | value/10 |
| 0x69 | Modulations-Soll (OpenTherm, optional) | value/10 |
| **Eingänge Raumregelung** | | |
| 0x120 | Raumtemperatur (optional) | value/10 |
| 0x121 | Außentemperatur (optional) | value/10 |
| 0x122 | rel. Feuchte (optional) | value/10 |
| 0x123 | Raum-Soll (optional) | value/10 |
| 0x124 | Modulation (optional) | value/10 |
| **Wärmepumpenmodul 1 / 2** | | |
| 0x400 / 0x800 | Zustand | -1 Störung · 0 aus · 1 Heizen Bereitschaft · 2 Pumpenanlauf · 3 Verdichteranlauf · 4 Heizen aktiv · 5 Nachlauf · 6 Kühlen Bereitschaft · 7 Pumpenanlauf · 8 Verdichteranlauf · 9 Kühlen aktiv · 10 Nachlauf |
| 0x422 / 0x822 | Verdichterfrequenz | value/10 |
| 0x441 / 0x841 | Quellenpumpe Ansteuerung % | value/10 |
| 0x461 / 0x861 | Umwälzpumpe Ansteuerung % | value/10 |
| 0x4A1 / 0x8A1 | Kühlventil | 0 = Heizen, 1 = Kühlen |
| 0x4C0 / 0x8C0 | Quelle Rücklauf (PVT → WP) | value/10 |
| 0x4C1 / 0x8C1 | Quelle Vorlauf (WP → PVT) | value/10 |
| 0x500 / 0x900 | Senke Rücklauf (Verteilung → WP) | value/10 |
| 0x501 / 0x901 | Senke Vorlauf (WP → Verteilung) | value/10 |
| 0x680 / 0xA80 | **elektrische Leistungsaufnahme (W)** | Wert |
| 0x681 / 0xA81 | Wärmeleistung (W), **negativ beim Kühlen** | Wert |
| 0x683 / 0xA83 | Verdichterstarts | Wert |

## Wie die Integration die Tabelle liest

- **Lesepläne nur über lückenlose Adressen.** 0x15 ist undokumentiert, also wird 0x0–0x14
  und 0x16 getrennt gelesen; ein Server darf eine Lücke mit „Illegal Data Address"
  beantworten, und dann wäre der ganze Block verloren.
- **Exception 2 auf ein Register** (ältere Firmware, fehlendes Zubehör): einmal einzeln
  nachlesen, danach wird es ausgelassen und die Entität ist „nicht verfügbar".
- **uint16 statt int16** für Leistungsbudget (0…65535 laut Doku), Verdichterstarts,
  Meldungscodes und Nennleistung.
- **RTU-Hardwareregister 0x520F–0x5211 werden nicht angeboten.** Wer sie per TCP verstellt,
  legt eine RS485-Kaskade (Duo) lahm.
- **Schreibzugriffe** werden gegen den dokumentierten Bereich geprüft, bevor sie die Anlage
  erreichen, und sofort zurückgelesen.

## Offene Punkte (am laufenden System zu prüfen)

Mit `tools/probe_modbus.py read` lässt sich jeder Punkt in einer Minute klären.

| # | Frage | Annahme in der Integration |
|---|---|---|
| 1 | **Unit-ID** bei TCP/TLS — die Doku nennt nur die RTU-Adresse 43 | 1 (wie das Hersteller-Demo, pymodbus-Standard); in den Optionen änderbar |
| 2 | **Einheit der Verdichterfrequenz** (0x422) | Hz |
| 3 | Was steht in den **„optional"**-Registern 0x120–0x124 ohne Fühler? | wird angezeigt wie gelesen; Feuchte, Raum-Soll, Modulation standardmäßig deaktiviert |
| 4 | **Format der Meldungscodes** 0x1–0x14 | roh als Zahl, 0 = kein Eintrag |
| 5 | Wirken **0xB101–0xB103** nur mit Steuerungstyp „Modbus-Steuerung"? Insbesondere: begrenzt das **Leistungsbudget** auch bei Heizkurve/OpenTherm? | Entitäten standardmäßig deaktiviert |
| 6 | Überlebt ein geschriebenes Profil einen **Zeitplanwechsel** in myTripleSolar? (Zeitpläne schalten Profile) | Automationen sollten Zeitpläne in myTripleSolar abschalten oder das Profil zyklisch setzen |
| 7 | Speichert die Anlage Schreibzugriffe im **Flash** (Verschleiß bei häufigem Schreiben)? | Automationen schreiben nur bei Zustandswechsel, nicht zyklisch |
