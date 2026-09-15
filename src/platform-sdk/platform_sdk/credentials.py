"""Local credentials cache for `platform login`'s device-flow tokens —
`~/.config/platform/credentials.json` (respects `XDG_CONFIG_HOME`, matching
the XDG Base Directory spec every other well-behaved Linux CLI follows, so
this doesn't invent its own dotfile convention).

Deliberately separate from config.py's `SDKSettings`: that's read-only
settings resolved fresh from env vars on every construction; this is
read-WRITE state that `platform login` creates once and `PlatformClient`
reads back across completely separate process invocations — the whole point
of a one-shot CLI staying "logged in" between commands without re-running
the device flow every time.

Written with `os.open(..., 0o600)` at creation time, not written-then-
chmod'd after. A write-then-chmod leaves a real (if brief) window where the
file exists world-readable before the permission fix lands; putting 0o600
in the `open()` flags themselves means there's no window at all — the file
is never briefly more permissive than its final mode.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from platform_sdk.models import TokenSet


def credentials_path() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / "platform" / "credentials.json"


def save_credentials(token_set: TokenSet) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "access_token": token_set.access_token,
            "refresh_token": token_set.refresh_token,
            "expires_at": token_set.expires_at.isoformat(),
            "preferred_username": token_set.preferred_username,
        },
        indent=2,
    )
    # 0o600 from the moment the file exists — see module docstring for why
    # this isn't write-then-chmod. O_TRUNC so a re-login (the normal
    # `platform login` re-run case) cleanly replaces stale contents rather
    # than leaving trailing bytes from a longer previous file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)


def load_credentials() -> TokenSet | None:
    """Returns None — not an exception — whether the file is simply absent
    (never logged in) or present but unreadable/corrupt (killed mid-write,
    hand-edited, wrong shape). Callers in either case need the same fix —
    `platform login` — so PlatformClient's NotAuthenticatedError doesn't
    need to distinguish "missing" from "unreadable" to give the right
    on-screen instruction.
    """
    path = credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            preferred_username=data.get("preferred_username"),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def clear_credentials() -> None:
    """Not wired up to a `platform logout` command this pass — nothing in
    the approved platform-gateway-auth plan calls for one (see that plan's
    "Explicitly deferred" section on refresh-token rotation/revocation UX
    beyond re-running `platform login`). Exists now for tests, and so a
    future `platform logout` has a one-line implementation rather than
    needing to know credentials.json's path/shape itself.
    """
    credentials_path().unlink(missing_ok=True)
