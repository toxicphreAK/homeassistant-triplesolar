# Regelung, Eingänge, § 14a und SG Ready — was davon für Home Assistant zählt

Auszug aus **Triple Solar, „PVT-Wärmepumpe 5.0: Regelungslogik und Parameter"**
(<https://triplesolar.tettra.site/pages/pvt-warmepumpe-50-regelungslogik-und-parameter-1>,
Stand 18.08.2026) und den Release Notes 2026.02.02 / 2026.04.13 / 2026.09.08.1, abgerufen
09.10.2026. Zusammengefasst, nicht wörtlich — maßgeblich ist das Original.

## Externe Eingänge und Relais (Abschnitt 4.1)

Ext. Input 1 = Warmwasser-Speicherfühler, Ext. Input 2 = Außenfühler — **fest**. Die
übrigen Eingänge (im Frontend `aux3…aux5InputFunction`) sind frei belegbar:

| Funktion | Art |
|---|---|
| Reset | potentialfreier Kontakt |
| **Leistungsbegrenzung** | **potentialfreier Kontakt** (§ 14a, s. u.) |
| An/Aus Heizen, An/Aus Kühlen | potentialfreier Kontakt (Thermostat) |
| Externe Vorlauftemperatur, Parallelpuffer oben/unten | NTC-Fühler |

Relais: Störung · Kühlen · An/Aus extern (Hybrid) · Heizen/Kühlen aktiv.

## § 14a: Leistungsbegrenzung (Abschnitt 4.3)

Einen freien Ext. Input auf „Leistungsbegrenzung" stellen. **Kontakt geschlossen → die
Anlage begrenzt ihre Leistungsaufnahme auf 3,5 kW**, das Heizelement dabei auf **1 kW**.
Der Kontakt muss potentialfrei sein.

Das ist der robuste Weg für die netzdienliche Steuerung: ein Kontakt, keine
Netzwerkabhängigkeit, kein Home Assistant im Pfad. Das Modbus-Leistungsbudget (0xB103) ist
die feinere Alternative, aber über LAN und — ohne Prüfung an der Anlage — mit offener
Wirkung außerhalb des Steuerungstyps „Modbus" (`modbus-api.md`, Offener Punkt 5).

3,5 kW liegt **unter** den 4,2 kW, die § 14a EnWG als Mindestleistung garantiert. Das ist
zulässig (die Anlage drosselt stärker als gefordert), verschenkt aber bis zu 0,7 kW
Heizleistung während einer Dimmung.

## SG Ready (Abschnitt 4.4)

„Das SG Ready Interface der Wärmepumpe ist **immer aktiv**" und reagiert auf externe
Schaltsignale:

| Zustand | Wirkung |
|---|---|
| Normalbetrieb | nach Konfiguration |
| Sperrbetrieb | Anlage darf nicht anlaufen |
| bevorzugter / erzwungener Betrieb | Wechsel in das **Komfort-Profil** für Heizung und Warmwasser |

Die Doku nennt weder die Klemmen noch die Belegung der beiden SG-Ready-Kontakte. Über
Modbus lässt sich dieselbe Wirkung ohne Verdrahtung erreichen: Profil **-1** (gesperrt)
bzw. **2** (comfort) in 0x1108 / 0x310A — das ist, was die Selects „Heizen Profil" und
„Warmwasser Profil" der Integration schreiben.

## Leistungsbudget über OpenTherm (Abschnitt 4.5)

Mit Steuerungstyp „OpenTherm-Thermostat" kann die Anlage ein über OpenTherm übermitteltes
Leistungsbudget einhalten (Einstellung „Leistungsbudget OpenTherm-Thermostat aktiviert").

## Profile und Zeitpläne (1.3, 2.1, 8)

Drei frei konfigurierbare Profile **Eco / Normal / Komfort** je für Heizen und Warmwasser;
Zeitpläne in myTripleSolar schalten zwischen ihnen, können Betrieb sperren und einen
Warmwasserzyklus erzwingen. **Ohne aktiven Zeitplan** gilt dauerhaft das im Dropdown
gewählte Profil — für eine Steuerung aus Home Assistant heraus die einfachere Ausgangslage.

Warmwasser: empfohlener Sollwert 50 °C für einen normalen Speicher, ≥ 60 °C bei der
thermischen Batterie (PCM). Höchste Effizienz bei **36 % Modulation**; mehr beschleunigt
die Beladung. Start erst nach **15 min** unter Soll − Hysterese (nach Neustart übersprungen).

## Elektrisches Heizelement (6)

Einstellbar 1-phasig (nur 1 kW) oder 3-phasig (1–6 kW, in 1-kW-Schritten parallel zum
Verdichter). Einsatz bei Störung (Warmwasser/Heizen getrennt wählbar) und als
Heizungsunterstützung unterhalb des Bivalenzpunkts (Außen- oder Quellentemperatur).

## Kommunikation, Updates, Zugriff

- CoAP + WireGuard-VPN zu den Triple-Solar-Servern; **OTA-Updates automatisch**.
- Der Kundendienst erreicht jede Anlage mit Internetverbindung.
- Erster registrierter Nutzer ist Eigentümer; Zugriffe verwaltet er unter „Benutzer".
- Modbus TLS/TCP-Doku und Demo-Repository seit Release 2026.09.08.1 öffentlich; im selben
  Release kamen Parallelpuffer, externe Modbus-Steuerung und Taupunktüberwachung aus der
  Beta.
