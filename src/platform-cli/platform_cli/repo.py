"""Git plumbing for `platform module install/uninstall` — the first platform-cli commands that
touch git at all (platform-module-lifecycle branch, 2026-09-03). Every prior command only ever
talked HTTP to gateway; this is a different kind of operation, per the user's own decision this
branch: `install`/`uninstall` should auto-commit AND auto-push, "operating on whatever local
checkout the CLI is invoked from" rather than requiring any separate config of their own — so
these helpers all resolve everything (repo root, remote URL, push target) from the CWD's existing
git state, the same checkout and credentials the operator already uses for everything else.

Plain `subprocess` calls to the `git` binary, not GitPython or similar — git is already a hard
requirement for anyone using this repo at all, and every operation here is exactly what a person
would type by hand, so there's no benefit to a heavier dependency standing between this code and
what it actually does.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class RepoError(Exception):
    """Something about the git checkout `platform module install/uninstall` was invoked from
    isn't in a state this command can safely proceed in — not inside a repo, a dirty working
    tree, no configured push target, or git itself failing. Always carries a message that's
    already readable on its own."""


def _run_git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def find_repo_root(start: Path) -> Path:
    """Walks up from `start` (normally Path.cwd()) to find the git repo root — the same
    `git rev-parse --show-toplevel` a person would run by hand. Every file this branch's
    commands touch (src/modules/, src/modules-enabled/, src/charts/) is resolved relative to
    this, not to `start` itself, so the command works the same whether it's invoked from the
    repo root or from three directories down."""
    try:
        top = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    except RepoError as exc:
        raise RepoError(
            f"{start} doesn't look like it's inside a git checkout — "
            "`platform module install`/`uninstall` need to run from inside a clone of the "
            "platform repo, since they commit and push directly to it."
        ) from exc
    return Path(top)


def require_clean_worktree(repo_root: Path) -> None:
    """Refuses to proceed if the working tree has any uncommitted changes at all — install/
    uninstall's own commit should contain exactly the one generated/removed file, never whatever
    else happened to be in progress in the same checkout. New: no prior platform-cli command
    touches git, so nothing before this branch needed this guard."""
    status = _run_git(["status", "--porcelain"], cwd=repo_root)
    if status:
        raise RepoError(
            "the working tree has uncommitted changes — commit or stash them first. "
            "`platform module install`/`uninstall` make exactly one commit of their own and "
            "refuse to bundle in anything else already in progress:\n" + status
        )


def discover_repo_url(repo_root: Path) -> str:
    """`git remote get-url origin`, embedded verbatim into a generated Application's `repoURL`
    (manifest.py's render_application_manifest). This is the one place this branch's design
    improves on the hand-authored "self-referencing apps" convention (argocd/README.md), which
    hardcodes repoURL with a standing "forkers: edit this" comment — generated content doesn't
    need that caveat, it can just ask git."""
    try:
        return _run_git(["remote", "get-url", "origin"], cwd=repo_root)
    except RepoError as exc:
        raise RepoError(
            "couldn't read the `origin` remote's URL (`git remote get-url origin`) — "
            "the generated Application manifest needs a real repoURL to point Argo CD at."
        ) from exc


def commit_and_push(repo_root: Path, paths: list[Path], message: str) -> str:
    """`git add <paths>`, `git commit -m <message>`, `git push` — no explicit remote/branch,
    relying on the checkout's already-configured upstream (erroring clearly, not guessing or
    forcing one, if none is set). Returns the new commit's short hash so install/uninstall can
    print it — every commit this makes is self-auditing in its own terminal output, not something
    the operator has to go check `git log` to confirm."""
    _run_git(["add", *[str(p) for p in paths]], cwd=repo_root)
    _run_git(["commit", "-m", message], cwd=repo_root)
    try:
        _run_git(["push"], cwd=repo_root)
    except RepoError as exc:
        raise RepoError(
            f"committed locally ({message!r}) but `git push` failed — the commit is safe in "
            "your local history, but nothing changed on the remote (and so nothing changed in "
            "the cluster). Set an upstream (`git push -u origin <branch>`) or resolve the push "
            f"error yourself, then push manually:\n{exc}"
        ) from exc
    return _run_git(["rev-parse", "--short", "HEAD"], cwd=repo_root)
