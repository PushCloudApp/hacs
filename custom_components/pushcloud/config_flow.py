"""Config flow for PushCloud.

One field, one call. The token names its own application via
`GET /v1/applications/me`, which supplies both the entry's title and its
unique id - so the user never types a display name and can never mistype one
into a service name they then have to live with.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    Application,
    PushCloudAuthError,
    PushCloudClient,
    PushCloudConnectionError,
)
from .const import CONF_APPLICATION_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})


class PushCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Turn an application token into a config entry."""

    VERSION = 1

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
