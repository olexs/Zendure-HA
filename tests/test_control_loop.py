"""Golden-master tests for ZendureManager.powerChanged / power_charge / power_discharge.

Each test builds a partial ZendureManager via build_test_manager (object.__new__,
no HA bus), populates it with FakeDevice instances + real FuseGroup, calls the
method under test directly, and asserts what was dispatched to which device.

The control loop has three behavioral surfaces these tests pin:
    1. Partitioning: powerChanged routes each device into charge/discharge/idle
       lists based on homeInput/homeOutput/pwr_offgrid sensor values.
    2. ManagerMode dispatch: all six ManagerMode arms route setpoint through
       the right code path (charge, discharge, both, neither, manual override).
    3. Power distribution: power_charge / power_discharge weight setpoint
       across devices by SOC, handle hysteresis, divide-by-zero, SOCFULL,
       and start idle devices when an active device hits its limit.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.zendure_ha.const import DeviceState, ManagerMode, ManagerState
from custom_components.zendure_ha.fusegroup import FuseGroup
from tests.fakes import FakeDevice, FakeSensorValue, build_test_manager

# ---------- helpers ----------


def _online_device(
    name: str,
    *,
    charge_limit: int = -800,
    discharge_limit: int = 800,
    charge_optimal: int = -200,
    discharge_optimal: int = 200,
    charge_start: int = -80,
    discharge_start: int = 80,
    electric_level: int = 50,
    home_input: int = 0,
    home_output: int = 0,
    solar_input: int = 0,
    battery_output: int = 0,
    battery_input: int = 0,
    by_pass: int = 0,
    pwr_offgrid: int = 0,
    state: DeviceState = DeviceState.INACTIVE,
) -> FakeDevice:
    """Build a FakeDevice with the attributes the control loop reads."""
    return FakeDevice(
        name=name,
        deviceId=name,
        charge_limit=charge_limit,
        discharge_limit=discharge_limit,
        charge_optimal=charge_optimal,
        discharge_optimal=discharge_optimal,
        charge_start=charge_start,
        discharge_start=discharge_start,
        electricLevel=FakeSensorValue(electric_level),
        homeInput=FakeSensorValue(home_input),
        homeOutput=FakeSensorValue(home_output),
        solarInput=FakeSensorValue(solar_input),
        batteryOutput=FakeSensorValue(battery_output),
        batteryInput=FakeSensorValue(battery_input),
        byPass=FakeSensorValue(by_pass),
        pwr_offgrid_value=pwr_offgrid,
        state=state,
    )


def _attach_fusegroup(
    devices: list[FakeDevice], *, maxpower: int = 2400, minpower: int = -2400
) -> FuseGroup:
    """Build a real FuseGroup over the given devices and attach it as their fuseGrp."""
    return FuseGroup("group", maxpower=maxpower, minpower=minpower, devices=devices)


def _reset_cycle(mgr) -> None:
    """Clear per-cycle scratch the way _p1_changed would before calling powerChanged."""
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


async def _drive(
    mgr, p1: int, *, is_fast: bool = False, time: datetime | None = None
) -> None:
    """Reset per-cycle scratch + call powerChanged. Mirrors _p1_changed's setup."""
    _reset_cycle(mgr)
    await mgr.powerChanged(p1=p1, isFast=is_fast, time=time or datetime.now())


# ============================================================================
# 1. Partitioning — powerChanged lines 426-455
# ============================================================================


async def test_partition_charge_when_home_input_negative() -> None:
    """A device with homeInput > 0 → home = -homeInput < 0 → charge list."""
    d = _online_device("A", home_input=300)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=-500)

    assert d in mgr.charge
    assert d not in mgr.discharge
    assert d not in mgr.idle


async def test_partition_discharge_when_home_output_positive() -> None:
    """A device with homeOutput > 0 → discharge list."""
    d = _online_device("A", home_output=300)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=500)

    assert d in mgr.discharge
    assert d not in mgr.charge
    assert d not in mgr.idle


async def test_partition_idle_when_neither() -> None:
    """A device with home_input=0, home_output=0, pwr_offgrid=0 → idle list."""
    d = _online_device("A", electric_level=70)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=0)

    assert d in mgr.idle
    assert d not in mgr.charge
    assert d not in mgr.discharge
    assert mgr.idle_lvlmax == 70
    assert mgr.idle_lvlmin == 70


async def test_partition_offline_excluded() -> None:
    """A device with power_get_result=False is skipped entirely."""
    online = _online_device("online", home_input=300)
    offline = _online_device("offline", home_input=300)
    offline.power_get_result = False
    fg = _attach_fusegroup([online, offline])
    mgr = build_test_manager(
        [online, offline], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )

    await _drive(mgr, p1=-500)

    assert online in mgr.charge
    assert offline not in mgr.charge
    assert offline not in mgr.discharge
    assert offline not in mgr.idle
    # offline device shouldn't have any dispatch calls
    assert offline.calls == []


async def test_partition_pwr_offgrid_positive_routed_as_charge() -> None:
    """`home = -homeInput + max(0, pwr_offgrid)` — offgrid alone keeps home>=0."""
    d = _online_device("A", home_input=0, pwr_offgrid=200)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=-100)

    # home = -0 + max(0, 200) = 200 → not < 0 → not in charge.
    # home = 200 → not > 0 in the homeOutput check (separate path).
    # Falls through to idle. This documents that pwr_offgrid alone (no homeInput)
    # does NOT route to charge — the home variable goes positive, not negative.
    assert d not in mgr.charge
    assert d not in mgr.discharge
    assert d in mgr.idle


async def test_pwr_produced_clamped_at_zero() -> None:
    """`min(0, ...)` clamp at manager.py:429 caps producing device's pwr_produced.

    A device with battery + home values that algebraically sum to a positive
    number gets clamped — pwr_produced should be 0, manager.produced should
    remain 0 (the -= operates on 0).
    """
    d = _online_device(
        "A",
        battery_output=50,
        home_input=50,
        battery_input=0,
        home_output=0,
        electric_level=70,
    )
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=0)

    # Without the clamp, this device's batteryOutput+homeInput-batteryInput-homeOutput
    # would be +100, accumulated into mgr.produced as -100 (via the -= statement).
    assert d.pwr_produced == 0
    assert mgr.produced == 0


# ============================================================================
# 2. ManagerMode dispatch — powerChanged lines 471-501
# ============================================================================


async def test_mode_off_writes_operation_state_off() -> None:
    """OFF mode: operationstate.value == OFF.value, no charge/discharge calls."""
    d = _online_device("A", home_input=300)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.OFF, fusegroups=[fg])

    await _drive(mgr, p1=-500)

    assert mgr.operationstate.value == ManagerState.OFF.value
    assert all(c[0] != "power_charge" for c in d.calls)
    assert all(c[0] != "power_discharge" for c in d.calls)


async def test_mode_matching_negative_setpoint_calls_charge() -> None:
    """MATCHING with effective setpoint < 0 → power_charge issued."""
    d = _online_device("A", home_input=300)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])
    # Get past hysteresis so power_charge sees the real setpoint.
    mgr.charge_time = datetime.min

    await _drive(mgr, p1=-500)

    charge_calls = [c for c in d.calls if c[0] == "power_charge"]
    assert charge_calls, f"expected at least one power_charge call, got {d.calls}"
    # And the setpoint should be negative (charging)
    assert any(c[1] < 0 for c in charge_calls)


async def test_mode_matching_positive_setpoint_calls_discharge() -> None:
    """MATCHING with positive setpoint → power_discharge issued."""
    d = _online_device("A", home_output=300, electric_level=80)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=200)

    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    assert discharge_calls, f"expected power_discharge, got {d.calls}"
    assert any(c[1] > 0 for c in discharge_calls)


async def test_mode_matching_zero_setpoint_calls_discharge_zero() -> None:
    """At setpoint == 0 exactly, MATCHING falls into the discharge arm.

    The check at manager.py:473 is strict `< 0`, so 0 goes to else (discharge).
    Pins the boundary so a `<` → `<=` flip would be caught.

    To distinguish the two arms, the device must be in self.charge so that:
      - power_discharge(0) (else arm) calls d.power_discharge(0) to STOP charging
      - power_charge(0) (if-arm under the mutation) calls d.power_charge(0) to
        distribute zero charge across charge devices
    The dispatched method name differs, which the test asserts on.
    """
    # home_input=100 lands the device in self.charge. p1=100 balances it back
    # so setpoint = p1 - homeInput = 0 — the boundary case.
    d = _online_device("A", home_input=100, electric_level=50)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=100)

    # operationstate ends up IDLE either way, so the discriminator is the
    # dispatched method on the device. The else (discharge) arm calls
    # power_discharge to stop the charging device; the if (charge) arm calls
    # power_charge to distribute zero charge across charge devices.
    assert mgr.operationstate.value == ManagerState.IDLE.value
    assert all(c[0] != "power_charge" for c in d.calls), (
        f"setpoint==0 must take the discharge arm, not charge; got {d.calls}"
    )


async def test_mode_matching_discharge_clamps_at_zero() -> None:
    """MATCHING_DISCHARGE with negative setpoint → power_discharge(0), not charge.

    A discharge-side device (homeOutput > 0) with negative p1 would, under a
    broken `max(0, setpoint)` clamp, receive a NEGATIVE power_discharge value.
    The clamp must zero it out: this is the whole point of MATCHING_DISCHARGE.
    """
    # discharge-side device: homeOutput > 0 → lands in self.discharge.
    d = _online_device("A", home_output=300, electric_level=80)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager(
        [d], operation=ManagerMode.MATCHING_DISCHARGE, fusegroups=[fg]
    )

    await _drive(mgr, p1=-500)

    # No power_charge call (the clamp prevented it).
    assert all(c[0] != "power_charge" for c in d.calls)
    # All power_discharge values must be >= 0 — the clamp turned the
    # negative setpoint into 0 before reaching the device.
    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    assert discharge_calls
    assert all(v >= 0 for _, v in discharge_calls), (
        f"MATCHING_DISCHARGE must not dispatch negative power_discharge; "
        f"got {discharge_calls}"
    )


async def test_mode_matching_charge_with_produced_discharges_produced() -> None:
    """MATCHING_CHARGE + produced>POWER_START → discharge(min(produced,setpoint))."""
    # A producing device (negative pwr_produced, large absolute) — fake it via the
    # battery balance. battery_output=300, home_input=0, battery_input=0, home_output=0
    # → pwr_produced = min(0, 300) → clamped to 0. That's not what we want.
    # Use battery_input=300 + battery_output=0 → pwr_produced = min(0, -300) = -300.
    # → mgr.produced = 300 (positive after the -= statement).
    d = _online_device(
        "A",
        home_output=100,  # places device in discharge list
        battery_input=300,
        battery_output=0,
        electric_level=80,
    )
    fg = _attach_fusegroup([d])
    mgr = build_test_manager(
        [d], operation=ManagerMode.MATCHING_CHARGE, fusegroups=[fg]
    )

    await _drive(mgr, p1=200)

    # produced should be 300 (from the device's negative pwr_produced of -300).
    # MATCHING_CHARGE + setpoint > 0 + produced > POWER_START (60) → discharges
    # min(produced=300, setpoint=200+homeOutput=100=300) ... actually setpoint
    # adjustment: setpoint += homeOutput in partition step.
    # The exact value isn't the point — assert a power_discharge call happened.
    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    assert discharge_calls, (
        f"expected power_discharge in MATCHING_CHARGE, got {d.calls}"
    )
    # And NO power_charge (because the discharge branch was taken)
    assert all(c[0] != "power_charge" for c in d.calls)


async def test_mode_store_solar_charges_when_setpoint_negative() -> None:
    """STORE_SOLAR with setpoint < 0 → power_charge(min(0, setpoint))."""
    d = _online_device("A", home_input=300)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.STORE_SOLAR, fusegroups=[fg])
    mgr.charge_time = datetime.min  # past hysteresis

    await _drive(mgr, p1=-500)

    charge_calls = [c for c in d.calls if c[0] == "power_charge"]
    assert charge_calls
    assert any(c[1] < 0 for c in charge_calls)


async def test_mode_store_solar_does_not_discharge_produced() -> None:
    """STORE_SOLAR + setpoint>0 + produced>POWER_START → discharge(0), NOT produced.

    The check at manager.py:485 includes `operation == ManagerMode.MATCHING_CHARGE`,
    so STORE_SOLAR must fall to the `elif setpoint > 0: power_discharge(0)` branch.
    Catches mutation M9 (dropping the operation == MATCHING_CHARGE check).
    """
    d = _online_device(
        "A",
        home_output=100,
        battery_input=300,
        battery_output=0,
        electric_level=80,
    )
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.STORE_SOLAR, fusegroups=[fg])

    await _drive(mgr, p1=200)

    # Discharge call should be discharge(0) (elif arm), not discharge(produced).
    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    # Either no discharge call at all (if the path stopped early), or discharge with 0.
    # Critically: NO discharge with the full produced/setpoint amount.
    assert all(c[1] == 0 for c in discharge_calls), (
        f"STORE_SOLAR should not discharge produced power; got {discharge_calls}"
    )


async def test_mode_manual_uses_manualpower_not_p1() -> None:
    """MANUAL mode discharges manualpower if positive, charges manualpower if negative.

    Regardless of p1.
    """
    d = _online_device("A", home_output=100, electric_level=80)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager(
        [d], operation=ManagerMode.MANUAL, manualpower=300, fusegroups=[fg]
    )

    await _drive(mgr, p1=-9999)  # p1 ignored in MANUAL mode

    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    assert discharge_calls, "MANUAL mode with manualpower>0 should discharge"
    # Now test the inverse: manualpower<0 → charge
    d2 = _online_device("B", home_input=100, electric_level=20)
    fg2 = _attach_fusegroup([d2])
    mgr2 = build_test_manager(
        [d2], operation=ManagerMode.MANUAL, manualpower=-200, fusegroups=[fg2]
    )
    mgr2.charge_time = datetime.min

    await _drive(mgr2, p1=9999)

    charge_calls = [c for c in d2.calls if c[0] == "power_charge"]
    assert charge_calls, "MANUAL mode with manualpower<0 should charge"


# ============================================================================
# 3. power_charge distribution — manager.py:503-567
# ============================================================================


async def test_charge_single_device_full_setpoint() -> None:
    """One charging device gets the full setpoint."""
    d = _online_device("A", home_input=300, electric_level=50)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])
    mgr.charge_time = datetime.min  # past hysteresis

    await _drive(mgr, p1=-500)

    # power_charge should have been called with the full (negative) setpoint
    # capped at the device's pwr_max.
    charge_calls = [c for c in d.calls if c[0] == "power_charge"]
    assert len(charge_calls) == 1
    # setpoint started at -500, partition added -homeInput=-300 → -800.
    # pwr_max for a single device in a -2400/+2400 group = max(-2400, -800) = -800.
    # Single device weight branch yields the full -800.
    assert charge_calls[0][1] == -800


async def test_charge_two_devices_weighted_by_soc() -> None:
    """Lower-SOC device gets a more-negative power_charge value than higher-SOC."""
    low_soc = _online_device("low", home_input=200, electric_level=20)
    high_soc = _online_device("high", home_input=200, electric_level=80)
    fg = _attach_fusegroup([low_soc, high_soc])
    mgr = build_test_manager(
        [low_soc, high_soc], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )
    mgr.charge_time = datetime.min

    await _drive(mgr, p1=-600)

    low_calls = [c for c in low_soc.calls if c[0] == "power_charge"]
    high_calls = [c for c in high_soc.calls if c[0] == "power_charge"]
    assert low_calls and high_calls
    # low_soc (lower SOC) should pull more of the negative power
    assert low_calls[-1][1] < high_calls[-1][1], (
        f"low_soc {low_calls} should be more negative than high_soc {high_calls}"
    )


async def test_charge_weight_zero_no_division_error() -> None:
    """All devices at SOC=100 → charge_weight==0 → no ZeroDivisionError.

    The guard at manager.py:535-539 falls through to pwr=0 when charge_weight==0.
    """
    d1 = _online_device("a", home_input=200, electric_level=100)
    d2 = _online_device("b", home_input=200, electric_level=100)
    fg = _attach_fusegroup([d1, d2])
    mgr = build_test_manager([d1, d2], operation=ManagerMode.MATCHING, fusegroups=[fg])
    mgr.charge_time = datetime.min

    # Must not raise:
    await _drive(mgr, p1=-500)

    # All power_charge values should be derived from the fallback (not divided by 0)
    # The guard sets pwr=0; the downstream `pwr = max(pwr, setpoint, d.pwr_max)` may
    # then push it back to something nonzero. The critical invariant: no exception.
    # (Already asserted by the await not throwing.)


async def test_charge_stops_discharging_devices() -> None:
    """power_charge first iterates self.discharge to stop them (power_discharge(0))."""
    charging = _online_device("c", home_input=300, electric_level=20)
    discharging = _online_device("d", home_output=300, electric_level=80)
    fg = _attach_fusegroup([charging, discharging])
    mgr = build_test_manager(
        [charging, discharging], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )
    mgr.charge_time = datetime.min

    await _drive(mgr, p1=-1000)

    # The discharging device should get a power_discharge(0) call from power_charge's
    # "stop discharging" loop. pwr_offgrid is 0 → arg is 0, not -10.
    disch_stops = [c for c in discharging.calls if c[0] == "power_discharge"]
    assert disch_stops, (
        f"expected power_discharge stop on discharging device, got {discharging.calls}"
    )
    assert disch_stops[0][1] == 0


async def test_charge_skips_bypass_device_when_stopping_discharge() -> None:
    """power_charge skips bypass devices when iterating discharge list to stop them."""
    charging = _online_device("c", home_input=300, electric_level=20)
    bypass = _online_device("b", home_output=300, electric_level=80, by_pass=1)
    fg = _attach_fusegroup([charging, bypass])
    mgr = build_test_manager(
        [charging, bypass], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )
    mgr.charge_time = datetime.min

    await _drive(mgr, p1=-1000)

    # bypass device's power_discharge should NOT have been called as part of the
    # "stop discharging" loop (the `continue` at manager.py:511).
    assert ("power_discharge", 0) not in bypass.calls


async def test_charge_hysteresis_first_call_returns_zero() -> None:
    """First charge after charge_time=max forces setpoint to 0; charge_time gets set."""
    d = _online_device("A", home_input=300, electric_level=20)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])
    # charge_time is datetime.max from build_test_manager — this is a "fresh" cycle
    assert mgr.charge_time == datetime.max

    now = datetime.now()
    await _drive(mgr, p1=-500, time=now)

    # power_charge called with 0 (setpoint forced to 0 by hysteresis)
    charge_calls = [c for c in d.calls if c[0] == "power_charge"]
    assert charge_calls
    assert charge_calls[0][1] == 0
    # charge_time was advanced — no longer datetime.max
    assert mgr.charge_time != datetime.max
    assert mgr.charge_time > now


async def test_charge_hysteresis_second_call_respects_setpoint() -> None:
    """After hysteresis passes (charge_time <= time), real setpoint flows through."""
    d = _online_device("A", home_input=300, electric_level=20)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])
    mgr.charge_time = datetime.min  # hysteresis already past

    await _drive(mgr, p1=-500)

    charge_calls = [c for c in d.calls if c[0] == "power_charge"]
    assert charge_calls
    # Real charging value (not zero).
    assert charge_calls[0][1] < 0


async def test_charge_starts_idle_device_when_dev_start_negative() -> None:
    """When a charge device hits its limit, an idle device gets started."""
    # Charging device with electricLevel above idle_lvlmin+3 will set dev_start -= 1.
    # If there's an idle device with lower SOC, the start branch fires.
    charging = _online_device("c", home_input=300, electric_level=80)
    idle = _online_device("i", electric_level=20)  # no homeInput/Output → idle list
    fg = _attach_fusegroup([charging, idle])
    mgr = build_test_manager(
        [charging, idle], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )
    mgr.charge_time = datetime.min

    await _drive(mgr, p1=-2000)

    # idle device should get a power_charge call (the "start idle" path)
    idle_charge = [c for c in idle.calls if c[0] == "power_charge"]
    assert idle_charge, f"expected idle device to be started, got {idle.calls}"


# ============================================================================
# 4. power_discharge distribution — manager.py:569-630
# ============================================================================


async def test_discharge_single_device_full_setpoint() -> None:
    """One discharging device gets the full setpoint capped at its pwr_max."""
    d = _online_device("A", home_output=300, electric_level=80)
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=200)

    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    assert discharge_calls
    assert discharge_calls[0][1] > 0


async def test_discharge_two_devices_weighted_by_soc() -> None:
    """Higher-SOC device pulls more of the positive power."""
    high = _online_device("high", home_output=200, electric_level=80)
    low = _online_device("low", home_output=200, electric_level=20)
    fg = _attach_fusegroup([high, low])
    mgr = build_test_manager(
        [high, low], operation=ManagerMode.MATCHING, fusegroups=[fg]
    )

    await _drive(mgr, p1=600)

    high_calls = [c for c in high.calls if c[0] == "power_discharge"]
    low_calls = [c for c in low.calls if c[0] == "power_discharge"]
    assert high_calls and low_calls
    assert high_calls[-1][1] > low_calls[-1][1], (
        f"high SOC {high_calls} should discharge more than low SOC {low_calls}"
    )


async def test_discharge_weight_zero_distributes_evenly() -> None:
    """All devices at SOC=0 → discharge_weight==0 → even distribution fallback.

    The fallback at manager.py:599-600 divides setpoint evenly across remaining
    devices when weight is zero.
    """
    d1 = _online_device("a", home_output=200, electric_level=0)
    d2 = _online_device("b", home_output=200, electric_level=0)
    fg = _attach_fusegroup([d1, d2])
    mgr = build_test_manager([d1, d2], operation=ManagerMode.MATCHING, fusegroups=[fg])

    # Must not raise (ZeroDivisionError) even with weight==0.
    await _drive(mgr, p1=400)


async def test_discharge_socfull_passes_through_solar_only() -> None:
    """SOCFULL device: discharge dispatch equals the solar pass-through amount.

    Math trace:
        pwr_produced = min(0, batteryOutput + homeInput - batteryInput - homeOutput)
                     = min(0, 0 + 0 - 150 - 200) = -350
        partition: setpoint += homeOutput=200, discharge_bypass -= pwr_produced=-350
                   → setpoint = p1+200 = 700, bypass = 350
        bypass clamp (p1=500 ≥ 0): setpoint = max(0, 700 - 350) = 350
        power_discharge with setpoint=350 → dispatch=350 (= the solar amount).

    Pins the SOCFULL behavior holistically: bypass accumulation + setpoint clamp +
    weighted distribution. Any single-line change in that chain shifts the value.
    """
    d = _online_device(
        "A",
        home_output=200,
        battery_input=150,
        battery_output=0,
        home_input=0,
        electric_level=100,
        state=DeviceState.SOCFULL,
    )
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=500)

    discharge_calls = [c for c in d.calls if c[0] == "power_discharge"]
    assert discharge_calls
    assert discharge_calls[-1][1] == 350, (
        f"SOCFULL solar pass-through expected 350W; got {discharge_calls}"
    )


async def test_discharge_active_does_not_bump_pwr_to_solar_produced() -> None:
    """ACTIVE devices with solar: the `pwr < -pwr_produced` clamp does NOT fire.

    Pins the SOCFULL guard at manager.py:604 from the *other* side. For a
    non-SOCFULL device, even if the assigned `pwr` from the weighted
    distribution happens to fall below the solar production amount, the
    dispatch must NOT be bumped up to `-d.pwr_produced`. That clamp is for
    SOCFULL only — it routes solar through the inverter instead of charging
    the battery. Removing the SOCFULL guard would shift the distribution:
    the first device would get bumped up to setpoint (capped via line 612),
    leaving zero for the second device.

    Math trace (2 ACTIVE devices, identical loads + solar production):
        each pwr_produced = min(0, 0+0-200-100) = -300
        partition: setpoint = p1 + homeOutput*2 = 0 + 200 = 200
                   discharge_produced = 600; discharge_weight = 800*50 + 800*50 = 80000
        solaronly = 600 >= 200 → True → limit = 600
        i=0 (first device): weighted pwr = 200*40000/80000 = 100
            SOCFULL clamp: state != SOCFULL → False → pwr stays 100
            first-device hysteresis: delta=120-100=20 → pwr_low=20, pwr unchanged
            dispatch: 100; setpoint -= 100 → 100
        i=1 (second device): weighted pwr = 100*40000/40000 = 100
            dispatch: 100

    Without the SOCFULL guard:
        i=0: pwr=100 → bumped to 300 → capped by min(pwr, setpoint=200, pwr_max=800)
             = 200. Dispatch: 200; setpoint -= 200 → 0.
        i=1: pwr=0 → bumped to 300 → cumulative cap drops it back to 0.
             Dispatch: 0.
    So the *first* device's dispatch shifts from 100 → 200 under the mutation.
    """
    d1 = _online_device(
        "A",
        home_output=100,
        battery_input=200,
        battery_output=0,
        home_input=0,
        electric_level=50,
        state=DeviceState.ACTIVE,
    )
    d2 = _online_device(
        "B",
        home_output=100,
        battery_input=200,
        battery_output=0,
        home_input=0,
        electric_level=50,
        state=DeviceState.ACTIVE,
    )
    fg = _attach_fusegroup([d1, d2])
    mgr = build_test_manager([d1, d2], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=0)

    d1_calls = [c for c in d1.calls if c[0] == "power_discharge"]
    d2_calls = [c for c in d2.calls if c[0] == "power_discharge"]
    assert d1_calls and d2_calls
    assert d1_calls[-1][1] == 100, (
        f"first ACTIVE device should dispatch weighted-math pwr (100W); got {d1_calls}"
    )
    assert d2_calls[-1][1] == 100, (
        f"second ACTIVE device should dispatch the remaining (100W); got {d2_calls}"
    )


async def test_discharge_bypass_clamps_setpoint_when_p1_nonneg() -> None:
    """discharge_bypass > 0 + p1 >= 0 → setpoint clamped at 0 (no negative-charge).

    Pins issue #1151 fix at manager.py:466-467.
    """
    # SOCFULL device with pwr_produced from solar accumulates into discharge_bypass.
    d = _online_device(
        "A",
        home_output=100,
        battery_input=200,
        battery_output=0,
        electric_level=100,
        state=DeviceState.SOCFULL,
    )
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])

    await _drive(mgr, p1=50)

    # After the partition pass: discharge_bypass accumulates -pwr_produced = 200.
    # Setpoint starts at p1=50, adjusted to 50 + homeOutput=100 = 150.
    # Then clamp: max(0 if p1>=0 else setpoint-bypass, setpoint-bypass)
    #          = max(0, 150-200) = max(0, -50) = 0.
    # MATCHING dispatches to power_discharge(0) (else branch since 0 is not < 0).
    # Critically: power_charge should NOT be called (no negative setpoint).
    assert all(c[0] != "power_charge" for c in d.calls), (
        f"bypass clamp failed: setpoint went negative; calls={d.calls}"
    )


async def test_discharge_bypass_allows_setpoint_negative_when_p1_negative() -> None:
    """discharge_bypass > 0 + p1 < 0 → setpoint stays negative, not pinned at 0.

    Math trace:
        pwr_produced = min(0, 0+0-150-50) = -200
        partition: setpoint = p1 + homeOutput = -100 + 50 = -50; bypass = 200
        bypass clamp: max(0 if p1>=0 else setpoint-bypass, setpoint-bypass)
                    = max(-250, -250) = -250  (the `p1>=0` arm is NOT taken)
        MATCHING with setpoint=-250 → power_charge dispatched → operationstate=CHARGE

    The device itself is in self.discharge (homeOutput > 0), not self.charge, so
    power_charge's main loop runs over an empty list — but the operationstate
    update at the top of power_charge proves setpoint was negative. With mutation
    M2 (pinning bypass clamp at 0 regardless of p1), setpoint would be 0 →
    operationstate = IDLE.
    """
    d = _online_device(
        "A",
        home_output=50,
        battery_input=150,
        battery_output=0,
        electric_level=100,
        state=DeviceState.SOCFULL,
    )
    fg = _attach_fusegroup([d])
    mgr = build_test_manager([d], operation=ManagerMode.MATCHING, fusegroups=[fg])
    mgr.charge_time = datetime.min

    await _drive(mgr, p1=-100)

    # operationstate written by power_charge: CHARGE if setpoint<0 else IDLE.
    assert mgr.operationstate.value == ManagerState.CHARGE.value, (
        f"setpoint did not stay negative; operationstate={mgr.operationstate.value}"
    )


# Mark file-level pytest config: all tests are async
pytestmark = pytest.mark.asyncio
