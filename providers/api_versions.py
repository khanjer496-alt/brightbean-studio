"""Telling the operators that a platform has stopped serving the API version we pin.

A platform that versions its API (LinkedIn's ``LinkedIn-Version`` header) retires a version
about a year after releasing it, and every call then fails at once. The provider recognises
that response, fails the request with ``ApiVersionSunsetError``, and calls
``report_version_sunset`` so staff hear about it before customers do.
The update procedure is in ``docs/launch/platform-api-versions.md``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def report_version_sunset(platform_name: str, version: str, setting: str) -> None:
    """Log a critical line and notify staff, at most once every few hours per platform and version."""
    message = (
        f"{platform_name} no longer accepts API version {version}. Every {platform_name} request fails "
        f"until {setting} is set to a current version (docs/launch/platform-api-versions.md)."
    )
    logger.critical(message)
    try:
        from apps.notifications.operators import alert_staff

        alert_staff(
            key=f"api-version-sunset:{platform_name}:{version}",
            title=f"{platform_name} API version {version} has been retired",
            body=message,
        )
    except Exception:
        logger.exception("Could not notify staff about the %s API version sunset", platform_name)
