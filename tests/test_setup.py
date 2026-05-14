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

CONNECT_RESULT: dict[str, Any] = {
    "deviceList": [
        {
            "deviceKey": "test-device-001",
            "deviceName": "Hyper 2000",
            "productModel": "Hyper 2000",
            "productKey": "test-prodkey",
            "snNumber": "HYP-TEST-001",
            "ip": "",
        }
    ],
    "mqtt": {
        "clientId": "test-client",
        "url": "mqtt.test:1883",
        "username": "test-user",
        "password": "test-pass",
    },
}


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
    # before the test ran.
    fake_cloud = MagicMock()
    fake_cloud.is_connected.return_value = False
    fake_local = MagicMock()
    fake_local.is_connected.return_value = False
    monkeypatch.setattr(Api, "mqttCloud", fake_cloud)
    monkeypatch.setattr(Api, "mqttLocal", fake_local)
    # Reset the per-device registry so we don't carry state across tests.
    monkeypatch.setattr(Api, "devices", {})
    # Api.Init calls `Api.mqttCloud.__init__(CallbackAPIVersion.VERSION2, ...)`
    # which would re-init our MagicMock with a spec arg, restricting its
    # attributes. Stub the whole method out — we don't need its side effects.
    monkeypatch.setattr(Api, "Init", lambda *_args, **_kwargs: None)

    entry = MockConfigEntry(
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


pytestmark = pytest.mark.asyncio
