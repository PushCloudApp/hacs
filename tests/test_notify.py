"""Tests for the notify service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.notify import (
    ATTR_DATA,
    ATTR_MESSAGE,
    ATTR_TITLE,
)
from homeassistant.components.notify import (
    DOMAIN as NOTIFY_DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushcloud.api import (
    Application,
    PushCloudAuthError,
    PushCloudConnectionError,
    PushCloudError,
    PushCloudPlanError,
    PushCloudRateLimitError,
)

from .conftest import APP_ID, APP_NAME

# slugify("PushCloud: Home Assistant"). This is what a user types into an
# automation, so it is part of the contract and worth asserting literally.
SERVICE = "pushcloud_home_assistant"


@pytest.fixture
async def send(hass: HomeAssistant, mock_config_entry: MockConfigEntry):
    """Set up the entry and hand back the patched send call."""
    with (
        patch(
            "custom_components.pushcloud.PushCloudClient.async_get_application",
            new_callable=AsyncMock,
            return_value=Application(id=APP_ID, name=APP_NAME),
        ),
        patch(
            "custom_components.pushcloud.notify.PushCloudClient.async_send",
            new_callable=AsyncMock,
        ) as mocked_send,
    ):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        yield mocked_send


async def call(hass: HomeAssistant, **kwargs) -> None:
    """Call the notify service the way an automation would."""
    await hass.services.async_call(NOTIFY_DOMAIN, SERVICE, kwargs, blocking=True)


async def test_the_service_is_named_after_the_application(
    hass: HomeAssistant, send: AsyncMock
) -> None:
    """One entry, one service, named so it cannot collide with another domain."""
    assert hass.services.has_service(NOTIFY_DOMAIN, SERVICE)


async def test_message_and_title(hass: HomeAssistant, send: AsyncMock) -> None:
    """The two arguments the notify service supplies itself."""
    await call(hass, **{ATTR_MESSAGE: "It works.", ATTR_TITLE: "Backup"})

    assert send.call_args.args[0] == {"message": "It works.", "title": "Backup"}


async def test_title_is_omitted_when_not_given(
    hass: HomeAssistant, send: AsyncMock
) -> None:
    """No title means no field, not an empty one.

    Home Assistant defaults the title to "Home Assistant"; sending that would
    put a redundant word at the top of every notification. Leaving it out lets
    the application's own name do that job on the phone.
    """
    await call(hass, **{ATTR_MESSAGE: "It works."})

    assert send.call_args.args[0] == {"message": "It works."}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("priority", 1),
        ("sound", "chime"),
        ("url", "https://example.com"),
        ("url_title", "Open the dashboard"),
    ],
)
async def test_data_keys_map_one_to_one(
    hass: HomeAssistant, send: AsyncMock, key: str, value: object
) -> None:
    """Each supported `data:` key becomes the same-named API field."""
    await call(hass, **{ATTR_MESSAGE: "hi", ATTR_DATA: {key: value}})

    assert send.call_args.args[0][key] == value


async def test_unknown_data_keys_are_ignored(
    hass: HomeAssistant, send: AsyncMock
) -> None:
    """A key we do not support must not reach the API or fail the call.

    Rejecting would break an automation over a harmless typo, and forwarding
    would let the server reject it for us with a worse message.
    """
    await call(
        hass, **{ATTR_MESSAGE: "hi", ATTR_DATA: {"priority": 1, "nonsense": "x"}}
    )

    assert send.call_args.args[0] == {"message": "hi", "priority": 1}


async def test_401_on_send_starts_reauth(
    hass: HomeAssistant, send: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The token was rotated in the panel. Ask for the new one."""
    send.side_effect = PushCloudAuthError("Invalid application token")

    with pytest.raises(HomeAssistantError):
        await call(hass, **{ATTR_MESSAGE: "hi"})
    await hass.async_block_till_done()

    assert any(mock_config_entry.async_get_active_flows(hass, {"reauth"}))


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (PushCloudRateLimitError, "The free plan allows 3000 messages a month"),
        (PushCloudPlanError, "Critical priority requires Pro"),
        (PushCloudConnectionError, "no route to host"),
        (PushCloudError, "message is required"),
    ],
)
async def test_failures_reach_the_automation_trace_intact(
    hass: HomeAssistant, send: AsyncMock, error: type[Exception], message: str
) -> None:
    """The server's wording is the only thing that explains the failure.

    A free account sending `priority: 2` gets a hard 403 from the native
    endpoint rather than the clamping the compat paths do, so this message is
    the user's only clue about what to change.
    """
    send.side_effect = error(message)

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, **{ATTR_MESSAGE: "hi", ATTR_DATA: {"priority": 2}})

    assert message in str(err.value)
