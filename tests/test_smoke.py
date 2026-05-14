"""Smoke test: confirms the test harness can import the integration."""

from __future__ import annotations


def test_imports() -> None:
    from custom_components.zendure_ha import manager

    assert manager.ZendureManager is not None
