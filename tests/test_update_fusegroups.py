"""Golden-master tests for ZendureManager.update_fusegroups partition logic.

update_fusegroups runs in three passes (manager.py:168-250):
    1. Build pass: instantiate a FuseGroup per device based on its fuseGroup.state.
    2. Rewrite the fuseGroup select options to include "Part of X fusegroup" labels.
    3. Add devices to other devices' fusegroups (the "Part of X" share-join via
       string-key lookup in `fuseGroups.get(device.fuseGroup.value)`).
    4. Split: if a multi-device group's combined limits fit, split into single-
       device groups; otherwise keep shared.

These tests pin the partition behavior for the 2A refactor — none of these
should change behaviorally after 2A unless the refactor explicitly intends to.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.const import ManagerMode
from tests.fakes import FakeDevice, FakeSelect, build_test_manager


def _device(
    name: str,
    *,
    fuse_state: str = "group800",
    fuse_value: object = 2,
    charge_limit: int = -800,
    discharge_limit: int = 800,
) -> FakeDevice:
    """Build a FakeDevice configured with a fuseGroup select state."""
    return FakeDevice(
        name=name,
        deviceId=name,
        charge_limit=charge_limit,
        discharge_limit=discharge_limit,
        fuseGroup=FakeSelect(value=fuse_value, state=fuse_state),
    )


async def test_single_device_group800_creates_own_fusegroup() -> None:
    """A device with fuseGroup=group800 produces one FuseGroup at +800/-1200."""
    d = _device("A", fuse_state="group800", fuse_value=2)
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    assert len(mgr.fuseGroups) == 1
    fg = mgr.fuseGroups[0]
    assert fg.maxpower == 800
    assert fg.minpower == -1200
    # After split-or-keep loop, single-device group lands in mgr.fuseGroups
    # with the device attached.
    assert d in fg.devices
    assert d.fuseGrp is fg


async def test_unused_fusegroup_calls_power_off_when_not_off_mode() -> None:
    """fuseGroup=unused + operation != OFF → device.power_off() is called.

    The `case "unused"` arm at manager.py:196-199 calls power_off() and then
    `continue`s — so the device doesn't get a FuseGroup at all.
    """
    d = _device("A", fuse_state="unused", fuse_value=0)
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    assert ("power_off", None) in d.calls
    # Device is not in any FuseGroup (the case "unused" continues before fg is built)
    assert mgr.fuseGroups == []


async def test_unused_fusegroup_skips_power_off_when_off_mode() -> None:
    """fuseGroup=unused + operation == OFF → power_off NOT called (if-guard wins)."""
    d = _device("A", fuse_state="unused", fuse_value=0)
    mgr = build_test_manager([d], operation=ManagerMode.OFF)

    await mgr.update_fusegroups()

    assert ("power_off", None) not in d.calls
    assert mgr.fuseGroups == []


async def test_part_of_x_share_join_produces_shared_group() -> None:
    """B with fuseGroup.value=A's deviceId is added to A's group's devices list.

    But — this test also pins the pre-existing "orphan group" quirk: B's own
    group (created in the build pass) is NOT discarded. So B ends up in BOTH
    A's shared group AND its own orphan group. B.fuseGrp at the end is the
    LAST one written, which is the orphan (because of dict iteration order).

    The 2A refactor inherits this quirk explicitly (per the 2A plan's
    "Known Quirks" section); changing it without intent should fail this test.
    """
    a = _device("A", fuse_state="group800", fuse_value=2)
    b = _device(
        "B",
        fuse_state="group800",
        fuse_value="A",
        charge_limit=-400,
        discharge_limit=400,
    )
    mgr = build_test_manager([a, b], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    # B appears in A's group's devices list (the share-join worked).
    a_group = next(fg for fg in mgr.fuseGroups if fg.name == "A")
    assert b in a_group.devices

    # The orphan group (B's own, from the build pass) is also in mgr.fuseGroups.
    b_group = next(fg for fg in mgr.fuseGroups if fg.name == "B")
    assert b in b_group.devices

    # Quirk: b.fuseGrp ends up pointing at the LAST group assigned in the
    # split/keep loop iteration — the orphan group_B, not the shared group_A.
    assert b.fuseGrp is b_group


async def test_split_when_combined_limits_fit_individual() -> None:
    """Combined limits fit caps → shared group splits into single-device groups.

    The split heuristic at manager.py:244-246: if maxpower >= sum(discharge_limit)
    AND minpower <= sum(charge_limit), the shared group is replaced by N
    single-device groups in mgr.fuseGroups.

    Setup: both devices group800 (max=800, min=-1200). Two devices each at
    charge_limit=-300, discharge_limit=300. Sum: -600 and 600. minpower=-1200
    ≤ -600 ✓; maxpower=800 ≥ 600 ✓. → SPLIT.

    The orphan group (B's own from the build pass) is also kept in
    mgr.fuseGroups, so the final count is 3 (2 split + 1 orphan).
    """
    a = _device(
        "A", fuse_state="group800", fuse_value=2, charge_limit=-300, discharge_limit=300
    )
    b = _device(
        "B",
        fuse_state="group800",
        fuse_value="A",
        charge_limit=-300,
        discharge_limit=300,
    )
    mgr = build_test_manager([a, b], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    # 2 split single-device groups (A split into per-device caps) + 1 orphan (B's own)
    assert len(mgr.fuseGroups) == 3
    # Each group has exactly one device after split
    for fg in mgr.fuseGroups:
        assert len(fg.devices) == 1


async def test_no_split_when_combined_limits_exceed() -> None:
    """Combined limits exceed caps → group stays merged. Orphan group also kept.

    Setup: both group800 (max=800, min=-1200). Two devices each charge_limit=-800,
    discharge_limit=800. Sum: -1600 and 1600. maxpower=800 < 1600 (fails the
    `>=` check) → NO split. One shared group + one orphan = 2 in mgr.fuseGroups.
    """
    a = _device(
        "A", fuse_state="group800", fuse_value=2, charge_limit=-800, discharge_limit=800
    )
    b = _device(
        "B",
        fuse_state="group800",
        fuse_value="A",
        charge_limit=-800,
        discharge_limit=800,
    )
    mgr = build_test_manager([a, b], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    # Shared group (with [A, B]) + B's own orphan (with [B])
    assert len(mgr.fuseGroups) == 2

    # The shared group named "A" has both devices
    shared = next(fg for fg in mgr.fuseGroups if fg.name == "A")
    assert len(shared.devices) == 2
    assert a in shared.devices
    assert b in shared.devices

    # The orphan group "B" only has B
    orphan = next(fg for fg in mgr.fuseGroups if fg.name == "B")
    assert orphan.devices == [b]


async def test_setStatus_called_per_device() -> None:
    """update_fusegroups calls device.setStatus() once per device (manager.py:239)."""
    a = _device("A", fuse_state="group800", fuse_value=2)
    b = _device("B", fuse_state="group800", fuse_value=2)
    mgr = build_test_manager([a, b], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    assert a.setStatus_calls == 1
    assert b.setStatus_calls == 1


async def test_part_of_x_join_when_value_does_not_match_falls_through() -> None:
    """If device.fuseGroup.value doesn't match any deviceId → no fuseGrp assignment.

    Pre-existing quirk at manager.py:236-238: the lookup uses int keys (0..7)
    against a string-keyed dict (keyed by deviceId). Predefined options
    (group800 etc, ints 1-7) never match the string-keyed dict, so fuseGrp
    stays unassigned in this loop — BUT each device's OWN group from the
    build pass is already in fuseGroups[device.deviceId], and the device was
    appended to it at line 205. So the device's `fuseGrp` ends up assigned
    via the split/keep loop's `d.fuseGrp = fg` line.
    """
    d = _device("A", fuse_state="group800", fuse_value=2)
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING)

    await mgr.update_fusegroups()

    # Device ends up in its own group (single-device path).
    assert d.fuseGrp is not None
    assert d.fuseGrp.maxpower == 800
    assert d.fuseGrp.minpower == -1200


pytestmark = pytest.mark.asyncio
