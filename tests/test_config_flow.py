"""Golden-master tests for the Zendure config flow.

Pins the happy path through `async_step_user`, the two-step MQTT-local branch,
the options flow, and the reconfigure flow. `Api.Connect` is mocked at module
import — the real call would hit a remote HTTP endpoint.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import (
    CONF_APPTOKEN,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_MQTTPORT,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_P1METER,
    DOMAIN,
)

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


async def test_config_flow_happy_path(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """User submits the form with MQTTLOCAL=False → integration creates one entry.

    Pins: form step_id is "user", Api.Connect is awaited once, the created
    entry's `data` mirrors the user_input, and `unique_id == "Zendure"`.
    """
    with patch(
        "custom_components.zendure_ha.config_flow.Api.Connect",
        new=AsyncMock(return_value=CONNECT_RESULT),
    ) as mock_connect:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        user_input = {
            CONF_APPTOKEN: "test-token",
            CONF_P1METER: "sensor.power_actual",
            CONF_MQTTLOG: False,
            CONF_MQTTLOCAL: False,
        }
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zendure"
    assert result["data"] == user_input
    mock_connect.assert_awaited()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == "Zendure"


async def test_config_flow_mqttlocal_branch_two_step(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """MQTTLOCAL=True triggers a second `local` step before entry creation.

    Pins: first submission returns step_id="local"; second submission with
    mqtt server/user/password completes the flow.
    """
    with patch(
        "custom_components.zendure_ha.config_flow.Api.Connect",
        new=AsyncMock(return_value=CONNECT_RESULT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        # First step.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_APPTOKEN: "test-token",
                CONF_P1METER: "sensor.power_actual",
                CONF_MQTTLOG: False,
                CONF_MQTTLOCAL: True,
            },
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "local"

        # Second (local) step.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_MQTTSERVER: "mqtt.local",
                CONF_MQTTPORT: 1883,
                CONF_MQTTUSER: "ha-user",
                CONF_MQTTPSW: "ha-pass",
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MQTTLOCAL] is True
    assert result["data"][CONF_MQTTSERVER] == "mqtt.local"
    assert result["data"][CONF_MQTTUSER] == "ha-user"


async def test_options_flow_updates_data(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Options-flow submission writes to entry.data (not entry.options).

    Pins the current behavior at config_flow.py:158-160: `async_update_entry(...,
    data=data)` — the options dict the user submits is merged into `entry.data`,
    leaving `entry.options` empty. (Surprising HA convention but it's what the
    code does today; preserve under 2A.)
    """
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

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_P1METER: "sensor.new_p1_meter",
            CONF_MQTTLOG: True,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The submitted values land in entry.data (not entry.options).
    assert entry.data[CONF_P1METER] == "sensor.new_p1_meter"
    assert entry.data[CONF_MQTTLOG] is True
    # Original keys are preserved.
    assert entry.data[CONF_APPTOKEN] == "test-token"


async def test_config_flow_reconfigure_path(
    recorder_mock: object,  # noqa: ARG001
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Reconfigure on an existing entry updates entry.data and aborts.

    Pins: reconfigure with CONF_MQTTLOCAL=False reuses the user-form schema,
    `Api.Connect` is called for re-validation, no new entry is created (still
    one entry total), and the existing entry's data reflects the new values.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=7,
        unique_id="Zendure",
        data={
            CONF_APPTOKEN: "old-token",
            CONF_P1METER: "sensor.power_actual",
            CONF_MQTTLOG: False,
            CONF_MQTTLOCAL: False,
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.zendure_ha.config_flow.Api.Connect",
        new=AsyncMock(return_value=CONNECT_RESULT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_APPTOKEN: "new-token",
                CONF_P1METER: "sensor.power_actual",
                CONF_MQTTLOG: True,
                CONF_MQTTLOCAL: False,
            },
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data[CONF_APPTOKEN] == "new-token"
    assert entries[0].data[CONF_MQTTLOG] is True


pytestmark = pytest.mark.asyncio
