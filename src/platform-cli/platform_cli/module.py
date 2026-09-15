"""`platform module {install,uninstall,scaffold}` — docs/architecture/module-lifecycle-plan.md's
items 4/5 (platform-module-lifecycle branch, 2026-09-03). Unlike every other command in this
package, these don't go through `ctx.obj`'s `PlatformClient` at all — no login, no gateway, no
workspace. They read/write files in the git checkout they're invoked from and talk to `git`
directly (`repo.py`), per the user's own decision this branch: install/uninstall should
auto-commit and auto-push, "operating on whatever local checkout the CLI is invoked from."

`install` is now the ONE exception to "these never touch ctx.obj" (module-lifecycle-plan.md item
6, platform-module-deps branch, 2026-09-03) — but only conditionally: it reuses the same
PlatformClient main.py's root callback already builds on `ctx.obj` (same as workspace.py/
dataset.py/function.py do) to call gateway's GET /modules/check-requirements, and ONLY when the
module being installed actually declares `requires: [...]`. A module with no real dependencies —
hello-module, and any freshly-scaffolded module, since the template's own default is `requires: []`
— never touches ctx.obj at all, so it stays exactly as login-free as before this branch. See
`_check_requires`'s own docstring below for the full behavior, and `--skip-requires-check` for the
escape hatch.

`build-index` (ui-shell-plan.md item 6, feature/gateway-module-catalog branch, 2026-09-09) is a
fourth command, added later than the three above — it also never touches git (see its own
docstring), so `handle_module_errors` still covers its one failure surface (`ManifestError` from a
malformed module.yaml) without needing a third decorator.

`install`'s `--repo-url` (ui-shell-plan.md item 7's mutation mechanism, feature/gateway-module-
lifecycle-dispatch branch, 2026-09-10): an optional override for the repoURL embedded in the
generated Application, replacing `discover_repo_url()`'s `git remote get-url origin` when set. Every
plain interactive call leaves this unset and is completely unaffected — it exists for
`.github/workflows/module-lifecycle.yml`, whose `actions/checkout`-provided `origin` is the HTTPS
form, not the SSH form every self-referencing Application (and Argo CD's own configured repo
credential) actually uses; see that workflow's own comment for the full "why."

install/uninstall are the only two of these four that touch git — `scaffold` deliberately
doesn't commit anything (see its own docstring below). Both install and uninstall:
1. Resolve the repo root from the CWD (`repo.find_repo_root`).
2. Refuse to run against a dirty working tree (`repo.require_clean_worktree`) — this is the first
   platform-cli surface that commits on the operator's behalf, so it shouldn't ever sweep up
   unrelated in-progress changes into its own commit.
3. Do their actual work (validate + render, or just locate the file to remove).
4. `--dry-run` stops here, printing what *would* happen.
5. Otherwise, `repo.commit_and_push` — and print the resulting commit hash, so every run is
   self-auditing in its own terminal output without needing a separate `git log` check.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer
import yaml
from platform_sdk import PlatformClient

from platform_cli.errors import handle_api_errors, handle_module_errors
from platform_cli.manifest import (
    ManifestError,
    ModuleManifest,
    load_module_manifest,
    render_application_manifest,
)
from platform_cli.module_index import build_static_module_index
from platform_cli.repo import commit_and_push, discover_repo_url, find_repo_root, require_clean_worktree

app = typer.Typer(no_args_is_help=True)

MODULES_DIR = "src/modules"
MODULES_ENABLED_DIR = "src/modules-enabled"
CHARTS_DIR = "src/charts"


def _run_helm_template(chart_dir: Path, values_yaml: str) -> None:
    """Decision 5's optional pre-commit safety check: if `helm` is on PATH, actually render the
    chart with the computed values and abort (before anything is written or committed) if it
    fails. If `helm` isn't found, print one warning and move on — confirmed via this sandbox that
    it can't be assumed present everywhere, and install shouldn't hard-block on a box that
    doesn't have it. `helm`'s presence on homelab-dev is a live-verification item, not assumed."""
    helm = shutil.which("helm")
    if helm is None:
        typer.secho(
            "warning: `helm` not found on PATH — skipping the helm-template safety check. "
            "The generated Application will still be written and pushed; if the chart is "
            "actually broken, Argo CD will surface that as a degraded sync instead of this "
            "command catching it up front.",
            fg=typer.colors.YELLOW,
        )
        return
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(values_yaml)
        values_path = f.name
    try:
        result = subprocess.run(
            [helm, "template", str(chart_dir), "-f", values_path],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(values_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise ManifestError(f"`helm template {chart_dir}` failed:\n{result.stderr.strip()}")


@app.command("install")
@handle_api_errors
@handle_module_errors
def install(
    ctx: typer.Context,
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and render, but write/commit nothing."),
    skip_requires_check: bool = typer.Option(
        False,
        "--skip-requires-check",
        help="Skip verifying this module's `requires: [...]` are installed and healthy first.",
    ),
    repo_url_override: str | None = typer.Option(
        None,
        "--repo-url",
        help="Override the repoURL embedded in the generated Application, instead of discovering it "
        "from `git remote get-url origin`. For automated callers whose checkout's own `origin` "
        "doesn't match what Argo CD is configured to read from — see .github/workflows/"
        "module-lifecycle.yml, which passes this explicitly rather than trusting actions/checkout's "
        "HTTPS-form origin (a real, live-confirmed mismatch — see this branch's plan for the full "
        "reasoning). Every plain interactive invocation should leave this unset.",
    ),
) -> None:
    repo_root = find_repo_root(Path.cwd())
    manifest_path = repo_root / MODULES_DIR / name / "module.yaml"
    manifest = load_module_manifest(manifest_path)

    chart_dir = repo_root / CHARTS_DIR / manifest.id
    if not chart_dir.is_dir():
        raise ManifestError(
            f"{manifest_path} validates, but its chart ({chart_dir}) doesn't exist — "
            f"run `platform module scaffold {name}` first, or write the chart by hand."
        )

    _check_requires(ctx, manifest, skip_requires_check)

    repo_url = repo_url_override or discover_repo_url(repo_root)
    chart_path = f"{CHARTS_DIR}/{manifest.id}"
    application_yaml = render_application_manifest(manifest, repo_url=repo_url, chart_path=chart_path)

    # Re-derive just the values block for the helm-template check, so what's checked is exactly
    # what will be pushed, not a second independent computation of it.
    values_yaml = yaml.safe_load(application_yaml)["spec"]["source"]["helm"]["values"]
    _run_helm_template(chart_dir, values_yaml)

    if dry_run:
        typer.echo(f"--dry-run: would write {MODULES_ENABLED_DIR}/{manifest.id}.yaml:\n")
        typer.echo(application_yaml)
        return

    require_clean_worktree(repo_root)

    enabled_dir = repo_root / MODULES_ENABLED_DIR
    enabled_dir.mkdir(parents=True, exist_ok=True)
    target = enabled_dir / f"{manifest.id}.yaml"
    target.write_text(application_yaml)

    commit_hash = commit_and_push(repo_root, [target], f"install module: {manifest.id}")
    rel_target = target.relative_to(repo_root)
    typer.echo(f"Installed {manifest.id!r} — wrote and pushed {rel_target} ({commit_hash}).")
    typer.echo("Argo CD (via modules-root) will pick it up on its next reconcile.")


def _check_requires(ctx: typer.Context, manifest: ModuleManifest, skip: bool) -> None:
    """module-lifecycle-plan.md item 6 (platform-module-deps branch, 2026-09-03): before writing
    or committing anything, block install if a declared `requires: [...]` entry isn't installed
    AND healthy right now, per gateway's GET /modules/check-requirements (the one place this
    satisfied/not-satisfied comparison lives — see gateway/app/modules.py's docstring; this
    function is just the CLI-side caller, not a second implementation of the check itself).

    Two ways this is skipped entirely, both leaving `ctx.obj`'s PlatformClient completely
    untouched — no login, no gateway call, no kubectl:
    - `manifest.requires` is empty (the template's own default, and hello-module's) — the module
      genuinely has no dependency to verify.
    - `--skip-requires-check` was passed — an explicit, visibly-warned bypass, not a silent one;
      for bootstrapping the first module of a dependency chain, or installing while gateway/the
      cluster is unreachable.
    """
    if not manifest.requires:
        return
    if skip:
        typer.secho(
            f"warning: --skip-requires-check set — NOT verifying requires: {manifest.requires} "
            "are installed and healthy. If they aren't, this module may come up broken.",
            fg=typer.colors.YELLOW,
        )
        return

    client: PlatformClient = ctx.obj
    results = client.check_module_requirements(manifest.requires)
    unsatisfied = [r for r in results if not r.satisfied]
    if not unsatisfied:
        return

    typer.secho(
        f"{manifest.id!r} declares requires: {manifest.requires} — not all of them are ready:",
        fg=typer.colors.RED,
        err=True,
    )
    for r in unsatisfied:
        reason = "not installed" if r.status == "not installed" else f"status is {r.status!r}, not Healthy"
        typer.secho(f"  - {r.module_id}: {reason}", fg=typer.colors.RED, err=True)
    typer.secho(
        "Install/fix those first (`platform module install <name>`), then retry — or pass "
        "--skip-requires-check to install anyway (not recommended unless you know this module "
        "still works without them).",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


@app.command("uninstall")
@handle_module_errors
def uninstall(
    name: str,
    purge_data: bool = typer.Option(
        False, "--purge-data", help="Also print the kubectl command to delete this module's PVCs."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen, but change nothing."),
) -> None:
    repo_root = find_repo_root(Path.cwd())
    target = repo_root / MODULES_ENABLED_DIR / f"{name}.yaml"
    if not target.is_file():
        raise ManifestError(
            f"{target.relative_to(repo_root)} doesn't exist — {name!r} isn't installed "
            "(`platform module install` writes this file; nothing to remove)."
        )

    if dry_run:
        typer.echo(f"--dry-run: would remove {target.relative_to(repo_root)} and push that removal.")
        if purge_data:
            _print_purge_command(name)
        return

    require_clean_worktree(repo_root)
    target.unlink()

    message = f"uninstall module: {name}" + (" (data purge requested)" if purge_data else "")
    commit_hash = commit_and_push(repo_root, [target], message)
    rel_target = target.relative_to(repo_root)
    typer.echo(f"Uninstalled {name!r} — removed and pushed the removal of {rel_target} ({commit_hash}).")
    typer.echo(
        "Argo CD will prune the Deployment/Service on its next reconcile. Any PersistentVolumeClaim "
        "the chart marked `argocd.argoproj.io/sync-options: Delete=false` survives on purpose "
        "(ARCHITECTURE.md §3) — reinstalling gets its data back."
    )
    if purge_data:
        _print_purge_command(name)


def _print_purge_command(name: str) -> None:
    # Deliberately printed, not run: platform-cli only ever talks to git and gateway today, never
    # straight to the cluster, and PVC deletion has no undo — see this branch's plan file, decision
    # 4, and the AskUserQuestion this was confirmed with. The `sudo` prefix matches how cluster
    # access has actually worked in this session so far, not an assumption this command can verify.
    #
    # The two-step confirm-then-delete shape below (rather than just the delete command, which is
    # what this originally printed) was added after live-verifying this exact command recreated
    # the PVC it was trying to delete: run the delete before <name>'s own Application has actually
    # been pruned, and that Application's still-live selfHeal recreates the PVC right back, since
    # it's still a resource its Helm release declares. "The uninstall above has synced" can't be
    # taken on faith — Argo's automated sync after a push has repeatedly lagged past its poll
    # interval in live testing (docs/known-issues.md) — so this spells out how to actually confirm
    # it first instead of just asserting it.
    typer.echo("")
    typer.secho("--purge-data: do NOT run the delete below yet.", bold=True, fg=typer.colors.YELLOW)
    typer.echo(f"First confirm the {name!r} Application is really gone:")
    typer.echo(f"  sudo kubectl -n argocd get application {name}")
    typer.echo(
        "That must come back NotFound before you continue. If it still shows the Application, "
        "Argo hasn't actually synced the removal yet (this has repeatedly lagged well past its "
        "poll interval in live testing — see docs/known-issues.md for how to force it). Deleting "
        f"the PVC while {name!r}'s Application is still live just recreates it via that "
        "Application's own selfHeal, which is exactly what happens if you skip this check."
    )
    typer.echo("")
    typer.echo("Once it's really gone, run this yourself (platform-cli won't — no credentials, no undo):")
    typer.echo(f"  sudo kubectl delete pvc -n {name} -l platform.io/module={name}")


@app.command("scaffold")
@handle_module_errors
def scaffold(name: str) -> None:
    """Generates `src/modules/<name>/module.yaml` (from src/modules/_template/) and
    `src/charts/<name>/` (from src/charts/_template/) — ARCHITECTURE.md §3: "generates the
    modules/<name>/ skeleton (chart + module.yaml), you write the actual service." Deliberately
    does NOT commit or push, unlike install/uninstall: those two only ever toggle a
    machine-generated pointer file, the exact mechanical operation the user asked to automate:
    a scaffold is a skeleton the operator is expected to actually edit before it means anything,
    and auto-committing an empty one would be premature."""
    if not re.match(r"^[a-z0-9-]+$", name):
        raise ManifestError(
            f"module names must match ^[a-z0-9-]+$ (got {name!r}) — used as a namespace and label value."
        )

    repo_root = find_repo_root(Path.cwd())
    module_dir = repo_root / MODULES_DIR / name
    chart_dir = repo_root / CHARTS_DIR / name
    if module_dir.exists():
        raise ManifestError(f"{module_dir.relative_to(repo_root)} already exists.")
    if chart_dir.exists():
        raise ManifestError(f"{chart_dir.relative_to(repo_root)} already exists.")

    display_name = name.replace("-", " ").replace("_", " ").title()
    module_replacements = {
        "__MODULE_ID__": name,
        "__Display Name__": display_name,
        "__module_id__": name,
        "__service__": name,
        "__namespace__": name,
    }
    chart_replacements = {
        "__MODULE_ID__": name,
        "__DISPLAY_NAME__": display_name,
    }

    _copy_and_substitute(repo_root / MODULES_DIR / "_template", module_dir, module_replacements)
    _copy_and_substitute(repo_root / CHARTS_DIR / "_template", chart_dir, chart_replacements)

    typer.echo(f"Scaffolded {module_dir.relative_to(repo_root)} and {chart_dir.relative_to(repo_root)}.")
    typer.echo("Next: edit the chart to actually do something, review module.yaml, then:")
    typer.echo(f"  platform module install {name}")


@app.command("build-index")
@handle_module_errors
def build_index(
    out: Path = typer.Argument(..., help="Where to write the generated JSON module index."),
) -> None:
    """Scans every `src/modules/*/module.yaml` into a single JSON file — ui-shell-plan.md item 6,
    ARCHITECTURE.md §3's "static module index built from every modules/*/module.yaml at release
    time." The scan/validate logic itself lives in `module_index.py`'s
    `build_static_module_index()`, pure and unit-tested on its own — this command is just the thin
    CLI wrapper `build-and-push-gateway` (ci.yml) actually invokes, as a step before gateway's own
    Docker build, writing the JSON straight into gateway's build context so its image ships with a
    fresh catalog with no Dockerfile change needed. Unlike `install`/`uninstall`, this never
    touches git — it only ever writes the one file at `out`, which is gitignored and regenerated
    on every gateway build, never committed."""
    repo_root = find_repo_root(Path.cwd())
    modules = build_static_module_index(repo_root / MODULES_DIR)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"modules": modules}, indent=2) + "\n")
    typer.echo(f"Wrote {len(modules)} module(s) to {out}.")


def _copy_and_substitute(src: Path, dst: Path, replacements: dict[str, str]) -> None:
    for src_file in src.rglob("*"):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        text = src_file.read_text()
        for token, value in replacements.items():
            text = text.replace(token, value)
        dst_file.write_text(text)
