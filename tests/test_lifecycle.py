"""Tests for the integration's lifecycle handlers.

`ZendureManager.update_operation` is invoked when the user changes the
operation-mode select. `update_listener` (in `__init__.py`) is invoked when
the options flow saves new values. Both are uncovered today but 2A rewires
them — `update_listener` will switch from passing `CONF_P1METER` (string) to
`CONF_P1METERS` (list), so we pin the contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.zendure_ha import update_listener
from custom_components.zendure_ha.const import (
    CONF_MQTTLOG,
    CONF_P1METER,
    CONF_SIM,
    ManagerMode,
)
from tests.fakes import FakeDevice, build_test_manager

# ---------- update_operation ----------


async def test_update_operation_off_powers_all_devices_off() -> None:
    """OFF mode + p1 subscribed + devices → every device gets power_off()."""
    a = FakeDevice(name="A", deviceId="A")
    b = FakeDevice(name="B", deviceId="B")
    mgr = build_test_manager([a, b])
    mgr.hass = MagicMock()
    mgr.p1meterEvent = MagicMock()  # simulate "subscribed"

    entity = SimpleNamespace(value=ManagerMode.OFF.value)
    await mgr.update_operation(entity, None)

    assert mgr.operation == ManagerMode.OFF
    assert ("power_off", None) in a.calls
    assert ("power_off", None) in b.calls


async def test_update_operation_no_op_when_p1meter_not_subscribed() -> None:
    """p1meterEvent is None → guard skips the whole match block.

    Pinning this protects the "manager not yet wired up" startup window.
    """
    a = FakeDevice(name="A", deviceId="A")
    mgr = build_test_manager([a])
    mgr.hass = MagicMock()
    mgr.p1meterEvent = None  # not subscribed yet

    entity = SimpleNamespace(value=ManagerMode.OFF.value)
    await mgr.update_operation(entity, None)

    # Operation is still recorded (the unconditional `self.operation = ...`),
    # but no power_off call was dispatched.
    assert mgr.operation == ManagerMode.OFF
    assert all(c[0] != "power_off" for c in a.calls)


async def test_update_operation_warns_when_no_devices_online() -> None:
    """non-OFF mode + no online devices → early return, notification created."""
    a = FakeDevice(name="A", deviceId="A")
    a.online_value = False
    mgr = build_test_manager([a])
    mgr.hass = MagicMock()
    mgr.p1meterEvent = MagicMock()  # subscribed

    entity = SimpleNamespace(value=ManagerMode.MATCHING.value)

    with patch(
        "custom_components.zendure_ha.manager.persistent_notification.async_create"
    ) as notify:
        await mgr.update_operation(entity, None)

    notify.assert_called_once()
    # No discharge/charge dispatch on the offline device (early return prevents it).
    assert all(c[0] not in {"power_charge", "power_discharge"} for c in a.calls)


async def test_update_operation_off_with_no_devices_no_op() -> None:
    """OFF mode with zero devices → match-block runs but the for-loop is empty.

    Pins the `len(self.devices) > 0` guard inside the OFF case.
    """
    mgr = build_test_manager([])
    mgr.hass = MagicMock()
    mgr.p1meterEvent = MagicMock()

    entity = SimpleNamespace(value=ManagerMode.OFF.value)
    # Must not raise even with no devices.
    await mgr.update_operation(entity, None)

    assert mgr.operation == ManagerMode.OFF


# ---------- update_listener ----------


async def test_update_listener_propagates_p1meter_change() -> None:
    """Options-flow submit re-runs update_listener → forwards to manager.update_p1meter.

    Pins the wire path that 2A will rewrite to pass a list rather than a string.
    Today: `manager.update_p1meter(entry.data[CONF_P1METER])`.
    """
    manager = MagicMock()
    manager.update_p1meter = MagicMock()

    entry = MagicMock()
    entry.data = {
        CONF_P1METER: "sensor.new_meter",
        CONF_MQTTLOG: False,
        CONF_SIM: False,
    }
    entry.runtime_data = manager

    await update_listener(MagicMock(), entry)

    manager.update_p1meter.assert_called_once_with("sensor.new_meter")


async def test_update_listener_defaults_to_power_actual_when_unset() -> None:
    """Missing CONF_P1METER → defaults to 'sensor.power_actual'.

    Documents the fallback string baked in at `__init__.py:45`.
    """
    manager = MagicMock()
    manager.update_p1meter = MagicMock()

    entry = MagicMock()
    entry.data = {CONF_MQTTLOG: False, CONF_SIM: False}  # no CONF_P1METER
    entry.runtime_data = manager

    await update_listener(MagicMock(), entry)

    manager.update_p1meter.assert_called_once_with("sensor.power_actual")


async def test_update_listener_propagates_mqttlog_flag() -> None:
    """`Api.mqttLogging` flips to entry.data[CONF_MQTTLOG].

    Class-level flag write; pinned so the wire path is observable.
    """
    manager = MagicMock()
    entry = MagicMock()
    entry.data = {CONF_MQTTLOG: True, CONF_SIM: False, CONF_P1METER: "sensor.x"}
    entry.runtime_data = manager

    with patch("custom_components.zendure_ha.Api") as api_cls:
        await update_listener(MagicMock(), entry)
        assert api_cls.mqttLogging is True


pytestmark = pytest.mark.asyncio
