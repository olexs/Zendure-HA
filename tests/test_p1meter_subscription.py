"""Tests for ZendureManager.update_p1meter — subscription wiring and p1_factor.

The control loop is driven by HA state-change events on the P1 entity. The
`update_p1meter` method (manager.py:306-316) is the only thing that wires
that subscription up and tears it down. The 2A refactor turns this single
subscription into N (one per configured P1 entity), so any regression in
the unsubscribe / re-subscribe cleanup logic would silently leak listeners.

These tests pin the current single-P1 behavior so 2A can extend it without
breaking the contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.fakes import FakeDevice, build_test_manager


def _manager_with_hass(
    devices: list[FakeDevice] | None = None,
) -> tuple[object, MagicMock]:
    """Build a partial ZendureManager with a MagicMock hass + states."""
    mgr = build_test_manager(devices or [])
    hass = MagicMock()
    hass.states.get.return_value = None  # default: entity has no state yet
    mgr.hass = hass
    return mgr, hass


async def test_update_p1meter_first_call_subscribes() -> None:
    """From p1meterEvent=None, set entity → exactly one subscribe call."""
    mgr, hass = _manager_with_hass()

    with patch(
        "custom_components.zendure_ha.manager.async_track_state_change_event"
    ) as track:
        track.return_value = MagicMock()  # the "unsubscribe" callable returned
        mgr.update_p1meter("sensor.power_actual")

    track.assert_called_once_with(hass, ["sensor.power_actual"], mgr._p1_changed)
    assert mgr.p1meterEvent is track.return_value


async def test_update_p1meter_replace_unsubscribes_then_subscribes() -> None:
    """From sensor X to sensor Y: old unsubscribe is called, new subscribe runs."""
    mgr, _ = _manager_with_hass()

    with patch(
        "custom_components.zendure_ha.manager.async_track_state_change_event"
    ) as track:
        first_unsub = MagicMock(name="first_unsub")
        second_unsub = MagicMock(name="second_unsub")
        track.side_effect = [first_unsub, second_unsub]

        mgr.update_p1meter("sensor.X")
        mgr.update_p1meter("sensor.Y")

    # First unsubscribe was called exactly once during the replace.
    first_unsub.assert_called_once_with()
    # The new subscription is now stored.
    assert mgr.p1meterEvent is second_unsub
    # async_track_state_change_event was called twice with the two entities.
    assert track.call_count == 2
    assert track.call_args_list[0].args[1] == ["sensor.X"]
    assert track.call_args_list[1].args[1] == ["sensor.Y"]


async def test_update_p1meter_none_unsubscribes_only() -> None:
    """From sensor X to None: unsubscribe runs, no new subscribe."""
    mgr, _ = _manager_with_hass()

    with patch(
        "custom_components.zendure_ha.manager.async_track_state_change_event"
    ) as track:
        first_unsub = MagicMock(name="first_unsub")
        track.return_value = first_unsub

        mgr.update_p1meter("sensor.X")
        track.reset_mock()
        mgr.update_p1meter(None)

    first_unsub.assert_called_once_with()
    track.assert_not_called()
    assert mgr.p1meterEvent is None


async def test_update_p1meter_kw_unit_sets_factor_1000() -> None:
    """If the P1 entity's unit_of_measurement is 'kW', p1_factor becomes 1000."""
    mgr, hass = _manager_with_hass()
    state = MagicMock()
    state.attributes = {"unit_of_measurement": "kW"}
    hass.states.get.return_value = state

    with patch(
        "custom_components.zendure_ha.manager.async_track_state_change_event"
    ) as track:
        track.return_value = MagicMock()
        mgr.update_p1meter("sensor.kw_meter")

    assert mgr.p1_factor == 1000


async def test_update_p1meter_w_unit_keeps_factor_1() -> None:
    """Default 'W' unit (or absent state) leaves p1_factor unchanged at 1."""
    mgr, hass = _manager_with_hass()
    state = MagicMock()
    state.attributes = {"unit_of_measurement": "W"}
    hass.states.get.return_value = state

    with patch(
        "custom_components.zendure_ha.manager.async_track_state_change_event"
    ) as track:
        track.return_value = MagicMock()
        mgr.update_p1meter("sensor.w_meter")

    assert mgr.p1_factor == 1


pytestmark = pytest.mark.asyncio
