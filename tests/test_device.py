"""Tests for the shared device."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.const import DOMAIN
from custom_components.meest.device import build_device_info


def test_tracking_hub_is_named_plainly():
    entry = MockConfigEntry(domain=DOMAIN, data={"source": "tracking"})
    assert build_device_info(entry)["name"] == "Meest"


def test_accounts_are_told_apart_by_login():
    entry = MockConfigEntry(
        domain=DOMAIN, data={"source": "account", "login": "a@example.test"}
    )
    assert build_device_info(entry)["name"] == "Meest (a@example.test)"
    assert build_device_info(entry)["identifiers"] == {(DOMAIN, entry.entry_id)}
