"""Constants for the PushCloud integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "pushcloud"

# Stashed by `async_setup` so `async_setup_entry` can hand the YAML config to
# `discovery.async_load_platform`, which the legacy notify platform requires.
DATA_HASS_CONFIG: Final = "pushcloud_hass_config"

# Entry data keys. The credential is an *application* token (`pca_`), never an
# API key, so it is stored under `token` rather than Home Assistant's
# `CONF_API_KEY` - the distinction is the whole security argument for this
# design and a misleading key name would erode it.
CONF_APPLICATION_ID: Final = "application_id"

# `data:` keys accepted on a notify call, each mapping to the same-named field
# on POST /v1/messages.
ATTR_PRIORITY: Final = "priority"
ATTR_SOUND: Final = "sound"
ATTR_URL: Final = "url"
ATTR_URL_TITLE: Final = "url_title"
