"""Config flow for the Meest parcel tracker integration."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .account.client import (
    MeestAccountApiError,
    MeestAccountClient,
    MeestAccountInvalidCredentials,
)
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_LOGIN,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .tracking.coordinator import tracked_direction

CONF_PASSWORD = "password"


def normalize_tracking_code(value: str) -> str:
    """Return the code trimmed, upper-cased and without spaces.

    A hyphen is kept: it is stripped only when the code is sent, so what the
    user sees is what they typed.
    """
    return re.sub(r"\s+", "", (value or "")).upper()


def valid_tracking_code(value: str) -> bool:
    """Accept every non-empty code.

    A false negative from a guessed shape locks a user out of a code the
    carrier would have resolved; a bad code simply comes back "not found".
    """
    return bool(value)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _default_options() -> dict[str, Any]:
    return {
        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
    }


class MeestConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Meest integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MeestOptionsFlowHandler:
        """Return the options flow handler."""
        return MeestOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick where parcels come from: an account, or tracking codes."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the tracking hub — no account and nothing to ask.

        Codes are added afterwards through the options, the ``track_parcel``
        service or a dashboard button.
        """
        await self.async_set_unique_id(SOURCE_TRACKING)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Meest",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={CONF_PARCELS: [], **_default_options()},
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in once and persist only the token pair, never the password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            login = str(user_input[CONF_LOGIN]).strip()
            password = str(user_input[CONF_PASSWORD])
            if not login or not password:
                errors["base"] = "invalid_auth"
            else:
                client = MeestAccountClient(async_get_clientsession(self.hass))
                try:
                    tokens = await client.async_login(login, password)
                except MeestAccountInvalidCredentials:
                    errors["base"] = "invalid_auth"
                except MeestAccountApiError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"account:{login.lower()}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Meest ({login})",
                        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_LOGIN: login, **tokens},
                        options=_default_options(),
                    )
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=vol.Schema(
                {vol.Required(CONF_LOGIN): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Reauthenticate the fixed account; never silently change accounts."""
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace only the rotated tokens after a successful login."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if user_input is not None:
            client = MeestAccountClient(async_get_clientsession(self.hass))
            try:
                tokens = await client.async_login(
                    entry.data[CONF_LOGIN], user_input[CONF_PASSWORD]
                )
            except MeestAccountInvalidCredentials:
                errors["base"] = "invalid_auth"
            except MeestAccountApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(entry.unique_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=tokens)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"login": entry.data[CONF_LOGIN]},
            errors=errors,
        )


class MeestOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels and integration settings.

    A tracking entry gets a menu: incoming and outgoing parcels each open the
    same list editor, because a code alone cannot say which way a parcel is
    going, so the user files it in one list or the other. An account entry has
    no parcel list and goes straight to the settings.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        if self.config_entry.data.get(CONF_SOURCE, SOURCE_TRACKING) != SOURCE_TRACKING:
            return await self.async_step_settings()
        return self.async_show_menu(
            step_id="init",
            menu_options=["incoming_parcels", "outgoing_parcels", "settings"],
        )

    async def async_step_incoming_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being received."""
        return await self._async_step_parcel_list(DIRECTION_INCOMING, user_input)

    async def async_step_outgoing_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being sent."""
        return await self._async_step_parcel_list(DIRECTION_OUTGOING, user_input)

    async def _async_step_parcel_list(
        self, direction: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show and handle one direction's complete tracked-code list.

        Both directions share one ``CONF_PARCELS`` list, so a submission
        replaces this direction's entries and leaves the other's alone — except
        a code that was filed the other way, which moves rather than being
        rejected: re-entering it here is how a wrongly filed parcel is fixed.
        """
        if user_input is not None:
            codes = list(
                dict.fromkeys(
                    code
                    for value in user_input.get("tracking_codes", [])
                    if (code := normalize_tracking_code(value))
                )
            )
            kept = [
                parcel
                for parcel in _current_parcels(self.config_entry)
                if tracked_direction(parcel) != direction
                and parcel.get(CONF_TRACKING_CODE) not in codes
            ]
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_PARCELS: kept
                    + [
                        {CONF_TRACKING_CODE: code, CONF_DIRECTION: direction}
                        for code in codes
                    ],
                },
            )
        current_codes = [
            parcel[CONF_TRACKING_CODE]
            for parcel in _current_parcels(self.config_entry)
            if tracked_direction(parcel) == direction
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id=f"{direction}_parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DELIVERED_FILTER_TYPE,
                        default=current.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["days", "parcels"],
                            translation_key=CONF_DELIVERED_FILTER_TYPE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_DELIVERED_FILTER_AMOUNT,
                        default=current.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_INCLUDE_HISTORY,
                        default=current.get(
                            CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
