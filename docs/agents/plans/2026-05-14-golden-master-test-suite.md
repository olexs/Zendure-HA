---
date: 2026-05-14T15:45:24+00:00
git_commit: c4a56fd75ec61584130e13f962553139c833df55
branch: master
topic: "Golden master test suite as 2A refactor safety net"
tags: [plan, tests, control-loop, fusegroup, migration, config-flow, ci]
status: draft
---

# Golden Master Test Suite Implementation Plan

## Overview

Establish a behavioral test suite that pins the current observable behavior of `zendure_ha` — especially the control loop in `ZendureManager.powerChanged` / `power_charge` / `power_discharge`, the `FuseGroup` partitioning and per-device power-limit math, the `update_fusegroups` partition logic, `async_migrate_entry`, and the config-flow happy path — so the planned 2A multi-P1 refactor ([2026-05-14-multiple-p1-sensors.md](2026-05-14-multiple-p1-sensors.md)) can be executed with confidence that regressions are caught. The suite uses a duck-typed `FakeDevice` to drive the control loop in pure Python (no MQTT, HTTP, or BLE) and `pytest-homeassistant-custom-component` only where a real HA bus is genuinely needed (migration, config flow, end-to-end setup smoke). A new GitHub Actions workflow runs `pytest` alongside the existing HACS and hassfest gates. The final phase is a manual mutation-fuzz exercise that validates the suite actually catches regressions before greenlighting 2A.

## Current State Analysis

- **No tests exist.** No `tests/` directory, no `pytest-homeassistant-custom-component` dependency, no test workflow. CI runs HACS validation (`.github/workflows/validate.yaml`) and hassfest (`.github/workflows/hassfest.yaml`) only.
- **`requirements.txt`** pins `homeassistant>=2026.4.3`, `paho.mqtt==2.1.0`, `bleak-retry-connector>=4.6.0`, `aiohttp`, `voluptuous`, `colorlog`, `pip`, `ruff==0.15.12`. No test-time deps.
- **`scripts/setup`** installs `requirements.txt` and scaffolds `config/`. **`scripts/lint`** runs `ruff format . && ruff check . --fix`. **`scripts/develop`** runs HA against the local custom component. No test runner script.
- **`.ruff.toml`** has `select = ["ALL"]` with a long ignore list, `target-version = "py312"`, `max-complexity = 25`. Tests will trip many of the enabled rules unless `tests/` gets its own ignore set (commonly: `S101` for asserts, `D` for docstrings, `ANN` for missing annotations on test helpers, `PLR2004` for magic numbers).
- **Control loop entry points and read surface** (manager.py:360-630):
  - `_p1_changed(event)` — debounce + stddev gating + per-cycle scratch reset + calls `powerChanged`.
  - `powerChanged(p1, isFast, time)` — partition devices into `charge / discharge / idle` lists, dispatch by `ManagerMode`.
  - `power_charge(setpoint, time)` — stop discharging, weight-distribute charge across `charge` list, start idle devices if needed; uses `charge_time / charge_last / pwr_low` for hysteresis.
  - `power_discharge(setpoint)` — symmetric for discharge; same hysteresis state.
  - Per-device attribute reads (15+): `batteryOutput.asInt`, `homeInput.asInt`, `homeOutput.asInt`, `solarInput.asInt`, `batteryInput.asInt`, `byPass.asInt`, `electricLevel.asInt`, `kWh`, `actualKwh`, `availableKwh.asNumber`, `socSet.asNumber`, `socLimit.asInt`, `minSoc.asNumber`, `pwr_offgrid` (property), `pwr_produced`, `pwr_max`, `state` (DeviceState), `online` (property based on `connectionStatus.asInt >= 10`), `charge_limit`, `discharge_limit`, `charge_optimal`, `discharge_optimal`, `charge_start`, `discharge_start`, `fuseGrp` (FuseGroup), `name`, `deviceId`.
  - Per-device async calls: `power_get()`, `power_charge(power)`, `power_discharge(power)`, `power_off()`.
- **`FuseGroup`** (fusegroup.py): 73 lines, pure Python except for type-only `from .device import ZendureDevice`. `charge_limit(d)` / `discharge_limit(d)` run weighted distribution on first call per cycle (`initPower` guard), and the manager resets `fg.initPower = True` for every fuseGroup at the top of each cycle (manager.py:409-410). Reads `homeInput.asInt`, `homeOutput.asInt`, `electricLevel.asInt`, `charge_limit`, `discharge_limit`, `charge_start`, `discharge_start`, sets `pwr_max`.
- **`update_fusegroups`** (manager.py:168-250): three-pass partition logic; the existing 2A plan describes its quirks in detail (manager.py:236-238 dead-string-key lookup; orphan-group fall-through at line 244-250).
- **`async_migrate_entry`** (__init__.py:20-26): only runs Migration when `minor_version < 5`, then unconditionally bumps to 5. Since `config_flow.MINOR_VERSION = 7`, this is effectively a "downgrade to 5" call on any entry not already at 5. Worth a test to document the actual behavior, *not* to fix it now.
- **`Migration.async_migrate`** (migration.py:84-194): touches `dr.async_get`, `er.async_get`, `rs.async_get`, walks `hass.config.path(".storage")` and `hass.config.config_dir` to rewrite yaml/json files. The file-walking part needs a tmp config dir (already provided by the `hass` fixture).
- **Config flow** (config_flow.py): `async_step_user` calls `Api.Connect` → posts to a real URL. Test path must mock `Api.Connect`. Two-step `async_step_local` branch when `CONF_MQTTLOCAL=True`. Options flow at `ZendureOptionsFlowHandler`.
- **`ZendureManager.__init__`** can't run without a real `hass` because of `DataUpdateCoordinator.__init__` + `EntityDevice.__init__` (which calls `dr.async_get(hass)`). For the control-loop tests, construct managers via `object.__new__(ZendureManager)` and set only the attributes the methods under test actually read.
- **`pytest-homeassistant-custom-component`** version → HA version mapping is by patch number; latest is 0.13.330 targeting HA 2026.5.1. For HA 2026.4.3 we need to identify a compatible release (likely in the 0.13.32x range) at implementation time.

## Desired End State

- A `tests/` directory at the repo root with:
  - `tests/conftest.py` — pytest configuration, `enable_custom_integrations` autouse fixture, shared HA fixtures.
  - `tests/fakes.py` — `FakeDevice`, `FakeSensorValue`, `FakeNumber`, `FakeSelect`, `build_test_manager()` helper.
  - `tests/test_fusegroup.py` — pure-pytest unit tests of `FuseGroup` math.
  - `tests/test_control_loop.py` — pure-pytest tests driving `powerChanged` / `power_charge` / `power_discharge` directly via the helper-built manager.
  - `tests/test_update_fusegroups.py` — pure-pytest tests pinning the partition logic.
  - `tests/test_migration.py` — `hass`-fixture tests of `async_migrate_entry` + `Migration.async_migrate`.
  - `tests/test_config_flow.py` — `hass`-fixture tests of the config-flow happy paths.
  - `tests/test_setup.py` — `hass`-fixture smoke test for full `async_setup_entry` + `async_unload_entry`.
- `requirements-test.txt` listing test-only deps (`pytest`, `pytest-asyncio`, `pytest-homeassistant-custom-component` pinned to an HA-2026.4.x-compatible version, `pytest-cov` for local-only coverage runs).
- `.github/workflows/tests.yaml` running `pytest tests/` as a hard gate on push and pull request.
- `.ruff.toml` extended with a `tests/` per-file-ignores section so test code passes lint without ceremony.
- `scripts/test` shell script as a parallel to `scripts/lint` / `scripts/develop`, runnable locally.
- A documented mutation-fuzz protocol (`tests/MUTATION_FUZZ.md`) listing the deliberate regressions to introduce and the expected failing test(s) for each.
- All tests pass `green` against the current `master` (commit c4a56fd) — *zero* tests for behaviors the user wants to change as part of 2A; this is a behavioral snapshot of today.

## What We're NOT Doing

- No tests of MQTT message payloads, paho client behavior, or the per-device cloud bridge — out of scope and not what 2A touches.
- No tests of BLE provisioning (`bleMqtt`, `bleAdapter`, `bleCommand`) — same reason.
- No tests of HTTP communication with ZenSDK devices (`httpGet`, `httpPost`).
- No tests of the auto-MQTT-user creation path (`CONF_AUTO_MQTT_USER`) — sidecar feature, not control-loop.
- No tests of HACS or hassfest validation — those are existing CI gates.
- No coverage threshold gate. Coverage is collectable locally via `pytest --cov` but not enforced in CI; behavioral coverage matters more than line %.
- No property-based / hypothesis tests. Per-scenario asserts with chosen inputs are clearer and easier to debug.
- No fixing of pre-existing quirks (the `async_migrate_entry` "downgrade to 5" issue, the `manager.py:236-238` dead lookup, the line-205/238 double-append). Tests pin current behavior. Fix-then-test is a separate decision.
- No literal golden-master snapshot files (record-and-diff JSON). Per-scenario asserts only.

## Architecture and Code Reuse

The test suite has two distinct slices, separated by whether they need a real `HomeAssistant` instance:

```
tests/
  conftest.py        ┐
  fakes.py           │  shared
  MUTATION_FUZZ.md   ┘

  test_fusegroup.py          ┐
  test_control_loop.py       │  pure pytest (no hass fixture)
  test_update_fusegroups.py  ┘  → fast (~ms per test), no event loop except pytest-asyncio

  test_migration.py     ┐
  test_config_flow.py   │  pytest-homeassistant-custom-component (hass fixture)
  test_setup.py         ┘  → slower (~100ms per test), full HA bus
```

**Reusable test fixtures (`tests/fakes.py`):**

- `FakeSensorValue(value: int|float)` — duck-typed `ZendureSensor` exposing `.asInt`, `.asNumber`, `.update_value(v)`. The simplest possible stub.
- `FakeNumber(value: float)` — exposes `.asNumber`. For `manualpower`.
- `FakeSelect(value: int, state: str)` — exposes `.value`, `.state`, `.current_option`, `.setDict(...)`, `.onchanged`. For `fuseGroup`.
- `FakeFuseGroup(name, maxpower, minpower)` — duck-typed wrapper; **for tests that don't exercise the weighted math, use this stub; for tests that do, use the real `FuseGroup` from `fusegroup.py`**.
- `FakeDevice` — exposes the full read surface listed in Current State Analysis, plus call-recording async methods `power_get / power_charge / power_discharge / power_off / charge / discharge`. Records calls into `device.calls: list[tuple[str, int|None]]` for assertion. Has a `power_get_result: bool = True` knob and `state: DeviceState = INACTIVE` so individual scenarios can toggle online/offline and SOC states.
- `build_test_manager(devices, *, operation=ManagerMode.MATCHING, manualpower=0, fusegroups=None)` — constructs `ZendureManager` via `object.__new__`, sets the minimum attribute surface for `powerChanged` / `power_charge` / `power_discharge` to run (`devices`, `fuseGroups`, `operation`, `simulation=False`, per-cycle scratch fields zeroed, hysteresis fields at sentinel values, stub `manualpower` / `power` / `availableKwh` / `operationstate` entities). Returns the manager.

**Reusable HA-bus fixtures (`tests/conftest.py`):**

- `enable_custom_integrations` — autouse, from `pytest_homeassistant_custom_component`. Makes the `custom_components/zendure_ha` discoverable as a custom integration.
- `mock_zendure_token` — a base64-encoded `<api_url>.<appKey>` string usable in config-flow tests.
- `mock_api_connect` — fixture that monkeypatches `Api.Connect` and `Api.ApiHA` to return a synthetic `deviceList` + `mqtt` response. Parameterizable by device list.
- `synthetic_device_definition(model: str, deviceId: str, sn: str) -> dict` — builds the `definition` dict that `ZendureDevice.__init__` expects (`productKey`, `productModel`, `snNumber`, optional `ip`, `deviceName`).
- `mock_paho_client` — context manager that swaps `paho.mqtt.client.Client` with a `unittest.mock.MagicMock` so `Api.Init` doesn't actually open sockets during setup smoke tests.

**File-level change summary:**

- `tests/`
  - `__init__.py` — empty
  - `conftest.py` — fixtures listed above; pytest-asyncio mode = `auto`
  - `fakes.py` — `FakeSensorValue`, `FakeNumber`, `FakeSelect`, `FakeFuseGroup`, `FakeDevice`, `build_test_manager`
  - `test_fusegroup.py` — ~10 cases against real `FuseGroup`
  - `test_control_loop.py` — ~20 cases across `powerChanged` / `power_charge` / `power_discharge` / all `ManagerMode` branches
  - `test_update_fusegroups.py` — ~6 cases pinning partition behavior
  - `test_migration.py` — ~5 cases across migration boundaries
  - `test_config_flow.py` — ~4 cases: happy path, MQTTLOCAL branch, options flow, reconfigure
  - `test_setup.py` — 1 smoke test for `async_setup_entry` + `async_unload_entry`
  - `MUTATION_FUZZ.md` — mutation protocol
- `requirements-test.txt` — new
- `.github/workflows/tests.yaml` — new
- `.ruff.toml` — append `[lint.per-file-ignores]` for `tests/*`
- `scripts/test` — new, mirrors `scripts/lint`

## Performance Considerations

- Pure-pytest tests (FuseGroup, control loop, update_fusegroups) run in milliseconds. Whole suite of ~36 scenarios should finish in <5s locally.
- `pytest-homeassistant-custom-component` tests boot a real `HomeAssistant`; each `hass` fixture costs ~100-300ms. Migration tests (~5), config flow tests (~4), setup smoke (~1) = ~10 tests total = ~3s.
- CI workflow target: full suite under 60s (most of which is pip install). No parallel-pytest needed at this volume.

## Migration Notes

N/A — this is greenfield test scaffolding. No existing tests to migrate.

---

## Phase 1: Test scaffolding & CI

[Dependencies: none — must be first.]

Lay down the directory layout, dependencies, fakes, and CI workflow. By the end of this phase, `pytest tests/` runs and discovers zero tests (or one trivial smoke test) — no actual coverage yet, but the rails exist.

**Tasks**:

- [x] Create `tests/__init__.py` (empty, to make tests a package).
- [x] Create `tests/conftest.py` with:
  - [x] `pytest_plugins = ["pytest_homeassistant_custom_component"]`
  - [x] `pytestmark = pytest.mark.asyncio` is **not** set globally — instead set `asyncio_mode = "auto"` in `pyproject.toml` or `pytest.ini`
  - [x] `enable_custom_integrations` autouse fixture (re-exported / aliased from the plugin)
  - [x] `mock_zendure_token` fixture: returns `base64.b64encode(b"https://api.test.example.appkey-xyz").decode()` — `Api.ApiHA` does `rsplit(".", 1)` on the decoded value, so the last `.` must separate url from appkey.
  - [x] `synthetic_device_definition(model, deviceId, sn)` helper
  - [x] `mock_api_connect` fixture: monkeypatches `custom_components.zendure_ha.api.Api.Connect` to return `{"deviceList": [...], "mqtt": {"clientId": "test", "url": "mqtt.test:1883", "username": "u", "password": "p"}}`
  - [x] `mock_paho_client` fixture: monkeypatches `paho.mqtt.client.Client` to a `MagicMock`
- [x] Create `tests/fakes.py`:
   ```python
   from dataclasses import dataclass, field
   from custom_components.zendure_ha.const import DeviceState

   @dataclass
   class FakeSensorValue:
       value: float = 0
       @property
       def asInt(self) -> int: return int(self.value)
       @property
       def asNumber(self) -> float: return float(self.value)
       def update_value(self, v): self.value = v if isinstance(v, (int, float)) else 0

   @dataclass
   class FakeSelect:
       value: int = 0
       state: str = ""
       current_option: str = ""
       onchanged: object = None
       def setDict(self, options): pass

   @dataclass
   class FakeDevice:
       name: str = "Fake"
       deviceId: str = "fake-1"
       kWh: float = 1.0
       charge_limit: int = -800
       discharge_limit: int = 800
       charge_optimal: int = -200
       discharge_optimal: int = 200
       charge_start: int = -80
       discharge_start: int = 80
       pwr_max: int = 0
       pwr_produced: int = 0
       pwr_offgrid_value: int = 0   # exposed via property below
       state: DeviceState = DeviceState.INACTIVE
       online_value: bool = True
       actualKwh: float = 0.0
       power_get_result: bool = True

       # ... entity stubs (one FakeSensorValue per accessed attribute)
       batteryOutput: FakeSensorValue = field(default_factory=FakeSensorValue)
       batteryInput: FakeSensorValue = field(default_factory=FakeSensorValue)
       homeInput: FakeSensorValue = field(default_factory=FakeSensorValue)
       homeOutput: FakeSensorValue = field(default_factory=FakeSensorValue)
       solarInput: FakeSensorValue = field(default_factory=FakeSensorValue)
       byPass: FakeSensorValue = field(default_factory=FakeSensorValue)
       electricLevel: FakeSensorValue = field(default_factory=lambda: FakeSensorValue(50))
       socSet: FakeSensorValue = field(default_factory=lambda: FakeSensorValue(100))
       socLimit: FakeSensorValue = field(default_factory=FakeSensorValue)
       minSoc: FakeSensorValue = field(default_factory=FakeSensorValue)
       availableKwh: FakeSensorValue = field(default_factory=FakeSensorValue)

       fuseGroup: FakeSelect = field(default_factory=FakeSelect)
       fuseGrp: object = None  # set by tests, real FuseGroup or stub

       calls: list = field(default_factory=list)

       @property
       def pwr_offgrid(self) -> int: return self.pwr_offgrid_value
       @property
       def online(self) -> bool: return self.online_value

       async def power_get(self) -> bool:
           self.actualKwh = self.availableKwh.asNumber
           return self.power_get_result

       async def power_charge(self, power: int) -> int:
           self.calls.append(("power_charge", power)); return power

       async def power_discharge(self, power: int) -> int:
           self.calls.append(("power_discharge", power)); return power

       async def power_off(self) -> None:
           self.calls.append(("power_off", None))

       async def charge(self, power: int) -> int:
           self.calls.append(("charge", power)); return power

       async def discharge(self, power: int) -> int:
           self.calls.append(("discharge", power)); return power

       setStatus_calls: int = 0
       def setStatus(self): self.setStatus_calls += 1

   def build_test_manager(devices, *, operation=None, manualpower=0, fusegroups=None):
       from custom_components.zendure_ha.manager import ZendureManager
       from custom_components.zendure_ha.const import ManagerMode
       from datetime import datetime
       from collections import deque
       m = object.__new__(ZendureManager)
       m.devices = devices
       m.fuseGroups = fusegroups or []
       m.operation = operation or ManagerMode.MATCHING
       m.simulation = False
       m.p1_history = deque([25, -25], maxlen=8)
       m.p1_factor = 1
       m.zero_next = datetime.min
       m.zero_fast = datetime.min
       m.charge = []
       m.charge_limit = 0; m.charge_optimal = 0; m.charge_weight = 0
       m.charge_time = datetime.max; m.charge_last = datetime.min
       m.discharge = []; m.discharge_bypass = 0; m.discharge_produced = 0
       m.discharge_limit = 0; m.discharge_optimal = 0; m.discharge_weight = 0
       m.idle = []; m.idle_lvlmax = 0; m.idle_lvlmin = 100; m.produced = 0
       m.pwr_low = 0; m.update_count = 0; m.p1meterEvent = None
       m.manualpower = FakeNumber(manualpower)
       m.power = FakeSensorValue()
       m.availableKwh = FakeSensorValue()
       m.operationstate = FakeSensorValue()
       return m
   ```
- [x] Create `requirements-test.txt`:
   ```
   pytest>=8.0
   pytest-asyncio>=0.24
   pytest-cov>=5.0
   pytest-homeassistant-custom-component  # pin to specific version (see below)
   ```
- [x] Resolve the `pytest-homeassistant-custom-component` pin: from the master changelog, pick the latest release whose `ha_version` is ≤ 2026.4.3 + 1 patch (to avoid SDK drift). Verify by running `pip install homeassistant>=2026.4.3 pytest-homeassistant-custom-component==<candidate>` in a clean venv and confirming the resolver picks an HA version that doesn't conflict. Pin to that exact version in `requirements-test.txt`. **Resolved: `>=0.13.324`** lands `pytest-homeassistant-custom-component==0.13.330` + `homeassistant==2026.5.1` on Python 3.14.4 (HA 2026.4+ requires Python ≥3.14.2).
- [x] Create `pyproject.toml` (or add to existing — none exists in the repo currently) with a minimal pytest config. **Added `pythonpath = ["."]` to expose the repo root for `from custom_components.zendure_ha ...` imports.**
- [x] Append to `.ruff.toml`:
   ```toml
   [lint.per-file-ignores]
   "tests/*" = ["S101", "D", "ANN", "PLR2004", "PT011", "SLF001", "INP001"]
   ```
   - `S101` (asserts allowed in tests), `D` (docstrings optional), `ANN` (annotations optional on test helpers), `PLR2004` (magic numbers OK), `PT011` (broad `pytest.raises`), `SLF001` (private member access — needed for `object.__new__(ZendureManager)` setup), `INP001` (tests/ namespace package is intentional).
- [x] Create `scripts/test`:
   ```bash
   #!/usr/bin/env bash
   set -e
   cd "$(dirname "$0")/.."
   python3 -m pip install -q --requirement requirements.txt --requirement requirements-test.txt
   python3 -m pytest tests/ "$@"
   ```
   `chmod +x scripts/test`.
- [x] Create `.github/workflows/tests.yaml` (Python 3.14 in CI to match HA 2026.4+ runtime requirement):
   ```yaml
   name: Tests
   on:
     push:
     pull_request:
     workflow_dispatch:
   jobs:
     pytest:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v6
         - uses: actions/setup-python@v5
           with:
             python-version: "3.12"
         - name: Install deps
           run: |
             python -m pip install --upgrade pip
             pip install -r requirements.txt -r requirements-test.txt
         - name: Run pytest
           run: pytest tests/ -q
   ```
- [x] Create a placeholder `tests/test_smoke.py` with one trivial test that imports `custom_components.zendure_ha.manager` and asserts the module loaded — to prove the CI is wired up before any real tests exist:
   ```python
   def test_imports():
       from custom_components.zendure_ha import manager
       assert manager.ZendureManager is not None
   ```

**Automated Verification**:
- [x] `scripts/lint` passes (ruff doesn't trip on `tests/`).
- [x] `scripts/test` runs locally, discovers and passes `tests/test_smoke.py`.
- [x] `pytest tests/ -q` exits 0 with one passing test.
- [ ] GitHub Actions `Tests` workflow runs green on the first push. *(awaits push to remote — not yet pushed)*

**Manual Verification**:
- (Omitted — internal infrastructure phase. Validation is in the automated checks above.)

**Implementation notes**:
- Used Python **3.14.4** (not 3.12 as the original plan implied). HA 2026.4+ requires Python ≥ 3.14.2; the venv would not install otherwise. CI workflow pinned to `python-version: "3.14"`.
- Resolved `pytest-homeassistant-custom-component>=0.13.324` lands `0.13.330` + `homeassistant==2026.5.1`. The plugin's resolver picked a newer HA than the `>=2026.4.3` minimum but it's compatible.
- HA's `homeassistant.components.bluetooth` (imported by `device.py:22`) requires several transitive deps not pulled in by the homeassistant wheel: `aiousbwatcher`, `serialx`, `dbus-fast`, `aioesphomeapi`. Added to `requirements-test.txt`.
- `pythonpath = ["."]` in `pyproject.toml` was insufficient on its own; the plugin's `hass` fixture mutates `custom_components.__path__` at runtime. Solution: dropped the `auto_enable_custom_integrations` autouse — pure-pytest tests (Phase 2/3/4) use plain Python imports without `hass`; Phase 5 tests opt in to `enable_custom_integrations` explicitly and will need a symlink/copy of `custom_components/zendure_ha` into the plugin's `testing_config` dir at that phase.

---

## Phase 2: FuseGroup unit tests

[Dependencies: **Phase 1**.]

Pin the weighted distribution math in `FuseGroup.charge_limit` and `FuseGroup.discharge_limit`, plus the edge cases the existing code defends against (`weight=0`, `weight<0` flip in charge path, `homeInput<=0`/`homeOutput<=0` skipping). Pure-pytest, real `FuseGroup` + `FakeDevice`.

**Tasks**:

- [x] Create `tests/test_fusegroup.py` with these scenarios — for each, instantiate `FuseGroup(name, maxpower, minpower, devices=[FakeDevice(...)])`, manipulate `initPower` and per-device sensor values, call `charge_limit(d)` / `discharge_limit(d)`, and assert resulting `d.pwr_max`:

  - [x] `test_single_device_charge_caps_at_minpower` — group `maxpower=800, minpower=-1200`, device `charge_limit=-1500`. Expect `d.pwr_max == max(-1200, -1500) == -1200`.
  - [x] `test_single_device_discharge_caps_at_maxpower` — symmetric, expect `d.pwr_max == min(800, 1500) == 800`.
  - [x] `test_single_device_within_limits` — group `maxpower=800, minpower=-1200`, device `charge_limit=-1000, discharge_limit=600`. Expect `pwr_max` unchanged at `-1000` / `600`.
  - [x] `test_multi_device_charge_weighted_by_remaining_capacity` — two devices, both with `homeInput.asInt=100` (so they pass the `> 0` guard), `charge_limit=-800`, `electricLevel=50` for device A, `electricLevel=80` for device B. Expect device A (lower SOC, more capacity remaining) gets a larger share of the negative limit.
  - [x] `test_multi_device_discharge_weighted_by_soc` — symmetric, two devices with `homeOutput.asInt=100`, `discharge_limit=800`, `electricLevel=80` for A, `electricLevel=50` for B. Expect A (higher SOC) gets larger share.
  - [x] `test_multi_device_skips_zero_homeinput_in_charge` — three devices, two with `homeInput.asInt>0`, one with `homeInput.asInt=0`. Expect the zero-homeInput device's `pwr_max` is not touched by `charge_limit` math.
  - [x] `test_multi_device_all_zero_homeinput_charge` — all devices have `homeInput.asInt=0`. Expect `pwr_max` unchanged on all (no math runs because `weight == 0`).
  - [x] `test_initPower_consumed_only_once` — call `charge_limit(d)` twice without resetting `initPower`. Expect the second call returns the cached `d.pwr_max` from the first call (no recomputation, no mutation).
  - [x] `test_initPower_reset_recomputes` — call `charge_limit(d)`, change device SOC, set `fg.initPower=True`, call again. Expect new value based on updated SOC.
  - [x] `test_charge_weight_zero_uses_charge_start_fallback` — synthesize the degenerate case for the `pwr_max = ... if weight < 0 else fd.charge_start` ternary at fusegroup.py:41. Since `charge_limit` is always negative and `(100 - electricLevel)` is non-negative, `weight = sum((100 - SOC) * charge_limit)` is normally `≤ 0`; the `else fd.charge_start` branch fires only when `weight == 0` (all devices at SOC=100 or all `charge_limit==0`). Construct that degenerate input and assert each device's `pwr_max == fd.charge_start`.
  - [x] **Bonus**: parametrized `test_single_device_charge_uses_max_of_minpower_and_limit` covering 3 boundary cases (tighter / looser / equal) — pins the single-device `max()` clamp at boundary values.

**Automated Verification**:
- [x] `pytest tests/test_fusegroup.py -q` passes (13 tests — 10 named + 3 parametrized).
- [x] `scripts/lint` clean.

**Manual Verification**:
- (Omitted — pure-function math, fully covered by automated tests.)

---

## Phase 3: Control-loop unit tests

[Dependencies: **Phase 1**, **Phase 2** (FakeFuseGroup math validated).]

The largest phase. Build per-scenario unit tests for `powerChanged`, `power_charge`, `power_discharge` covering all `ManagerMode` branches, partition outcomes, device-state transitions, hysteresis, and the defensive math the code already contains.

Each test follows the shape:
```python
async def test_X():
    dev_a = FakeDevice(name="A", electricLevel=FakeSensorValue(50), ...)
    fg = FuseGroup("A", maxpower=800, minpower=-1200, devices=[dev_a])
    dev_a.fuseGrp = fg
    mgr = build_test_manager([dev_a], operation=ManagerMode.MATCHING, fusegroups=[fg])
    await mgr.powerChanged(p1=-500, isFast=False, time=datetime.now())
    assert ("power_charge", -500) in dev_a.calls
```

**Tasks** — grouped by what each scenario pins:

*Partitioning (`powerChanged` lines 426-455):*
- [x] `test_partition_charge_when_home_input_negative`
- [x] `test_partition_discharge_when_home_output_positive`
- [x] `test_partition_idle_when_neither` — also asserts `idle_lvlmax/min` track the device's SOC.
- [x] `test_partition_offline_excluded` — `power_get_result=False` device has no dispatch calls.
- [x] `test_partition_pwr_offgrid_positive_routed_as_charge` — **deviation**: pinned the observed behavior, which is `home = -homeInput + max(0, pwr_offgrid)` = positive when only `pwr_offgrid > 0`. So the device falls through to `idle`, NOT `charge`. The plan misread `max(0, pwr_offgrid)` semantics. Documented in the test.
- [x] `test_pwr_produced_clamped_at_zero` — pins `min(0, ...)` clamp.

*ManagerMode dispatch (`powerChanged` lines 471-501):*
- [x] `test_mode_off_writes_operation_state_off`
- [x] `test_mode_matching_negative_setpoint_calls_charge`
- [x] `test_mode_matching_positive_setpoint_calls_discharge`
- [x] `test_mode_matching_zero_setpoint_calls_discharge_zero` — asserts `operationstate == IDLE` (proves the else-arm took setpoint=0).
- [x] `test_mode_matching_discharge_clamps_at_zero`
- [x] `test_mode_matching_charge_with_produced_discharges_produced`
- [x] `test_mode_store_solar_charges_when_setpoint_negative`
- [x] `test_mode_store_solar_does_not_discharge_produced` — pins the M9 mutation guard.
- [x] `test_mode_manual_uses_manualpower_not_p1` — covers both positive and negative manualpower.

*`power_charge` distribution (manager.py:503-567):*
- [x] `test_charge_single_device_full_setpoint` — pins exact dispatch value (`power_charge(-800)`).
- [x] `test_charge_two_devices_weighted_by_soc` — qualitative assertion (low_soc < high_soc).
- [x] `test_charge_weight_zero_no_division_error` — no exception raised.
- [x] `test_charge_stops_discharging_devices` — discharge device gets `power_discharge(0)`.
- [x] `test_charge_skips_bypass_device_when_stopping_discharge`
- [x] `test_charge_hysteresis_first_call_returns_zero`
- [x] `test_charge_hysteresis_second_call_respects_setpoint`
- [x] `test_charge_starts_idle_device_when_dev_start_negative`

*`power_discharge` distribution (manager.py:569-630):*
- [x] `test_discharge_single_device_full_setpoint`
- [x] `test_discharge_two_devices_weighted_by_soc` — qualitative (high > low).
- [x] `test_discharge_weight_zero_distributes_evenly` — no exception.
- [x] `test_discharge_socfull_passes_through_solar_only` — **deviation**: pinned exact dispatch value (350W). Plan's expected value (200) was based on a misread of the `pwr_produced` math (it doesn't equal `-battery_input`). The full math chain (bypass accumulation + setpoint clamp + weighted distribution + final cap) is documented in the test docstring.
- [x] `test_discharge_bypass_clamps_setpoint_when_p1_nonneg` — pins issue #1151.
- [x] `test_discharge_bypass_allows_setpoint_negative_when_p1_negative` — **deviation**: asserts `mgr.operationstate.value == CHARGE` instead of `("power_charge", ...) in calls`. The device is in `self.discharge`, so `power_charge`'s main loop doesn't dispatch to it, but `operationstate` reflects the post-clamp setpoint sign.

**Automated Verification**:
- [x] `pytest tests/test_control_loop.py -q` passes (29 tests).
- [x] `scripts/lint` clean.

**Manual Verification**:
- (Omitted — every assertion is mechanical and verified by pytest. The "did the suite catch real bugs?" question is answered in Phase 6.)

---

## Phase 4: `update_fusegroups` partition tests

[Dependencies: **Phase 1**.]

Pin the partition behavior. These tests use `build_test_manager` + `FakeDevice` (with `FakeSelect` for `fuseGroup`), call `await mgr.update_fusegroups()`, and assert the shape of `mgr.fuseGroups` and the `pwr_max` / `fuseGrp` assignments on each device.

**Tasks**:

- [x] `test_single_device_group800_creates_own_fusegroup`
- [x] `test_unused_fusegroup_calls_power_off_when_not_off_mode`
- [x] `test_unused_fusegroup_skips_power_off_when_off_mode`
- [x] `test_part_of_x_share_join_produces_shared_group` — **important deviation**: pinned the **orphan-group quirk** the plan calls out in the 2A doc. When B does "Part of A", B ends up in BOTH A's shared group AND its own orphan group (from the build pass). `b.fuseGrp` ends up pointing at the orphan (the LAST write in the split/keep loop), NOT at A's shared group. This is intended pre-existing behavior; the 2A plan preserves it. Test asserts: (a) `b in a_group.devices`, (b) `b_group` exists in `mgr.fuseGroups`, (c) `b.fuseGrp is b_group`.
- [x] `test_split_when_combined_limits_fit_individual` — **deviation**: final count is 3 (2 split single-device groups + 1 orphan), not 2 as the plan said. The orphan-group quirk applies here too.
- [x] `test_no_split_when_combined_limits_exceed` — **deviation**: final count is 2 (1 shared + 1 orphan), not 1.
- [x] `test_setStatus_called_per_device` — asserts each device's `setStatus_calls == 1` (using the FakeDevice counter).
- [x] **Bonus**: `test_part_of_x_join_when_value_does_not_match_falls_through` — covers the predefined-int-key case (value=2 for group800) where the dict lookup never matches, documenting that the integer-key never-matches behavior is the normal path for single-device installs.

**Automated Verification**:
- [x] `pytest tests/test_update_fusegroups.py -q` passes (8 tests).
- [x] `scripts/lint` clean.

**Manual Verification**:
- (Omitted — internal partition logic, no user-facing surface here.)

---

## Phase 5: Migration & config-flow integration tests

[Dependencies: **Phase 1**.]

Tests that need a real `HomeAssistant` instance. Uses the `hass` fixture from `pytest-homeassistant-custom-component`.

**Tasks** (`tests/test_migration.py`):

- [ ] `test_migrate_entry_at_minor_5_unchanged` — create a `MockConfigEntry(version=1, minor_version=5, data={...})`, add to `hass`, call `async_migrate_entry(hass, entry)`. Assert returns `True`, `entry.minor_version == 5` (unchanged). Pin current behavior.
- [ ] `test_migrate_entry_at_minor_4_runs_migration_then_bumps` — entry at `minor_version=4`. Mock `Migration.async_migrate` to a recorder. Assert it was called, and `entry.minor_version == 5` afterwards.
- [ ] `test_migrate_entry_at_minor_7_pins_downgrade_quirk` — entry at `minor_version=7` (current `MINOR_VERSION` from config_flow). Call migrate. Pin whatever HA actually does (`async_update_entry(minor_version=5)` on a 7 entry either downgrades, no-ops, or errors — document the observed behavior in the test docstring). This test is intentionally pinning a pre-existing quirk so 2A's migration changes don't accidentally "fix" it without anyone noticing.
- [ ] `test_migration_renames_stale_entity` — pre-seed `entity_registry` with an entity whose `unique_id` follows the old (pre-rename) format, then call `Migration.async_migrate`. Assert the entity's `unique_id` is rewritten to the new format. Use one representative entity from each category (sensor, select, number) if possible.
- [ ] `test_migration_no_op_for_already_clean_registry` — pre-seed only canonical entities, run migration, assert no `async_update_entity` calls (or no observable changes).

**Tasks** (`tests/test_config_flow.py`):

- [ ] `test_config_flow_happy_path` — use `mock_api_connect` to return a single synthetic Hyper2000. Call `hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})`. Submit the form with `CONF_APPTOKEN`, `CONF_P1METER="sensor.power_actual"`, `CONF_MQTTLOG=False`, `CONF_MQTTLOCAL=False`. Assert: result type is `create_entry`, `data` contains the four fields, `unique_id == "Zendure"`.
- [ ] `test_config_flow_mqttlocal_branch_two_step` — submit form with `CONF_MQTTLOCAL=True`. Assert: result type is `form` with `step_id == "local"`. Submit the local step with `CONF_MQTTSERVER, CONF_MQTTPORT, CONF_MQTTUSER, CONF_MQTTPSW`. Assert: final result type is `create_entry`.
- [ ] `test_options_flow_updates_data` — start from a `MockConfigEntry`. Call `hass.config_entries.options.async_init(entry.entry_id)`. Submit with a changed `CONF_P1METER`. Assert: `entry.data[CONF_P1METER]` is updated. (This pins the options flow's "write to data not options" current behavior — note the `async_update_entry(... data=data)` in `config_flow.py:159`.)
- [ ] `test_config_flow_reconfigure_path` — call reconfigure on an existing entry, assert it flows through and updates the entry without creating a duplicate.

**Tasks** (`tests/test_setup.py`):

- [ ] `test_async_setup_entry_loads_devices` — with `mock_api_connect` + `mock_paho_client`, create a `MockConfigEntry(version=1, minor_version=7, data={CONF_APPTOKEN: "...", CONF_P1METER: "sensor.power_actual", CONF_MQTTLOG: False, CONF_MQTTLOCAL: False})`, add to `hass`, call `await hass.config_entries.async_setup(entry.entry_id)`. Assert: state is `LOADED`, `entry.runtime_data` is a `ZendureManager`, `len(entry.runtime_data.devices) == 1` (matching the mocked deviceList). Then unload: assert state is `NOT_LOADED`.

**Automated Verification**:
- [ ] `pytest tests/test_migration.py tests/test_config_flow.py tests/test_setup.py -q` passes (10 tests).
- [ ] `scripts/lint` clean.
- [ ] Total suite (`pytest tests/ -q`) passes ~50 tests.

**Manual Verification**:
- [ ] **Run the full suite locally and observe pass count + timing**:
   1. Run `scripts/test`.
   2. Confirm exit code 0, ~50 tests passed, total runtime under 30s.
- [ ] **Confirm CI green on a feature branch**:
   1. Push the branch.
   2. Wait for the `Tests` workflow job to finish.
   3. Confirm it's green (and `HACS validation`, `Validate with hassfest` are still green).

---

## Phase 6: Mutation-fuzz validation

[Dependencies: **Phases 1-5** complete and green.]

The "did this safety net actually catch regressions?" gate before 2A starts. Manual, but follows a documented protocol so it's reproducible and the result is auditable. The output of this phase is a filled-in checklist in `tests/MUTATION_FUZZ.md` showing which mutations were caught.

**Tasks**:

- [ ] Create `tests/MUTATION_FUZZ.md` with the mutation protocol and the table below pre-populated (results empty, to be filled during execution). The mutations are deliberately small, targeted code edits — flip an operator, drop a guard, swap two variables — applied one at a time, with `pytest tests/` run between each. If a mutation is **not caught** by any test, add a test that catches it before reverting.

  **Mutation list** (apply each, run tests, revert):

  | # | File | Location | Mutation | Expected to catch |
  |---|------|----------|----------|-------------------|
  | M1 | `manager.py:429` | `min(0, d.batteryOutput.asInt + d.homeInput.asInt - d.batteryInput.asInt - d.homeOutput.asInt)` | Remove the `min(0, ...)` clamp | `test_pwr_produced_clamped_at_zero` |
  | M2 | `manager.py:466-467` | `if self.discharge_bypass > 0:` clamp | Delete the entire `if` block (so the clamp never runs, even when bypass is positive) | `test_discharge_bypass_clamps_setpoint_when_p1_nonneg` |
  | M3 | `manager.py:528` | `sorted(self.charge, key=lambda d: d.electricLevel.asInt, reverse=True)` | Change `reverse=True` to `reverse=False` | `test_charge_two_devices_weighted_by_soc` |
  | M4 | `manager.py:535-539` | `if self.charge_weight != 0:` divide-by-zero guard | Remove the `if`, always use the division | `test_charge_weight_zero_no_division_error` |
  | M5 | `manager.py:589` | `sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=False)` | Change `reverse=False` to `reverse=True` | `test_discharge_two_devices_weighted_by_soc` |
  | M6 | `manager.py:604-605` | `if pwr < -d.pwr_produced and d.state == DeviceState.SOCFULL:` SOCFULL guard | Drop the `and d.state == DeviceState.SOCFULL` | `test_discharge_socfull_passes_through_solar_only` |
  | M7 | `manager.py:478` | `MATCHING_DISCHARGE: await self.power_discharge(max(0, setpoint))` | Change `max(0, setpoint)` to plain `setpoint` | `test_mode_matching_discharge_clamps_at_zero` |
  | M8 | `manager.py:473` | `if setpoint < 0:` boundary check in MATCHING dispatch | Change `< 0` to `<= 0` | `test_mode_matching_zero_setpoint_calls_discharge_zero` |
  | M9 | `manager.py:485` | `if setpoint > 0 and self.produced > SmartMode.POWER_START and self.operation == ManagerMode.MATCHING_CHARGE` | Drop the `self.operation == ManagerMode.MATCHING_CHARGE` check | `test_mode_store_solar_does_not_discharge_produced` |
  | M10 | `manager.py:196-199` | `case "unused": if self.operation != ManagerMode.OFF: await device.power_off()` | Drop the `if` guard | `test_unused_fusegroup_skips_power_off_when_off_mode` |
  | M11 | `manager.py:244` | `fg.maxpower >= sum(d.discharge_limit for d in fg.devices)` | Flip `>=` to `<` | `test_split_when_combined_limits_fit_individual` + `test_no_split_when_combined_limits_exceed` |
  | M12 | `fusegroup.py:37-38` | `if fd.homeInput.asInt > 0:` guard in charge_limit | Change `> 0` to `>= 0` | `test_multi_device_skips_zero_homeinput_in_charge` |
  | M13 | `fusegroup.py:30` | `d.pwr_max = max(self.minpower, d.charge_limit)` single-device case | Change `max` to `min` | `test_single_device_charge_caps_at_minpower` |
  | M14 | `__init__.py:25` | `hass.config_entries.async_update_entry(entry, version=1, minor_version=5)` | Change literal `5` to `4` | `test_migrate_entry_at_minor_5_unchanged` |
  | M15 | `migration.py:135-143` | The `entity_registry.async_update_entity(...)` call | Skip the call entirely | `test_migration_renames_stale_entity` |

- [ ] Execute each mutation in turn (one at a time, revert after):
  1. Apply the mutation via `git stash`-able edit.
  2. Run `pytest tests/ -q`.
  3. If any test fails → record the failing test name in the table, **caught: yes**. Revert.
  4. If all tests pass → mutation **escaped**. Record **caught: no**. Revert. Write a new test that *does* catch the mutation. Re-run mutation; confirm new test fails. Revert mutation, keep new test.
- [ ] Update `tests/MUTATION_FUZZ.md` with the filled-in caught/escaped column and (if any) the IDs of new tests added during fuzz.
- [ ] At the end of the phase, every row should read **caught: yes**. If two consecutive mutations escape, pause and reconsider the test scenarios — there's likely a category missing.

**Automated Verification**:
- [ ] No new automated check beyond Phase 5's `pytest tests/` (still passes).
- [ ] `scripts/lint` clean (any newly-added tests also lint-clean).

**Manual Verification**:
- [ ] **Mutation log is complete and every mutation is caught**:
   1. Open `tests/MUTATION_FUZZ.md`.
   2. Confirm every row in the table has **caught: yes** and a failing-test reference.
   3. Confirm `git log tests/` shows commits for any new tests added during fuzz, with messages referencing the mutation IDs.
- [ ] **Run the suite one final time after all mutations reverted**:
   1. Run `scripts/test`.
   2. Confirm exit 0 and the full pass count.
   3. Confirm no leftover scratch files (`simulation.csv`, `.pytest_cache/` is fine).

---

## References

- 2A multi-P1 plan being protected: [docs/agents/plans/2026-05-14-multiple-p1-sensors.md](2026-05-14-multiple-p1-sensors.md)
- Control loop being pinned: `custom_components/zendure_ha/manager.py:360-630` (`_p1_changed`, `powerChanged`, `power_charge`, `power_discharge`)
- FuseGroup math: `custom_components/zendure_ha/fusegroup.py`
- Migration entry point: `custom_components/zendure_ha/__init__.py:20-26`
- Migration logic: `custom_components/zendure_ha/migration.py:84-194`
- Config flow: `custom_components/zendure_ha/config_flow.py`
- Existing CI workflows (siblings to new `tests.yaml`): `.github/workflows/validate.yaml`, `.github/workflows/hassfest.yaml`
- `pytest-homeassistant-custom-component` README: [github.com/MatthewFlamm/pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
- Issue #1151 (the `discharge_bypass` clamp this suite pins): referenced in `manager.py:461-467`
