"""End-to-end smoke test for async_setup_entry + async_unload_entry.

This is the only test in the suite that exercises the full HA setup pipeline:
config entry → platform forwards → ZendureManager construction → Api.Connect →
device instantiation → first refresh → unload. The cloud HTTP call and MQTT
sockets are mocked; everything else runs against the real HA bus.

The intent is regression-detection at the "does the integration still start up?"
level — a sanity check before the 2A refactor lands, not a functional test of
any particular feature.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.api import Api
from custom_components.zendure_ha.const import (
    CONF_APPTOKEN,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_P1METER,
    DOMAIN,
)
from custom_components.zendure_ha.manager import ZendureManager

_HYPER_DEVICE = {
    "deviceKey": "test-device-001",
    "deviceName": "Hyper 2000",
    "productModel": "Hyper 2000",
    "productKey": "test-prodkey",
    "snNumber": "HYP-TEST-001",
    "ip": "",
}

_MQTT_BLOCK = {
    "clientId": "test-client",
    "url": "mqtt.test:1883",
    "username": "test-user",
    "password": "test-pass",
}

CONNECT_RESULT: dict[str, Any] = {"deviceList": [_HYPER_DEVICE], "mqtt": _MQTT_BLOCK}


def _patch_api_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace class-level paho clients + stub Api.Init.

    Shared across the setup tests below — see the smoke test docstring for
    the rationale on each line.
    """
    fake_cloud = MagicMock()
    fake_cloud.is_connected.return_value = False
    fake_local = MagicMock()
    fake_local.is_connected.return_value = False
    monkeypatch.setattr(Api, "mqttCloud", fake_cloud)
    monkeypatch.setattr(Api, "mqttLocal", fake_local)
    monkeypatch.setattr(Api, "devices", {})
    monkeypatch.setattr(Api, "Init", lambda *_args, **_kwargs: None)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=7,
        unique_id="Zendure",
        data={
            CONF_APPTOKEN: "test-token",
            CONF_P1METER: "sensor.power_actual",
            CONF_MQTTLOG: False,
            CONF_MQTTLOCAL: False,
        },
    )


async def test_async_setup_entry_loads_devices(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async_setup_entry constructs the manager, loads one device, and unloads cleanly.

    Pins the *shape* of a successful setup, not behavior:
        - entry.state == LOADED after setup
        - entry.runtime_data is a ZendureManager
        - len(manager.devices) == 1 (matches the mocked deviceList)
        - async_unload_entry returns True and clears runtime state
    """
    # Replace the class-level paho clients with MagicMocks. The patch
    # on `mqtt_client.Client` would only affect NEW Client() instances; the
    # class-level Api.mqttCloud / Api.mqttLocal were created at api.py:76-77
    # before the test ran. Api.Init re-inits the existing instance, so it
    # must be stubbed too — passing CallbackAPIVersion as the first positional
    # arg would set `spec=` on the MagicMock and restrict its attributes.
    _patch_api_singletons(monkeypatch)

    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.zendure_ha.manager.Api.Connect",
        new=AsyncMock(return_value=CONNECT_RESULT),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state == ConfigEntryState.LOADED
    manager = entry.runtime_data
    assert isinstance(manager, ZendureManager)
    assert len(manager.devices) == 1
    assert manager.devices[0].deviceId == "test-device-001"

    # Unload cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.NOT_LOADED


async def test_load_devices_empty_device_list(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty `deviceList` loads cleanly with zero devices.

    Pins the "no devices yet" startup window (e.g. user just authenticated
    but hasn't added devices to their Zendure account yet). loadDevices's
    for-loop is empty; `self.devices = list(Api.devices.values())` yields [].
    """
    _patch_api_singletons(monkeypatch)
    entry = _entry()
    entry.add_to_hass(hass)

    response = {"deviceList": [], "mqtt": _MQTT_BLOCK}
    with patch(
        "custom_components.zendure_ha.manager.Api.Connect",
        new=AsyncMock(return_value=response),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state == ConfigEntryState.LOADED
    manager = entry.runtime_data
    assert isinstance(manager, ZendureManager)
    assert manager.devices == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_load_devices_unknown_product_model_skipped(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with an unrecognized productModel is silently skipped.

    `Api.createdevice.get(prodModel.lower().strip(), None)` returns None →
    the `init is None` guard at manager.py:122-124 continues past it. The
    integration still loads; this device just doesn't appear in
    manager.devices. Pinned so 2A doesn't accidentally change the skip
    behavior to a hard failure (or vice versa).
    """
    _patch_api_singletons(monkeypatch)
    entry = _entry()
    entry.add_to_hass(hass)

    response = {
        "deviceList": [
            _HYPER_DEVICE,  # known model — included
            {
                "deviceKey": "future-device-002",
                "deviceName": "Hypothetical 9000",
                "productModel": "Hypothetical 9000",  # not in Api.createdevice
                "productKey": "future-prodkey",
                "snNumber": "FUTURE-002",
                "ip": "",
            },
        ],
        "mqtt": _MQTT_BLOCK,
    }
    with patch(
        "custom_components.zendure_ha.manager.Api.Connect",
        new=AsyncMock(return_value=response),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state == ConfigEntryState.LOADED
    manager = entry.runtime_data
    # The known model loaded; the unknown one was skipped.
    device_ids = [d.deviceId for d in manager.devices]
    assert device_ids == ["test-device-001"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_load_devices_missing_mqtt_block_returns_early(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Connect response without a `mqtt` key triggers loadDevices's early return.

    The early-return guard at manager.py:95-96 means: no devices loaded,
    none of the manager-level entities (operationstate, totalKwh, ...)
    are created. The integration's first refresh after that will hit an
    AttributeError, so `async_setup_entry` fails and the entry transitions
    to SETUP_RETRY (or SETUP_ERROR, depending on HA version). Either is
    fine to pin — what matters is that it doesn't silently land in LOADED.
    """
    _patch_api_singletons(monkeypatch)
    entry = _entry()
    entry.add_to_hass(hass)

    # Response intentionally missing the `mqtt` key. async_setup may return
    # False, raise, or schedule a retry — accept any as long as we don't
    # end up LOADED.
    response = {"deviceList": [_HYPER_DEVICE]}
    with (
        patch(
            "custom_components.zendure_ha.manager.Api.Connect",
            new=AsyncMock(return_value=response),
        ),
        contextlib.suppress(Exception),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state != ConfigEntryState.LOADED, (
        f"missing mqtt block must NOT result in LOADED state; got {entry.state}"
    )


pytestmark = pytest.mark.asyncio
