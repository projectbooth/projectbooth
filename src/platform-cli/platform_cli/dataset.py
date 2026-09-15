"""`platform dataset {list,create,get,update,delete}` — same "thin mirror
of PlatformClient" shape as workspace.py.
"""
from __future__ import annotations

import typer
from platform_sdk import PlatformClient, Visibility

from platform_cli.errors import handle_api_errors

app = typer.Typer(no_args_is_help=True)


@app.command("list")
@handle_api_errors
def list_datasets(ctx: typer.Context) -> None:
    client: PlatformClient = ctx.obj
    datasets = client.list_datasets()
    if not datasets:
        typer.echo("No datasets visible to you in this workspace.")
        return
    for d in datasets:
        typer.echo(f"{d.id}  {d.name:<30} {d.visibility.value:<10} {d.created_by or '-'}")


@app.command("create")
@handle_api_errors
def create_dataset(
    ctx: typer.Context,
    name: str,
    visibility: Visibility = typer.Option(Visibility.PRIVATE, "--visibility", case_sensitive=False),
    description: str | None = typer.Option(None, "--description"),
    location_uri: str | None = typer.Option(None, "--location-uri"),
) -> None:
    client: PlatformClient = ctx.obj
    dataset = client.create_dataset(
        name, visibility=visibility, description=description, location_uri=location_uri
    )
    typer.echo(f"Created dataset {dataset.name!r} ({dataset.id}).")


@app.command("get")
@handle_api_errors
def get_dataset(ctx: typer.Context, dataset_id: str) -> None:
    client: PlatformClient = ctx.obj
    d = client.get_dataset(dataset_id)
    typer.echo(f"id            : {d.id}")
    typer.echo(f"name          : {d.name}")
    typer.echo(f"visibility    : {d.visibility.value}")
    typer.echo(f"description   : {d.description or '-'}")
    typer.echo(f"location_uri  : {d.location_uri or '-'}")
    typer.echo(f"created_by    : {d.created_by or '-'}")
    typer.echo(f"created_at    : {d.created_at}")
    typer.echo(f"updated_at    : {d.updated_at}")


@app.command("update")
@handle_api_errors
def update_dataset(
    ctx: typer.Context,
    dataset_id: str,
    description: str | None = typer.Option(None, "--description"),
    visibility: Visibility | None = typer.Option(None, "--visibility", case_sensitive=False),
    location_uri: str | None = typer.Option(None, "--location-uri"),
) -> None:
    # Only send fields the user actually passed — matches catalog-service's
    # own PATCH semantics (app/schemas.py's _EntityUpdate: unset fields are
    # left alone, not overwritten with None). Sending {"description": None}
    # for a flag the user never touched would silently blank it out.
    fields = {
        k: v
        for k, v in {
            "description": description,
            "visibility": visibility.value if visibility else None,
            "location_uri": location_uri,
        }.items()
        if v is not None
    }
    if not fields:
        typer.echo("Nothing to update — pass at least one of --description/--visibility/--location-uri.")
        raise typer.Exit(code=1)
    client: PlatformClient = ctx.obj
    dataset = client.update_dataset(dataset_id, **fields)
    typer.echo(f"Updated dataset {dataset.name!r} ({dataset.id}).")


@app.command("delete")
@handle_api_errors
def delete_dataset(ctx: typer.Context, dataset_id: str) -> None:
    client: PlatformClient = ctx.obj
    client.delete_dataset(dataset_id)
    typer.echo(f"Deleted dataset {dataset_id}.")
