"""Tests for the config flow.

The flow's whole job is to turn a pasted `pca_` token into an entry that knows
which application it is. Everything below is a way that can go wrong.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushcloud.api import (
    Application,
    PushCloudAuthError,
    PushCloudConnectionError,
)
from custom_components.pushcloud.const import CONF_APPLICATION_ID, DOMAIN

from .conftest import APP_ID, APP_NAME, ENTRY_TITLE, TOKEN

OTHER_TOKEN = "pca_ffffffffffffffffffffffffffffffff"


@pytest.fixture
def mock_get_application():
    """Patch the one network call the flow makes."""
    with patch(
        "custom_components.pushcloud.config_flow.PushCloudClient.async_get_application",
        new_callable=AsyncMock,
    ) as mocked:
        mocked.return_value = Application(id=APP_ID, name=APP_NAME)
        yield mocked


async def test_user_flow_creates_an_entry_titled_from_the_endpoint(
    hass: HomeAssistant, mock_get_application: AsyncMock
) -> None:
    """The user types a token and nothing else; the name comes from the API."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ENTRY_TITLE
    assert result["data"] == {
        "token": TOKEN,
        CONF_APPLICATION_ID: APP_ID,
        "name": APP_NAME,
    }
    assert result["result"].unique_id == APP_ID


async def test_invalid_token_shows_an_error_and_lets_the_user_retry(
    hass: HomeAssistant, mock_get_application: AsyncMock
) -> None:
    """A typo must not abort the flow - the form comes back with the reason."""
    mock_get_application.side_effect = PushCloudAuthError("Invalid application token")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "pca_wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_get_application.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unreachable_host_shows_cannot_connect(
    hass: HomeAssistant, mock_get_application: AsyncMock
) -> None:
    """An offline setup is a different message from a bad token."""
    mock_get_application.side_effect = PushCloudConnectionError("no route to host")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_same_application_cannot_be_added_twice(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """unique_id is the application id, so a second copy aborts.

    Note this holds even when the *token* differs - rotating a token and
    re-adding it would otherwise produce two entries for one application, and
    two notify services that fight over the same name.
    """
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": OTHER_TOKEN}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_second_application_can_be_added(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """One entry per application is the design, not a limit of one entry."""
    mock_config_entry.add_to_hass(hass)
    mock_get_application.return_value = Application(id="app_other", name="Grafana")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": OTHER_TOKEN}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "PushCloud: Grafana"


async def test_reauth_replaces_the_token(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Rotating a token in the panel is repaired by pasting the new one."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": OTHER_TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data["token"] == OTHER_TOKEN


async def test_reauth_refuses_a_token_for_a_different_application(
    hass: HomeAssistant,
    mock_get_application: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-auth must not silently repoint an entry at another application.

    Every automation naming this service would start delivering somewhere else,
    with nothing in the UI to say so. Better to refuse and make the user add
    the other application as its own entry.
    """
    mock_config_entry.add_to_hass(hass)
    mock_get_application.return_value = Application(id="app_other", name="Grafana")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": OTHER_TOKEN}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_application"
    assert mock_config_entry.data["token"] == TOKEN
