# myTripleSolar-Cloud — inoffizielle Schnittstelle

**Nicht dokumentiert.** Abgeleitet am 09.10.2026 aus dem Web-Frontend
<https://my.triplesolar.eu> (SvelteKit + Houdini-GraphQL, Bundle `_app/immutable/…`).
Jedes Frontend-Update kann sie ändern. Die Integration nutzt sie **nur lesend** und nur
für das, was Modbus nicht liefert: die **kumulierten Energiezähler je Betriebsart**.

Die Anlage selbst spricht mit den Triple-Solar-Servern über **CoAP in einem
WireGuard-VPN** (eingebaute SIM oder LAN; Regelungslogik-Doku, Abschnitt „Hinweis zu
verwendeten Diensten").

## Anmeldung

| Aufruf | Body | Antwort |
|---|---|---|
| `POST /auth/login` | `{"email", "password"}` | `{"accessToken", "refreshToken"}` — Access-Token ist ein JWT mit `exp` |
| `POST /auth/refresh` | `{"refreshToken"}`, Header `Authorization: Bearer <altes Access-Token>` | neue Tokens; 400/401 mit Text „access token still valid", wenn zu früh |
| `POST /graphql` | `{"query", "variables"}`, Header `Authorization: Bearer …` | GraphQL; ohne Header **401 „no authorization header"** |

**Herkunftsprüfung** (festgestellt am 14.10.2026): Der Server nimmt nur Anfragen mit
`Origin: https://my.triplesolar.eu` an. Ohne diesen Header (oder mit einem fremden) antwortet
er **403 „Unknown origin"**, noch vor der Prüfung der Zugangsdaten. Mit dem Header kommt bei
falschen Zugangsdaten **401 „Wrong credentials"**. Der Browser setzt `Origin` selbst, die
Integration schickt ihn bei jedem Aufruf mit. Ein 403 „Unknown origin" trotz Header meldet sie
als eigenen Fehler (`cloud_rejected`), nicht als falsche Zugangsdaten. Dann hat sich die
Schnittstelle geändert.

Das Frontend erneuert 15 s vor Ablauf. Weitere Endpunkte unter `/auth/`:
`activate-user`, `change-password`, `delete-account`, `request-activation-link`,
`request-password-reset`, `reset-password`.

Die Wärmepumpen-ID steht im Pfad `https://my.triplesolar.eu/heat-pump/<ID>`. Das
Frontend warnt: **„Die Wärmepumpen-ID gewährt Zugriff auf die Wärmepumpe"** — sie gehört
nicht in Logs, Screenshots oder ein öffentliches Repo.

## Verwendete Abfragen

```graphql
query HeatPumps { heatPumps { id name isOnline alerts { code severity message } } }
```

```graphql
query HeatPumpStatus($id: String!, $refresh: Boolean) {
  heatPump(id: $id, refresh: $refresh) {
    id alerts { code severity message } softwareVersion connectionType lastStatus
    controller {
      spaceConditioning {
        heatingStatus coolingStatus isHeatingActive isCoolingActive
        hasHeatingDemand hasCoolingDemand roomTemp roomSetpTemp sinkSupplySetpTemp ambientTemp
        heatingModulationLevels { heatPump resistanceHeater external }
        coolingModulationLevels { heatPump }
      }
      domesticHotWater { status isActive hasDemand tankTemp tankSetpTemp
                         modulationLevels { heatPump resistanceHeater } }
    }
    heatPumpModule { sensors { sourceReturnTemp sourceSupplyTemp sinkReturnTemp
                               sinkSupplyTemp sinkPumpFlow sourcePumpFlow } }
    ... on PikaHeatPump { pressures { sink source } }
  }
}
```

`refresh: true` holt den Zustand live von der Anlage (über SIM/VPN) — die Integration setzt
es nie. Statuswerte (`heatingStatus` …) sind im Frontend `ACTIVE`, `DEMAND`, `BLOCKED`,
`FAULT`, `STANDBY`.

```graphql
query Performance($id: String!, $from: Time!, $until: Time, $interval: Duration) {
  timeSeries(id: $id, from: $from, until: $until, interval: $interval) {
    id
    data { timestamp
      ... on PikaTimeSeriesEntry { statistics {
        spaceHeatingTotalElectricEnergyConsumedkWh spaceHeatingTotalThermalEnergyProducedkWh
        dhwTotalElectricEnergyConsumedkWh          dhwTotalThermalEnergyProducedkWh
        coolingTotalElectricEnergyConsumedkWh      coolingTotalThermalEnergyProducedkWh } } }
  }
}
```

- `interval` ist ein String `HH:MM`: das Frontend nimmt **`01:00`** bis 2 Tage Zeitraum,
  **`24:00`** bis 90 Tage, sonst **`720:00`**.
- `from`/`until` sind ISO-8601-Zeitpunkte (`Date.toISOString()`).
- Die `…TotalkWh`-Felder sind **Zählerstände**: das Frontend bildet
  `max(0, aktueller − voriger)` je Intervall. Die Integration nimmt daher je Feld den
  jüngsten Wert ≠ null der letzten 48 h und meldet ihn als `total_increasing`.
- Triple Solar (Release Notes 2026.04.13): die Berechnung „ersetzt keine geeichte
  Wärmemengenmessung und darf nicht für Abrechnungszwecke genutzt werden".

## Weitere Operationen im Frontend (nicht verwendet)

Abfragen: `AlertHistory`, `GetHeatPumpBasic`, `GetHeatPumpExpertSettings`,
`GetHeatPumpSchemaSettings`, `GetHeatPumpSettings`, `GetHeatPumpSettingsChangeLog`,
`TimeSeries`, `Me`, `UserNotifications` u. a.

Mutationen: `SetHeatPumpSettings`, `SetHeatPumpExpertSettings`, `RebootHeatPump`,
`ResetHeatPump`, `StartHeatPumpDefrostProgram`, `StartHeatPumpFlushingProgram`,
`StartHeatPumpHWDetectProgram`, `StopHeatPumpProgram`, **`EnableHeatPumpLocalCSRService`**
(= „Kopplungsmodus einschalten" für Modbus TLS), `EnableHeatPumpRealTimeLogs` u. a.

In den Einstellungen (`PikaHardwareSettings`) liegen u. a. `modbusTCP.serverMode`
(`OFF`/`SECURE`/`INSECURE`), `modbusRTU`, `auxIO.aux3…5InputFunction` (u. a.
`POWER_LIMIT`, `ON_OFF_HEATING`), `powerBudget.isOTThermPowerBudgetEnabled` und ein
festes `sgReady: true`. Steuerungstypen: `OPENTHERM`, `ON_OFF_FIXED_SINK_SUPPLY_TEMP`,
`CURVE`, `ON_OFF_CURVE`, **`MODBUS`**.

## Offene Punkte

| # | Frage | Stand |
|---|---|---|
| 1 | Einheiten von `sinkPumpFlow`/`sourcePumpFlow` und `pressures` | Sensoren ohne Einheit, bis gegen die App abgeglichen (`tools/probe_cloud.py`) |
| 2 | Liefert `heatPumps` auch geteilte Anlagen (Rolle „Nutzer" statt „Eigentümer")? | ungeprüft |
| 3 | Rate-Limits | unbekannt; Standard-Abfrageintervall 300 s, Minimum 120 s |
