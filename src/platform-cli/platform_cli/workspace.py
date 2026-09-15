"""`platform workspace {list,create,get,invite}` — list/create/get mirror
PlatformClient's workspace methods 1:1, same "thin" principle platform-sdk's
own client.py states up front. `invite` is different: it doesn't touch
catalog-service or the shared PlatformClient on ctx.obj at all — it talks to
Keycloak directly via KeycloakAdminClient, because membership is
Keycloak-group territory, not catalog data (see catalog-service's
app/routers/workspaces.py docstring, and platform_sdk.keycloak_admin's own
module docstring for the full design).
"""
from __future__ import annotations

import typer
from platform_sdk import KeycloakAdminClient, PlatformClient, Role
from platform_sdk.config import SDKSettings

from platform_cli.errors import handle_api_errors

app = typer.Typer(no_args_is_help=True)


@app.command("list")
@handle_api_errors
def list_workspaces(ctx: typer.Context) -> None:
    client: PlatformClient = ctx.obj
    workspaces = client.list_workspaces()
    if not workspaces:
        typer.echo("No workspaces.")
        return
    for w in workspaces:
        typer.echo(f"{w.id}  {w.name:<20} {w.display_name}")


@app.command("create")
@handle_api_errors
def create_workspace(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Short name, e.g. 'nfl-betting'."),
    display_name: str = typer.Option(None, "--display-name", help="Defaults to NAME.title()."),
) -> None:
    client: PlatformClient = ctx.obj
    workspace = client.create_workspace(name, display_name or name.title())
    typer.echo(f"Created workspace {workspace.name!r} ({workspace.id}).")


@app.command("get")
@handle_api_errors
def get_workspace(ctx: typer.Context, workspace_id: str) -> None:
    client: PlatformClient = ctx.obj
    workspace = client.get_workspace(workspace_id)
    typer.echo(f"id           : {workspace.id}")
    typer.echo(f"name         : {workspace.name}")
    typer.echo(f"display_name : {workspace.display_name}")
    typer.echo(f"created_at   : {workspace.created_at}")


@app.command("invite")
@handle_api_errors
def invite(
    username: str = typer.Argument(
        ..., help="An EXISTING Keycloak username — this does not create users. See --help below."
    ),
    workspace: str = typer.Option(
        None, "--workspace", "-w", help="Defaults to PLATFORM_WORKSPACE / 'personal', same as other commands."
    ),
    role: Role = typer.Option(
        Role.VIEWER, "--role", case_sensitive=False, help="owner, editor, or viewer. Defaults to viewer."
    ),
) -> None:
    """Add an existing Keycloak user to a workspace's owner/editor/viewer
    group. Doesn't create the user — src/core/auth/realm-platform.yaml sets
    registrationAllowed: false and seeds no users on purpose, so the
    username has to already exist in Keycloak some other way (its own admin
    console, for now). Manages its own short-lived port-forward to Keycloak
    for the duration of this one command — nothing to set up first, but it
    does need `kubectl` + the same sudo access every other script in this
    repo assumes, and PLATFORM_KEYCLOAK_CLIENT_SECRET set (see
    bootstrap/keycloak-bootstrap-cli-client.sh).

    Unlike every other command in this file, this one does NOT use the
    shared PlatformClient on ctx.obj — see this module's docstring.
    """
    settings = SDKSettings()
    target_workspace = workspace or settings.workspace
    with KeycloakAdminClient() as admin:
        result = admin.invite(username, workspace=target_workspace, role=role)
    verb = "Created" if result.group_created else "Reused"
    typer.echo(f"{username!r} added to {result.group_path} ({verb} that Keycloak group).")
