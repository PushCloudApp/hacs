"""The PushCloud integration.

One config entry per PushCloud application, holding that application's `pca_`
send token. Setting the entry up confirms the token still names the application
it was added for, then loads the single notify service that entry provides.
"""

from __future__ import annotations

import logging
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
from homeassistant.util import slugify

from .api import Application, PushCloudAuthError, PushCloudClient, PushCloudError
from .const import DATA_HASS_CONFIG, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# How much of an application id is borrowed to tell two same-named applications
# apart. Long enough to be unique in any account a person actually has, short
# enough to type into an automation.
_DISAMBIGUATOR_LENGTH = 6


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


def _platform_name(
    hass: HomeAssistant, entry: PushCloudConfigEntry, application: Application
) -> str:
    """The name the legacy notify platform slugifies into the service name.

    Application names are not unique, and two different ones can still slugify
    to a single service: `Home Assistant` and `Home-Assistant` both want
    `notify.pushcloud_home_assistant`. The legacy platform registers whichever
    claim arrives first and returns silently on the second, so the loser would
    have no service at all and no log line saying why.

    The oldest entry keeps the plain name, so automations written against it go
    on working when a colliding application is added years later. Anything that
    collides with it is told apart by the tail of its application id, which is
    stable across renames and restarts where the name is neither.
    """
    preferred = f"PushCloud: {application.name}"
    slug = slugify(preferred)

    rivals = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        and slugify(f"PushCloud: {other.data.get(CONF_NAME, '')}") == slug
    ]
    if not rivals or all(entry.created_at < other.created_at for other in rivals):
        return preferred

    disambiguated = f"{preferred} {slugify(application.id)[-_DISAMBIGUATOR_LENGTH:]}"
    _LOGGER.warning(
        "Another PushCloud application already answers to notify.%s, so %s sends"
        " through notify.%s instead. Renaming one of them in the PushCloud panel"
        " and reloading gives both a plain name again",
        slug,
        application.name,
        slugify(disambiguated),
    )
    return disambiguated


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
            {
                CONF_NAME: _platform_name(hass, entry, application),
                "entry_id": entry.entry_id,
            },
            hass.data[DATA_HASS_CONFIG],
        ),
        # Not eagerly, which is the default. This platform reaches
        # `notify.async_get_service`, and that reads the `runtime_data` set
        # above - an eager start would make the order of these two statements
        # load-bearing with nothing here to say so. Deferring by a tick is also
        # what setup does on a real start, where the notify component is not
        # loaded yet and the load has to wait for it either way.
        eager_start=False,
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

    An entry unloaded while its platform is still loading has no service here
    yet, and needs none: `async_get_service` checks the entry is still loaded
    before it builds one, so a load arriving late bows out instead of reaching
    for the `runtime_data` this unload has already taken away.
    """
    if (service := entry.runtime_data.service) is not None:
        await service.async_unregister_services()
        registered = hass.data.get(NOTIFY_SERVICES, {}).get(DOMAIN)
        if registered and service in registered:
            registered.remove(service)

    return True
