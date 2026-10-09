"""Registerkarte der Triple Solar PVT-Wärmepumpe 5.0 (Modbus).

Quelle: Triple Solar, „Modbus API Documentation", gültig ab Firmware
2026.09.01.0, Stand 25.09.2026 —
https://triplesolar.tettra.site/zusatzliche-dokumente/modbus-api-documentation
(Abschrift in ``docs/modbus-api.md``).

Die Tabelle ist die einzige Stelle, an der Adressen, Skalierung und
Wertebereiche stehen. Entitäten und Lesepläne werden daraus abgeleitet —
eine Adresse, die hier fehlt, wird nicht gelesen, und eine, die hier
falsch steht, ist an genau einer Stelle zu korrigieren.

Hersteller: „all modbus registers are 16 bit numbers (int16)". Ausnahmen
sind Zähler und das Leistungsbudget (0…65535), die als uint16 gelesen
werden — sonst würde ein Budget von 40 kW als negativer Wert ankommen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class RegisterType(StrEnum):
    """Modbus-Tabelle, aus der ein Wert stammt."""

    INPUT = "input"  # Funktionscode 0x04, nur lesen
    HOLDING = "holding"  # Funktionscode 0x03 lesen, 0x06 schreiben


class HeatPumpState(IntEnum):
    """Register 0x0 — Gesamtzustand (``coapstatus.State`` der Firmware)."""

    FAULT = -1
    OFF = 0
    DHW_PRIO = 1
    DHW_NO_PRIO = 2
    SPACE_HEATING_PRIO = 3
    SPACE_HEATING_NO_PRIO = 4
    SPACE_COOLING_PRIO = 5
    SPACE_COOLING_NO_PRIO = 6
    PROGRAM_ACTIVE = 7
    DHW_HANDOVER = 8
    SPACE_HEATING_HANDOVER = 9
    SPACE_COOLING_HANDOVER = 10


class CircuitStatus(IntEnum):
    """Register 0x20 / 0x40 / 0x60 — Status von Warmwasser, Heizen, Kühlen.

    Die beiden Ventilzustände (4, 5) meldet nur der Warmwasserkreis.
    """

    FAULT = -1
    OFF = 0
    RUNNING = 1
    BLOCKED = 2
    AWAIT_HEAT_SOURCES = 3
    PREPARE_ENABLE_DHW_VALVE = 4
    PREPARE_DISABLE_DHW_VALVE = 5


class Profile(IntEnum):
    """Leistungsprofil von Heizen, Kühlen und Warmwasser."""

    BLOCKED = -1
    ECO = 0
    NORMAL = 1
    COMFORT = 2


class ModuleState(IntEnum):
    """Register 0x400 / 0x800 — Zustand eines Wärmepumpenmoduls."""

    FAULT = -1
    OFF = 0
    HEATING_STANDBY = 1
    HEATING_PUMP_STARTUP = 2
    HEATING_COMPRESSOR_STARTUP = 3
    HEATING_ACTIVE = 4
    HEATING_WIND_DOWN = 5
    COOLING_STANDBY = 6
    COOLING_PUMP_STARTUP = 7
    COOLING_COMPRESSOR_STARTUP = 8
    COOLING_ACTIVE = 9
    COOLING_WIND_DOWN = 10


class OperatingMode(IntEnum):
    """Register 0xB101 — Betriebsart bei externer Modbus-Steuerung."""

    OFF = 0
    HEATING = 1
    COOLING = 2


@dataclass(frozen=True, slots=True)
class Register:
    """Ein dokumentiertes Register.

    ``scale`` ist der Divisor des Herstellers („value/10" → 10), ``signed``
    die Interpretation des 16-Bit-Rohwerts. ``minimum``/``maximum`` gelten
    für den skalierten Wert und begrenzen Schreibzugriffe; ``enum`` macht
    aus dem Rohwert einen Zustand.
    """

    key: str
    address: int
    type: RegisterType
    scale: int = 1
    signed: bool = True
    enum: type[IntEnum] | None = None
    minimum: float | None = None
    maximum: float | None = None

    @property
    def writable(self) -> bool:
        return self.type is RegisterType.HOLDING

    def decode(self, raw: int) -> float | int | IntEnum | None:
        """Rohwert (0…65535) → skalierter Wert, Enum oder ``None``.

        ``None`` steht für einen Wert, den die Firmware nicht kennt — ein
        Enum außerhalb der Dokumentation wird nicht geraten.
        """
        value = to_signed(raw) if self.signed else raw
        if self.enum is not None:
            try:
                return self.enum(value)
            except ValueError:
                return None
        if self.scale == 1:
            return value
        return value / self.scale

    def encode(self, value: float | int) -> int:
        """Skalierter Wert → Rohwert für Funktionscode 0x06.

        Prüft gegen den dokumentierten Bereich, bevor etwas die Anlage
        erreicht: ein Schreibfehler hier ist eine Exception, keine falsche
        Speichertemperatur.
        """
        if not self.writable:
            raise ValueError(f"{self.key} (0x{self.address:04X}) ist nicht beschreibbar")
        numeric = int(value) if self.enum is not None else value
        if self.minimum is not None and numeric < self.minimum:
            raise ValueError(f"{self.key}: {value} < Minimum {self.minimum}")
        if self.maximum is not None and numeric > self.maximum:
            raise ValueError(f"{self.key}: {value} > Maximum {self.maximum}")
        raw = round(numeric * self.scale)
        if self.signed:
            if not -0x8000 <= raw <= 0x7FFF:
                raise ValueError(f"{self.key}: {raw} passt nicht in int16")
            return raw & 0xFFFF
        if not 0 <= raw <= 0xFFFF:
            raise ValueError(f"{self.key}: {raw} passt nicht in uint16")
        return raw


def to_signed(raw: int) -> int:
    """uint16 → int16 (0xFFFF = -1 = Störung)."""
    return raw - 0x10000 if raw >= 0x8000 else raw


IN = RegisterType.INPUT
HO = RegisterType.HOLDING

# Anzahl der Meldungsregister 0x1…0x14 („first 20 active messages").
MESSAGE_COUNT = 20

# Basisadresse je Wärmepumpenmodul: Modul 1 = 0x400, Modul 2 (Duo) = 0x800.
MODULE_BASES: tuple[int, ...] = (0x400, 0x800)


def _general() -> list[Register]:
    regs = [Register("heat_pump_state", 0x0, IN, enum=HeatPumpState)]
    regs += [Register(f"message_{n}", n, IN, signed=False) for n in range(1, MESSAGE_COUNT + 1)]
    regs.append(Register("rated_power", 0x16, IN, signed=False))
    return regs


def _dhw() -> list[Register]:
    return [
        Register("dhw_status", 0x20, IN, enum=CircuitStatus),
        Register("dhw_demand", 0x21, IN),
        Register("dhw_modulation", 0x22, IN, scale=10),
        Register("dhw_tank_temp", 0x25, IN, scale=10),
        Register("dhw_tank_setpoint", 0x26, IN, scale=10),
        Register("dhw_valve", 0x27, IN),
        Register("dhw_profile", 0x28, IN, enum=Profile),
    ]


def _space_heating() -> list[Register]:
    return [
        Register("sh_status", 0x40, IN, enum=CircuitStatus),
        Register("sh_demand", 0x41, IN),
        Register("sh_modulation", 0x42, IN, scale=10),
        Register("sh_supply_temp", 0x45, IN, scale=10),
        Register("sh_supply_setpoint", 0x46, IN, scale=10),
        Register("sh_profile", 0x4A, IN, enum=Profile),
    ]


def _space_cooling() -> list[Register]:
    return [
        Register("sc_status", 0x60, IN, enum=CircuitStatus),
        Register("sc_demand", 0x61, IN),
        Register("sc_modulation", 0x62, IN, scale=10),
        Register("sc_supply_temp", 0x65, IN, scale=10),
        Register("sc_supply_setpoint", 0x66, IN, scale=10),
        Register("sc_modulation_setpoint", 0x69, IN, scale=10),
    ]


def _space_conditioning_input() -> list[Register]:
    # Alle „optional": ohne Raumfühler/Thermostat steht hier kein Messwert.
    return [
        Register("room_temp", 0x120, IN, scale=10),
        Register("ambient_temp", 0x121, IN, scale=10),
        Register("relative_humidity", 0x122, IN, scale=10),
        Register("room_setpoint", 0x123, IN, scale=10),
        Register("input_modulation", 0x124, IN, scale=10),
    ]


def module_registers(index: int) -> list[Register]:
    """Register von Wärmepumpenmodul ``index`` (1 oder 2)."""
    base = MODULE_BASES[index - 1]
    p = f"m{index}_"
    return [
        Register(p + "state", base + 0x00, IN, enum=ModuleState),
        Register(p + "compressor_frequency", base + 0x22, IN, scale=10),
        Register(p + "source_pump", base + 0x41, IN, scale=10),
        Register(p + "sink_pump", base + 0x61, IN, scale=10),
        Register(p + "cooling_valve", base + 0xA1, IN),
        Register(p + "source_return_temp", base + 0xC0, IN, scale=10),
        Register(p + "source_supply_temp", base + 0xC1, IN, scale=10),
        Register(p + "sink_return_temp", base + 0x100, IN, scale=10),
        Register(p + "sink_supply_temp", base + 0x101, IN, scale=10),
        Register(p + "electric_power", base + 0x280, IN),
        Register(p + "sink_power", base + 0x281, IN),
        Register(p + "compressor_starts", base + 0x283, IN, signed=False),
    ]


def _dhw_profile(name: str, base: int) -> list[Register]:
    return [
        Register(f"dhw_{name}_setpoint", base + 1, HO, scale=10, minimum=10, maximum=70),
        Register(f"dhw_{name}_hysteresis", base + 2, HO, scale=10, minimum=0, maximum=50),
        Register(f"dhw_{name}_modulation", base + 3, HO, scale=10, minimum=0, maximum=100),
    ]


def _holding() -> list[Register]:
    regs = [
        Register("sh_enable", 0x1101, HO, minimum=0, maximum=1),
        Register("sh_profile_set", 0x1108, HO, enum=Profile, minimum=-1, maximum=2),
        Register("sc_enable", 0x2101, HO, minimum=0, maximum=1),
        Register("sc_profile_set", 0x2107, HO, enum=Profile, minimum=-1, maximum=2),
        Register("dhw_enable", 0x3101, HO, minimum=0, maximum=1),
        Register("dhw_profile_set", 0x310A, HO, enum=Profile, minimum=-1, maximum=2),
    ]
    regs += _dhw_profile("eco", 0x3600)
    regs += _dhw_profile("normal", 0x3700)
    regs += _dhw_profile("comfort", 0x3800)
    regs += [
        Register("control_operating_mode", 0xB101, HO, enum=OperatingMode, minimum=0, maximum=2),
        Register("control_modulation", 0xB102, HO, minimum=0, maximum=100),
        Register("control_power_budget", 0xB103, HO, signed=False, minimum=0, maximum=65535),
    ]
    return regs


# Bewusst NICHT in der Tabelle: 0x520F–0x5211 (RTU-Adresse, Baudrate,
# Parität). Wer die per TCP verstellt, legt eine RS485-Kaskade (Duo) lahm;
# das gehört in myTripleSolar, nicht in eine Hausautomation.


def all_registers(modules: int = 1) -> list[Register]:
    """Alle Register für eine Anlage mit ``modules`` Wärmepumpenmodulen."""
    if modules not in (1, 2):
        raise ValueError("Die PVT-Wärmepumpe 5.0 hat ein Modul, als Duo zwei")
    regs = _general() + _dhw() + _space_heating() + _space_cooling() + _space_conditioning_input()
    for index in range(1, modules + 1):
        regs += module_registers(index)
    regs += _holding()
    return regs


def registers_by_key(modules: int = 1) -> dict[str, Register]:
    regs = all_registers(modules)
    table = {reg.key: reg for reg in regs}
    if len(table) != len(regs):
        raise AssertionError("doppelter Registerschlüssel")
    return table


@dataclass(frozen=True, slots=True)
class ReadBlock:
    """Zusammenhängender Lesezugriff: ``count`` Register ab ``address``."""

    type: RegisterType
    address: int
    count: int
    keys: tuple[str, ...]


def plan_reads(registers: list[Register], max_count: int = 64) -> list[ReadBlock]:
    """Fasst Register zu möglichst wenigen Lesezugriffen zusammen.

    Nur LÜCKENLOS aufeinanderfolgende Adressen werden zusammengelegt: eine
    undokumentierte Adresse mitten im Block darf der Server mit „Illegal
    Data Address" beantworten, und dann wäre der ganze Block verloren.
    """
    blocks: list[ReadBlock] = []
    for reg_type in (RegisterType.INPUT, RegisterType.HOLDING):
        regs = sorted((r for r in registers if r.type is reg_type), key=lambda r: r.address)
        run: list[Register] = []
        for reg in regs:
            if run and (reg.address != run[-1].address + 1 or len(run) >= max_count):
                blocks.append(_block(run))
                run = []
            run.append(reg)
        if run:
            blocks.append(_block(run))
    return blocks


def _block(run: list[Register]) -> ReadBlock:
    return ReadBlock(run[0].type, run[0].address, len(run), tuple(r.key for r in run))
