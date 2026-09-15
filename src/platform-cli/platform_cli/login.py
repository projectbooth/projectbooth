"""`platform login` — the device-flow login command (RFC 8628). Prints a
verification URL for the user to open in a browser, polls until they
approve (or deny it / it expires), and saves the resulting tokens to
`~/.config/platform/credentials.json` for every other `platform` command to
read back (see `platform_sdk.credentials`).

Deliberately NOT built on the shared `PlatformClient` on `ctx.obj` the rest
of this package's commands use (see main.py's root callback) — constructs
its own `KeycloakLoginFlow` instead. Logging in is what *creates* the
credentials `PlatformClient` later depends on; building this command on top
of `PlatformClient` would be circular. Same "this one command talks to
Keycloak directly, not gateway/catalog-service" shape as `workspace invite`
(see workspace.py's own module docstring) — a different Keycloak client and
a different grant type, but the same reason for standing apart from
`ctx.obj`.

Not registered as its own `typer.Typer` sub-app the way `workspace`/
`dataset` are (this is a single flat command, `platform login`, with no
sub-commands of its own) — main.py registers the `login` function below
directly via `app.command()`.
"""
from __future__ import annotations

import typer
from platform_sdk import KeycloakLoginFlow
from platform_sdk.credentials import save_credentials

from platform_cli.errors import handle_api_errors


@handle_api_errors
def login() -> None:
    """Log in via your browser (OAuth device flow) and save credentials for
    every other `platform` command to use. Safe to re-run any time — a
    fresh login always replaces whatever was saved before, expired or not.
    """
    with KeycloakLoginFlow() as flow:
        device_auth = flow.start_device_authorization()

        if device_auth.verification_uri_complete:
            typer.echo(f"Open this URL to log in: {device_auth.verification_uri_complete}")
        else:
            # RFC 8628 makes verification_uri_complete optional — fall back
            # to the two-step form (open the plain URL, then type the code)
            # if a Keycloak build/config ever omits it, rather than assuming
            # it's always present.
            typer.echo(f"Open {device_auth.verification_uri} and enter code: {device_auth.user_code}")
        typer.echo("Waiting for approval in your browser...")

        # save_credentials happens after this `with` block closes (not
        # inside it) — no reason to hold the port-forward open a moment
        # longer than it takes to get the tokens back.
        token_set = flow.poll_for_token(device_auth)

    save_credentials(token_set)
    who = token_set.preferred_username or "you"
    typer.secho(f"Logged in as {who}.", fg=typer.colors.GREEN)
