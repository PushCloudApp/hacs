"""Tests for the PushCloud HTTP client.

This module needs no Home Assistant at all - it is the one part of the
integration that can be tested purely against `aioresponses`, which is why it
is built first.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.pushcloud.api import (
    APPLICATION_DEVICES_URL,
    APPLICATIONS_ME_URL,
    MESSAGES_URL,
    PushCloudAuthError,
    PushCloudClient,
    PushCloudConnectionError,
    PushCloudError,
    PushCloudPlanError,
    PushCloudRateLimitError,
)

from .conftest import APP_ID, APP_NAME, TOKEN


def error_body(code: str, message: str) -> dict:
    """The PushCloud error envelope. `code` is the stable half."""
    return {"error": {"code": code, "message": message}}


@pytest.fixture
async def client():
    """A client over a real session, with aioresponses intercepting the wire."""
    async with aiohttp.ClientSession() as session:
        yield PushCloudClient(session, TOKEN)


async def test_get_application_returns_id_and_name(client: PushCloudClient) -> None:
    """The happy path: a live token names its own application."""
    with aioresponses() as mocked:
        mocked.get(
            APPLICATIONS_ME_URL,
            payload={
                "application": {
                    "id": APP_ID,
                    "name": APP_NAME,
                    "icon_url": None,
                    "default_priority": 0,
                    "default_sound": None,
                    "enabled": True,
                }
            },
        )
        application = await client.async_get_application()

    assert application.id == APP_ID
    assert application.name == APP_NAME


async def test_get_application_sends_the_token_as_a_bearer(
    client: PushCloudClient,
) -> None:
    """The credential goes in the Authorization header, not a query string."""
    with aioresponses() as mocked:
        mocked.get(
            APPLICATIONS_ME_URL,
            payload={"application": {"id": APP_ID, "name": APP_NAME}},
        )
        await client.async_get_application()

    request = next(iter(mocked.requests.values()))[0]
    assert request.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_send_posts_the_payload(client: PushCloudClient) -> None:
    """A send is a JSON POST to the native messages endpoint."""
    with aioresponses() as mocked:
        mocked.post(MESSAGES_URL, status=201, payload={"message": {"id": "msg_1"}})
        await client.async_send({"message": "hello", "title": "hi"})

    request = next(iter(mocked.requests.values()))[0]
    assert request.kwargs["json"] == {"message": "hello", "title": "hi"}
    assert request.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("code", ["INVALID_APP_TOKEN", "MISSING_TOKEN"])
async def test_401_is_an_auth_error(client: PushCloudClient, code: str) -> None:
    """A rotated or mistyped token is the re-auth case, not a transient one."""
    with aioresponses() as mocked:
        mocked.get(
            APPLICATIONS_ME_URL, status=401, payload=error_body(code, "Invalid token")
        )
        with pytest.raises(PushCloudAuthError):
            await client.async_get_application()


async def test_429_is_a_rate_limit_error_carrying_the_message(
    client: PushCloudClient,
) -> None:
    """Quota exhaustion and rate limiting share a status.

    They are distinguishable only by the message, and nothing in the
    integration needs to tell them apart - but the user does, so the server's
    wording has to survive into the automation trace.
    """
    with aioresponses() as mocked:
        mocked.post(
            MESSAGES_URL,
            status=429,
            payload=error_body(
                "QUOTA_EXCEEDED", "The free plan allows 3000 messages a month"
            ),
        )
        with pytest.raises(PushCloudRateLimitError) as err:
            await client.async_send({"message": "hello"})

    assert "3000 messages a month" in str(err.value)


@pytest.mark.parametrize("code", ["PLAN_REQUIRED", "PLAN_LIMIT", "ACCOUNT_SUSPENDED"])
async def test_403_is_a_plan_error_carrying_the_message(
    client: PushCloudClient, code: str
) -> None:
    """403 is never an auth failure here.

    A suspended account holds a perfectly valid token, and so does a free
    account sending critical priority. Mapping either to a re-auth prompt would
    ask the user to paste the same working token again and again.
    """
    with aioresponses() as mocked:
        mocked.post(
            MESSAGES_URL,
            status=403,
            payload=error_body(code, "Critical priority requires Pro"),
        )
        with pytest.raises(PushCloudPlanError) as err:
            await client.async_send({"message": "hello", "priority": 2})

    assert "Critical priority requires Pro" in str(err.value)


async def test_500_is_a_connection_error(client: PushCloudClient) -> None:
    """A server fault is worth retrying, so it joins the transport failures."""
    with aioresponses() as mocked:
        mocked.post(MESSAGES_URL, status=502, body="bad gateway")
        with pytest.raises(PushCloudConnectionError):
            await client.async_send({"message": "hello"})


async def test_transport_failure_is_a_connection_error(
    client: PushCloudClient,
) -> None:
    """DNS, resets and timeouts all look the same to a caller."""
    with aioresponses() as mocked:
        mocked.get(APPLICATIONS_ME_URL, exception=aiohttp.ClientError("boom"))
        with pytest.raises(PushCloudConnectionError):
            await client.async_get_application()


async def test_timeout_is_a_connection_error(client: PushCloudClient) -> None:
    """A timeout is not an aiohttp error, so it needs catching separately."""
    with aioresponses() as mocked:
        mocked.get(APPLICATIONS_ME_URL, exception=TimeoutError())
        with pytest.raises(PushCloudConnectionError):
            await client.async_get_application()


async def test_other_4xx_is_a_plain_error_carrying_the_message(
    client: PushCloudClient,
) -> None:
    """A 400 has no dedicated class - the caller only needs the wording."""
    with aioresponses() as mocked:
        mocked.post(
            MESSAGES_URL,
            status=400,
            payload=error_body("VALIDATION_ERROR", "message is required"),
        )
        with pytest.raises(PushCloudError) as err:
            await client.async_send({})

    assert "message is required" in str(err.value)


async def test_unparseable_error_body_still_raises(client: PushCloudClient) -> None:
    """An error page from a proxy is not JSON. It must not become a KeyError."""
    with aioresponses() as mocked:
        mocked.get(APPLICATIONS_ME_URL, status=401, body="<html>nope</html>")
        with pytest.raises(PushCloudAuthError):
            await client.async_get_application()


@pytest.mark.parametrize(
    "response",
    [
        {"body": "<html>hello from your captive portal</html>"},
        {"payload": {}},
        {"payload": {"application": None}},
        {"payload": {"application": "app_abc123"}},
        {"payload": {"application": {"name": APP_NAME}}},
        {"payload": {"application": {"id": APP_ID}}},
        {"payload": {"application": {"id": 17, "name": APP_NAME}}},
    ],
)
async def test_a_200_of_the_wrong_shape_is_a_connection_error(
    client: PushCloudClient, response: dict
) -> None:
    """Success is a status *and* a body. Neither alone is enough.

    Everything above is a 200 that no amount of retrying will make useful right
    now, and none of it is a reason to send the user back to the token field.
    What it must never be is a KeyError or a TypeError: this module's whole
    contract is that callers see the exceptions defined here and nothing else.
    """
    with aioresponses() as mocked:
        mocked.get(APPLICATIONS_ME_URL, status=200, **response)
        with pytest.raises(PushCloudConnectionError):
            await client.async_get_application()


async def test_list_devices_returns_the_targets_in_order(
    client: PushCloudClient,
) -> None:
    """The device list, as the options flow draws it into a picker."""
    with aioresponses() as mocked:
        mocked.get(
            APPLICATION_DEVICES_URL,
            payload={
                "devices": [
                    {
                        "id": "dev_1",
                        "name": "Bobby's iPhone",
                        "slug": "bobbys-iphone",
                        "platform": "ios",
                    },
                    {
                        "id": "dev_2",
                        "name": "Study Mac",
                        "slug": "study-mac",
                        "platform": "web",
                    },
                ]
            },
        )
        devices = await client.async_list_devices()

    # Server order, not re-sorted here: it already orders by slug, and a second
    # opinion about ordering is a second thing to keep in step.
    assert [device.slug for device in devices] == ["bobbys-iphone", "study-mac"]
    assert devices[0].name == "Bobby's iPhone"
    assert devices[0].id == "dev_1"


async def test_a_device_targets_by_slug_and_falls_back_to_its_id(
    client: PushCloudClient,
) -> None:
    """`slug` is what a send names, but it is null on an unslugged device.

    The id is accepted anywhere a slug is, so the fallback keeps a device
    registered before slugs existed selectable rather than hiding it.
    """
    with aioresponses() as mocked:
        mocked.get(
            APPLICATION_DEVICES_URL,
            payload={
                "devices": [
                    {
                        "id": "dev_1",
                        "name": "Slugged",
                        "slug": "slugged",
                        "platform": "ios",
                    },
                    {
                        "id": "dev_2",
                        "name": "Unslugged",
                        "slug": None,
                        "platform": "ios",
                    },
                ]
            },
        )
        devices = await client.async_list_devices()

    assert [device.target for device in devices] == ["slugged", "dev_2"]


async def test_list_devices_labels_a_nameless_device_by_its_target(
    client: PushCloudClient,
) -> None:
    """`name` is nullable too, and a picker row with a blank label is unpickable."""
    with aioresponses() as mocked:
        mocked.get(
            APPLICATION_DEVICES_URL,
            payload={
                "devices": [
                    {"id": "dev_1", "name": None, "slug": "kitchen", "platform": "ios"}
                ]
            },
        )
        devices = await client.async_list_devices()

    assert devices[0].label == "kitchen"


async def test_list_devices_accepts_an_account_with_no_devices(
    client: PushCloudClient,
) -> None:
    """Nobody has signed in on a phone yet. An empty list, not an error."""
    with aioresponses() as mocked:
        mocked.get(APPLICATION_DEVICES_URL, payload={"devices": []})
        assert await client.async_list_devices() == []


async def test_list_devices_sends_the_token_as_a_bearer(
    client: PushCloudClient,
) -> None:
    """The same `pca_` credential, and no other."""
    with aioresponses() as mocked:
        mocked.get(APPLICATION_DEVICES_URL, payload={"devices": []})
        await client.async_list_devices()

    request = next(iter(mocked.requests.values()))[0]
    assert request.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_list_devices_maps_a_401_to_an_auth_error(
    client: PushCloudClient,
) -> None:
    """A rotated token on this path is the same problem it is on every other."""
    with aioresponses() as mocked:
        mocked.get(
            APPLICATION_DEVICES_URL,
            status=401,
            payload=error_body("INVALID_APP_TOKEN", "Invalid application token"),
        )
        with pytest.raises(PushCloudAuthError):
            await client.async_list_devices()


@pytest.mark.parametrize(
    "response",
    [
        {"body": "<html>hello from your captive portal</html>"},
        {"payload": {}},
        {"payload": {"devices": None}},
        {"payload": {"devices": {"dev_1": "Phone"}}},
        {"payload": {"devices": ["dev_1"]}},
        {"payload": {"devices": [{"name": "Phone", "slug": "phone"}]}},
        {"payload": {"devices": [{"id": 17, "name": "Phone", "slug": "phone"}]}},
        {"payload": {"devices": [{"id": "dev_1", "name": 17, "slug": "phone"}]}},
        {"payload": {"devices": [{"id": "dev_1", "name": "Phone", "slug": 17}]}},
    ],
)
async def test_a_device_list_of_the_wrong_shape_is_a_connection_error(
    client: PushCloudClient, response: dict
) -> None:
    """Same contract as `async_get_application`: our exceptions, never a TypeError.

    A partial list is deliberately not a thing this returns. Dropping the rows
    it could not read would present a picker missing a device with nothing
    saying so, and someone would conclude their phone had unregistered.
    """
    with aioresponses() as mocked:
        mocked.get(APPLICATION_DEVICES_URL, status=200, **response)
        with pytest.raises(PushCloudConnectionError):
            await client.async_list_devices()
