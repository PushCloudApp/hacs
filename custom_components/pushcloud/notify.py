"""The PushCloud notify service.

One service per config entry, sending to that entry's one application. There is
no `targets` mechanism and no slug arithmetic - the entry already names exactly
one destination.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.notify import (
    ATTR_DATA,
    ATTR_TITLE,
    BaseNotificationService,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .api import PushCloudAuthError, PushCloudClient, PushCloudError
from .const import ATTR_PRIORITY, ATTR_SOUND, ATTR_URL, ATTR_URL_TITLE

_LOGGER = logging.getLogger(__name__)

# The `data:` keys this version understands. Each maps to the same-named field
# on POST /v1/messages, so there is no translation table to keep in step.
#
# `actions`, `attachment_key`, `expires_in`, `scheduled_for` and `encrypted`
# are deliberately absent - they are a v2 design, and this tuple is the one
# place that has to change when they arrive.
SUPPORTED_DATA_KEYS = (ATTR_PRIORITY, ATTR_SOUND, ATTR_URL, ATTR_URL_TITLE)


async def async_get_service(
    hass: HomeAssistant,
    config: ConfigType,
    discovery_info: DiscoveryInfoType | None = None,
) -> PushCloudNotificationService | None:
    """Build the service for one config entry."""
    if discovery_info is None:
        return None

    entry = hass.config_entries.async_get_entry(discovery_info["entry_id"])
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        # This runs two tasks removed from `async_setup_entry`: the load is
        # scheduled there and dispatches into a task of its own. An entry
        # unloaded or removed inside that window gets here with its
        # `runtime_data` already taken away, and the assignment below would
        # raise into the notify platform's error handling - a traceback in the
        # log blaming the integration for a reload the user was entitled to
        # make. There is nothing to serve, so serve nothing.
        return None

    service = PushCloudNotificationService(entry)
    # Handed back to `async_unload_entry`, which has no other way to find this
    # instance - see the comment there.
    entry.runtime_data.service = service
    return service


class PushCloudNotificationService(BaseNotificationService):
    """Sends notifications through one PushCloud application."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Hold the entry so the client and the re-auth path stay reachable."""
        self._entry = entry

    @property
    def _client(self) -> PushCloudClient:
        """The client built at setup.

        Read through the entry rather than captured at construction so a
        reload after re-auth is picked up without rebuilding the service.
        """
        return self._entry.runtime_data.client

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        """Send one notification."""
        payload = self._payload(message, kwargs)

        try:
            await self._client.async_send(payload)
        except PushCloudAuthError as err:
            # Started explicitly. Home Assistant converts a ConfigEntryAuthFailed
            # into a re-auth flow only on the setup and coordinator paths - from
            # inside a service call it is just another exception, and the user
            # would get a failed automation with no repair card offered.
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except PushCloudError as err:
            # Quota, rate limit, plan, suspension, network. All the user can
            # act on is the server's wording, so it goes through unchanged and
            # lands in the automation trace.
            raise HomeAssistantError(str(err)) from err

    @staticmethod
    def _payload(message: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Turn a notify call into a POST /v1/messages body."""
        payload: dict[str, Any] = {"message": message}

        # Only when the caller actually set one. Home Assistant defaults the
        # title to "Home Assistant", and sending that would head every
        # notification with a word the phone already shows.
        if (title := kwargs.get(ATTR_TITLE)) is not None:
            payload["title"] = title

        data = kwargs.get(ATTR_DATA) or {}
        for key in SUPPORTED_DATA_KEYS:
            if key in data:
                payload[key] = data[key]

        if unknown := set(data) - set(SUPPORTED_DATA_KEYS):
            # Ignored rather than rejected: a typo should not break an
            # automation, and forwarding it would only earn a worse error from
            # the server.
            _LOGGER.debug(
                "Ignoring unsupported data keys: %s", ", ".join(sorted(unknown))
            )

        return payload
