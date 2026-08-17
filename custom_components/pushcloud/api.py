"""The PushCloud HTTP client.

The only module here that knows PushCloud speaks HTTP. Everything above it
deals in typed exceptions and a small dataclass, so no caller ever inspects a
status code.

One credential throughout: a `pca_` application token. It can send from its own
application and it can identify itself, and that is the whole of its power - it
cannot read a message, list a device, or touch the account. That is why the
integration asks for one instead of a `pck_` API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import aiohttp

BASE_URL: Final = "https://pushcloud.app"
APPLICATIONS_ME_URL: Final = f"{BASE_URL}/v1/applications/me"
APPLICATION_DEVICES_URL: Final = f"{BASE_URL}/v1/applications/me/devices"
MESSAGES_URL: Final = f"{BASE_URL}/v1/messages"

TIMEOUT: Final = aiohttp.ClientTimeout(total=15)

# Error codes that mean "this credential is wrong", as opposed to "this account
# is not allowed to do that". Only these should ever produce a re-auth prompt.
_AUTH_STATUS: Final = 401
_RATE_LIMIT_STATUS: Final = 429
_PLAN_STATUS: Final = 403


class PushCloudError(Exception):
    """Base for every failure this client reports.

    Raised directly for a 4xx with no more specific meaning - a validation
    error, say. The caller wants only the server's wording in that case.
    """


class PushCloudAuthError(PushCloudError):
    """The token is not one PushCloud recognises, or no longer is."""


class PushCloudRateLimitError(PushCloudError):
    """Too many messages, or too many this month. Carries the server message."""


class PushCloudPlanError(PushCloudError):
    """The account may not do this: plan limit, or suspension.

    Deliberately not an auth error even though it is a 403. The token is valid;
    re-prompting for it would send the user round a loop pasting a credential
    that was never the problem.
    """


class PushCloudConnectionError(PushCloudError):
    """The request did not get an answer worth reading: transport, or 5xx."""


@dataclass(frozen=True)
class Application:
    """The application a token belongs to, as `/v1/applications/me` reports it."""

    id: str
    name: str


@dataclass(frozen=True)
class Device:
    """One device an account can be sent to, as `/v1/applications/me/devices`
    reports it.

    Both `name` and `slug` are nullable server-side, which is why neither is used
    bare - see `target` and `label`.
    """

    id: str
    name: str | None
    slug: str | None

    @property
    def target(self) -> str:
        """What to put in the `device` field of a send.

        The slug when there is one: it survives a phone's push token rotating,
        where the id does not - the app registers a new row and migrates the slug
        across. The id is the fallback because a device registered before slugs
        existed has none, and an id is accepted anywhere a slug is.
        """
        return self.slug or self.id

    @property
    def label(self) -> str:
        """What to show in a picker. Never blank, so no row is unidentifiable."""
        return self.name or self.target


class PushCloudClient:
    """Talks to PushCloud with one application token."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        """Store the shared session and the credential."""
        self._session = session
        self._token = token

    async def async_get_application(self) -> Application:
        """Ask the token which application it is.

        `GET /v1/applications/me`. This is both the config flow's validation
        step and the source of the entry's title - a send token cannot be
        checked any other way without actually notifying somebody.
        """
        data = await self._request("get", APPLICATIONS_ME_URL)
        application = data.get("application")

        # A 200 is not a promise of the right shape. A captive portal, a proxy,
        # or an edge that is halfway through a deploy all answer 200 with
        # something else entirely, and `_body` has already flattened anything
        # unparseable to `{}`. Indexing straight into that would put a KeyError
        # through callers this module promised only its own exceptions to.
        if not isinstance(application, dict) or not all(
            isinstance(application.get(field), str) for field in ("id", "name")
        ):
            raise PushCloudConnectionError(
                "PushCloud answered without naming an application"
            )

        return Application(id=application["id"], name=application["name"])

    async def async_list_devices(self) -> list[Device]:
        """List the account's enabled devices, to pick send targets from.

        `GET /v1/applications/me/devices`. Reachable by a send token because it
        tells one nothing it could not already learn: a send naming a device that
        does not exist answers 400 listing every enabled slug.

        Shape-checked as strictly as `async_get_application`, and for the same
        reason. A row this could not read is a failure, not a row to skip - a
        picker quietly missing a device would read as a phone that had signed
        itself out.
        """
        data = await self._request("get", APPLICATION_DEVICES_URL)
        devices = data.get("devices")

        if not isinstance(devices, list) or not all(
            isinstance(device, dict)
            and isinstance(device.get("id"), str)
            and isinstance(device.get("name"), str | None)
            and isinstance(device.get("slug"), str | None)
            for device in devices
        ):
            raise PushCloudConnectionError(
                "PushCloud answered without a readable list of devices"
            )

        # Server order kept: it already sorts by slug, and re-sorting here would
        # be a second opinion to keep in step with the first.
        return [
            Device(id=device["id"], name=device.get("name"), slug=device.get("slug"))
            for device in devices
        ]

    async def async_send(self, payload: dict[str, Any]) -> None:
        """Send one message. `POST /v1/messages`."""
        await self._request("post", MESSAGES_URL, json=payload)

    async def _request(
        self, method: str, url: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make the call and turn anything that went wrong into a typed error."""
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=TIMEOUT,
            ) as response:
                # Read the body before branching: an error envelope carries the
                # message that has to reach the automation trace, and a body
                # left unread on a failed request is a warning in the log.
                body = await self._body(response)
                if response.status >= 400:
                    raise self._error(response.status, body)
                return body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PushCloudConnectionError(str(err) or type(err).__name__) from err

    @staticmethod
    async def _body(response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse the body, tolerating anything that is not our JSON.

        A proxy or an edge error page answers with HTML, and a 204 with
        nothing at all. Neither should surface as a parse error on top of the
        real failure.
        """
        try:
            parsed = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _error(status: int, body: dict[str, Any]) -> PushCloudError:
        """Map one failed response onto the right exception.

        Branching on status rather than on `error.code` keeps this correct for
        codes that do not exist yet: a new 403 is still a plan problem, and a
        new 401 is still an auth problem.
        """
        error = body.get("error") or {}
        message = error.get("message") or f"PushCloud returned HTTP {status}"

        if status == _AUTH_STATUS:
            return PushCloudAuthError(message)
        if status == _RATE_LIMIT_STATUS:
            return PushCloudRateLimitError(message)
        if status == _PLAN_STATUS:
            return PushCloudPlanError(message)
        if status >= 500:
            return PushCloudConnectionError(message)
        return PushCloudError(message)
