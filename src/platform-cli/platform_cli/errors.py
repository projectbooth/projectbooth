"""One decorator, applied to every command, instead of a try/except block
repeated in each of them — turns platform_sdk's exception types into a
readable stderr message and exit code 1, not a raw traceback a CLI user
shouldn't have to read. All four land on the same decorator (rather than one
per exception type) because from platform-cli's point of view they're all
the same shape of problem — "something about talking to gateway/Keycloak
didn't work" — just from different backends or different failure stages:

  - PlatformAPIError    — gateway (proxying to catalog-service) rejected a
                           request with a non-2xx status.
  - KeycloakAdminError  — `workspace invite`'s direct-to-Keycloak Admin API
                           call failed.
  - PlatformLoginError  — `platform login`'s device-flow (or a silent
                           near-expiry refresh triggered by some other
                           command) failed.
  - NotAuthenticatedError — some other command found no usable saved
                           credentials and needs `platform login` run first.

`handle_module_errors`, added platform-module-lifecycle branch (2026-09-03), is a second,
deliberately separate decorator for `platform module install/uninstall/scaffold` — those commands
don't talk to gateway/Keycloak at all (git and local files only), so they get their own decorator
for their own failure surface (ManifestError, RepoError) rather than one more except clause bolted
onto this HTTP-shaped one.

functools.wraps matters here for a reason beyond the usual "preserve
__name__/__doc__": Typer builds each command's CLI signature (its options
and arguments) via inspect.signature() on the decorated function, and
inspect.signature() only sees through a wrapper to the original function
if __wrapped__ is set — which functools.wraps sets automatically. Skip it
and every `--flag` this decorator sits in front of silently disappears
from `--help` and stops being accepted at all, not an error you'd notice
until you tried to use one.
"""
from __future__ import annotations

import functools
from collections.abc import Callable

import typer
from platform_sdk import KeycloakAdminError, NotAuthenticatedError, PlatformAPIError, PlatformLoginError

from platform_cli.manifest import ManifestError
from platform_cli.repo import RepoError


def handle_api_errors[F: Callable](func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PlatformAPIError as exc:
            # "gateway error," not "catalog-service error" — platform-cli
            # only ever talks to gateway now (see platform_sdk/client.py's
            # module docstring); the detail message itself may have
            # originated deeper in catalog-service, but that's transparent
            # to a caller who never addresses catalog-service directly.
            message = f"gateway error ({exc.status_code}): {exc.detail}"
            typer.secho(message, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except KeycloakAdminError as exc:
            typer.secho(f"Keycloak error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except NotAuthenticatedError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except PlatformLoginError as exc:
            typer.secho(f"Login failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    return wrapper  # type: ignore[return-value]


def handle_module_errors[F: Callable](func: F) -> F:
    """`platform module install/uninstall/scaffold`'s own decorator (platform-module-lifecycle
    branch, 2026-09-03) — a separate one from handle_api_errors, not one more except clause added
    there, because these commands don't talk to gateway/Keycloak at all: they read module.yaml
    (manifest.py's ManifestError) and touch git (repo.py's RepoError), a genuinely different
    failure surface that deserves its own decorator rather than overloading the HTTP-shaped one."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ManifestError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except RepoError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    return wrapper  # type: ignore[return-value]
