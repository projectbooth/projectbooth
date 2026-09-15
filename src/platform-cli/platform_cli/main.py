"""The `platform` command's entry point. Root callback builds exactly one
PlatformClient per invocation and hangs it on the Click/Typer context
(`ctx.obj`) — every subcommand (in workspace.py, dataset.py, ...) pulls it
back out rather than constructing its own, so there's one place auth/config
resolution happens, not one per command.

CLI flags here are deliberately thin passthroughs — None by default, letting
PlatformClient's own constructor precedence (explicit arg > PLATFORM_* env
var > built-in default; see platform_sdk/config.py and client.py) do the
actual resolution. This file doesn't re-implement env var reading itself,
so there's exactly one definition of "what does unset mean," not two that
could disagree.

`--user`/`--role` REMOVED (2026-09-02, platform-gateway-auth branch) — this
is the breaking-change part of that branch, not an oversight. Identity and
role are no longer anything a caller declares from the client side; gateway
derives both from the caller's verified Keycloak token (see
platform_sdk/client.py's module docstring). There's no flag that could
still do anything useful here, so removing them (rather than leaving them
silently ignored) is the honest option — `platform --user alice ...` now
fails with Typer's own "no such option" instead of quietly doing nothing.
`--catalog-url` similarly RENAMED to `--gateway-url` — PlatformClient talks
to platform-gateway now, not catalog-service directly (see config.py's
gateway_url docstring for the same rename applied to its env var).

Constructing a PlatformClient here does NOT require being logged in — the
credentials check is lazy, on the first actual request a command makes (see
client.py's `_ensure_token`) — so `platform login` itself, and `--help`,
both work with no credentials on disk yet. `module.py`'s `uninstall`/
`scaffold` never touch `ctx.obj` at all — they don't talk to gateway, so
the lazy PlatformClient built here is simply unused for them, same as it is
for `--help`. `install` is the one exception (module-lifecycle-plan.md item
6, platform-module-deps branch, 2026-09-03): it reuses this same client to
call gateway's GET /modules/check-requirements, but ONLY when the module
being installed declares a non-empty `requires: [...]` — see module.py's
own module docstring and `_check_requires` for the full behavior.
"""
from __future__ import annotations

import typer
from platform_sdk import PlatformClient

from platform_cli.dataset import app as dataset_app
from platform_cli.errors import handle_api_errors
from platform_cli.function import app as function_app
from platform_cli.login import login as login_command
from platform_cli.module import app as module_app
from platform_cli.workspace import app as workspace_app

app = typer.Typer(
    name="platform",
    help="ARCHITECTURE.md §4/§10's platform CLI — talks to platform-gateway (which proxies to "
    "catalog-service) via platform-sdk.",
    no_args_is_help=True,
)
app.add_typer(workspace_app, name="workspace", help="Manage workspaces.")
app.add_typer(dataset_app, name="dataset", help="Manage datasets.")
app.add_typer(function_app, name="function", help="Manage functions.")
app.add_typer(module_app, name="module", help="Manage modules.")
app.command("login", help="Log in via your browser (OAuth device flow).")(login_command)


@app.callback()
def main(
    ctx: typer.Context,
    gateway_url: str | None = typer.Option(
        None, "--gateway-url", help="Overrides PLATFORM_GATEWAY_URL / the built-in default."
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", "-w", help="Overrides PLATFORM_WORKSPACE / the built-in default."
    ),
) -> None:
    client = PlatformClient(gateway_url=gateway_url, workspace=workspace)
    # Click's context teardown hook, not a try/finally wrapped around every
    # single command — one registration here covers all of them, and fires
    # whether the command succeeded, failed, or raised typer.Exit.
    ctx.call_on_close(client.close)
    ctx.obj = client


@app.command()
@handle_api_errors
def me(ctx: typer.Context) -> None:
    """Who does gateway think I am, based on my verified token — not
    catalog-service directly anymore (see platform_sdk/client.py's module
    docstring)."""
    client: PlatformClient = ctx.obj
    principal = client.me()
    typer.echo(f"workspace : {principal.workspace_name} ({principal.workspace_id})")
    typer.echo(f"user      : {principal.user_id}")
    typer.echo(f"role      : {principal.role}")
