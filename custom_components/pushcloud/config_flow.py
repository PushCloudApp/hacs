"""Config and options flows for PushCloud.

One field, one call. The token names its own application via
`GET /v1/applications/me`, which supplies both the entry's title and its
unique id - so the user never types a display name and can never mistype one
into a service name they then have to live with.

The options flow that follows works the same way: it lists the account's devices
from `GET /v1/applications/me/devices` and shows them as a picker, rather than
asking anyone to type a device name they would have to remember exactly.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    Application,
    PushCloudAuthError,
    PushCloudClient,
    PushCloudConnectionError,
    PushCloudError,
)
from .const import CONF_APPLICATION_ID, CONF_DEVICES, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})


class PushCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Turn an application token into a config entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> PushCloudOptionsFlow:
        """Offer the Configure button that picks which devices get notified."""
        return PushCloudOptionsFlow()

    async def _async_identify(
        self, token: str, errors: dict[str, str]
    ) -> Application | None:
        """Resolve a token to its application, or fill in `errors`."""
        client = PushCloudClient(async_get_clientsession(self.hass), token)
        try:
            return await client.async_get_application()
        except PushCloudAuthError:
            errors["base"] = "invalid_auth"
        except PushCloudConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error validating the PushCloud token")
            errors["base"] = "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for an application token and identify it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Trimmed, not validated. A token copied out of the panel arrives
            # with a trailing space or newline often enough that the alternative
            # is a rejection the user cannot see the cause of, every character
            # on screen looking exactly right. What a *live* token is remains
            # the server's business.
            token = user_input[CONF_TOKEN].strip()
            if (application := await self._async_identify(token, errors)) is not None:
                # The application id, not the token: rotating a token must not
                # let the same application be added a second time.
                await self.async_set_unique_id(application.id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"PushCloud: {application.name}",
                    data={
                        CONF_TOKEN: token,
                        CONF_APPLICATION_ID: application.id,
                        CONF_NAME: application.name,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-auth after a send came back 401."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement token for the same application."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if (application := await self._async_identify(token, errors)) is not None:
                # A token for a different application would quietly repoint
                # every automation using this service at another destination.
                # Refuse rather than rewrite: adding that application as its own
                # entry is both the honest fix and one click away.
                if application.id != entry.data[CONF_APPLICATION_ID]:
                    return self.async_abort(reason="wrong_application")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_TOKEN: token},
                )

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_SCHEMA, errors=errors
        )


class PushCloudOptionsFlow(OptionsFlow):
    """Choose which of the account's devices this entry's notifications go to.

    One step, and the step *is* the device list - which is why every failure here
    aborts rather than re-showing a form. A form with an empty picker invites
    somebody to save "no devices chosen", and that means the opposite of what they
    would intend: no choice is how you say "all of them".
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the account's devices, and save the ones ticked."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_DEVICES: user_input[CONF_DEVICES]}
            )

        # Built from `entry.data`, not from the client on `runtime_data`. An entry
        # that failed to load has no runtime data, and being unable to change this
        # setting because the network happened to be down at startup would be a
        # dead end with no way out but deleting the entry.
        client = PushCloudClient(
            async_get_clientsession(self.hass), self.config_entry.data[CONF_TOKEN]
        )
        try:
            devices = await client.async_list_devices()
        except PushCloudAuthError:
            return self.async_abort(reason="invalid_auth")
        except PushCloudError:
            return self.async_abort(reason="cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected error listing PushCloud devices")
            return self.async_abort(reason="unknown")

        if not devices:
            return self.async_abort(reason="no_devices")

        targets = {device.target for device in devices}
        # Anything saved whose device has since been deleted is dropped rather
        # than pre-ticked. The selector refuses a value outside its options, so
        # keeping it would make the form unsubmittable - the one person who most
        # needs to change this setting would be the one who could not.
        chosen = [
            target
            for target in self.config_entry.options.get(CONF_DEVICES) or []
            if target in targets
        ]

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_DEVICES): SelectSelector(
                            SelectSelectorConfig(
                                options=[
                                    SelectOptionDict(
                                        value=device.target, label=device.label
                                    )
                                    for device in devices
                                ],
                                multiple=True,
                                # Not a sorted dropdown: the server orders by slug
                                # already, and `custom_value` is off so nobody can
                                # type a target that does not exist.
                                mode=SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
                {CONF_DEVICES: chosen},
            ),
        )
