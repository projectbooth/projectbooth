"""One exception type for every non-2xx response — catalog-service's error
body shape is consistently `{"detail": "..."}` (FastAPI's default, and every
handler in that service either raises HTTPException or lets one bubble up),
so there's exactly one place that needs to know that shape, not one per
client method."""
from __future__ import annotations


class PlatformAPIError(Exception):
    """Raised for any catalog-service response with status >= 400.

    status_code and detail are pulled straight out of the response so
    calling code can branch on them (e.g. `except PlatformAPIError as e: if
    e.status_code == 404: ...`) without parsing the message string.
    """

    def __init__(self, status_code: int, detail: str, *, method: str, url: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.url = url
        super().__init__(f"{method} {url} -> {status_code}: {detail}")


class KeycloakAdminError(Exception):
    """Raised for anything that goes wrong on `KeycloakAdminClient`'s behalf
    — a missing/rejected client secret, a port-forward that never came up, a
    username Keycloak doesn't know about, an unexpected Admin API status.
    One exception type here too, same reasoning as PlatformAPIError above,
    even though the failure modes are more varied (this talks to kubectl and
    a port-forward, not just HTTP) — platform-cli still only needs to catch
    one thing."""


class PlatformLoginError(Exception):
    """Raised for anything that goes wrong during `platform login`'s
    device-flow (keycloak_login.py) — the device-authorization request
    itself failing, an unexpected token-endpoint response, or the RFC 8628
    poll loop ending in `access_denied`/`expired_token`. Also raised by a
    silent token refresh (PlatformClient's near-expiry check) when the
    refresh_token itself has been revoked or expired — same "something about
    getting/renewing a token went wrong" category, so it's the same
    exception type rather than a second one platform-cli would need to catch
    separately."""


class NotAuthenticatedError(Exception):
    """Raised by `PlatformClient` when no credentials file exists yet (or
    the one on disk is missing/unreadable/corrupt) — the fix is always
    "run `platform login`," never a retry, so this is deliberately a
    distinct type from PlatformLoginError: that one fires *during* `platform
    login` itself, this one fires when some other command discovers there's
    nothing to authenticate with in the first place. Keeping them separate
    lets platform-cli's error handler print the right one-line fix for each
    case instead of one generic "auth failed" message."""
