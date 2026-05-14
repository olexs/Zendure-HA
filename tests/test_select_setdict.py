"""Unit tests for ZendureSelect.setDict and setList — the stale-option fallback.

`setDict` (select.py:42-49) is what the 2A `p1Source` select relies on to
handle a removed-from-config P1 entity gracefully: if the restored option
isn't in the new options list, fall back to index 0.

`setList` (select.py:51-58) is a sibling that operates on a list of strings
instead of a key→label dict — same fallback contract.

These tests exercise the methods directly via `object.__new__` so we don't
need a real HA bus. The control-loop tests already cover the integration
end-to-end; these target just the data-mutation invariant.
"""

from __future__ import annotations

from custom_components.zendure_ha.select import ZendureRestoreSelect, ZendureSelect


def _bare_select() -> ZendureSelect:
    """Build a ZendureSelect bypassing __init__ (which would call self.add)."""
    sel = object.__new__(ZendureSelect)
    sel._options = {0: "first", 1: "second"}
    sel._attr_options = ["first", "second"]
    sel._attr_current_option = "first"
    sel.hass = None  # no HA bus → setDict's "write state" branch is skipped
    return sel


def _bare_restore_select() -> ZendureRestoreSelect:
    """Same shape as `_bare_select` but the Restore variant."""
    sel = object.__new__(ZendureRestoreSelect)
    sel._options = {0: "first", 1: "second"}
    sel._attr_options = ["first", "second"]
    sel._attr_current_option = "first"
    sel.hass = None
    return sel


def test_setdict_preserves_current_option_when_still_present() -> None:
    """If the current option appears in the new options dict, keep it."""
    sel = _bare_select()
    sel._attr_current_option = "second"

    sel.setDict({0: "first", 1: "second", 2: "third"})

    assert sel._attr_current_option == "second"
    assert sel._attr_options == ["first", "second", "third"]


def test_setdict_falls_back_to_index_0_when_current_option_dropped() -> None:
    """If the current option is NOT in the new dict, current → options[0].

    This is the stale-fallback the 2A `p1Source` select relies on. A user
    removes a P1 entity from the options flow; on next reload, devices that
    had picked the removed entity get reset to the first available P1.
    """
    sel = _bare_select()
    sel._attr_current_option = "removed_value"

    sel.setDict({0: "new_a", 1: "new_b"})

    assert sel._attr_current_option == "new_a"
    assert sel._attr_options == ["new_a", "new_b"]


def test_setdict_internal_options_reference_replaced() -> None:
    """`_options` is replaced by the new dict (not merged)."""
    sel = _bare_select()
    old_options = sel._options

    new_options = {0: "x", 1: "y"}
    sel.setDict(new_options)

    assert sel._options is new_options
    assert sel._options is not old_options


def test_setdict_no_state_write_when_hass_loop_not_running() -> None:
    """`self.async_write_ha_state` is NOT called when hass is None.

    Protects against an exception during boot — `loadDevices` calls setDict
    on each device before any of them have been added to hass.
    """

    class _Fake:
        called = False

        def async_write_ha_state(self) -> None:
            type(self).called = True

    sel = _bare_select()
    # Replace the method on this single instance only.
    sel.async_write_ha_state = _Fake().async_write_ha_state  # type: ignore[method-assign]

    sel.setDict({0: "x"})

    assert _Fake.called is False


def test_setlist_falls_back_to_index_0_when_current_dropped() -> None:
    """setList (the string-only sibling) has the same fallback contract."""
    sel = _bare_select()
    sel._attr_current_option = "old_value"

    sel.setList(["alpha", "beta"])

    assert sel._attr_current_option == "alpha"
    assert sel._attr_options == ["alpha", "beta"]
    # setList nukes _options (the dict-based lookup) — value lookups now return None.
    assert sel._options is None


def test_restore_select_setdict_same_contract() -> None:
    """The Restore variant inherits setDict — make sure the contract still holds.

    `ZendureRestoreSelect` is what `p1Source` will use. This test pins that
    the inherited setDict behavior is the one we just validated, not some
    overridden variant.
    """
    sel = _bare_restore_select()
    sel._attr_current_option = "removed_value"

    sel.setDict({0: "new_a", 1: "new_b"})

    assert sel._attr_current_option == "new_a"
