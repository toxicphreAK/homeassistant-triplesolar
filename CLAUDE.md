# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Home Assistant custom integration (`custom_components/triplesolar`, HACS) for the Triple Solar PVT heat pump 5.0 / 5.0 Duo. Code comments, docstrings, log messages, docs and the README are in **German** — keep it that way.

## Commands

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component ruff radon vulture

.venv/bin/python -m pytest                                   # all tests
.venv/bin/python -m pytest tests/test_init.py::test_writes_reach_the_heat_pump   # single test
.venv/bin/ruff check . && .venv/bin/ruff format --check .    # line length 110
.venv/bin/python tools/gen_translations.py                   # regenerate strings.json + translations/{en,de}.json
.venv/bin/python tools/quality/quality_scan.py --gate structural   # import cycles (config: .quality.json)
.venv/bin/python tools/quality/quality_scan.py [--update-baseline] # full maintainability scan / re-bank baseline
```

`tools/quality/` is vendored from llmdev (re-sync with `/quality-setup +update`, don't edit locally; excluded from ruff).

`tools/probe_modbus.py` (`pair`, `read`) and `tools/probe_cloud.py` run against a real unit. They write `certs/` (gitignored, contains a private key). The integration has **not yet run against a real heat pump**. Open questions are tracked under "Offene Punkte" in `docs/modbus-api.md` and `docs/cloud-api.md`.

## Architecture

**One config entry = one connection type** (`CONF_CONNECTION`: `modbus_tls`, `modbus_tcp`, `cloud`). Local and cloud entries for the same unit may coexist, but they are deliberately separate devices, because there is no documented mapping between cloud ID and hostname (`entity.device_identifier`). Cloud is **read-only** (only `sensor` and `binary_sensor`). Modbus adds `select`, `number` and `switch`. `entry.runtime_data` is a `ModbusCoordinator` or a `CloudCoordinator`.

**`registers.py` is the single source of truth** for Modbus addresses, scaling, signedness, enums and write ranges. Entities reference registers by key, and `plan_reads()` derives the read blocks. It only merges *gap-free contiguous* addresses, because one undocumented address would make the whole block fail. `Register.encode()` validates ranges before anything reaches the device. `tests/test_registers.py` keeps an independent copy of the documented addresses. When the register table changes, update that copy (and `docs/modbus-api.md`) too. RTU/RS485 settings (0x520F–0x5211) are intentionally excluded.

**`ModbusCoordinator`** (`coordinator.py`):
- Reads blocks. On Modbus exception 2 (illegal address) it falls back to per-register reads and remembers those keys in `unsupported`.
- On connection errors (TLS only) it rediscovers the unit's new IP via mDNS, by paired hostname, at most every 5 min, and persists the new IP into `entry.data`.
- `async_write()` writes FC 6, reads back immediately and pushes the value into coordinator data.
- The options update listener reloads only when `entry.options` differs from `options_snapshot`, so data-only updates (IP change) don't reload.

**`modbus.py`** is a self-written Modbus TCP/TLS client (FC 3/4/6). It deliberately avoids pymodbus because HA pins a version whose API keeps changing. TLS is mutual auth; the peer is verified by SAN against the paired hostname, not by IP.

**Pairing** (`pairing.py`, `config_flow.py`):
1. mDNS `_ts-csr._tcp`.
2. Generate an EC P-256 key and CSR, and get the cert signed.
3. Find `_mbap-s._tcp` with the **same hostname**.

The device's pairing window is 1 min, so the flow uses a progress step with timeouts from `const.py`. Certs and keys live in the config entry data and are redacted in diagnostics.

**Cloud** (`cloud.py`) uses the unofficial myTripleSolar GraphQL API (`docs/cloud-api.md`): login/refresh with JWT, status and kWh counters. Auth failures become `ConfigEntryAuthFailed`, which triggers reauth. The heat pump ID is treated as a secret: it is kept out of the coordinator name and logs, and redacted in diagnostics.

**Sensors** (`sensor.py`):
- Register sensors use descriptions whose `key` equals the register key.
- Per-module entities use the `m{n}_` key prefix, with a `{module}` translation placeholder.
- Some sensors are derived: COP, active messages, and `IntegratedEnergySensor`. The latter uses trapezoid integration of power and is a `RestoreSensor`. It doesn't bridge gaps longer than `MAX_INTEGRATION_GAP_S`.

## Translations

Never edit `strings.json` or `translations/*.json` by hand. Edit the tables in `tools/gen_translations.py` (EN and DE tuples) and regenerate. `tests/test_misc.py` parses the platform sources (AST) and checks that every `translation_key` and enum state exists in all three files. Dynamically set keys must be added to `_used_translation_keys()` there.

## Tests

`tests/fakes.py` provides a fake Modbus server (TCP/TLS) with its own PKI that mimics the device's mutual TLS, plus a CSR signer. `test_cloud.py` contains a fake GraphQL cloud. `conftest.py` auto-enables custom integrations and mocks zeroconf for any test using `hass`. pytest runs with `asyncio_mode = "auto"`.

## Domain constraints

- § 14a EnWG power limiting is a hardware contact on the heat pump. Home Assistant only reads it and must never control it. See `docs/regelung-und-eingaenge.md`.
- Writes should happen only on change, not cyclically. It is unknown whether the device writes values to flash.
