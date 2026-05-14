"""Tests for the manager-level aggregate sensors (power, availableKwh).

These sensors are GLOBAL — `power` sums per-device contributions across the
whole device set, and `availableKwh` sums `actualKwh` per device. The 2A
refactor wants to preserve these as global sums (not per-group), so any
regression in the aggregation formula would silently shift visible energy
totals. Pinned here so 2A's per-group re-aggregation has a contract to meet.

The formulas at manager.py:454-459 are:
    availableKwh += d.actualKwh                          (every device)
    power += d.pwr_offgrid + home + d.pwr_produced       (every device)

where `home` is the partition outcome:
    charge device:    home = -homeInput + max(0, pwr_offgrid)
    discharge device: home = homeOutput
    idle device:      home = homeOutput  (= 0 in the idle case)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.zendure_ha.const import ManagerMode
from custom_components.zendure_ha.fusegroup import FuseGroup
from tests.fakes import FakeDevice, FakeSensorValue, build_test_manager


def _device(
    name: str,
    *,
    home_input: int = 0,
    home_output: int = 0,
    battery_input: int = 0,
    battery_output: int = 0,
    pwr_offgrid: int = 0,
    available_kwh: float = 0.0,
    electric_level: int = 50,
) -> FakeDevice:
    return FakeDevice(
        name=name,
        deviceId=name,
        electricLevel=FakeSensorValue(electric_level),
        homeInput=FakeSensorValue(home_input),
        homeOutput=FakeSensorValue(home_output),
        batteryInput=FakeSensorValue(battery_input),
        batteryOutput=FakeSensorValue(battery_output),
        availableKwh=FakeSensorValue(available_kwh),
        pwr_offgrid_value=pwr_offgrid,
    )


def _attach_fg(devices: list[FakeDevice]) -> FuseGroup:
    return FuseGroup("group", maxpower=2400, minpower=-2400, devices=devices)


async def _drive(mgr: object, p1: int) -> None:
    # Reset per-cycle scratch (matches _p1_changed setup).
    mgr.charge = []
    mgr.charge_limit = 0
    mgr.charge_optimal = 0
    mgr.charge_weight = 0
    mgr.discharge = []
    mgr.discharge_bypass = 0
    mgr.discharge_limit = 0
    mgr.discharge_optimal = 0
    mgr.discharge_produced = 0
    mgr.discharge_weight = 0
    mgr.idle = []
    mgr.idle_lvlmax = 0
    mgr.idle_lvlmin = 100
    mgr.produced = 0
    for fg in mgr.fuseGroups:
        fg.initPower = True
    await mgr.powerChanged(p1=p1, isFast=False, time=datetime.now())


async def test_power_sums_three_device_contributions() -> None:
    """power = sum over devices of (pwr_offgrid + home + pwr_produced).

    Setup:
        charge device   A: home_input=200, battery_input=300, pwr_offgrid=0
            pwr_produced = min(0, 0+200-300-0) = -100
            home = -200 + 0 = -200
            contribution = 0 + (-200) + (-100) = -300
        discharge device B: home_output=200, battery_output=350, battery_input=200
            pwr_produced = min(0, 350+0-200-200) = -50
            home = 200
            contribution = 0 + 200 + (-50) = 150
        idle device     C: no flows
            pwr_produced = 0, home = 0
            contribution = 0
        total: -300 + 150 + 0 = -150
    """
    a = _device("A", home_input=200, battery_input=300, electric_level=50)
    b = _device(
        "B", home_output=200, battery_output=350, battery_input=200, electric_level=80
    )
    c = _device("C", electric_level=40)
    fg = _attach_fg([a, b, c])
    mgr = build_test_manager([a, b, c], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=0)

    assert mgr.power.value == -150, (
        f"power should be sum of per-device contributions; got {mgr.power.value}"
    )


async def test_availablekwh_sums_actualkwh_across_devices() -> None:
    """availableKwh = sum of device.actualKwh after power_get.

    power_get sets `actualKwh = availableKwh.asNumber`, so the manager's
    availableKwh is just the sum of each device's availableKwh sensor.
    """
    a = _device("A", home_input=200, battery_input=300, available_kwh=2.5)
    b = _device(
        "B", home_output=200, battery_output=350, battery_input=200, available_kwh=3.5
    )
    c = _device("C", available_kwh=1.0)
    fg = _attach_fg([a, b, c])
    mgr = build_test_manager([a, b, c], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=0)

    assert mgr.availableKwh.value == pytest.approx(7.0)


async def test_offline_device_does_not_contribute_to_aggregates() -> None:
    """A device with power_get_result=False is skipped entirely.

    Its availableKwh and power contributions must NOT be added.
    """
    online = _device("on", home_input=200, battery_input=300, available_kwh=2.0)
    offline = _device("off", home_input=200, battery_input=300, available_kwh=99.0)
    offline.power_get_result = False
    fg = _attach_fg([online, offline])
    mgr = build_test_manager(
        [online, offline], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )

    await _drive(mgr, p1=0)

    # Only online device's actualKwh contributes; offline's 99.0 is excluded.
    assert mgr.availableKwh.value == pytest.approx(2.0)
    # power contribution from online only: -300; if the offline device leaked
    # into the aggregation, the total would have been -600.
    assert mgr.power.value == -300


async def test_pwr_offgrid_contributes_to_power_aggregate() -> None:
    """pwr_offgrid (positive or negative) is summed into power directly.

    A device's pwr_offgrid component is independent of the partition branch.
    Verifies the `power += d.pwr_offgrid + home + d.pwr_produced` formula
    accounts for offgrid even on idle devices where home == 0.
    """
    d = _device("A", pwr_offgrid=150)  # idle (no home_input, no home_output)
    fg = _attach_fg([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=0)

    # Idle device: home=0, pwr_produced=0, contribution = pwr_offgrid + 0 + 0 = 150.
    assert mgr.power.value == 150


pytestmark = pytest.mark.asyncio
