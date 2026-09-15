# Local git hooks

Client-side backstop for the branch rules in `docs/architecture/ARCHITECTURE.md` §10 — not a
substitute for GitHub branch protection, a complement to it. These only run on a machine that has
opted in (below), and anyone can bypass them with `--no-verify`, so they're a "catch my own
mistake before it leaves my laptop" tool, not an enforcement boundary. The actual boundary is
still the GitHub-side rule, once it's turned on.

## One-time setup (per clone)

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push
```

That's it — git will call these on every `commit` and `push` in this working copy from now on.
There's no way to make this run automatically on `git clone` itself (hooks aren't versioned into
history the normal way, which is exactly why `core.hooksPath` pointing at a tracked folder is the
usual workaround), so this one command is worth putting in a README/CONTRIBUTING note for future-you
or anyone else who clones the repo.

## What each hook does

- **pre-commit** — refuses to commit while checked out directly on `main`, `test`, or `dev`.
  Catches the mistake at the earliest possible point, before there's even a commit to undo.
- **pre-push** — refuses to push a ref onto `main`, `test`, or `dev` on the remote, even if a
  commit somehow ended up on one of them locally (a rebase, a `--no-verify` commit, a manual merge).
