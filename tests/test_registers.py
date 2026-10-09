"""Registertabelle gegen die Hersteller-Doku (Stand 25.09.2026)."""

from __future__ import annotations

import pytest

from custom_components.triplesolar.registers import (
    CircuitStatus,
    HeatPumpState,
    Profile,
    RegisterType,
    all_registers,
    plan_reads,
    registers_by_key,
    to_signed,
)

# Abschrift der Adressen aus docs/modbus-api.md — bewusst unabhängig von
# registers.py notiert, damit ein Tippfehler dort hier auffällt.
DOC_INPUT = {
    0x0,
    *range(0x1, 0x15),
    0x16,
    0x20,
    0x21,
    0x22,
    0x25,
    0x26,
    0x27,
    0x28,
    0x40,
    0x41,
    0x42,
    0x45,
    0x46,
    0x4A,
    0x60,
    0x61,
    0x62,
    0x65,
    0x66,
    0x69,
    0x120,
    0x121,
    0x122,
    0x123,
    0x124,
    0x400,
    0x422,
    0x441,
    0x461,
    0x4A1,
    0x4C0,
    0x4C1,
    0x500,
    0x501,
    0x680,
    0x681,
    0x683,
}
DOC_INPUT_MODULE2 = {0x800, 0x822, 0x841, 0x861, 0x8A1, 0x8C0, 0x8C1, 0x900, 0x901, 0xA80, 0xA81, 0xA83}
DOC_HOLDING = {
    0x1101,
    0x1108,
    0x2101,
    0x2107,
    0x3101,
    0x310A,
    0x3601,
    0x3602,
    0x3603,
    0x3701,
    0x3702,
    0x3703,
    0x3801,
    0x3802,
    0x3803,
    0xB101,
    0xB102,
    0xB103,
}


def addresses(modules: int, reg_type: RegisterType) -> set[int]:
    return {r.address for r in all_registers(modules) if r.type is reg_type}


def test_addresses_match_documentation() -> None:
    assert addresses(1, RegisterType.INPUT) == DOC_INPUT
    assert addresses(2, RegisterType.INPUT) == DOC_INPUT | DOC_INPUT_MODULE2
    assert addresses(1, RegisterType.HOLDING) == DOC_HOLDING


def test_rtu_hardware_registers_are_not_exposed() -> None:
    # 0x520F–0x5211 verstellen die RS485-Schnittstelle einer Duo-Kaskade.
    assert not {0x520F, 0x5210, 0x5211} & addresses(2, RegisterType.HOLDING)


def test_keys_unique_and_modules_bounded() -> None:
    assert len(registers_by_key(2)) == len(all_registers(2))
    with pytest.raises(ValueError):
        all_registers(3)


def test_to_signed() -> None:
    assert to_signed(0xFFFF) == -1
    assert to_signed(0x8000) == -32768
    assert to_signed(0x7FFF) == 32767


def test_decode() -> None:
    regs = registers_by_key()
    assert regs["heat_pump_state"].decode(0xFFFF) is HeatPumpState.FAULT
    assert regs["dhw_status"].decode(5) is CircuitStatus.PREPARE_DISABLE_DHW_VALVE
    assert regs["dhw_profile"].decode(0xFFFF) is Profile.BLOCKED
    assert regs["dhw_tank_temp"].decode(452) == 45.2
    assert regs["ambient_temp"].decode(-35 & 0xFFFF) == -3.5
    assert regs["m1_sink_power"].decode(-2500 & 0xFFFF) == -2500  # Kühlen: negativ
    assert regs["control_power_budget"].decode(40000) == 40000  # uint16
    assert regs["m1_compressor_starts"].decode(50000) == 50000
    # Ein undokumentierter Enum-Wert wird nicht geraten.
    assert regs["heat_pump_state"].decode(42) is None


def test_encode_ranges() -> None:
    regs = registers_by_key()
    assert regs["dhw_normal_setpoint"].encode(50) == 500
    assert regs["dhw_normal_setpoint"].encode(52.5) == 525
    assert regs["dhw_profile_set"].encode(Profile.BLOCKED) == 0xFFFF
    assert regs["control_power_budget"].encode(65535) == 65535
    with pytest.raises(ValueError):
        regs["dhw_normal_setpoint"].encode(75)  # > 70 °C
    with pytest.raises(ValueError):
        regs["dhw_normal_setpoint"].encode(5)
    with pytest.raises(ValueError):
        regs["sh_profile_set"].encode(3)
    with pytest.raises(ValueError):
        regs["dhw_tank_temp"].encode(50)  # Input-Register


def test_plan_reads_only_contiguous() -> None:
    blocks = plan_reads(all_registers(1))
    covered = set()
    for block in blocks:
        addrs = range(block.address, block.address + block.count)
        assert len(block.keys) == block.count
        covered |= {(block.type, a) for a in addrs}
    expected = {(RegisterType.INPUT, a) for a in DOC_INPUT} | {(RegisterType.HOLDING, a) for a in DOC_HOLDING}
    # Jede dokumentierte Adresse genau einmal, keine undokumentierte dazwischen.
    assert covered == expected
    general = next(b for b in blocks if b.address == 0x0)
    assert general.count == 0x15  # 0x0…0x14, 0x15 ist undokumentiert


def test_plan_reads_respects_max_count() -> None:
    blocks = plan_reads(all_registers(1), max_count=4)
    assert all(b.count <= 4 for b in blocks)
