# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

HACS custom integration `zendure_ha` for Home Assistant (min HA 2025.5+, Python 3.12). All integration code lives under `custom_components/zendure_ha/`. There is no test suite; CI runs HACS validation and hassfest only (`.github/workflows/`).

## Commands

- `scripts/setup` — install `requirements.txt`, fetch HACS, scaffold `config/` (run once).
- `scripts/develop` — launches a debug HA instance with this repo's `custom_components` on `PYTHONPATH` and `./config` as the HA config dir. Use this (or the VS Code launch config "Start Home Assistant") to exercise changes end-to-end — there is no unit-test command.
- `scripts/lint` — `ruff format . && ruff check . --fix`. Config in `.ruff.toml` (py312, `select = ["ALL"]` with a long ignore list, max-complexity 25).

## Project conventions

- **Never add a `Co-Authored-By: Claude …` trailer to commit messages or PRs in this repo.**
- The `version` field in `custom_components/zendure_ha/manifest.json` is rewritten by `.github/workflows/release.yml` from the GitHub release tag — don't bump it by hand in PRs.
- `config/*` and `custom_components/*` (except `zendure_ha/`) are gitignored; `config/configuration.yaml` is the dev-only HA config and may contain machine-specific tokens.

## Architecture

The integration is a `DataUpdateCoordinator` that owns a static device registry plus two MQTT clients, and per-model classes that translate Home Assistant intents into device-specific MQTT/HTTP payloads.

### Entry points (`__init__.py`)
Standard HA lifecycle. Sets up 6 platforms: binary_sensor, button, number, select, sensor, switch. `async_migrate_entry` calls `Migration.async_migrate` for entries below minor version 5; current schema is `VERSION=1, MINOR_VERSION=7` (see `config_flow.py`). `manifest.json` declares `single_config_entry: true` — only one Zendure entry is allowed.

### `Api` (`api.py`) — transport layer
Singleton-style class (most state is class-level) that owns:
- `Api.mqttCloud` — paho client to Zendure's cloud broker, authenticated via the App Token flow (`ApiHA` posts a SHA1-signed request to `{api_url}/api/ha/deviceList` using `CONF_HAKEY` as the signing secret).
- `Api.mqttLocal` — optional paho client to the user's own broker (configured via `CONF_MQTTSERVER` etc.).
- Per-device `device.zendure` clients — additional paho clients **acting as the device** (deviceId username, md5-derived password) that bridge local messages back to Zendure cloud. Local messages are republished to cloud with `"isHA": True` to suppress feedback loops; both sides filter on that flag.
- `Api.createdevice` — `lowercased productModel string → constructor` lookup. **To support a new device model, add an entry here and a class under `devices/`.**

### `ZendureManager` (`manager.py`) — coordinator
Extends `DataUpdateCoordinator[None]` and `EntityDevice`. 60-second base poll. Owns:
- `self.devices: list[ZendureDevice]` — populated in `loadDevices()` from the cloud `deviceList` response.
- `self.fuseGroups: list[FuseGroup]` — computed in `update_fusegroups()` from each device's `fuseGroup` select (`unused`, `owncircuit`, `group800`, `group800_2400`, `group1200`, `group2000`, `group2400`, `group3600`).
- P1 meter subscription — reads a user-selected `sensor.*` entity (default `sensor.power_actual`) and tracks a deque of recent values to compute stddev-gated update timing (`SmartMode.P1_STDDEV_*` in `const.py`).
- Operation mode (`ManagerMode` enum): off, manual, three smart-matching variants, store_solar.
- Optional auto-creation of HA local-only auth users (one per deviceId) for local MQTT, gated by `CONF_AUTO_MQTT_USER`.

### Device class hierarchy (`device.py`)
```
EntityDevice (entity.py)
└─ ZendureDevice           # base; defines entities, limits, aggregator sensors
   ├─ ZendureLegacy        # old devices, MQTT-only, BLE provisioning, `connection: cloud|local`
   └─ ZendureZenSdk        # newer devices; falls back to HTTP at http://{ipAddress}/properties/{read,write,report}
                           #   `connection: cloud|zenSDK`
└─ ZendureBattery          # subordinate battery pack registered as its own HA device, parented to a ZendureDevice
```
Concrete model classes live in `custom_components/zendure_ha/devices/*.py`. Each one:
1. Calls `super().__init__(...)` with the cloud-supplied `definition` dict.
2. Calls `self.setLimits(charge_W_negative, discharge_W_positive)` and sets `self.maxSolar` (also negative).
3. Overrides `charge(power)`, `discharge(power)`, `power_off()` to emit the right MQTT `deviceAutomation` payload or ZenSDK HTTP/MQTT property write.
4. `SolarFlow800Pro` is an example of a model that adds extra entities (off-grid power) on top of the base.

`pwr_max` on each device is set by `FuseGroup.charge_limit/discharge_limit` — the fuse group divides the circuit's headroom across its members weighted by SoC. Don't write to `pwr_max` from device code directly.

### Entities (`entity.py` and platform files)
- `EntityZendure` is the common base; `_attr_has_entity_name = True`, no polling.
- `EntityDevice` registers the HA `DeviceInfo` (one HA device per Zendure device or battery).
- Translation keys are derived via `snakecase()` and must have matching entries in `translations/{de,en,fr,nl}.json`.
- The "aggregator" sensors (`aggrCharge`, `aggrDischarge`, `aggrSolar`, `aggrHomeInput`, `aggrHomeOut`, `aggrSwitchCount`) are `ZendureRestoreSensor` instances that accumulate energy via `aggregate(now, value)` from inside `entityUpdate`.

### MQTT message flow
Topics use the pattern `iot/{prodkey}/{deviceId}/properties/{read,write,report}` and `…/function/invoke`. `Api.mqttMsgCloud`/`mqttMsgLocal` route incoming messages to `device.mqttMessage(topic_suffix, payload)`, which updates entities via `entityUpdate(key, value)`. When a device is heard on the local broker, the integration also instantiates a per-device cloud bridge (`device.zendure`) so the cloud account stays "online."

### Config flow (`config_flow.py`)
Required at setup: App Token (base64 of `<api_url>.<appKey>`), a P1 meter entity, two MQTT flags. If `CONF_MQTTLOCAL` is true, a second step collects local MQTT and optional WiFi credentials (the latter is used for BLE provisioning of legacy devices to the local broker). Options flow is the same form.

## When extending

- Adding a model: new class in `devices/`, register in `Api.createdevice` (lowercase model string), set limits, implement `charge`/`discharge`/`power_off`. Inherit `ZendureZenSdk` for HTTP-capable devices, `ZendureLegacy` for MQTT-only ones.
- Adding entities visible in HA: add the entity object inside `create_entities()` (or the model subclass `__init__`) and add the translation key to all four `translations/*.json` files.
- Bumping the config-entry schema: increment `MINOR_VERSION` in `config_flow.py`, extend `async_migrate_entry` in `__init__.py`, and add the rename logic to `migration.py:Migration` (it uses HA's `device_registry` / `entity_registry` / `restore_state` to rewrite stored ids).
