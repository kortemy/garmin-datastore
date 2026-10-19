"""Garmin source adapter (FR-23): throttled calls with errors mapped to engine outcomes."""

import random
import time
from typing import Any

from garminconnect import Garmin
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)


class SourceError(Exception):
    """Base for errors the engine reacts to."""


class RateLimited(SourceError):
    """Garmin returned 429: stop the tick and cool down the account."""


class Blocked(SourceError):
    """Garmin returned 403 / a bot challenge: stop and cool down longer."""


class AuthFailed(SourceError):
    """The token is no longer accepted: the account needs `gds auth`."""


class Transient(SourceError):
    """Anything else (5xx, timeouts, odd payloads): retry this unit later."""


class Invalid(SourceError):
    """A write's arguments were rejected before anything was sent."""


def map_error(e: Exception) -> Exception:
    if isinstance(e, SourceError):
        return e
    if isinstance(e, GarminConnectTooManyRequestsError):
        return RateLimited(str(e)[:200])
    if isinstance(e, GarminConnectAuthenticationError):
        return AuthFailed(str(e)[:200])
    if isinstance(e, GarminConnectConnectionError) and "403" in str(e):
        return Blocked(str(e)[:200])
    return Transient(f"{type(e).__name__}: {str(e)[:200]}")


class Source:
    """One Garmin session for one tick. Every request goes through `call`."""

    def __init__(self, client: Garmin, min_interval_s: float = 1.0):
        self.client = client
        self.min_interval_s = min_interval_s
        self.requests = 0
        self._last = 0.0

    def _wait(self) -> None:
        gap = self.min_interval_s * random.uniform(0.85, 1.25)
        sleep_for = self._last + gap - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._last = time.monotonic()

    def call(self, method: str, *args: Any) -> Any:
        self._wait()
        self.requests += 1
        try:
            return getattr(self.client, method)(*args)
        except GarminConnectNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            raise map_error(e) from e

    def write(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """A write (M5). Unlike `call`, a 404 is an error: the target doesn't exist.
        The library's own argument checks (ValueError) surface as `Invalid`."""
        self._wait()
        self.requests += 1
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except ValueError as e:
            raise Invalid(str(e)[:300]) from e
        except GarminConnectNotFoundError as e:
            raise Transient(f"not found: {str(e)[:200]}") from e
        except Exception as e:  # noqa: BLE001
            raise map_error(e) from e
