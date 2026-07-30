"""The PushCloud integration.

One config entry per PushCloud application, holding that application's `pca_`
send token. Setting the entry up confirms the token still names the application
it was added for, then loads the single notify service that entry provides.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.notify import BaseNotificationService
from homeassistant.components.notify.legacy import NOTIFY_SERVICES
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import Application, PushCloudAuthError, PushCloudClient, PushCloudError
from .const import DATA_HASS_CONFIG, DOMAIN

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class PushCloudData:
    """What the notify platform needs, hung off the entry."""

    client: PushCloudClient
    application: Application
    # Filled in by `notify.async_get_service` once discovery has run, and used
    # only to take the service away again on unload.
    service: BaseNotificationService | None = None


type PushCloudConfigEntry = ConfigEntry[PushCloudData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Keep the YAML config for `discovery.async_load_platform`.

    The legacy notify platform is loaded through discovery rather than
    `async_forward_entry_setups`, and discovery insists on being handed the
    original config. Every config-entry integration with a legacy notify
    service does this.
    """
    hass.data[DATA_HASS_CONFIG] = config
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PushCloudConfigEntry) -> bool:
    """Set up one application from a config entry."""
    client = PushCloudClient(async_get_clientsession(hass), entry.data[CONF_TOKEN])

    try:
        application = await client.async_get_application()
    except PushCloudAuthError as err:
        # The token was rotated, or the application deleted. Nothing but a new
        # token will fix it, so ask for one instead of retrying forever.
        raise ConfigEntryAuthFailed(str(err)) from err
    except PushCloudError as err:
        # Everything else - unreachable, 5xx, a suspended account - is worth
        # retrying. A suspension in particular must NOT reach the branch above:
        # the token is valid, and prompting for it teaches the user nothing.
        raise ConfigEntryNotReady(str(err)) from err

    # Follow a rename made in the PushCloud panel. The title is left alone on
    # purpose: Home Assistant lets people rename entries and this should not
    # undo that on every restart.
    if entry.data.get(CONF_NAME) != application.name:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_NAME: application.name}
        )

    entry.runtime_data = PushCloudData(client=client, application=application)

    hass.async_create_task(
        discovery.async_load_platform(
            hass,
            Platform.NOTIFY,
            DOMAIN,
            # `name` is what the legacy platform slugifies into the service
            # name, so this string is the one users type into automations.
            {CONF_NAME: f"PushCloud: {application.name}", "entry_id": entry.entry_id},
            hass.data[DATA_HASS_CONFIG],
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PushCloudConfigEntry) -> bool:
    """Unload the entry and take its notify service away.

    Not `async_unload_platforms`. That unloads *entity* platforms, and a legacy
    notify service is not one - calling it with `[Platform.NOTIFY]` raises
    "Config entry was never loaded", because the entry never forwarded a notify
    entity platform in the first place. Several integrations upstream have
    exactly that bug.

    So the service unregisters itself, and is dropped from the legacy
    platform's own bookkeeping so a later `notify.async_reload` cannot bring a
    service back that points at an entry which no longer exists. One entry's
    service goes; every other entry's survives, which matters here because one
    entry per application means several are normal.
    """
    if (service := entry.runtime_data.service) is not None:
        await service.async_unregister_services()
        registered = hass.data.get(NOTIFY_SERVICES, {}).get(DOMAIN)
        if registered and service in registered:
            registered.remove(service)

    return True
