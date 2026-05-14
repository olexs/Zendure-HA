"""Structural integrity checks on the translation JSON files.

The integration ships translations for de/en/fr/nl. 2A renames the
`p1meter` key to `p1meters` under `config.step.*.data` and `options.step.*.data`,
and adds a new `entity.select.p1_source.name`. A test that flags missing or
mis-located translation keys catches a class of typo regressions that
otherwise only surface in the HA UI as `_state_unknown` chips.

These tests deliberately pin only what 2A is about to change — the suite
isn't meant to be a translation-completeness audit (the non-en locales are
known to be incomplete today, and that's a separate cleanup).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_TRANSLATIONS = Path("custom_components/zendure_ha/translations")
_LOCALES = ("de", "en", "fr", "nl")


@pytest.fixture(scope="module")
def loaded() -> dict[str, dict]:
    """Parse all 4 locale JSONs once per test module."""
    return {
        loc: json.loads((_TRANSLATIONS / f"{loc}.json").read_text("utf-8"))
        for loc in _LOCALES
    }


@pytest.mark.parametrize("locale", _LOCALES)
def test_translation_file_parses(locale: str) -> None:
    """Each locale JSON is well-formed and has the expected top-level keys.

    Pins the `entity`, `config`, `options` sections — 2A modifies all three.
    The `exceptions` section is locale-specific (de has it, nl currently does
    not), so it's not enforced here.
    """
    data = json.loads((_TRANSLATIONS / f"{locale}.json").read_text("utf-8"))
    assert isinstance(data, dict), f"{locale}.json must parse as a JSON object"
    assert "config" in data, f"{locale}.json missing top-level 'config'"
    assert "options" in data, f"{locale}.json missing top-level 'options'"
    # `entity` is the section 2A adds keys to; require it.
    assert "entity" in data, f"{locale}.json missing top-level 'entity'"


def test_p1meter_field_label_present_in_en_user_step(loaded: dict[str, dict]) -> None:
    """en.json has a label for the P1 entity field on the user step.

    2A renames this from `p1meter` to `p1meters`. This test will need to be
    updated as part of 2A — at that point the rename is observable: the
    diff against this test shows the field-name migration explicitly.

    Path: `config.step.user.data.p1meter`.
    """
    en = loaded["en"]
    step_user = en.get("config", {}).get("step", {}).get("user", {})
    assert "data" in step_user, "en.json config.step.user must have a 'data' section"
    assert "p1meter" in step_user["data"], (
        "en.json config.step.user.data must define 'p1meter' (pre-2A). "
        "When 2A renames this key, update this test to assert 'p1meters'."
    )


def test_p1meter_field_label_present_in_en_options_step(
    loaded: dict[str, dict],
) -> None:
    """en.json has a label for the P1 entity field on the options-flow step.

    Same rename consideration as the user-step test above.
    Path: `options.step.init.data.p1meter`.
    """
    en = loaded["en"]
    step_init = en.get("options", {}).get("step", {}).get("init", {})
    assert "data" in step_init, "en.json options.step.init must have a 'data' section"
    assert "p1meter" in step_init["data"], (
        "en.json options.step.init.data must define 'p1meter' (pre-2A)."
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_operation_state_sensor_translated(
    locale: str, loaded: dict[str, dict]
) -> None:
    """All 4 locales translate the `operation_state` sensor.

    2A keeps the legacy `sensor.zendure_manager_operation_state` entity id
    for the first P1 group (preserving existing automations/dashboards). If
    any locale drops this translation key, the user will see `_state_unknown`
    chip values in the UI for existing installs after upgrade.
    """
    sensor_section = loaded[locale].get("entity", {}).get("sensor", {})
    assert "operation_state" in sensor_section, (
        f"{locale}.json missing entity.sensor.operation_state — the legacy "
        "operation-state entity that 2A preserves"
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_fuse_group_select_translated(locale: str, loaded: dict[str, dict]) -> None:
    """All 4 locales translate the `fuse_group` select.

    The fuseGroup select is the *pattern* the upcoming `p1_source` select
    will follow in 2A. Pin its current presence so when 2A adds the new
    select, a matching translation key is required in all 4 locales (caught
    by adding a sibling test rather than this one).
    """
    select_section = loaded[locale].get("entity", {}).get("select", {})
    assert "fuse_group" in select_section, (
        f"{locale}.json missing entity.select.fuse_group"
    )
