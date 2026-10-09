"""Gemeinsame Fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Lädt custom_components/ in jedem Test mit ``hass``."""
    return


@pytest.fixture(autouse=True)
def zeroconf_mocked(request):
    """Die Integration hängt von ``zeroconf`` ab; im Test ohne echtes mDNS.

    Ohne Attrappe startet HA das echte zeroconf, das Discovery-Flows für
    Fremdintegrationen (Cast …) auslöst. Reine Bibliothekstests ohne
    ``hass`` brauchen sie nicht.
    """
    if "hass" in request.fixturenames:
        request.getfixturevalue("mock_async_zeroconf")
