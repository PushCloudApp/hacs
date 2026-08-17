"""Tests for the options flow - choosing which devices get the notifications.

The flow exists because the alternative is typing a device slug from memory, and
a mistyped one only shows up as a failed automation later.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushcloud.api import (
    Application,
    Device,
    PushCloudAuthError,
    PushCloudConnectionError,
)
from custom_components.pushcloud.const import CONF_DEVICES, DOMAIN

from .conftest import APP_ID, APP_NAME

PHONE = Device(id="dev_1", name="Bobby's iPhone", slug="bobbys-iphone")
MAC = Device(id="dev_2", name="Study Mac", slug="study-mac")
UNSLUGGED = Device(id="dev_3", name="Old Tablet", slug=None)


@pytest.fixture
async def entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry):
    """A loaded entry, which is what the options flow is opened from."""
    with patch(
        "custom_components.pushcloud.PushCloudClient.async_get_application",
        new_callable=AsyncMock,
        return_value=Application(id=APP_ID, name=APP_NAME),
    ):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


@pytest.fixture
def mock_list_devices():
    """Patch the one network call the options flow makes."""
    with patch(
        "custom_components.pushcloud.config_flow.PushCloudClient.async_list_devices",
        new_callable=AsyncMock,
    ) as mocked:
        mocked.return_value = [PHONE, MAC]
        yield mocked


def options_of(result: dict) -> list[dict]:
    """The picker's rows, dug out of the selector in the returned schema."""
    schema = result["data_schema"].schema
    selector = next(schema[key] for key in schema if str(key) == CONF_DEVICES)
    return selector.config["options"]


async def open_flow(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Open the options flow, as the Configure button does."""
    return await hass.config_entries.options.async_init(entry.entry_id)


async def test_the_picker_lists_the_account_s_devices(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """Names to read, slugs to send. The user never sees the slug."""
    result = await open_flow(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert options_of(result) == [
        {"value": "bobbys-iphone", "label": "Bobby's iPhone"},
        {"value": "study-mac", "label": "Study Mac"},
    ]


async def test_a_device_with_no_slug_is_offered_by_id(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """It is still a valid target, so hiding it would lose a real device."""
    mock_list_devices.return_value = [UNSLUGGED]

    result = await open_flow(hass, entry)

    assert options_of(result) == [{"value": "dev_3", "label": "Old Tablet"}]


async def test_choosing_devices_saves_them_as_options(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """The saved value is the send targets, ready to join with commas."""
    result = await open_flow(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICES: ["study-mac"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_DEVICES: ["study-mac"]}


async def test_choosing_nothing_clears_the_option(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """Un-ticking everything goes back to sending to the whole account.

    The picker has to allow this. Without it the only way back from "just the
    iPhone" would be deleting the entry and re-adding the token.
    """
    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["bobbys-iphone"]}
    )

    result = await open_flow(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICES: []}
    )

    assert entry.options == {CONF_DEVICES: []}


async def test_the_current_choice_is_ticked_when_the_form_opens(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """Reopening the form shows what is set, not a blank slate."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["study-mac"]}
    )

    result = await open_flow(hass, entry)

    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == CONF_DEVICES)
    assert key.description["suggested_value"] == ["study-mac"]


async def test_a_device_that_has_since_gone_is_dropped_from_the_choice(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """A saved slug whose device was deleted cannot be pre-ticked.

    The selector rejects a value that is not one of its options, so leaving it in
    would make the form unsubmittable - a user who deleted a phone in the panel
    would find they could no longer change this setting at all. Dropping it
    silently is self-healing: opening the form and saving fixes the entry.
    """
    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["study-mac", "sold-on-ebay"]}
    )

    result = await open_flow(hass, entry)

    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == CONF_DEVICES)
    assert key.description["suggested_value"] == ["study-mac"]


async def test_the_form_rejects_a_device_that_is_not_offered(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """The guard behind the dropping above: an unknown target never gets saved."""
    result = await open_flow(hass, entry)

    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_DEVICES: ["not-a-device"]}
        )


async def test_an_account_with_no_devices_says_so(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """Nobody has signed in on a phone yet.

    An empty picker would look broken, and there is nothing to choose from, so
    the flow says why instead of showing one.
    """
    mock_list_devices.return_value = []

    result = await open_flow(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PushCloudConnectionError("no route to host"), "cannot_connect"),
        (PushCloudAuthError("Invalid application token"), "invalid_auth"),
    ],
)
async def test_a_failed_fetch_aborts_rather_than_showing_an_empty_picker(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_list_devices: AsyncMock,
    error: Exception,
    reason: str,
) -> None:
    """There is no form to show errors on - the form *is* the device list.

    Aborting says what went wrong. Showing an empty picker would invite someone
    to save "no devices", which means the opposite of what they would intend.
    """
    mock_list_devices.side_effect = error

    result = await open_flow(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_the_flow_uses_the_entry_s_own_token(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """Read from `entry.data`, not from a client on `runtime_data`.

    An entry that failed to load has no `runtime_data`, and being unable to
    change this setting because the network was down at startup would be a
    pointless dead end.
    """
    hass.config_entries.async_update_entry(entry, options={})
    assert await open_flow(hass, entry)


async def test_devices_are_not_listed_until_the_flow_is_opened(
    hass: HomeAssistant, entry: MockConfigEntry, mock_list_devices: AsyncMock
) -> None:
    """Setting the entry up must not cost a request nobody asked for."""
    assert mock_list_devices.call_count == 0

    await open_flow(hass, entry)

    assert mock_list_devices.call_count == 1


async def test_the_options_flow_is_offered_on_the_entry(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """No Configure button means the setting is unreachable in the UI."""
    assert entry.supports_options
