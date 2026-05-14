---
date: 2026-05-14T14:13:19+00:00
git_commit: a2611fb0d12f11f41b65b7e2f741c45acf10043d
branch: master
topic: "Support multiple P1 power sensors (Option 2A from issue #1358)"
tags: [plan, manager, config-flow, migration, devices]
status: draft
---

# Multiple P1 Power Sensors — Implementation Plan

## Overview

Allow a single `ZendureManager` instance to drive devices on multiple separate electrical circuits, each measured by its own P1 / grid-power sensor. Devices choose their P1 source via a new per-device select entity. The manager's per-cycle control state is extracted into a `P1Group` dataclass and replicated per configured P1 sensor; the control loop runs independently per group. Existing installations migrate without user action — a single-P1 setup remains behaviorally identical to today.

Tracking: GitHub issue [Zendure/Zendure-HA#1358](https://github.com/Zendure/Zendure-HA/issues/1358), scope decision = "Option 2A".

## Current State Analysis

- `CONF_P1METER` is a single entity id (`const.py:9`).
- `ZendureManager.update_p1meter` subscribes one entity via `async_track_state_change_event` (`manager.py:306`). The callback `_p1_changed` (`manager.py:360`) debounces with `p1_history`, `zero_next`, `zero_fast`, then resets a large set of per-cycle scratch fields on the manager and calls `powerChanged`.
- `powerChanged` (`manager.py:420`) iterates `self.devices`, partitions into `charge / discharge / idle` lists, computes a setpoint across the whole device set, and dispatches to `power_charge` / `power_discharge`.
- `update_fusegroups` (`manager.py:168`) works in three passes:
   1. **Build** (lines 176-210): for each device, instantiate a fresh `FuseGroup` named after the device, using power limits from the device's `fuseGroup` select profile (`group800` → 800/-1200, etc.). Append the device to its own group. Index this transient dict by `device.deviceId` (string).
   2. **Rewrite the per-device `fuseGroup` select options** (lines 212-232): each device's select gets all *other* devices' deviceIds added as `"Part of <other name> fusegroup"` entries. This is how a user expresses "device B shares a circuit with device A" — by selecting that option on device B.
   3. **Apply selection + split** (lines 234-250): for each device, look up the transient dict by `device.fuseGroup.value` — which is an int key like `2` for `group800` *or* a deviceId string like `"abc123"` if the user picked "Part of X". If the lookup matches another device's group (the "Part of X" case), the current device is added to that shared group. The split heuristic at lines 241-250 then checks if a shared group's combined limit fits each device's individual limit and, if so, splits it back into one-device groups.
- So in practice, a single-device install always ends up with one FuseGroup per device. The shared/multi-device path is only reached when a user explicitly selects "Part of X" on another device's group.
- There is also an obvious dead-code-ish quirk in the build pass: `fg.devices.append(device)` runs at line 205 *and* again at line 238 for any device whose lookup matches its own group. For the predefined-int-key options (`group800` etc.), `fuseGroups.get(int_key)` never matches the string-keyed dict, so line 238 is a no-op there. For the "Part of X" string-keyed options, line 238 *does* fire and adds the device to the shared group exactly once (the original line-205 append put it in its *own* group, which then becomes orphan'd because no one looks it up — that orphan group is what falls out of the dict iteration at line 243 and gets `self.fuseGroups.append(fg)`'d at line 250). Net: when "Part of X" is used, the current device appears in *two* FuseGroups (its own orphan, and X's shared one). This is pre-existing behavior; the plan preserves it.
- Operation mode (`self.operation`), `manualpower`, and aggregate sensors (`self.power`, `self.availableKwh`, `self.operationstate`) live on the manager.
- `ZendureDevice.create_entities` (`device.py:131`) currently creates the `fuseGroup` select but no `p1Source` select.
- `ZendureRestoreSelect` (`select.py:96`) restores the last-known string option from HA state in `async_added_to_hass` (line 103-116). `setDict` (line 42) silently falls back to index 0 if the restored option is missing from the current option list. `current_option` returns the option *value* string (e.g. the entity id), while `value` returns the option *key* (e.g. an int).
- `async_migrate_entry` (`__init__.py:20`) currently migrates entries below minor version 5; `MINOR_VERSION = 7` in `config_flow.py:39`.
- `__init__.py` call sites that need updating: `update_listener` calls `manager.update_p1meter(...)` at line 45; `async_unload_entry` calls `manager.update_p1meter(None)` at line 62.

Cross-cycle hysteresis state I noticed during analysis: `self.charge_time`, `self.charge_last`, `self.pwr_low` (`manager.py:75-76, 90`) are reset deliberately *across* `_p1_changed` calls (in `power_charge` and `power_discharge`). These must move onto `P1Group` too — leaving them on the manager would let hysteresis on group A bleed into group B.

## Desired End State

- Config entry stores `CONF_P1METERS: list[str]` (one or more entities); `CONF_P1METER` is gone after migration.
- Each `ZendureDevice` has a `p1Source` `ZendureRestoreSelect` entity whose options are the configured P1 entity ids. The default is the first entry of the list.
- `ZendureManager` holds `self.p1Groups: list[P1Group]` and dispatches each P1 state-change event to the matching group's control loop. Each group has its own debounce timing, history, hysteresis, and per-cycle scratch state.
- `update_fusegroups` respects per-device P1 assignments: when device B has picked "Part of A's fusegroup", the share-join only happens if A and B point at the same P1 sensor; otherwise B falls back to its own group. Devices nominally in the same profile (`group800` etc.) but on different P1s are never balanced together.
- Operation mode stays global. In MANUAL mode, each group independently targets the (identical) `manualpower` value.
- `operationstate` becomes per-group: the first P1 in the configured list keeps the legacy `sensor.zendure_manager_operation_state` entity; additional groups get sibling `operation_state_<slug>` sensors.
- Aggregate `self.power` and `self.availableKwh` continue to aggregate across all devices/groups.
- `writeSimulation` writes one CSV per group: `simulation_<slug>.csv`.
- Single-P1 installs (the migrated and the default-new case) behave identically to today.

## What We're NOT Doing

- No changes to `Api` singletons, MQTT clients, the per-device cloud bridge, or `single_config_entry: true`. (That's Option 1 from the issue, scoped out separately.)
- No per-group `manualpower` entity. MANUAL mode applies the same global value to every group.
- No per-group `power` / `availableKwh` sensors. Only `operationstate` becomes per-group; the energy/power aggregates stay global.
- No UI for editing `p1Source` from the config flow — only via the device's select entity.
- No user-facing error when fuse-group / P1-source assignments are inconsistent. The partitioning silently does the right thing.
- No support for empty P1 list. Config flow requires at least one entry.
- No user-defined named fuse groups (issue's Option 2.5 / Option 2B).

## UI Mockups

### Config flow — initial setup step

Before:
```
┌────────────────────────────────────────────────┐
│ Zendure Token            [____________________]│
│ P1 Sensor for smart matching                   │
│                          [sensor.power_actual ]│  ← single EntitySelector
│ Log MQTT communication   [ ]                   │
│ Use local MQTT           [ ]                   │
│                                  [Submit]      │
└────────────────────────────────────────────────┘
```
After:
```
┌────────────────────────────────────────────────┐
│ Zendure Token            [____________________]│
│ P1 Sensors for smart matching                  │
│   • sensor.power_actual               [Remove] │  ← multi EntitySelector
│   • sensor.power_balcony_2            [Remove] │
│                                       [+ Add]  │
│ Log MQTT communication   [ ]                   │
│ Use local MQTT           [ ]                   │
│                                  [Submit]      │
└────────────────────────────────────────────────┘
```

### Per-device select entity

```
Device: Hyper 2000 12345
   Device Fuse Group        [FuseGroup max. 800w / 1200w charge ▾]
   P1 Source                [sensor.power_actual                ▾]   ← NEW
   Connection Mode          [Local                              ▾]
   ...
```

### Zendure Manager device — operation_state entities

Single-P1 install (today and post-migration):
```
Zendure Manager
   Operation State          Charging
   Operation Mode           [Smart Matching ▾]
   Manual Power             0 W
   ...
```

Multi-P1 install (two P1 sensors configured):
```
Zendure Manager
   Operation State                                   Charging      ← legacy entity, mapped to first P1
   Operation State (sensor.power_balcony_2)          Idle          ← new sibling sensor
   Operation Mode                                    [Smart Matching ▾]
   Manual Power                                      0 W
   ...
```

## Architecture and Code Reuse

The refactor is mostly internal to `manager.py`. The control loop today already partitions devices into `charge / discharge / idle` lists per cycle — that partitioning logic is unchanged, it just runs N times against pre-filtered device subsets. The biggest mechanical change is moving the per-cycle and cross-cycle state from `self` to a `P1Group` instance.

```
ZendureManager
  ├── operation (ManagerMode)              ← stays global
  ├── manualpower (NumberEntity)           ← stays global
  ├── power, availableKwh, totalKwh        ← stay global; aggregated from groups
  ├── devices: list[ZendureDevice]         ← stays
  ├── fuseGroups: list[FuseGroup]          ← stays (now built per (fuseGroup, p1Source))
  └── p1Groups: list[P1Group]              ← NEW
        ├── sensor: str                    (HA entity id)
        ├── slug: str                      (sanitized for filenames / entity_ids)
        ├── operationstate                 (per-group sensor; first group reuses legacy unique_id)
        ├── unsubscribe: Callable          (from async_track_state_change_event)
        ├── devices: list[ZendureDevice]   (subset assigned to this P1)
        ├── fuseGroups: list[FuseGroup]    (subset that lives on this P1)
        ├── p1_history, p1_factor          (debounce state)
        ├── zero_next, zero_fast           (cycle gating)
        ├── charge_time, charge_last, pwr_low   (cross-cycle hysteresis)
        └── per-cycle scratch              (reset at the start of each cycle)
              charge[], charge_limit, charge_optimal, charge_weight,
              discharge[], discharge_bypass, discharge_produced,
              discharge_limit, discharge_optimal, discharge_weight,
              idle[], idle_lvlmax, idle_lvlmin, produced
```

Reuse: `ZendureRestoreSelect` for the per-device `p1Source` entity. Its existing `setDict` fallback (`select.py:42-47`) covers the "stale restored value" case for free; we add a log line when it kicks in. The existing `entity.snakecase` helper handles the entity-id sluggification.

### File-level change summary

- `custom_components/zendure_ha/const.py`
  - Add `CONF_P1METERS = "p1meters"`. Keep `CONF_P1METER = "p1meter"` exported (referenced once during migration).
- `custom_components/zendure_ha/config_flow.py`
  - `data_schema` / `mqtt_schema` / `async_step_user` / `async_step_local` / `async_step_reconfigure` / `ZendureOptionsFlowHandler`: switch `CONF_P1METER` (string) to `CONF_P1METERS` (list, `EntitySelector(multiple=True)`).
  - Bump `MINOR_VERSION = 8`.
- `custom_components/zendure_ha/__init__.py`
  - Extend `async_migrate_entry` to migrate minor_version <8 → 8 by transforming `entry.data[CONF_P1METER]` (string) into `entry.data[CONF_P1METERS]` (list). Wraps the legacy <5 migration path so both run when needed.
  - `update_listener`: read `CONF_P1METERS` (list) and call `manager.update_p1meters(list)`.
- `custom_components/zendure_ha/manager.py`
  - Replace `update_p1meter(p1meter: str|None)` with `update_p1meters(p1meters: list[str])`. Subscribes one tracker per entity.
  - Extract `P1Group` dataclass (in `manager.py` to avoid a new file; the symbols are tightly coupled). Move per-cycle scratch + cross-cycle hysteresis off `ZendureManager`.
  - Rewrite `_p1_changed` as a per-group method dispatched from a manager-level event handler.
  - Rewrite `powerChanged` / `power_charge` / `power_discharge` as `P1Group` methods. Operation mode and manualpower come from the manager.
  - Change `update_fusegroups` partition key from `device.fuseGroup.state` to `(device.fuseGroup.state, device.p1Source.current_option)`.
  - Re-aggregate `self.power` and `self.availableKwh` after each group's cycle (sum across groups).
  - `writeSimulation`: take a `P1Group`, write to `simulation_<slug>.csv`.
- `custom_components/zendure_ha/device.py`
  - `ZendureDevice.create_entities` (line 131): create `self.p1Source = ZendureRestoreSelect(self, "p1Source", {"unknown": "unknown"}, None)`. Options are populated by the manager once it knows the configured list.
- `custom_components/zendure_ha/translations/{de,en,fr,nl}.json`
  - Rename `p1meter` to `p1meters` under `config.step.*.data` and `options.step.init.data`.
  - Add `entity.select.p1_source.name` ("P1 Source" + translations).

`fusegroup.py` is unchanged. `Api`, the device class hierarchy, and `single_config_entry` are unchanged.

## Known Quirks & Non-Risks

- **Empty `CONF_P1METERS` list at runtime.** Config flow requires `≥1`, but `EntitySelector(multiple=True)` does not hard-reject empty selections in HA's UI in all versions. If `p1meters == []` reaches `update_p1meters`, the for-loop is skipped, `self.p1Groups = []`, and `update_operation`'s `any(g.unsubscribe ...)` gate evaluates `False` — so non-OFF modes silently no-op. This matches the existing "no p1meter" path (`manager.py:315-316`). Acceptable.
- **`p1_factor` snapshot at subscription time.** Each P1Group reads `unit_of_measurement` once when subscribing. If the source sensor's unit attribute is missing at startup (e.g. its providing integration hasn't initialized yet), the factor falls back to `1` even when the sensor will later report kW. Same bug exists today (`manager.py:313-314`). Not a regression; not fixed by this plan.
- **Dead lookup at `manager.py:236-238` is preserved.** The `fuseGroups.get(device.fuseGroup.value)` call uses an int key against a string-keyed dict for the predefined `group800` etc. options (never matches), and a deviceId-string key for "Part of X" options (does match). The proposed P1-source check at step (1) of the `update_fusegroups` rewrite slots into this existing logic without changing the int-key-never-matches behavior.
- **First-device `pwr_low` hysteresis branches are silent-mutation territory.** Mutation-fuzz Run 1 (see `tests/MUTATION_FUZZ.md`, M3/M5) demonstrated that the sort-direction flip in `power_charge` / `power_discharge` produces no observable per-cycle behavior change in the current test suite — the weighted-distribution math is order-invariant. The first-device hysteresis branches at `manager.py:549-551` (charge) and `manager.py:615-617` (discharge) are the only place iteration order matters, and they're effectively dead code in single-tick scenarios: `pwr_low` is non-negative, `charge_optimal` is negative (so `pwr_low < charge_optimal` is unreachable on the charge side); `pwr_low > discharge_optimal` IS reachable on the discharge side but only after `pwr_low` accumulates across several cycles where the first device gets `pwr < discharge_start*1.5`. **If 2A simplifies away or restructures the `pwr_low` branches during the P1Group extraction, call it out explicitly in the commit message** — the existing test suite will not catch the behavior change. If the branches stay structurally identical, no extra coverage is needed.

## Performance Considerations

- N P1 subscriptions instead of 1. Each is a thin `async_track_state_change_event` listener; N is small (typically 1-3). Negligible.
- The control loop runs once per group per P1 update. Each group only iterates *its* device subset, so total work across groups equals today's work for the same set of devices. No regression.
- `update_fusegroups` runs once per device-select change, and now partitions by a 2-tuple instead of a 1-tuple. O(devices). Negligible.

## Migration Notes

- Zero user action required. `async_migrate_entry` detects `minor_version < 8`, reads `entry.data["p1meter"]`, and rewrites to `entry.data["p1meters"] = [<that_value>]`. The original key is removed.
- The migrated single-element list becomes the only option in each device's new `p1Source` select; `ZendureRestoreSelect`'s default behavior is to pick option index 0 when no prior restore state exists, so all devices land on the legacy P1 automatically.
- `operation_state` sensor: the first P1 in the migrated list takes the legacy `sensor.zendure_manager_operation_state` entity (same unique_id), preserving existing automations/dashboards. Additional groups only show up if the user adds more P1s later.
- The < 5 migration in `__init__.py:22-24` is left untouched; both migration steps run in sequence for very old entries.

---

## Phase 1: Config schema & migration

Lay down the new `CONF_P1METERS` list and ensure existing entries migrate cleanly. Nothing user-facing in the control loop yet — the manager still reads only the first P1 entity from the list.

**Tasks**:
- [ ] Add `CONF_P1METERS = "p1meters"` to `const.py`. Keep `CONF_P1METER = "p1meter"` (still used during migration).
- [ ] In `config_flow.py`, bump `MINOR_VERSION = 8`.
- [ ] In `config_flow.py`, replace the `CONF_P1METER` field in `data_schema` with:
   ```python
   vol.Required(CONF_P1METERS, default=["sensor.power_actual"]):
       selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))
   ```
   Apply the same change to the reconfigure path (`async_step_reconfigure`).
- [ ] In `ZendureOptionsFlowHandler.async_step_init`, swap `CONF_P1METER` for `CONF_P1METERS` in the options schema and default to `self.config_entry.data.get(CONF_P1METERS, ["sensor.power_actual"])`.
- [ ] In `__init__.py`, extend `async_migrate_entry` to additionally handle `minor_version < 8`:
   ```python
   if entry.version == 1 and entry.minor_version < 8:
       data = dict(entry.data)
       if CONF_P1METER in data and CONF_P1METERS not in data:
           legacy = data.pop(CONF_P1METER)
           data[CONF_P1METERS] = [legacy] if isinstance(legacy, str) and legacy else []
           hass.config_entries.async_update_entry(entry, data=data)
   hass.config_entries.async_update_entry(entry, version=1, minor_version=8)
   ```
- [ ] In `__init__.py:update_listener` (line 45 — the call site, def at line 40), read `CONF_P1METERS` (list) and call `manager.update_p1meter(p1meters[0] if p1meters else None)` (transitional — still single-string method; renamed in Phase 3).
- [ ] In `manager.py:loadDevices` (line 165), change the call to pass the first element of the configured list (transitional — full multi-P1 wiring happens in Phase 3):
   ```python
   p1meters = self.config_entry.data.get(CONF_P1METERS) or ["sensor.power_actual"]
   self.update_p1meter(p1meters[0] if p1meters else None)
   ```
- [ ] In `translations/{de,en,fr,nl}.json`, rename `p1meter` key to `p1meters` everywhere it appears (`config.step.user.data`, `config.step.reconfigure.data`, `options.step.init.data`). Update the value to e.g. "P1 Sensors for smart matching" (plural).

**Automated Verification**:
- [ ] `scripts/lint` runs clean.
- [ ] `python3 -c "import json; [json.load(open(f'custom_components/zendure_ha/translations/{l}.json')) for l in ['de','en','fr','nl']]"` succeeds.

**Manual Verification**:
- [ ] **Fresh install:** Run `scripts/develop`, add the integration, observe the multi-entity selector in the config-flow form. Submit with the default single entry; entry data shows `p1meters: ["sensor.power_actual"]`.
   1. Run `scripts/develop`.
   2. Add the Zendure integration in the HA UI.
   3. In the form, confirm the P1 field is now multi-select and the default value is `sensor.power_actual`.
   4. Submit, then check the entry's stored data via the HA "Configuration → Integrations → Zendure → Download Diagnostics" or `cat config/.storage/core.config_entries | jq '.data.entries[] | select(.domain=="zendure_ha")'`.
- [ ] **Migration from an existing install:** Start `scripts/develop` with a `config/.storage/core.config_entries` containing a minor_version=7 entry that has `"p1meter": "sensor.power_actual"`. Verify after startup the entry shows `minor_version=8`, `p1meters: ["sensor.power_actual"]`, and no `p1meter` key.
   1. Stop the dev HA, manually edit `core.config_entries` (or copy a snapshot from a real install) to set `minor_version: 7` and re-add `"p1meter": "sensor.power_actual"`.
   2. Restart `scripts/develop`.
   3. After startup, inspect the stored entry and confirm migration ran.
   4. Confirm a warning-free startup log.

---

## Phase 2: Per-device `p1Source` select entity

Add the new entity on each `ZendureDevice` and have the manager populate its options from the configured P1 list. The control loop still ignores it (consumed in Phase 3).

**Tasks**:
- [ ] In `device.py:create_entities` (line 131), add:
   ```python
   self.p1Source = ZendureRestoreSelect(self, "p1Source", {0: "unknown"}, None)
   ```
   The "unknown" placeholder gets immediately replaced by the manager at `loadDevices` time.
- [ ] In `manager.py`, add a helper `_apply_p1_options_to_devices(p1meters: list[str])` that builds the options dict, calls `setDict` on each device's `p1Source`, and logs when a previously-restored selection was dropped:
   ```python
   def _apply_p1_options_to_devices(self, p1meters: list[str]) -> None:
       options = {i: entity for i, entity in enumerate(p1meters)} or {0: "unknown"}
       for d in self.devices:
           before = d.p1Source.current_option
           d.p1Source.setDict(options)
           if before not in (None, "unknown") and before != d.p1Source.current_option:
               _LOGGER.warning("Device %s p1Source %r no longer configured, fell back to %r",
                               d.name, before, d.p1Source.current_option)
   ```
- [ ] In `manager.py:loadDevices`, after the device list is built (just before the call to `update_fusegroups()` at line 164), invoke `self._apply_p1_options_to_devices(p1meters)` so the device-side options dict is correct before the first control cycle. (In Phase 3 this call moves into `update_p1meters`; Phase 2 wires it explicitly because there's no `update_p1meters` yet.)
- [ ] **Restore-state ordering note** (no code change, documentation only): `setDict` runs synchronously from `loadDevices`, but `ZendureRestoreSelect.async_added_to_hass` (`select.py:103-116`) — which restores the persisted selection — fires *later* on the event loop. The restored value will end up overriding the `setDict` default, which is correct: as long as the restored value is one of the entries in the options dict, it sticks; otherwise `setDict`'s next invocation (triggered by the restore's `onchanged` callback or an options-flow reload) does the fall-back. The "stale value silently dropped" warning therefore only fires on the *next* `_apply_p1_options_to_devices` call after a P1 was removed, not on the initial boot. Document this in a code comment near `_apply_p1_options_to_devices` so future readers don't expect immediate diagnostics.
- [ ] In `translations/{de,en,fr,nl}.json`, add under `entity.select`:
   ```json
   "p1_source": { "name": "P1 Source" }
   ```
   (state values are dynamic, so no static state translations.)

**Automated Verification**:
- [ ] `scripts/lint` runs clean.
- [ ] JSON parses (same one-liner as Phase 1).

**Manual Verification**:
- [ ] **Single-P1 install:** P1 source select appears on every device, options list contains one entry, default selected.
   1. Run `scripts/develop` with a single-P1 config.
   2. Open a device page in HA UI.
   3. Confirm the "P1 Source" select is visible and shows the single configured entity.
   4. Restart HA. Confirm the select restores its value.
- [ ] **Multi-P1 install:** Add a second P1 in the options flow. Reload the integration. Each device's select now offers two options; default remains the previously-selected one.
- [ ] **Stale fallback:** Manually edit `.storage/core.restore_state` to set a device's `p1Source` to a non-configured value. Restart HA. Confirm the warning log appears and the select displays the first configured P1.

---

## Phase 3: Extract `P1Group`, refactor control loop

The core refactor. Move per-cycle scratch and cross-cycle hysteresis off `ZendureManager` onto a new `P1Group` dataclass; run the control loop independently per group.

**Tasks**:
- [ ] Define `P1Group` near the top of `manager.py` (after imports, before `ZendureManager`):
   ```python
   @dataclass
   class P1Group:
       manager: ZendureManager       # back-pointer for global state (operation, manualpower, hass)
       sensor: str                   # HA entity id
       slug: str                     # snakecase(sensor)
       operationstate: ZendureSensor # legacy id for first group, slugged for rest
       devices: list[ZendureDevice]  = field(default_factory=list)
       fuseGroups: list[FuseGroup]   = field(default_factory=list)
       unsubscribe: Callable[[], None] | None = None
       # debounce
       p1_history: deque[int] = field(default_factory=lambda: deque([25, -25], maxlen=8))
       p1_factor: int = 1
       zero_next: datetime = field(default_factory=lambda: datetime.min)
       zero_fast: datetime = field(default_factory=lambda: datetime.min)
       # hysteresis (cross-cycle)
       charge_time: datetime = field(default_factory=lambda: datetime.max)
       charge_last: datetime = field(default_factory=lambda: datetime.min)
       pwr_low: int = 0
       # per-cycle scratch (reset at top of each cycle)
       charge: list[ZendureDevice] = field(default_factory=list)
       charge_limit: int = 0
       charge_optimal: int = 0
       charge_weight: int = 0
       discharge: list[ZendureDevice] = field(default_factory=list)
       discharge_bypass: int = 0
       discharge_produced: int = 0
       discharge_limit: int = 0
       discharge_optimal: int = 0
       discharge_weight: int = 0
       idle: list[ZendureDevice] = field(default_factory=list)
       idle_lvlmax: int = 0
       idle_lvlmin: int = 100
       produced: int = 0
       # last-cycle aggregates (read by manager for global power/availableKwh)
       last_power: int = 0
       last_availableKwh: float = 0.0
   ```
   Methods defined on `P1Group` (bodies are direct moves from the existing manager methods, with `self.X` → group-local references and `self.operation` / `self.manualpower` / `self.power` / `self.availableKwh` → `self.manager.*`):
   - [ ] `async on_p1_changed(self, event)` — body from `manager.py:_p1_changed` (lines 360-418). The `ZendureManager.simulation` check stays as-is (it's still a global flag). `writeSimulation(...)` now takes `self` so it can pick the right CSV.
   - [ ] `async powerChanged(self, p1, isFast, time)` — body from lines 420-501. Reads `self.manager.operation` / `self.manager.manualpower` instead of `self.operation`/`self.manualpower`. Writes `self.operationstate` (group-local), not `self.manager.operationstate`. Updates `self.last_power` / `self.last_availableKwh` instead of writing global sensors directly; the manager re-aggregates after the cycle.
   - [ ] `async power_charge(self, setpoint, time)` — body from lines 503-567. Reads/writes `self.charge_time`, `self.charge_last`, `self.pwr_low`, `self.charge`, `self.discharge`, `self.idle`, `self.charge_limit`, `self.charge_weight`, etc.
   - [ ] `async power_discharge(self, setpoint)` — body from lines 569-630. Same pattern.
- [ ] In `ZendureManager.__init__`, delete the per-cycle / hysteresis fields (`self.zero_next`, `self.zero_fast`, `self.p1meterEvent`, `self.p1_history`, `self.p1_factor`, `self.charge*`, `self.discharge*`, `self.idle*`, `self.produced`, `self.pwr_low`). Replace with `self.p1Groups: list[P1Group] = []`.
- [ ] Replace `update_p1meter` with:
   ```python
   def update_p1meters(self, p1meters: list[str]) -> None:
       """Rebuild the P1 group list and (re-)subscribe to all configured P1 entities."""
       for g in self.p1Groups:
           if g.unsubscribe:
               g.unsubscribe()
       self.p1Groups = []
       for idx, sensor in enumerate(p1meters):
           slug = snakecase(sensor)
           # First group keeps the legacy operation_state entity id for backwards compat;
           # additional groups get suffixed unique ids. The legacy entity uses uniqueid
           # "operation_state", matching what ZendureSensor produced today.
           uniqueid = "operation_state" if idx == 0 else f"operation_state_{slug}"
           opstate = ZendureSensor(self, uniqueid)
           p1_factor = 1
           if (st := self.hass.states.get(sensor)) is not None and \
              st.attributes.get("unit_of_measurement", "W") in ("kW", "kilowatt", "kilowatts"):
               p1_factor = 1000
           group = P1Group(manager=self, sensor=sensor, slug=slug,
                           operationstate=opstate, p1_factor=p1_factor)
           group.unsubscribe = async_track_state_change_event(
               self.hass, [sensor], group.on_p1_changed)
           self.p1Groups.append(group)
       self._apply_p1_options_to_devices(p1meters)
   ```
   Note: `self.operationstate` (the line currently at `manager.py:108`) is deleted — it's replaced by the per-group sensor on the first group.
- [ ] Update `loadDevices` to read `CONF_P1METERS` and call `update_p1meters` instead of `update_p1meter`. Drop the `self.operationstate = ZendureSensor(...)` line at `manager.py:108`; the operation_state sensor is owned by the P1Group from now on (the first group's instance shadows the legacy entity id).
- [ ] In `update_fusegroups` (`manager.py:168`), make three targeted edits — *do not* rewrite the three-pass structure, just constrain the existing "Part of X" join to respect P1 ownership and then assign groups to P1Groups:
   1. **Constrain the "Part of X" join at lines 236-238** so it only joins if the two devices share a P1 source:
      ```python
      for device in self.devices:
          if fg := fuseGroups.get(device.fuseGroup.value):
              if any(d.p1Source.current_option != device.p1Source.current_option
                     for d in fg.devices):
                  _LOGGER.info("Device %s asked to join %s but p1Source differs, kept separate",
                               device.name, fg.name)
                  continue
              device.fuseGrp = fg
              fg.devices.append(device)
          device.setStatus()
      ```
      Net effect for the single-P1 case: behavior is byte-identical (the `any(...)` always evaluates to `False`). For multi-P1 case: cross-P1 "Part of X" requests are silently rejected and the device gets its own group from the orphan path.
   2. **Install the `p1Source.onchanged` callback** alongside the existing `fuseGroup.onchanged` guard at lines 178-179:
      ```python
      if device.fuseGroup.onchanged is None:
          device.fuseGroup.onchanged = updateFuseGroup
      if device.p1Source.onchanged is None:
          device.p1Source.onchanged = updateFuseGroup
      ```
      The same `updateFuseGroup` async callback (line 172-173) is reused — it just re-runs `update_fusegroups`. Note: `ZendureRestoreSelect.async_added_to_hass` (`select.py:112-116`) fires `onchanged` once during state restore; with the callback installed, that triggers one `update_fusegroups` per device at startup. That's redundant work but not incorrect — it converges within a couple of iterations.
   3. **After the split loop builds `self.fuseGroups`, assign each group to its P1Group**:
      ```python
      for g in self.p1Groups:
          g.devices = [d for d in self.devices
                       if d.p1Source.current_option == g.sensor]
          # A FuseGroup belongs to a P1Group iff all its devices share that P1.
          # The constraint at step (1) guarantees no FuseGroup spans P1Groups.
          g.fuseGroups = [fg for fg in self.fuseGroups
                          if fg.devices and fg.devices[0].p1Source.current_option == g.sensor]
      ```
- [ ] After each P1 group's cycle completes (end of `P1Group.powerChanged`), the manager re-aggregates globals:
   ```python
   def _refresh_globals(self) -> None:
       total_power = sum(g.last_power for g in self.p1Groups)
       total_kwh = sum(g.last_availableKwh for g in self.p1Groups)
       self.power.update_value(total_power)
       self.availableKwh.update_value(total_kwh)
   ```
   Call `self.manager._refresh_globals()` at the end of `P1Group.powerChanged`.
- [ ] In `writeSimulation`, take a `P1Group` argument and write to `Path(f"simulation_{group.slug}.csv")`. Iterate only `group.devices` for the per-device columns (the CSV header is regenerated per file on first write, so different groups will have different column sets — this is fine, just call it out in a code comment so anyone post-processing the CSVs knows). Each call from `on_p1_changed` passes `self` (the group).
- [ ] In `update_operation` (`manager.py:252`), the gating condition `if self.p1meterEvent is not None` becomes `if any(g.unsubscribe for g in self.p1Groups)`.
- [ ] In `async_unload_entry`, the `manager.update_p1meter(None)` call (`__init__.py:62`) becomes `manager.update_p1meters([])`.
- [ ] In `update_listener`, the `manager.update_p1meter(...)` call (`__init__.py:45`) becomes `manager.update_p1meters(entry.data.get(CONF_P1METERS, ["sensor.power_actual"]))`. (This supersedes the Phase 1 transitional shim.)

**Automated Verification**:
- [ ] `scripts/lint` runs clean.
- [ ] Module imports: `python3 -c "from custom_components.zendure_ha import manager"` from the repo root with `PYTHONPATH=custom_components` resolves without error (smoke test that the dataclass + method moves are syntactically and import-time correct).

**Manual Verification**:
- [ ] **Single-P1 install smoke test:** Run `scripts/develop` with a 1-element `p1meters` list. Verify HA boots without errors, `_p1_changed` is dispatched (look for `P1 ======>` log lines), Smart Matching reaches a charge/discharge setpoint within one or two cycles, and the existing `sensor.zendure_manager_operation_state` entity transitions exactly as in the pre-Phase-3 baseline. (Full end-to-end multi-P1 validation is in Phase 4.)
  1. Start `scripts/develop` against the single-P1 dev config.
  2. Drive the P1 sensor through a -300 → +300 → 0 sweep using `dev tools → states`.
  3. Confirm `P1 ======>` log lines appear with sensible setpoints and no tracebacks.

---

## Phase 4: Per-group `operation_state` sensors

Make the per-group `operationstate` sensor user-visible. This is the only entity-shape change for end users; everything else from Phase 3 is internal.

**Tasks**:
- [ ] No code changes beyond what Phase 3 already wrote (the per-group `operationstate` is created inside `update_p1meters`). This phase is the validation step.
- [ ] In `translations/{de,en,fr,nl}.json`, ensure the `entity.sensor.operation_state` key has translations covering all four ManagerState values (0=Idle, 1=Charging, 2=Discharging, 3=Off) — already exists in en.json, verify others are present.
- [ ] In `manager.py:update_p1meters`, double-check the uniqueid logic so existing 1-P1 installs land on exactly `sensor.zendure_manager_operation_state` (uniqueid = `"operation_state"`, identical to the previous manager-level sensor).
- [ ] **Display name for secondary groups' `operation_state`:** rely on the unique-id suffix alone — do not add `_attr_translation_placeholders` or a custom friendly name. HA will show identical "Operation State" labels for all per-group sensors but distinct entity ids; the entity-id suffix carries the disambiguation. Rationale: the per-group sensor's identity is already visible as `sensor.zendure_manager_operation_state_<slug>`, and adding a friendly-name suffix would require new translation keys + placeholder plumbing for marginal UX gain. (Open Question resolved.)

**Automated Verification**:
- [ ] `scripts/lint` runs clean.

**Manual Verification**:
- [ ] **Migrated single-P1 install — byte-identical behavior:** With one P1 sensor configured (migrated from a pre-existing install), confirm:
   1. `sensor.zendure_manager_operation_state` exists with the same entity id as before, on the same Zendure Manager device.
   2. Smart Matching mode: setpoint values logged at `P1 ======> p1:... setpoint:...` match a pre-refactor baseline for the same conditions (run the same dev HA against the same Zendure account before and after, compare `home-assistant.log`).
   3. Existing automations that reference `sensor.zendure_manager_operation_state` continue to fire as before.
- [ ] **Multi-P1 install — independent control loops:** Configure two P1 sensors (e.g. `sensor.power_actual` and a manually-controllable input number wrapped as a sensor template), assign a device to each.
   1. Drive sensor.power_actual to -200 (excess generation), confirm only the device assigned to it switches to CHARGE; the second device's `sensor.zendure_manager_operation_state_<slug>` stays IDLE.
   2. Drive the second P1 sensor to +400 (consumption), confirm only the second device switches to DISCHARGE.
   3. Verify two CSV files appear: `simulation_sensor_power_actual.csv` and `simulation_<other>.csv` when simulation mode is enabled.
- [ ] **Fuse-group partition correctness:** Put two devices both in `group800` but on different P1 sources. Trigger `update_fusegroups` (e.g. by switching one device's `fuseGroup` value and switching it back). Confirm the manager log shows two separate FuseGroup objects, not one shared.
- [ ] **Stale p1Source handling:** Remove a P1 sensor from the options flow that has at least one device pointing to it. After reload, confirm those devices' `p1Source` select shows the first remaining P1 and a warning is logged.

---

## References

- GitHub issue: [Zendure/Zendure-HA#1358](https://github.com/Zendure/Zendure-HA/issues/1358) — feature request with Options 1, 2, 2.5.
- Prior scoping discussion (in chat history) — Option 2A chosen over 2B because dynamic fuse-group splitting (`manager.py:241-250`) means fuse groups lack stable identity, so the P1 mapping must hang off devices instead.
- Existing control loop: `manager.py:360-630` — `_p1_changed`, `powerChanged`, `power_charge`, `power_discharge`.
- Existing migration framework: `__init__.py:20-26` and `migration.py` — pattern to extend in Phase 1.
- `ZendureRestoreSelect.setDict` (`select.py:42-49`) — provides the stale-fallback semantics consumed in Phase 2.
