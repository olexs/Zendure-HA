"""Duck-typed fakes for the zendure_ha control-loop tests.

The control loop in ZendureManager reads ~15 attributes per device and calls a
handful of async methods (power_get, power_charge, power_discharge, power_off).
None of those require a real HomeAssistant bus or paho client — we duck-type
the device surface with FakeDevice + tiny FakeSensorValue / FakeSelect stubs,
build a ZendureManager via object.__new__ (skipping its heavy __init__), and
run the methods under test directly.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class FakeSensorValue:
    """Duck-typed ZendureSensor exposing .asInt / .asNumber / update_value."""

    value: float = 0

    @property
    def asInt(self) -> int:
        return int(self.value)

    @property
    def asNumber(self) -> float:
        return float(self.value)

    def update_value(self, v: Any) -> bool:
        if isinstance(v, (int, float)):
            self.value = v
            return True
        if hasattr(v, "value"):
            self.value = v.value
            return True
        self.value = 0
        return False


@dataclass
class FakeNumber:
    """Duck-typed ZendureRestoreNumber exposing .asNumber."""

    value: float = 0

    @property
    def asNumber(self) -> float:
        return float(self.value)

    @property
    def asInt(self) -> int:
        return int(self.value)


@dataclass
class FakeSelect:
    """Duck-typed ZendureRestoreSelect.

    fuseGroup.value (int) is the integer key (e.g. 2 for group800).
    fuseGroup.state (str) is the option string (e.g. "group800").
    """

    value: Any = 0
    state: str = ""
    current_option: str = ""
    onchanged: Callable[..., Any] | None = None

    def setDict(self, _options: dict[Any, str]) -> None:
        return


@dataclass
class FakeDevice:
    """Duck-typed ZendureDevice for control-loop tests.

    Records every async method call into `calls` so tests can assert dispatch
    behavior. Defaults make the device look online and idle at 50% SOC.
    """

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
    pwr_offgrid_value: int = 0
    actualKwh: float = 0.0
    power_get_result: bool = True
    online_value: bool = True
    setStatus_calls: int = 0
    maxSolar: int = 0

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
    fuseGrp: Any = None
    # 2A introduces a per-device `p1Source` ZendureRestoreSelect (see
    # docs/agents/plans/2026-05-14-multiple-p1-sensors.md). Today this field
    # is unused by the integration; the default mirrors the single legacy P1
    # so the post-2A FuseGroup partition key
    # `(fuseGroup.state, p1Source.current_option)` keeps single-P1 behavior
    # byte-identical. 2A multi-P1 tests can override `current_option` to
    # exercise the new partition.
    p1Source: FakeSelect = field(
        default_factory=lambda: FakeSelect(current_option="sensor.power_actual")
    )

    state: Any = None  # DeviceState; set lazily because of import-cycle concerns

    calls: list[tuple[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Defer DeviceState import to keep import-time impact minimal.
        if self.state is None:
            from custom_components.zendure_ha.const import DeviceState

            self.state = DeviceState.INACTIVE

    @property
    def pwr_offgrid(self) -> int:
        return self.pwr_offgrid_value

    @property
    def online(self) -> bool:
        return self.online_value

    async def power_get(self) -> bool:
        self.actualKwh = self.availableKwh.asNumber
        return self.power_get_result

    async def power_charge(self, power: int) -> int:
        self.calls.append(("power_charge", power))
        return power

    async def power_discharge(self, power: int) -> int:
        self.calls.append(("power_discharge", power))
        return power

    async def power_off(self) -> None:
        self.calls.append(("power_off", None))

    async def charge(self, power: int) -> int:
        self.calls.append(("charge", power))
        return power

    async def discharge(self, power: int) -> int:
        self.calls.append(("discharge", power))
        return power

    def setStatus(self) -> None:
        self.setStatus_calls += 1


def cycle_reset(mgr: Any) -> None:
    """Clear the manager's per-cycle scratch — same shape as `_p1_changed` does
    before invoking `powerChanged` (manager.py:393-410).

    After 2A this resets the equivalent fields on a `P1Group` instance instead.
    Update this helper (or add a sibling) at that point.
    """
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


async def drive(
    mgr: Any, p1: int, *, is_fast: bool = False, time: datetime | None = None
) -> None:
    """Reset per-cycle scratch and dispatch a powerChanged cycle.

    Multi-cycle tests can omit `cycle_reset` calls and use `drive` directly —
    it runs the full `_p1_changed`-equivalent setup each invocation. Tests
    that want fine control over scratch state should call `cycle_reset`
    separately.
    """
    cycle_reset(mgr)
    await mgr.powerChanged(p1=p1, isFast=is_fast, time=time or datetime.now())


def build_test_manager(
    devices: list[FakeDevice],
    *,
    operation: Any = None,
    manualpower: float = 0,
    fusegroups: list[Any] | None = None,
) -> Any:
    """Construct a ZendureManager via object.__new__ bypassing __init__.

    The control loop methods (powerChanged / power_charge / power_discharge)
    read a finite set of attributes; we set them all here. Anything they
    don't touch is left unset on purpose — accessing it will raise loudly.
    """
    from custom_components.zendure_ha.const import ManagerMode
    from custom_components.zendure_ha.manager import ZendureManager

    m = object.__new__(ZendureManager)
    m.devices = devices
    m.fuseGroups = fusegroups or []
    m.operation = operation if operation is not None else ManagerMode.MATCHING
    m.simulation = False

    m.p1_history = deque([25, -25], maxlen=8)
    m.p1_factor = 1
    m.zero_next = datetime.min
    m.zero_fast = datetime.min
    m.p1meterEvent = None
    m.update_count = 0
    m.check_reset = datetime.min

    m.charge = []
    m.charge_limit = 0
    m.charge_optimal = 0
    m.charge_weight = 0
    m.charge_time = datetime.max
    m.charge_last = datetime.min

    m.discharge = []
    m.discharge_bypass = 0
    m.discharge_produced = 0
    m.discharge_limit = 0
    m.discharge_optimal = 0
    m.discharge_weight = 0

    m.idle = []
    m.idle_lvlmax = 0
    m.idle_lvlmin = 100
    m.produced = 0
    m.pwr_low = 0

    m.manualpower = FakeNumber(manualpower)
    m.power = FakeSensorValue()
    m.availableKwh = FakeSensorValue()
    m.operationstate = FakeSensorValue()

    return m
