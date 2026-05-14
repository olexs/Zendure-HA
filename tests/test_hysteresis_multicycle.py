"""Multi-cycle tests for the hysteresis state on ZendureManager.

Three fields survive across `_p1_changed` cycles (deliberately — they're not
reset in `_reset_cycle`):
    charge_time   — when the hysteresis window expires (datetime.max = idle)
    charge_last   — last time we entered charge mode (drives 2s vs 60s window)
    pwr_low       — accumulator for the first-device branch (dead in practice;
                    not exercised here)

2A moves these onto `P1Group` (one set per P1 group). The risk is that group
A's hysteresis state bleeds into group B's. Today they live on the manager
and the single-group case is the only one observed — so pinning the
multi-cycle progression of these fields gives 2A's refactor a contract:
after the refactor, the same sequence of cycles on ONE group must produce
the same charge_time / charge_last / dispatched-pwr transitions.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.zendure_ha.const import ManagerMode
from custom_components.zendure_ha.fusegroup import FuseGroup
from tests.fakes import FakeDevice, FakeSensorValue, build_test_manager, drive


def _charge_device(name: str, *, electric_level: int = 50) -> FakeDevice:
    return FakeDevice(
        name=name,
        deviceId=name,
        electricLevel=FakeSensorValue(electric_level),
        homeInput=FakeSensorValue(200),  # puts the device in self.charge
        charge_limit=-800,
        discharge_limit=800,
    )


def _discharge_device(name: str, *, electric_level: int = 50) -> FakeDevice:
    return FakeDevice(
        name=name,
        deviceId=name,
        electricLevel=FakeSensorValue(electric_level),
        homeOutput=FakeSensorValue(200),  # puts the device in self.discharge
        charge_limit=-800,
        discharge_limit=800,
    )


def _fg(devices: list[FakeDevice]) -> FuseGroup:
    return FuseGroup("g", maxpower=2400, minpower=-2400, devices=devices)


# Imported from tests.fakes. Aliased so this file's call sites keep their
# explicit `_drive(mgr, p1=..., time=...)` shape that documents intent.
_drive = drive


async def test_cold_start_charge_clamps_first_cycle_releases_second() -> None:
    """Cold start (charge_last=min) → 2s window. Cycle 1 clamps to 0, cycle 2 flows.

    Pins the cold-start contract:
        cycle 1 at t0:    charge_time(max)→t0+2s,  dispatch 0
        cycle 2 at t0+5s: charge_time<time, skip,  dispatch actual setpoint
    """
    d = _charge_device("A")
    fg = _fg([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    t0 = datetime(2026, 5, 14, 12, 0, 0)
    await _drive(mgr, p1=-500, time=t0)

    # Cold start sets a 2-second window (charge_last was datetime.min → huge gap).
    assert mgr.charge_time == t0 + timedelta(seconds=2)
    assert mgr.charge_last == mgr.charge_time
    # The first charge dispatch was clamped to 0.
    first = [c for c in d.calls if c[0] == "power_charge"]
    assert first and first[-1][1] == 0, (
        f"cold start should clamp first power_charge to 0; got {first}"
    )

    # Past the window — setpoint should flow.
    await _drive(mgr, p1=-500, time=t0 + timedelta(seconds=5))
    second = [c for c in d.calls if c[0] == "power_charge"]
    assert second[-1][1] != 0, (
        f"second call past hysteresis window should dispatch non-zero; got {second}"
    )


async def test_warm_window_holds_zero_then_releases() -> None:
    """Recent charge_last (<300s ago) → 60s window. Cycles 1+2 clamp, cycle 3 flows.

    Pre-seed charge_last to a recent time so the +60s arm of the conditional
    fires. This exercises the in-window iteration: setpoint=0 every call
    until the window expires.
    """
    d = _charge_device("A")
    fg = _fg([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    t0 = datetime(2026, 5, 14, 12, 0, 0)
    # Pre-seed: charge_last is 100s before t0, so the (time - charge_last) gap
    # is 100 ≤ 300, picking the 60-second window.
    mgr.charge_last = t0 - timedelta(seconds=100)

    await _drive(mgr, p1=-500, time=t0)
    assert mgr.charge_time == t0 + timedelta(seconds=60), (
        f"warm start should set a 60-second window; got {mgr.charge_time - t0}"
    )

    # Mid-window: setpoint stays at 0.
    await _drive(mgr, p1=-500, time=t0 + timedelta(seconds=30))
    midcycle_calls = [c for c in d.calls if c[0] == "power_charge"]
    assert midcycle_calls[-1][1] == 0, (
        f"mid-window cycle must still dispatch 0; got {midcycle_calls}"
    )
    # charge_time is unchanged — only the first entry to power_charge writes it.
    assert mgr.charge_time == t0 + timedelta(seconds=60)

    # Past window: setpoint flows.
    await _drive(mgr, p1=-500, time=t0 + timedelta(seconds=90))
    post_window = [c for c in d.calls if c[0] == "power_charge"]
    assert post_window[-1][1] != 0, (
        f"post-window cycle should dispatch the actual setpoint; got {post_window}"
    )


async def test_discharge_resets_charge_window() -> None:
    """Entering DISCHARGE mode resets charge_time back to datetime.max.

    Pins manager.py:575-577. After the reset, a subsequent CHARGE cycle
    re-enters the cold/warm-start logic (whichever applies based on the
    new charge_last). Without this reset, the manager could be stuck in
    hysteresis from a stale charge-mode session.
    """
    d_charge = _charge_device("C")
    d_disch = _discharge_device("D")
    fg = _fg([d_charge, d_disch])
    mgr = build_test_manager(
        [d_charge, d_disch], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )

    t0 = datetime(2026, 5, 14, 12, 0, 0)

    # Cycle 1: charge. charge_time becomes t0+2s.
    await _drive(mgr, p1=-500, time=t0)
    assert mgr.charge_time != datetime.max
    cycle1_time = mgr.charge_time

    # Cycle 2: discharge (positive p1 → discharge arm). charge_time resets to max.
    await _drive(mgr, p1=+500, time=t0 + timedelta(seconds=10))
    assert mgr.charge_time == datetime.max, (
        f"power_discharge must reset charge_time to max; got {mgr.charge_time}"
    )

    # Cycle 3: back to charge. The reset → datetime.max means the inner-if
    # at line 517 fires again. Because charge_last was set in cycle 1 to
    # cycle1_time = t0+2s, the gap (t0+20s - (t0+2s)) = 18s ≤ 300 → +60s.
    await _drive(mgr, p1=-500, time=t0 + timedelta(seconds=20))
    assert mgr.charge_time == t0 + timedelta(seconds=20) + timedelta(seconds=60), (
        f"cycle 3 should re-arm with the warm-window (60s); "
        f"got {mgr.charge_time - t0}, cycle1 was {cycle1_time - t0}"
    )


async def test_charge_last_persists_across_cycles() -> None:
    """charge_last is set on the first power_charge entry and persists.

    A multi-charge sequence shows charge_last sticks to the FIRST entry's
    timestamp until power_discharge resets the hysteresis. Subsequent
    `power_charge` calls within the window do NOT update charge_last.
    """
    d = _charge_device("A")
    fg = _fg([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    t0 = datetime(2026, 5, 14, 12, 0, 0)
    await _drive(mgr, p1=-500, time=t0)
    first_charge_last = mgr.charge_last

    # Two more cycles within the 2-second window — charge_last must NOT change.
    await _drive(mgr, p1=-500, time=t0 + timedelta(milliseconds=500))
    await _drive(mgr, p1=-500, time=t0 + timedelta(seconds=1, milliseconds=500))

    assert mgr.charge_last == first_charge_last, (
        "charge_last must remain pinned to the first entry's stamp during the window"
    )


pytestmark = pytest.mark.asyncio
