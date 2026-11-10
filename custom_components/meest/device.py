"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_LOGIN, CONF_SOURCE, DOMAIN, SOURCE_ACCOUNT

CONFIGURATION_URL = "https://meest.com"

ATTRIBUTION = "Data provided by Meest"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this entry.

    An account entry is named after its login so several accounts stay apart.
    """
    login = entry.data.get(CONF_LOGIN)
    is_account = entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Meest ({login})" if is_account and login else "Meest",
        manufacturer="Meest",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
