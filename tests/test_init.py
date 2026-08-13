"""Tests for entry setup and unload."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushcloud.api import (
    Application,
    PushCloudAuthError,
    PushCloudConnectionError,
    PushCloudPlanError,
)
from custom_components.pushcloud.const import CONF_APPLICATION_ID, DOMAIN

from .conftest import APP_ID, APP_NAME


@pytest.fixture
def mock_get_application():
    """Patch the identity call setup makes."""
    with patch(
        "custom_components.pushcloud.PushCloudClient.async_get_application",
        new_callable=AsyncMock,
    ) as mocked:
        mocked.return_value = Application(id=APP_ID, name=APP_NAME)
        yield mocked


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up one entry."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def build_entry(
    application_id: str, name: str, created_at: datetime
) -> MockConfigEntry:
    """An entry for one application, added at a time of the test's choosing.

    `created_at` decides which of two colliding applications keeps the plain
    service name, and `MockConfigEntry` stamps it with the wall clock rather
    than accepting one, so it is written afterwards the way Home Assistant
    writes it itself.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"PushCloud: {name}",
        unique_id=application_id,
        data={
            "token": f"pca_{application_id}",
            CONF_APPLICATION_ID: application_id,
            "name": name,
        },
    )
    object.__setattr__(entry, "created_at", created_at)
    return entry


async def test_setup_loads_and_records_the_application(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setup confirms the token is still live and keeps what it learned."""
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.application.name == APP_NAME


async def test_setup_refreshes_a_renamed_application(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Renaming in the PushCloud panel is picked up by reloading the entry.

    The stored name follows the server. The entry *title* deliberately does
    not: Home Assistant lets people rename entries, and rewriting that on every
    restart would undo their choice.
    """
    mock_get_application.return_value = Application(id=APP_ID, name="Renamed")

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.data["name"] == "Renamed"
    assert mock_config_entry.title == "PushCloud: Home Assistant"


async def test_auth_failure_at_setup_asks_for_reauth(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A token rotated while Home Assistant was down is a re-auth, not a retry."""
    mock_get_application.side_effect = PushCloudAuthError("Invalid application token")

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(mock_config_entry.async_get_active_flows(hass, {"reauth"}))


async def test_connection_failure_at_setup_retries(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """PushCloud being briefly unreachable must not break the entry."""
    mock_get_application.side_effect = PushCloudConnectionError("no route to host")

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_suspended_account_retries_rather_than_reauthing(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A suspension is not a credential problem.

    Prompting for re-auth would have the user paste a token that is perfectly
    valid, over and over, and learn nothing about why it fails.
    """
    mock_get_application.side_effect = PushCloudPlanError("This account is suspended")

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not any(mock_config_entry.async_get_active_flows(hass, {"reauth"}))


async def test_unload(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Removing the integration takes its notify service with it."""
    await setup_entry(hass, mock_config_entry)

    assert hass.services.has_service("notify", "pushcloud_home_assistant")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service("notify", "pushcloud_home_assistant")


async def test_unload_before_the_platform_has_finished_loading(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry unloaded mid-load must bow out quietly.

    Setup returns as soon as the platform load is scheduled, so this window is
    real: reloading twice in quick succession, or removing an entry during
    startup, lands in it. The load then arrives to find `runtime_data` gone and
    the platform logs a traceback against the integration, for something the
    user did nothing wrong to cause.

    Note the missing `async_block_till_done` before the unload. That call is
    what closes the window, and closing it here would test nothing.
    """
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service("notify", "pushcloud_home_assistant")
    assert "Error setting up platform pushcloud" not in caplog.text


@pytest.fixture
def mock_applications():
    """Answer the identity call by the token that asked.

    Setting a component up sets up every one of its entries at once, so the
    class-level patch other tests use cannot say which entry it is answering.
    This replaces the client instead, keyed by the token it was built with.
    """
    applications: dict[str, Application] = {}

    def build_client(_session, token: str) -> AsyncMock:
        client = AsyncMock()
        client.async_get_application.return_value = applications[token]
        return client

    with patch("custom_components.pushcloud.PushCloudClient", side_effect=build_client):
        yield applications


@pytest.mark.parametrize("newer_first", [False, True])
async def test_applications_with_colliding_names_keep_separate_services(
    hass: HomeAssistant,
    mock_applications: dict[str, Application],
    newer_first: bool,
) -> None:
    """Two names that slugify alike must not cost either one its service.

    `Home Assistant` and `Home-Assistant` are different applications with one
    service name between them, and the legacy notify platform hands it to
    whichever asks first and says nothing to the other. The older entry keeps
    the plain name whichever order they are set up in, so an automation written
    before the second application existed goes on delivering where it did.
    """
    now = dt_util.utcnow()
    older = build_entry(APP_ID, APP_NAME, now - timedelta(days=30))
    newer = build_entry("app_zzz999", "Home-Assistant", now)

    for entry in (newer, older) if newer_first else (older, newer):
        mock_applications[entry.data["token"]] = Application(
            id=entry.data[CONF_APPLICATION_ID], name=entry.data["name"]
        )
        entry.add_to_hass(hass)

    await hass.config_entries.async_setup(older.entry_id)
    await hass.async_block_till_done()

    # The suffix comes from the newer application's id, so naming both services
    # says which entry ended up with which: the older kept the plain name.
    assert hass.services.has_service("notify", "pushcloud_home_assistant")
    assert hass.services.has_service("notify", "pushcloud_home_assistant_zzz999")
    assert older.state is ConfigEntryState.LOADED
    assert newer.state is ConfigEntryState.LOADED
