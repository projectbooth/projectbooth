"""`platform function {list,create,get,update,delete,versions,publish,promote}`
— same "thin mirror of PlatformClient" shape as dataset.py, mirrored onto
catalog-service's Function resource (platform-function-promote branch,
2026-09-03). Two differences from dataset.py worth knowing before reading
further: `update` has no `--location-uri` equivalent (FunctionUpdate has no
extra field beyond name/visibility/description — module_path only ever
changes via `publish`), and there are two function-specific subcommands
(`versions`, `publish`, `promote`) with no dataset equivalent at all.
"""
from __future__ import annotations

import typer
from platform_sdk import PlatformClient, Visibility

from platform_cli.errors import handle_api_errors

app = typer.Typer(no_args_is_help=True)


@app.command("list")
@handle_api_errors
def list_functions(ctx: typer.Context) -> None:
    client: PlatformClient = ctx.obj
    functions = client.list_functions()
    if not functions:
        typer.echo("No functions visible to you in this workspace.")
        return
    for f in functions:
        typer.echo(
            f"{f.id}  {f.name:<30} {f.visibility.value:<10} v{f.current_version:<3} {f.created_by or '-'}"
        )


@app.command("create")
@handle_api_errors
def create_function(
    ctx: typer.Context,
    name: str,
    visibility: Visibility = typer.Option(Visibility.PRIVATE, "--visibility", case_sensitive=False),
    description: str | None = typer.Option(None, "--description"),
    module_path: str | None = typer.Option(None, "--module-path"),
) -> None:
    client: PlatformClient = ctx.obj
    function = client.create_function(
        name, visibility=visibility, description=description, module_path=module_path
    )
    typer.echo(f"Created function {function.name!r} ({function.id}).")


@app.command("get")
@handle_api_errors
def get_function(ctx: typer.Context, function_id: str) -> None:
    client: PlatformClient = ctx.obj
    f = client.get_function(function_id)
    typer.echo(f"id               : {f.id}")
    typer.echo(f"name             : {f.name}")
    typer.echo(f"visibility       : {f.visibility.value}")
    typer.echo(f"description      : {f.description or '-'}")
    typer.echo(f"current_version  : {f.current_version}")
    typer.echo(f"module_path      : {f.module_path or '-'}")
    typer.echo(f"created_by       : {f.created_by or '-'}")
    typer.echo(f"created_at       : {f.created_at}")
    typer.echo(f"updated_at       : {f.updated_at}")


@app.command("update")
@handle_api_errors
def update_function(
    ctx: typer.Context,
    function_id: str,
    description: str | None = typer.Option(None, "--description"),
    visibility: Visibility | None = typer.Option(None, "--visibility", case_sensitive=False),
) -> None:
    # Same "only send flags the user actually passed" rule dataset.py's
    # update_dataset follows, for the same reason (catalog-service's PATCH
    # leaves unset fields alone — see app/schemas.py's _EntityUpdate).
    fields = {
        k: v
        for k, v in {
            "description": description,
            "visibility": visibility.value if visibility else None,
        }.items()
        if v is not None
    }
    if not fields:
        typer.echo("Nothing to update — pass at least one of --description/--visibility.")
        raise typer.Exit(code=1)
    client: PlatformClient = ctx.obj
    function = client.update_function(function_id, **fields)
    typer.echo(f"Updated function {function.name!r} ({function.id}).")


@app.command("delete")
@handle_api_errors
def delete_function(ctx: typer.Context, function_id: str) -> None:
    client: PlatformClient = ctx.obj
    client.delete_function(function_id)
    typer.echo(f"Deleted function {function_id}.")


@app.command("versions")
@handle_api_errors
def list_function_versions(ctx: typer.Context, function_id: str) -> None:
    client: PlatformClient = ctx.obj
    versions = client.list_function_versions(function_id)
    if not versions:
        typer.echo("No published versions yet — run `platform function publish` first.")
        return
    for v in versions:
        typer.echo(f"v{v.version}  {v.module_path}  published_by={v.published_by or '-'}  {v.published_at}")


@app.command("publish")
@handle_api_errors
def publish_function(
    ctx: typer.Context,
    function_id: str,
    signature: str = typer.Option(
        ..., "--signature", help="The function's signature, e.g. 'def f(x: int) -> int'."
    ),
    module_path: str = typer.Option(
        ..., "--module-path", help="Where the code actually lives, e.g. 'pkg.mod.f'."
    ),
    docstring: str | None = typer.Option(None, "--docstring"),
    published_by: str | None = typer.Option(
        None, "--published-by", help="Defaults to the token's own user_id if omitted."
    ),
) -> None:
    # Not the @platform.function decorator's job (that extracts signature/
    # docstring automatically from real code, and doesn't exist yet — see
    # ARCHITECTURE.md §3/§4 and this branch's own plan for why that's out
    # of scope here). This is the explicit, user-supplied-strings CLI path
    # against catalog-service's already-built /publish endpoint.
    client: PlatformClient = ctx.obj
    version = client.publish_function(
        function_id,
        signature=signature,
        module_path=module_path,
        docstring=docstring,
        published_by=published_by,
    )
    typer.echo(f"Published {function_id} as v{version.version} ({module_path}).")


@app.command("promote")
@handle_api_errors
def promote_function(ctx: typer.Context, function_id: str) -> None:
    # No flags — catalog-service's /promote is unconditional (always sets
    # visibility=public; see app/routers/functions.py's own docstring on
    # why it's one-directional). Demoting back is `function update
    # FUNCTION_ID --visibility workspace`, the plain PATCH path above.
    client: PlatformClient = ctx.obj
    function = client.promote_function(function_id)
    typer.echo(f"Promoted {function.name!r} ({function.id}) to {function.visibility.value}.")
