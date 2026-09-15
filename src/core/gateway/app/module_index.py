"""Static module-catalog index — the read half of ui-shell-plan.md item 6 (ARCHITECTURE.md §3:
"platform-gateway reads a static module index built from every modules/*/module.yaml at release
time... so it can list modules that aren't installed yet"). The write half lives in platform-cli
(`platform_cli/module_index.py`'s `build_static_module_index()`, wired up as
`platform module build-index <out-path>`) — a new step in `build-and-push-gateway` (ci.yml) runs
that command before the Docker build, writing the JSON `load_static_module_index()` below reads to
`settings.static_module_index_path` (default `app/module_catalog.json`, inside the Docker build
context gateway's own Dockerfile already `COPY app ./app`s — no Dockerfile change needed; see that
setting's own comment in config.py). `app/modules.py`'s `GET /modules/catalog` is the only caller.

"Missing file -> empty list, not a crash or a 503" is deliberate: it's the same "file exists -> real
value, else a real, distinct default" story `app/argocd.py`/`app/main.py` already tell twice for
their own mounted-file checks, but a genuinely different *kind* of failure than
`ArgoCDUnavailableError` — a missing static index means the release pipeline didn't generate one (a
real bug worth the warning log below), but not one that should take down proxying, `GET /modules`,
or `GET /modules/check-requirements`, none of which read this file at all. It also means a local
`docker build` run without first running the generator by hand (see README.md's "Running it
locally") still boots a working gateway, just with an empty Add-ons catalog rather than a crash.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


def load_static_module_index() -> list[dict]:
    """Returns the list of `{id, displayName, icon, navPath, requires, optional}` dicts
    `platform_cli.module_index.build_static_module_index()` wrote — module.yaml's own camelCase
    field names, deliberately unconverted here; `app/modules.py`'s `/modules/catalog` route is what
    does the snake_case translation into gateway's own response shape, the same split of concerns
    the generator's own docstring describes (a source-of-truth artifact, not a response shape).
    """
    path = Path(settings.static_module_index_path)
    if not path.is_file():
        logger.warning(
            "static module index not found at %s — treating the catalog as empty. A real "
            "deployment always has this (build-and-push-gateway's CI step generates it before "
            "building the image); see README.md's 'Running it locally' for generating it by hand.",
            path,
        )
        return []
    try:
        body = json.loads(path.read_text())
        return body["modules"]
    except (ValueError, KeyError, TypeError):
        logger.warning("static module index at %s is malformed — treating the catalog as empty.", path)
        return []
