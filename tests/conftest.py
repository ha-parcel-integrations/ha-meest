"""pytest configuration for the Meest test suite."""

import sys
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.plugins import hass  # noqa: F401

from custom_components.meest import status
from custom_components.meest.account import (
    client as account_client,
    parcels as account_parcels,
)
from custom_components.meest.tracking import api as tracking_api


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make ``custom_components.meest`` loadable from config-flow / setup tests."""


@pytest.fixture(autouse=True)
def default_zone():
    """Pin a known zone for the pure tests; Home Assistant tests bring their own."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    yield
    dt_util.set_default_time_zone(previous)


@pytest.fixture(autouse=True)
def reset_one_shot_warnings():
    """Clear the "already warned this session" state between tests.

    The one-shot warnings are module-level bookkeeping, so without this a
    test's assertion would depend on which test already triggered one first.
    """
    status._warned_codes.clear()
    tracking_api._warned_error_codes.clear()
    account_client._warned_shapes.clear()
    account_parcels._first_sighting_warned = False
    account_parcels._direction_modes.clear()
    account_parcels._direction_warned = False


if sys.platform == "win32":
    # pytest-homeassistant-custom-component blocks socket *creation*
    # (``disable_socket(allow_unix_socket=True)``) in its per-test setup hook.
    # That is fine on Linux, where asyncio's self-pipe is an AF_UNIX
    # socketpair — but Windows event loops build theirs from AF_INET sockets,
    # so every async test dies with ``SocketBlockedError`` while the event
    # loop fixture is being created. Neutralise the creation block on Windows
    # and keep the network guard as the plugin's connect-time allowlist
    # (``socket_allow_hosts(["127.0.0.1"])``, applied right before the
    # ``disable_socket`` call we swallow here).
    import pytest_socket

    pytest_socket.disable_socket = lambda allow_unix_socket=False: None

    # HA's aiohttp helper hardcodes aiohttp's AsyncResolver, whose aiodns
    # backend refuses the Proactor loop the suite runs on under Windows.
    # Swap in the threaded resolver for the tests — no test resolves DNS.
    from aiohttp.resolver import ThreadedResolver
    import homeassistant.helpers.aiohttp_client as _ha_aiohttp_client

    _ha_aiohttp_client.AsyncResolver = ThreadedResolver
