"""Rechenwege der Sensoren und Vollständigkeit der Übersetzungen."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from custom_components.triplesolar.const import MAX_INTEGRATION_GAP_S
from custom_components.triplesolar.sensor import active_messages, cop, integrate_step

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "triplesolar"


def test_integrate_step_trapezoid() -> None:
    delta, point = integrate_step(None, 0, 1000, 1)
    assert delta == 0 and point == (0, 1000)
    delta, point = integrate_step(point, 600, 2000, 1)
    assert delta == pytest.approx(0.25)  # (1 kW + 2 kW)/2 × 10 min


def test_integrate_step_direction() -> None:
    # Kühlen: Wärmeabgabe negativ → zählt nur mit sign=-1.
    _, p = integrate_step(None, 0, -3000, 1)
    delta, _ = integrate_step(p, 3600, -3000, 1)
    assert delta == 0
    _, p = integrate_step(None, 0, -3000, -1)
    delta, _ = integrate_step(p, 600, -3000, -1)
    assert delta == pytest.approx(0.5)


def test_integrate_step_gap_and_missing() -> None:
    _, p = integrate_step(None, 0, 1000, 1)
    delta, p2 = integrate_step(p, MAX_INTEGRATION_GAP_S + 1, 1000, 1)
    assert delta == 0 and p2 == (MAX_INTEGRATION_GAP_S + 1, 1000)
    delta, p3 = integrate_step(p2, p2[0] + 10, None, 1)
    assert delta == 0 and p3 is None
    delta, same = integrate_step(p2, p2[0], 500, 1)  # Uhr steht
    assert delta == 0 and same == p2


def test_cop() -> None:
    assert cop(1000, 4000) == 4.0
    assert cop(1000, -2500) == 2.5
    assert cop(50, 400) is None  # Standby
    assert cop(None, 400) is None


def test_active_messages() -> None:
    data = {f"message_{n}": 0 for n in range(1, 21)} | {"message_2": 17, "message_20": 3}
    assert active_messages(data) == [17, 3]


def _used_translation_keys() -> dict[str, set[str]]:
    """translation_key-Werte je Plattform, aus dem Quelltext gelesen."""
    used: dict[str, set[str]] = {}
    for platform in ("sensor", "binary_sensor", "select", "number", "switch"):
        keys: set[str] = set()
        tree = ast.parse((COMPONENT / f"{platform}.py").read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "translation_key"
                and isinstance(node.value, ast.Constant)
            ):
                keys.add(node.value.value)
            if (
                isinstance(node, ast.Assign)
                and any(
                    getattr(t, "attr", getattr(t, "id", "")) in ("_attr_translation_key",)
                    for t in node.targets
                )
                and isinstance(node.value, ast.Constant)
            ):
                keys.add(node.value.value)
        used[platform] = keys
    # Dynamisch gesetzte Schlüssel der integrierten Energiezähler.
    used["sensor"] |= {"electric_energy", "heat_energy_heating", "heat_energy_cooling"}
    return used


@pytest.mark.parametrize("lang", ["strings", "translations/en", "translations/de"])
def test_translations_complete(lang: str) -> None:
    data = json.loads((COMPONENT / f"{lang}.json").read_text())
    for platform, keys in _used_translation_keys().items():
        missing = keys - set(data["entity"][platform])
        assert not missing, f"{lang}: {platform} ohne Übersetzung: {missing}"


def test_translations_same_structure() -> None:
    def shape(node):
        if isinstance(node, dict):
            return {k: shape(v) for k, v in node.items()}
        return None

    en = json.loads((COMPONENT / "translations/en.json").read_text())
    de = json.loads((COMPONENT / "translations/de.json").read_text())
    strings = json.loads((COMPONENT / "strings.json").read_text())
    assert shape(en) == shape(de) == shape(strings)


def test_enum_states_translated() -> None:
    from custom_components.triplesolar.registers import CircuitStatus, HeatPumpState, ModuleState, Profile

    de = json.loads((COMPONENT / "translations/de.json").read_text())["entity"]["sensor"]
    for key, enum in (
        ("heat_pump_state", HeatPumpState),
        ("dhw_status", CircuitStatus),
        ("module_state", ModuleState),
        ("dhw_profile", Profile),
    ):
        assert set(de[key]["state"]) == {m.name.lower() for m in enum}, key


def test_integrate_step_long_scan_interval() -> None:
    """Bei 900 s Abfrageintervall muss mit passend großer Lücke weitergezählt werden."""
    _, p = integrate_step(None, 0, 1200, 1, max_gap_s=2.5 * 900)
    delta, _ = integrate_step(p, 905, 1200, 1, max_gap_s=2.5 * 900)
    assert delta == pytest.approx(1200 * 905 / 3_600_000)
