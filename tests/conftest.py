"""Shared pytest fixtures for zendure_ha tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `from custom_components.zendure_ha ...`
# resolves. pytest-homeassistant-custom-component does its own sys.path
# manipulation for HA's loader, which can mask `pythonpath` in pyproject.toml.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import base64  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

pytest_plugins = ["pytest_homeassistant_custom_component"]


# NOTE: `enable_custom_integrations` is intentionally NOT autouse. It depends
# on the `hass` fixture, which mutates `custom_components.__path__` to point
# at the plugin's bundled testing_config — breaking plain imports of
# `custom_components.zendure_ha` in pure-pytest tests (Phase 2/3/4). HA-bus
# tests (Phase 5) opt in by adding `enable_custom_integrations` to their
# argument list along with a symlink/copy step (added in Phase 5).


@pytest.fixture
def mock_zendure_token() -> str:
    """Return a valid-looking Zendure App Token.

    Api.ApiHA rsplits the b64-decoded value on the *last* '.' so the dot
    between url and appkey must be the rightmost one.
    """
    return base64.b64encode(b"https://api.test.example.appkey-xyz").decode()


def synthetic_device_definition(
    *,
    deviceId: str = "test-device-001",
    productModel: str = "Hyper 2000",
    snNumber: str = "HYP-TEST-001",
    productKey: str = "test-prodkey",
    deviceName: str | None = None,
    ip: str = "",
) -> dict[str, Any]:
    """Build a deviceList entry shaped like the cloud response."""
    return {
        "deviceKey": deviceId,
        "deviceName": deviceName or productModel,
        "productModel": productModel,
        "productKey": productKey,
        "snNumber": snNumber,
        "ip": ip,
    }


@pytest.fixture
def mock_api_connect() -> Iterator[MagicMock]:
    """Patch Api.Connect to return a synthetic deviceList + mqtt response.

    Tests can override the device list via the fixture's `device_list` attribute:

        def test_x(mock_api_connect):
            mock_api_connect.device_list = [synthetic_device_definition(...)]
            ...
    """
    response = {
        "deviceList": [synthetic_device_definition()],
        "mqtt": {
            "clientId": "test-client",
            "url": "mqtt.test:1883",
            "username": "test-user",
            "password": "test-pass",
        },
    }

    async def _connect(_hass: Any, _data: Any, _reload: bool) -> dict[str, Any]:
        return {
            "deviceList": list(_connect_mock.device_list),
            "mqtt": response["mqtt"],
        }

    _connect_mock = MagicMock(side_effect=_connect)
    _connect_mock.device_list = response["deviceList"]

    with patch(
        "custom_components.zendure_ha.api.Api.Connect",
        new=AsyncMock(side_effect=_connect),
    ) as mock:
        mock.device_list = response["deviceList"]
        yield mock


@pytest.fixture
def mock_paho_client() -> Iterator[MagicMock]:
    """Replace paho.mqtt.Client with a MagicMock so Api.Init doesn't open sockets."""
    with patch("custom_components.zendure_ha.api.mqtt_client.Client") as mock:
        instance = MagicMock()
        instance.is_connected.return_value = False
        instance.host = "mqtt.test"
        mock.return_value = instance
        yield mock
