"""Sensoren — Modbus-Register, daraus berechnete Werte und Cloud-Zählerstände."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import TripleSolarConfigEntry
from .const import MAX_INTEGRATION_GAP_S
from .coordinator import CloudCoordinator, ModbusCoordinator
from .entity import ModbusRegisterEntity, TripleSolarEntity
from .registers import (
    MESSAGE_COUNT,
    CircuitStatus,
    HeatPumpState,
    ModuleState,
    Profile,
)

TEMP = dict(
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
)
PCT = dict(native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT)
POWER = dict(
    native_unit_of_measurement=UnitOfPower.WATT,
    device_class=SensorDeviceClass.POWER,
    state_class=SensorStateClass.MEASUREMENT,
)
ENERGY_TOTAL = dict(
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
)


def enum_options(enum: type[IntEnum]) -> dict[str, Any]:
    return dict(device_class=SensorDeviceClass.ENUM, options=[m.name.lower() for m in enum])


@dataclass(frozen=True, kw_only=True)
class RegisterSensorDescription(SensorEntityDescription):
    """Sensor, dessen Wert direkt ein Register ist (Schlüssel = Registerschlüssel)."""


def _modbus_descriptions(modules: int) -> list[RegisterSensorDescription]:
    d = RegisterSensorDescription
    off = dict(entity_registry_enabled_default=False)
    items = [
        d(key="heat_pump_state", translation_key="heat_pump_state", **enum_options(HeatPumpState)),
        d(
            key="rated_power",
            translation_key="rated_power",
            entity_category=EntityCategory.DIAGNOSTIC,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
        ),
        d(key="dhw_status", translation_key="dhw_status", **enum_options(CircuitStatus)),
        d(key="dhw_modulation", translation_key="dhw_modulation", **PCT),
        d(key="dhw_tank_temp", translation_key="dhw_tank_temp", **TEMP),
        d(key="dhw_tank_setpoint", translation_key="dhw_tank_setpoint", **TEMP),
        d(key="dhw_profile", translation_key="dhw_profile", **enum_options(Profile)),
        d(key="sh_status", translation_key="sh_status", **enum_options(CircuitStatus)),
        d(key="sh_modulation", translation_key="sh_modulation", **PCT),
        d(key="sh_supply_temp", translation_key="sh_supply_temp", **TEMP),
        d(key="sh_supply_setpoint", translation_key="sh_supply_setpoint", **TEMP),
        d(key="sh_profile", translation_key="sh_profile", **enum_options(Profile)),
        # Kühlen: aktive Kühlung ist bei dieser Anlage nicht geplant → aus.
        d(key="sc_status", translation_key="sc_status", **enum_options(CircuitStatus), **off),
        d(key="sc_modulation", translation_key="sc_modulation", **PCT, **off),
        d(key="sc_supply_temp", translation_key="sc_supply_temp", **TEMP, **off),
        d(key="sc_supply_setpoint", translation_key="sc_supply_setpoint", **TEMP, **off),
        d(key="sc_modulation_setpoint", translation_key="sc_modulation_setpoint", **PCT, **off),
        # Eingänge der Raumregelung — laut Hersteller „optional": ohne
        # Fühler/Thermostat steht dort kein Messwert.
        d(key="ambient_temp", translation_key="ambient_temp", **TEMP),
        d(key="room_temp", translation_key="room_temp", **TEMP),
        d(key="room_setpoint", translation_key="room_setpoint", **TEMP, **off),
        d(
            key="relative_humidity",
            translation_key="relative_humidity",
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.HUMIDITY,
            state_class=SensorStateClass.MEASUREMENT,
            **off,
        ),
        d(key="input_modulation", translation_key="input_modulation", **PCT, **off),
    ]
    for n in range(1, modules + 1):
        p = f"m{n}_"
        ph = {"module": str(n)}
        items += [
            d(
                key=p + "state",
                translation_key="module_state",
                translation_placeholders=ph,
                **enum_options(ModuleState),
            ),
            # Die Doku nennt nur „Compressor freq, value/10" — als Hz
            # angenommen; in der Kompressor-Doku steht die Drehzahl in %.
            d(
                key=p + "compressor_frequency",
                translation_key="compressor_frequency",
                translation_placeholders=ph,
                native_unit_of_measurement=UnitOfFrequency.HERTZ,
                device_class=SensorDeviceClass.FREQUENCY,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            d(key=p + "source_pump", translation_key="source_pump", translation_placeholders=ph, **PCT),
            d(key=p + "sink_pump", translation_key="sink_pump", translation_placeholders=ph, **PCT),
            d(
                key=p + "source_return_temp",
                translation_key="source_return_temp",
                translation_placeholders=ph,
                **TEMP,
            ),
            d(
                key=p + "source_supply_temp",
                translation_key="source_supply_temp",
                translation_placeholders=ph,
                **TEMP,
            ),
            d(
                key=p + "sink_return_temp",
                translation_key="sink_return_temp",
                translation_placeholders=ph,
                **TEMP,
            ),
            d(
                key=p + "sink_supply_temp",
                translation_key="sink_supply_temp",
                translation_placeholders=ph,
                **TEMP,
            ),
            d(
                key=p + "electric_power",
                translation_key="electric_power",
                translation_placeholders=ph,
                **POWER,
            ),
            d(key=p + "sink_power", translation_key="sink_power", translation_placeholders=ph, **POWER),
            d(
                key=p + "compressor_starts",
                translation_key="compressor_starts",
                translation_placeholders=ph,
                state_class=SensorStateClass.TOTAL_INCREASING,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        ]
    return items


def register_value(value: Any) -> Any:
    """Registerwert → HA-Zustand (Enums als Kleinbuchstaben-Name)."""
    if isinstance(value, IntEnum):
        return value.name.lower()
    return value


def active_messages(data: dict[str, Any]) -> list[int]:
    """Nicht-leere Meldungscodes aus 0x1…0x14 (0 = kein Eintrag)."""
    codes = [data.get(f"message_{n}") for n in range(1, MESSAGE_COUNT + 1)]
    return [c for c in codes if c]


def cop(electric_w: float | None, thermal_w: float | None, *, min_electric_w: float = 100) -> float | None:
    """Momentane Leistungszahl; unter ``min_electric_w`` (Standby) keine Aussage."""
    if electric_w is None or thermal_w is None or electric_w < min_electric_w:
        return None
    return round(abs(thermal_w) / electric_w, 2)


async def async_setup_entry(
    hass: HomeAssistant, entry: TripleSolarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if isinstance(coordinator, CloudCoordinator):
        async_add_entities(_cloud_entities(coordinator))
        return
    entities: list[SensorEntity] = [
        ModbusRegisterSensor(coordinator, desc) for desc in _modbus_descriptions(coordinator.modules)
    ]
    entities.append(MessagesSensor(coordinator))
    for n in range(1, coordinator.modules + 1):
        entities.append(CopSensor(coordinator, n))
        entities.append(IntegratedEnergySensor(coordinator, n, "electric_energy", f"m{n}_electric_power", 1))
        entities.append(IntegratedEnergySensor(coordinator, n, "heat_energy_heating", f"m{n}_sink_power", 1))
        entities.append(IntegratedEnergySensor(coordinator, n, "heat_energy_cooling", f"m{n}_sink_power", -1))
    async_add_entities(entities)


class ModbusRegisterSensor(ModbusRegisterEntity, SensorEntity):
    entity_description: RegisterSensorDescription

    @property
    def native_value(self) -> Any:
        return register_value(self.coordinator.data.get(self.entity_description.key))


class MessagesSensor(TripleSolarEntity[ModbusCoordinator], SensorEntity):
    """Anzahl aktiver Meldungen; die Codes stehen in den Attributen.

    Die Doku beschreibt das Format der Codes nicht — sie werden roh
    weitergegeben, die Bedeutung zeigt myTripleSolar.
    """

    _attr_translation_key = "active_messages"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ModbusCoordinator) -> None:
        super().__init__(coordinator, "active_messages")

    @property
    def native_value(self) -> int:
        return len(active_messages(self.coordinator.data))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"codes": active_messages(self.coordinator.data)}


class CopSensor(TripleSolarEntity[ModbusCoordinator], SensorEntity):
    _attr_translation_key = "cop"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ModbusCoordinator, module: int) -> None:
        super().__init__(coordinator, f"m{module}_cop")
        self._module = module
        self._attr_translation_placeholders = {"module": str(module)}

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return cop(data.get(f"m{self._module}_electric_power"), data.get(f"m{self._module}_sink_power"))


def integrate_step(
    previous: tuple[float, float] | None,
    now_s: float,
    power_w: float | None,
    sign: int,
    max_gap_s: float = MAX_INTEGRATION_GAP_S,
) -> tuple[float, tuple[float, float] | None]:
    """Trapezregel für einen Schritt → (kWh-Zuwachs, neuer Stützpunkt).

    ``sign`` wählt die Richtung: +1 zählt nur positive Leistung (Heizen,
    Stromaufnahme), -1 nur negative (Kühlen — die Wärmeabgabe ist dort
    negativ). Über eine Pause > ``max_gap_s`` wird nicht
    integriert: eine Stunde Funkstille mit dem letzten Wert zu füllen wäre
    eine erfundene Kilowattstunde.
    """
    if power_w is None:
        return 0.0, None
    value = max(0.0, power_w * sign)
    if previous is None:
        return 0.0, (now_s, value)
    last_s, last_value = previous
    dt = now_s - last_s
    if dt <= 0:
        return 0.0, previous
    if dt > max_gap_s:
        return 0.0, (now_s, value)
    return (last_value + value) / 2 * dt / 3_600_000, (now_s, value)


class IntegratedEnergySensor(TripleSolarEntity[ModbusCoordinator], RestoreSensor):
    """Energiezähler aus der Momentanleistung — für das Energie-Dashboard.

    Die Modbus-Schnittstelle liefert nur Leistung (W), keinen Zähler. Die
    Summe überlebt Neustarts (RestoreSensor). Genauer sind die Zähler der
    Cloud-Verbindung; dieser hier funktioniert ohne Internet.
    """

    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: ModbusCoordinator, module: int, kind: str, source_key: str, sign: int
    ) -> None:
        super().__init__(coordinator, f"m{module}_{kind}")
        self._attr_translation_key = kind
        self._attr_translation_placeholders = {"module": str(module)}
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._source_key = source_key
        self._sign = sign
        self._total = 0.0
        self._last: tuple[float, float] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (restored := await self.async_get_last_sensor_data()) is not None:
            try:
                self._total = float(restored.native_value or 0)
            except (TypeError, ValueError):
                self._total = 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.last_update_success:
            power = self.coordinator.data.get(self._source_key)
            delta, self._last = integrate_step(
                self._last, dt_util.utcnow().timestamp(), power, self._sign, self._max_gap_s()
            )
            self._total += delta
        super()._handle_coordinator_update()

    def _max_gap_s(self) -> float:
        """Lücke, ab der nicht mehr integriert wird.

        Mindestens ``MAX_INTEGRATION_GAP_S``, aber nie unter dem 2,5-Fachen
        des Abfrageintervalls — sonst verwürfe ein langes Intervall (Optionen
        erlauben bis 3600 s) JEDEN Schritt, und der Zähler bliebe bei 0.
        """
        interval = self.coordinator.update_interval
        seconds = interval.total_seconds() if interval else 0
        return max(MAX_INTEGRATION_GAP_S, 2.5 * seconds)

    @property
    def native_value(self) -> float:
        return round(self._total, 4)


# --- Cloud -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CloudSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


def _path(*keys: str) -> Callable[[dict[str, Any]], Any]:
    def get(data: dict[str, Any]) -> Any:
        node: Any = data.get("status")
        for key in keys:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    return get


def _energy(field: str) -> Callable[[dict[str, Any]], Any]:
    return lambda data: data.get("energy", {}).get(field)


def _energy_sum(required: tuple[str, ...], optional: tuple[str, ...] = ()) -> Callable[[dict[str, Any]], Any]:
    """Summe von Zählerständen.

    Fehlt ein Pflichtfeld, gibt es keinen Wert (sonst sähe HA einen
    Rückgang und deutete ihn als Zählerwechsel). Optionale Felder — Kühlen,
    das nie lief — zählen als 0.
    """

    def get(data: dict[str, Any]) -> float | None:
        energy = data.get("energy", {})
        if any(energy.get(f) is None for f in required):
            return None
        return round(sum(energy.get(f) or 0.0 for f in required + optional), 3)

    return get


def _lower(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    return lambda data: v.lower() if isinstance(v := fn(data), str) else v


SC = ("controller", "spaceConditioning")
DHW = ("controller", "domesticHotWater")
HPM = ("heatPumpModule", "sensors")

CLOUD_SENSORS: tuple[CloudSensorDescription, ...] = (
    CloudSensorDescription(
        key="dhw_tank_temp", translation_key="dhw_tank_temp", value_fn=_path(*DHW, "tankTemp"), **TEMP
    ),
    CloudSensorDescription(
        key="dhw_tank_setpoint",
        translation_key="dhw_tank_setpoint",
        value_fn=_path(*DHW, "tankSetpTemp"),
        **TEMP,
    ),
    CloudSensorDescription(
        key="dhw_status_cloud", translation_key="dhw_status_cloud", value_fn=_lower(_path(*DHW, "status"))
    ),
    CloudSensorDescription(
        key="dhw_modulation",
        translation_key="dhw_modulation",
        value_fn=_path(*DHW, "modulationLevels", "heatPump"),
        **PCT,
    ),
    CloudSensorDescription(
        key="dhw_heater_modulation",
        translation_key="dhw_heater_modulation",
        value_fn=_path(*DHW, "modulationLevels", "resistanceHeater"),
        **PCT,
    ),
    CloudSensorDescription(
        key="heating_status_cloud",
        translation_key="heating_status_cloud",
        value_fn=_lower(_path(*SC, "heatingStatus")),
    ),
    CloudSensorDescription(
        key="sh_modulation",
        translation_key="sh_modulation",
        value_fn=_path(*SC, "heatingModulationLevels", "heatPump"),
        **PCT,
    ),
    CloudSensorDescription(
        key="sh_heater_modulation",
        translation_key="sh_heater_modulation",
        value_fn=_path(*SC, "heatingModulationLevels", "resistanceHeater"),
        **PCT,
    ),
    CloudSensorDescription(
        key="sh_supply_setpoint",
        translation_key="sh_supply_setpoint",
        value_fn=_path(*SC, "sinkSupplySetpTemp"),
        **TEMP,
    ),
    CloudSensorDescription(
        key="ambient_temp", translation_key="ambient_temp", value_fn=_path(*SC, "ambientTemp"), **TEMP
    ),
    CloudSensorDescription(
        key="room_temp", translation_key="room_temp", value_fn=_path(*SC, "roomTemp"), **TEMP
    ),
    CloudSensorDescription(
        key="room_setpoint", translation_key="room_setpoint", value_fn=_path(*SC, "roomSetpTemp"), **TEMP
    ),
    CloudSensorDescription(
        key="source_return_temp",
        translation_key="source_return_temp_cloud",
        value_fn=_path(*HPM, "sourceReturnTemp"),
        **TEMP,
    ),
    CloudSensorDescription(
        key="source_supply_temp",
        translation_key="source_supply_temp_cloud",
        value_fn=_path(*HPM, "sourceSupplyTemp"),
        **TEMP,
    ),
    CloudSensorDescription(
        key="sink_return_temp",
        translation_key="sink_return_temp_cloud",
        value_fn=_path(*HPM, "sinkReturnTemp"),
        **TEMP,
    ),
    CloudSensorDescription(
        key="sink_supply_temp",
        translation_key="sink_supply_temp_cloud",
        value_fn=_path(*HPM, "sinkSupplyTemp"),
        **TEMP,
    ),
    # Durchfluss und Drücke: Einheit undokumentiert → ohne Einheit, bis sie
    # an der laufenden Anlage gegen die App abgeglichen ist.
    CloudSensorDescription(
        key="sink_pump_flow",
        translation_key="sink_pump_flow",
        value_fn=_path(*HPM, "sinkPumpFlow"),
        state_class=SensorStateClass.MEASUREMENT,
    ),
    CloudSensorDescription(
        key="source_pump_flow",
        translation_key="source_pump_flow",
        value_fn=_path(*HPM, "sourcePumpFlow"),
        state_class=SensorStateClass.MEASUREMENT,
    ),
    CloudSensorDescription(
        key="sink_pressure",
        translation_key="sink_pressure",
        value_fn=_path("pressures", "sink"),
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    CloudSensorDescription(
        key="source_pressure",
        translation_key="source_pressure",
        value_fn=_path("pressures", "source"),
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    CloudSensorDescription(
        key="alerts",
        translation_key="alerts",
        value_fn=lambda d: len(_path("alerts")(d) or []),
        state_class=SensorStateClass.MEASUREMENT,
    ),
    CloudSensorDescription(
        key="software_version",
        translation_key="software_version",
        value_fn=_path("softwareVersion"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    CloudSensorDescription(
        key="connection_type",
        translation_key="connection_type",
        value_fn=_lower(_path("connectionType")),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Zählerstände (kWh) je Betriebsart — für das Energie-Dashboard.
    CloudSensorDescription(
        key="sh_electric_energy",
        translation_key="sh_electric_energy",
        value_fn=_energy("spaceHeatingTotalElectricEnergyConsumedkWh"),
        **ENERGY_TOTAL,
    ),
    CloudSensorDescription(
        key="sh_thermal_energy",
        translation_key="sh_thermal_energy",
        value_fn=_energy("spaceHeatingTotalThermalEnergyProducedkWh"),
        **ENERGY_TOTAL,
    ),
    CloudSensorDescription(
        key="dhw_electric_energy",
        translation_key="dhw_electric_energy",
        value_fn=_energy("dhwTotalElectricEnergyConsumedkWh"),
        **ENERGY_TOTAL,
    ),
    CloudSensorDescription(
        key="dhw_thermal_energy",
        translation_key="dhw_thermal_energy",
        value_fn=_energy("dhwTotalThermalEnergyProducedkWh"),
        **ENERGY_TOTAL,
    ),
    CloudSensorDescription(
        key="sc_electric_energy",
        translation_key="sc_electric_energy",
        value_fn=_energy("coolingTotalElectricEnergyConsumedkWh"),
        **ENERGY_TOTAL,
        entity_registry_enabled_default=False,
    ),
    CloudSensorDescription(
        key="sc_thermal_energy",
        translation_key="sc_thermal_energy",
        value_fn=_energy("coolingTotalThermalEnergyProducedkWh"),
        **ENERGY_TOTAL,
        entity_registry_enabled_default=False,
    ),
    CloudSensorDescription(
        key="total_electric_energy",
        translation_key="total_electric_energy",
        value_fn=_energy_sum(
            ("spaceHeatingTotalElectricEnergyConsumedkWh", "dhwTotalElectricEnergyConsumedkWh"),
            ("coolingTotalElectricEnergyConsumedkWh",),
        ),
        **ENERGY_TOTAL,
    ),
    CloudSensorDescription(
        key="total_thermal_energy",
        translation_key="total_thermal_energy",
        value_fn=_energy_sum(
            ("spaceHeatingTotalThermalEnergyProducedkWh", "dhwTotalThermalEnergyProducedkWh"),
        ),
        **ENERGY_TOTAL,
    ),
)


def _cloud_entities(coordinator: CloudCoordinator) -> list[SensorEntity]:
    return [CloudSensor(coordinator, desc) for desc in CLOUD_SENSORS]


class CloudSensor(TripleSolarEntity[CloudCoordinator], SensorEntity):
    entity_description: CloudSensorDescription

    def __init__(self, coordinator: CloudCoordinator, description: CloudSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "alerts":
            return None
        return {"alerts": _path("alerts")(self.coordinator.data) or []}
