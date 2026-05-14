# Mutation-Fuzz Protocol

The "did the safety net actually catch regressions?" gate before the 2A
multi-P1 refactor lands. This document is both the protocol and the result
log — each mutation row records whether the test suite caught it.

## Method

For each mutation in the table below:

1. Apply the edit to the target file.
2. Run `.venv/bin/python -m pytest tests/ -q`.
3. **If any test fails** → the suite caught it. Record `yes` and the
   first failing test's name. Revert the edit.
4. **If all tests pass** → the mutation escaped. Record `no`. Revert.
   Either write a new test that catches the mutation (re-apply mutation
   to confirm the new test fails, then revert mutation and keep the test),
   or — if the mutation is genuinely behaviorally silent at observable
   surfaces — document that finding instead.

If two consecutive mutations escape, pause and reconsider the test scenarios.

## Run 1 — 2026-05-14

Run on commit at top of branch, before 2A. Baseline 61 tests; ended with 62
(+1 new test catching M6).

| # | File:line | Mutation | Caught | Caught by | New test |
|---|-----------|----------|--------|-----------|----------|
| M1 | `manager.py:429` | Remove `min(0, ...)` clamp on `d.pwr_produced` | yes | `test_pwr_produced_clamped_at_zero` | |
| M2 | `manager.py:466` | Delete the `if self.discharge_bypass > 0:` block | yes | `test_discharge_socfull_passes_through_solar_only` ¹ | |
| M3 | `manager.py:528` | Flip charge sort `reverse=True` → `False` | **silent²** | — | — |
| M4 | `manager.py:535` | Remove `if self.charge_weight != 0:` guard | yes | `test_charge_weight_zero_no_division_error` | |
| M5 | `manager.py:589` | Flip discharge sort `reverse=False` → `True` | **silent²** | — | — |
| M6 | `manager.py:604` | Drop `and d.state == DeviceState.SOCFULL` | yes ³ | `test_discharge_active_does_not_bump_pwr_to_solar_produced` | ✓ added |
| M7 | `manager.py:480` | `max(0, setpoint)` → `setpoint` (MATCHING_DISCHARGE) | yes ³ | `test_mode_matching_discharge_clamps_at_zero` (tightened) | ✓ tightened |
| M8 | `manager.py:473` | `if setpoint < 0:` → `<= 0:` (MATCHING) | yes ³ | `test_mode_matching_zero_setpoint_calls_discharge_zero` (tightened) | ✓ tightened |
| M9 | `manager.py:485` | Drop `self.operation == ManagerMode.MATCHING_CHARGE` | yes | `test_mode_store_solar_does_not_discharge_produced` | |
| M10 | `manager.py:197` | Drop `if self.operation != ManagerMode.OFF:` guard | yes | `test_unused_fusegroup_skips_power_off_when_off_mode` | |
| M11 | `manager.py:244` | Flip `fg.maxpower >= sum(...)` to `<` | yes | `test_part_of_x_share_join_produces_shared_group` + `test_split_when_combined_limits_fit_individual` | |
| M12 | `fusegroup.py:35` | `if fd.homeInput.asInt > 0:` → `>= 0` | yes | `test_multi_device_skips_zero_homeinput_in_charge` + `test_multi_device_all_zero_homeinput_charge` | |
| M13 | `fusegroup.py:30` | `max(self.minpower, d.charge_limit)` → `min` | yes | `test_single_device_charge_caps_at_minpower` (and 5 others) | |
| M14 | `__init__.py:25` | Change `minor_version=5` → `4` | yes | `test_migrate_entry_at_minor_5_unchanged` (and 2 others) | |
| M15 | `migration.py:138` | Skip the `entity_registry.async_update_entity(...)` call | yes | `test_migration_renames_stale_entity` | |

**Final pass count after Run 1**: 62 tests (61 baseline + 1 new test
`test_discharge_active_does_not_bump_pwr_to_solar_produced`). M7 and M8 were
*tightened*, not added.

### Footnotes

¹ M2 was caught by a different test than the one the plan predicted
(`test_discharge_bypass_clamps_setpoint_when_p1_nonneg`). Both pin the same
end-to-end SOCFULL discharge path; the bypass clamp's behavioral effect
manifests one step downstream. Caught regardless.

² **M3 and M5 are behaviorally silent at observable function-call surfaces.**
The weighted-distribution math is order-invariant: each device's share is
`setpoint * device_weight / total_weight`, which gives the same value
regardless of iteration order. The first-device hysteresis branch
(`manager.py:549-551` and `manager.py:615-617`) is unreachable in
single-tick tests because `pwr_low` is non-negative and `charge_optimal` /
`discharge_optimal` thresholds aren't crossable from a cold start. Adding a
test would require either a multi-tick scenario with manufactured
`pwr_low` state, or exact-value pinning that would be fragile under any
refactor that changes truncation rounding. Marked as silent rather than
catchable. **Implication for 2A**: the sort-direction choice is essentially
cosmetic at the per-tick API surface, but may matter for hysteresis across
ticks once the 2A refactor lands. Worth a manual review of the
hysteresis branches in that PR.

³ M6, M7, and M8 escaped on first attempt because the existing tests
asserted on a too-loose property (presence/absence of a method call,
operationstate value). Tightened or new tests now pin the specific
*value* dispatched to the device, which is what the mutation actually
changes.

## Notes

- Locations are pinned to current `master` (commit at top of branch). If
  any of these line numbers drift, update the table when you re-run.
- Apply one mutation at a time. Don't stack — interactions can mask catches.
- Use `git diff` to confirm the revert is clean before the next mutation.
- After Run 1: 62 tests pass on baseline. 13/15 mutations caught; 2
  behaviorally silent (M3, M5).
