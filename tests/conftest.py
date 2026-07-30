"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pushcloud.const import CONF_APPLICATION_ID, DOMAIN

TOKEN = "pca_0123456789abcdef0123456789abcdef"
APP_ID = "app_abc123"
APP_NAME = "Home Assistant"
ENTRY_TITLE = f"PushCloud: {APP_NAME}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load custom_components/ in every test.

    Without this the integration is invisible to Home Assistant and every test
    fails with "Integration 'pushcloud' not found", which reads like a bug in
    the integration rather than a missing fixture.
    """
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry as the config flow would have created it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=ENTRY_TITLE,
        unique_id=APP_ID,
        data={"token": TOKEN, CONF_APPLICATION_ID: APP_ID, "name": APP_NAME},
    )
