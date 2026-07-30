"""Tests for entry setup and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushcloud.api import (
    Application,
    PushCloudAuthError,
    PushCloudConnectionError,
    PushCloudPlanError,
)

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
