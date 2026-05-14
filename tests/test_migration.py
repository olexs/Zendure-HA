"""Golden-master tests for async_migrate_entry and Migration.async_migrate.

These pin the current behavior of:
- `custom_components.zendure_ha.__init__.async_migrate_entry` (the HA hook)
- `custom_components.zendure_ha.migration.Migration.async_migrate`

Some of the pinned behaviors are pre-existing quirks (see test docstrings) —
the goal is *behavioral snapshot*, not fixing them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha import async_migrate_entry
from custom_components.zendure_ha.const import (
    CONF_APPTOKEN,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_P1METER,
    DOMAIN,
)
from custom_components.zendure_ha.migration import Migration


def _entry(minor_version: int) -> MockConfigEntry:
    """Helper to build a minimal Zendure config entry at a given minor_version."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=minor_version,
        unique_id="Zendure",
        data={
            CONF_APPTOKEN: "test-token",
            CONF_P1METER: "sensor.power_actual",
            CONF_MQTTLOG: False,
            CONF_MQTTLOCAL: False,
        },
    )


async def test_migrate_entry_at_minor_5_unchanged(
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Entry at minor_version=5 → no migration runs, minor_version stays 5.

    Pins `__init__.py:22-25`: the `if entry.minor_version < 5` guard prevents
    Migration from running, and `async_update_entry(version=1, minor_version=5)`
    is a no-op (already at 5).
    """
    entry = _entry(minor_version=5)
    entry.add_to_hass(hass)

    with patch.object(Migration, "async_migrate", new=AsyncMock()) as mock_migrate:
        result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 1
    assert entry.minor_version == 5
    mock_migrate.assert_not_called()


async def test_migrate_entry_at_minor_4_runs_migration_then_bumps(
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Entry at minor_version=4 → Migration runs, then minor_version bumps to 5.

    Pins the only "real" migration path that exists today: `< 5` triggers
    Migration.async_migrate, and afterwards minor_version is unconditionally
    set to 5.
    """
    entry = _entry(minor_version=4)
    entry.add_to_hass(hass)

    with patch.object(Migration, "async_migrate", new=AsyncMock()) as mock_migrate:
        result = await async_migrate_entry(hass, entry)

    assert result is True
    mock_migrate.assert_awaited_once_with(hass, entry.entry_id)
    assert entry.version == 1
    assert entry.minor_version == 5


async def test_migrate_entry_at_minor_7_pins_downgrade_quirk(
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Entry at minor_version=7 → Migration NOT run; minor_version downgraded to 5.

    PRE-EXISTING QUIRK (`__init__.py:25`):
    `async_update_entry(version=1, minor_version=5)` is called unconditionally
    outside the `if < 5` guard. If someone manually invokes this hook on an
    entry already at minor_version=7 (current value from
    `config_flow.MINOR_VERSION`), the entry gets DOWNGRADED to 5 without any
    data rewrite. HA itself won't normally invoke this path (it only calls
    async_migrate_entry when the stored minor_version is below the current
    declared one), but the in-code behavior is preserved here so 2A doesn't
    accidentally "fix" it without noticing.
    """
    entry = _entry(minor_version=7)
    entry.add_to_hass(hass)

    with patch.object(Migration, "async_migrate", new=AsyncMock()) as mock_migrate:
        result = await async_migrate_entry(hass, entry)

    assert result is True
    mock_migrate.assert_not_called()
    # The downgrade quirk: entry.minor_version was 7, now is 5.
    assert entry.minor_version == 5


@pytest.fixture
def _no_file_rewrite() -> object:
    """Stub out Migration._update_files — testing_config has no .storage dir.

    The registry-rename tests below exercise only the entity_registry/device_registry
    code paths. File rewriting (lovelace, automations, etc.) is out of scope.
    """
    with patch.object(Migration, "_update_files", return_value=False) as patched:
        yield patched


async def test_migration_renames_stale_entity(
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
    _no_file_rewrite: object,  # noqa: PT019
) -> None:
    """A device-attached entity with a stale unique_id is renamed to canonical.

    `Migration.async_migrate` derives the canonical unique_id from
    `snakecase(f"{device.name.lower()}_{snakecase(translation_key)}")`. For a
    device named "MyDevice" and an entity with translation_key="batteryInput",
    the canonical unique_id is "mydevice_battery_input" and the canonical
    entity_id is "sensor.mydevice_battery_input".
    """
    entry = _entry(minor_version=4)
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    # Create a device tied to the entry. No underscore in name — otherwise the
    # migration `continue`s past it (migration.py:104).
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "test-device")},
        name="MyDevice",
    )

    # Register an entity with a stale unique_id but the right translation_key
    # so Migration can compute the canonical replacement. `suggested_object_id`
    # forces the entity_id away from HA's default derivation so we can observe
    # the rename.
    entity_registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="stale_unique_id",
        suggested_object_id="stale_unique_id",
        translation_key="batteryInput",
        device_id=device.id,
        config_entry=entry,
    )

    await Migration.async_migrate(hass, entry.entry_id)

    # The new unique_id and entity_id follow the canonical format.
    new_entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, "mydevice_battery_input"
    )
    assert new_entity_id == "sensor.mydevice_battery_input"
    migrated = entity_registry.async_get(new_entity_id)
    assert migrated is not None
    assert migrated.unique_id == "mydevice_battery_input"
    assert migrated.translation_key == "battery_input"


async def test_migration_no_op_for_already_clean_registry(
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
    _no_file_rewrite: object,  # noqa: PT019
) -> None:
    """If the registry is already in canonical form, migration leaves it unchanged.

    Pre-seed an entity whose unique_id, entity_id, and translation_key already
    match the format Migration would produce, then assert nothing was rewritten.
    This is the "happy idempotent" path — running migration twice on a clean
    install must not churn the registry.
    """
    entry = _entry(minor_version=4)
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "clean-device")},
        name="CleanDevice",
    )
    entity_registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="cleandevice_battery_input",
        suggested_object_id="cleandevice_battery_input",
        translation_key="battery_input",
        device_id=device.id,
        config_entry=entry,
    )

    with patch.object(
        er.EntityRegistry,
        "async_update_entity",
        wraps=entity_registry.async_update_entity,
    ) as spy:
        await Migration.async_migrate(hass, entry.entry_id)

    # No rewrite happened — canonical state was already in place.
    spy.assert_not_called()

    refreshed = entity_registry.async_get("sensor.cleandevice_battery_input")
    assert refreshed is not None
    assert refreshed.unique_id == "cleandevice_battery_input"
    assert refreshed.translation_key == "battery_input"


pytestmark = pytest.mark.asyncio
