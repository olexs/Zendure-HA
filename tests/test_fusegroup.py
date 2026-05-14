"""Golden-master tests for FuseGroup.charge_limit / discharge_limit math.

Each test instantiates the real FuseGroup with a list of FakeDevices, sets the
per-device sensor values that the math reads, calls charge_limit(d) or
discharge_limit(d), and asserts the resulting per-device pwr_max.

The math:
    Single-device charge:  pwr_max = max(minpower, charge_limit)
    Single-device discharge: pwr_max = min(maxpower, discharge_limit)
    Multi-device: weighted distribution gated by homeInput/homeOutput > 0,
                  with fd.charge_start / fd.discharge_start as fallback when
                  weight == 0.
"""

from __future__ import annotations

import pytest

from custom_components.zendure_ha.fusegroup import FuseGroup
from tests.fakes import FakeDevice, FakeSensorValue


def _dev(
    name: str,
    *,
    charge_limit: int = -800,
    discharge_limit: int = 800,
    charge_start: int = -80,
    discharge_start: int = 80,
    electric_level: int = 50,
    home_input: int = 0,
    home_output: int = 0,
) -> FakeDevice:
    """Shorthand for building a FakeDevice with the attributes FuseGroup reads."""
    return FakeDevice(
        name=name,
        deviceId=name,
        charge_limit=charge_limit,
        discharge_limit=discharge_limit,
        charge_start=charge_start,
        discharge_start=discharge_start,
        electricLevel=FakeSensorValue(electric_level),
        homeInput=FakeSensorValue(home_input),
        homeOutput=FakeSensorValue(home_output),
    )


def test_single_device_charge_caps_at_minpower() -> None:
    d = _dev("A", charge_limit=-1500)
    fg = FuseGroup("A", maxpower=800, minpower=-1200, devices=[d])

    pwr = fg.charge_limit(d)

    assert pwr == -1200, f"expected min-power cap, got {pwr}"
    assert d.pwr_max == -1200


def test_single_device_discharge_caps_at_maxpower() -> None:
    d = _dev("A", discharge_limit=1500)
    fg = FuseGroup("A", maxpower=800, minpower=-1200, devices=[d])

    pwr = fg.discharge_limit(d)

    assert pwr == 800
    assert d.pwr_max == 800


def test_single_device_within_limits() -> None:
    d = _dev("A", charge_limit=-1000, discharge_limit=600)
    fg = FuseGroup("A", maxpower=800, minpower=-1200, devices=[d])

    assert fg.charge_limit(d) == -1000

    # second call won't recompute (initPower already consumed) — reset for symmetry
    fg.initPower = True
    assert fg.discharge_limit(d) == 600


def test_multi_device_charge_weighted_by_remaining_capacity() -> None:
    """Lower-SOC device should pull a larger (more negative) share of charge power."""
    low_soc = _dev("low", electric_level=20, home_input=100)
    high_soc = _dev("high", electric_level=80, home_input=100)
    fg = FuseGroup("shared", maxpower=1200, minpower=-1200, devices=[low_soc, high_soc])

    fg.charge_limit(low_soc)

    # low_soc has more remaining capacity → bigger weight → more negative pwr_max
    assert low_soc.pwr_max < high_soc.pwr_max
    # both are within the group's minpower
    assert low_soc.pwr_max >= -1200
    assert high_soc.pwr_max >= -1200


def test_multi_device_discharge_weighted_by_soc() -> None:
    """Higher-SOC device pulls a larger (more positive) share of discharge."""
    high_soc = _dev("high", electric_level=80, home_output=100)
    low_soc = _dev("low", electric_level=20, home_output=100)
    fg = FuseGroup("shared", maxpower=1200, minpower=-1200, devices=[high_soc, low_soc])

    fg.discharge_limit(high_soc)

    assert high_soc.pwr_max > low_soc.pwr_max
    assert high_soc.pwr_max <= 1200
    assert low_soc.pwr_max <= 1200


def test_multi_device_skips_zero_homeinput_in_charge() -> None:
    """Devices with homeInput == 0 should not be touched by the charge math."""
    active_a = _dev("a", home_input=100)
    active_b = _dev("b", home_input=100)
    idle = _dev("idle", home_input=0)  # excluded from the weight + assignment loop
    fg = FuseGroup(
        "shared", maxpower=1200, minpower=-1200, devices=[active_a, active_b, idle]
    )

    fg.charge_limit(active_a)

    # active devices got assigned non-zero pwr_max
    assert active_a.pwr_max != 0
    assert active_b.pwr_max != 0
    # idle device left at the FakeDevice default (0)
    assert idle.pwr_max == 0


def test_multi_device_all_zero_homeinput_charge() -> None:
    """If no device passes the homeInput > 0 guard, no pwr_max gets written."""
    d1 = _dev("a", home_input=0)
    d2 = _dev("b", home_input=0)
    fg = FuseGroup("shared", maxpower=1200, minpower=-1200, devices=[d1, d2])

    fg.charge_limit(d1)

    # neither device was inside the guard; pwr_max stays at default (0).
    # The call returns d1.pwr_max (which is 0).
    assert d1.pwr_max == 0
    assert d2.pwr_max == 0


def test_initPower_consumed_only_once() -> None:
    """Subsequent calls return the cached pwr_max without re-running the math."""
    d = _dev("A", charge_limit=-1000)
    fg = FuseGroup("A", maxpower=800, minpower=-1200, devices=[d])

    first = fg.charge_limit(d)

    # Mutate the math's inputs — should have no effect now that initPower is False.
    d.electricLevel.value = 99
    d.charge_limit = -9999

    second = fg.charge_limit(d)

    assert first == second == -1000


def test_initPower_reset_recomputes() -> None:
    """After setting initPower=True, the math runs again and reflects current inputs."""
    d = _dev("A", charge_limit=-1000)
    fg = FuseGroup("A", maxpower=800, minpower=-1200, devices=[d])

    fg.charge_limit(d)
    assert d.pwr_max == -1000

    # Replace the device's charge_limit with something more restrictive than minpower,
    # reset initPower, and re-run.
    d.charge_limit = -500
    fg.initPower = True

    pwr = fg.charge_limit(d)

    assert (
        pwr == -500
    )  # single-device case: max(minpower=-1200, charge_limit=-500) = -500


def test_charge_weight_zero_uses_charge_start_fallback() -> None:
    """Degenerate case: SOC=100 on both → weight==0 → fd.charge_start fallback.

    The ternary at fusegroup.py:41 reads:
        fd.pwr_max = int(avail * (...) / weight) if weight < 0 else fd.charge_start
    Charge weights = sum((100 - SOC) * charge_limit). With both devices at
    SOC=100, each term is 0 * negative = 0, so weight == 0. Else-branch fires.
    """
    d1 = _dev("a", electric_level=100, home_input=100, charge_start=-50)
    d2 = _dev("b", electric_level=100, home_input=100, charge_start=-60)
    fg = FuseGroup("shared", maxpower=1200, minpower=-1200, devices=[d1, d2])

    fg.charge_limit(d1)

    # Both devices fall through to the else branch — pwr_max starts at fd.charge_start,
    # then the limit-adjust clauses run. Assert it lands within the charge_limit cap.
    assert d1.pwr_max >= d1.charge_limit
    assert d2.pwr_max >= d2.charge_limit
    # The fallback set pwr_max to charge_start (or adjusted), so it should not be 0
    # (the FakeDevice default) — proving the else-branch was executed.
    assert d1.pwr_max != 0 or d2.pwr_max != 0


@pytest.mark.parametrize(
    ("name", "minpower", "device_charge_limit", "expected"),
    [
        ("limit-tighter-than-group", -1200, -500, -500),
        ("limit-looser-than-group", -1200, -1500, -1200),
        ("limit-equals-group", -1200, -1200, -1200),
    ],
)
def test_single_device_charge_uses_max_of_minpower_and_limit(
    name: str, minpower: int, device_charge_limit: int, expected: int
) -> None:
    """Parametrized table for the single-device max() — pins the boundary case."""
    d = _dev(name, charge_limit=device_charge_limit)
    fg = FuseGroup(name, maxpower=800, minpower=minpower, devices=[d])

    assert fg.charge_limit(d) == expected
