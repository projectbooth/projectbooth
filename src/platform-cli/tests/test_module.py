"""`platform module {install,uninstall,scaffold}` — real temporary git repos, not mocked git
calls, matching this repo's own preference for exercising real behavior over mocking (same
reasoning as catalog-service's migration/cascade tests). Each test gets a fresh bare "origin" repo
plus a working clone seeded with copies of this monorepo's own `src/modules/_template` and
`src/charts/_template` (the real templates, not fakes) — `git_repo`'s docstring below has the
full setup. `monkeypatch.chdir` puts each invocation's CWD inside that working clone, exactly
like a real operator running `platform module ...` from inside their checkout.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from platform_sdk import ModuleRequirementStatus
from typer.testing import CliRunner

from platform_cli.module import app

runner = CliRunner()

# This file lives at src/platform-cli/tests/test_module.py — two parents up is src/, where the
# real _template directories this branch built actually live. Copying the real ones (not
# reimplementing fakes here) means these tests fail if the templates themselves ever break.
REPO_SRC = Path(__file__).resolve().parents[2]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Sets up: a bare `origin.git` (standing in for GitHub) and a working clone `work/` with
    `origin` already remoted, `src/modules/_template` and `src/charts/_template` copied in from
    this monorepo's real ones, and one initial commit already pushed — so the working tree starts
    clean, same precondition every real `platform module install/uninstall` run needs. Yields
    `(repo_root, origin_url)`. Chdir's the test into `repo_root` for the duration.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(origin)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=work)
    _git(["config", "user.name", "Test"], cwd=work)
    _git(["remote", "add", "origin", str(origin)], cwd=work)

    (work / "src" / "modules").mkdir(parents=True)
    (work / "src" / "charts").mkdir(parents=True)
    shutil.copytree(REPO_SRC / "modules" / "_template", work / "src" / "modules" / "_template")
    shutil.copytree(REPO_SRC / "charts" / "_template", work / "src" / "charts" / "_template")
    (work / "README.md").write_text("test repo\n")

    _git(["add", "-A"], cwd=work)
    _git(["commit", "-m", "initial"], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)

    monkeypatch.chdir(work)
    return work, str(origin)


def _commit_all(repo_root: Path, message: str) -> None:
    _git(["add", "-A"], cwd=repo_root)
    _git(["commit", "-m", message], cwd=repo_root)
    _git(["push"], cwd=repo_root)


def _head(repo_root: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=repo_root).stdout.strip()


def _remote_head(repo_root: Path) -> str:
    return _git(["rev-parse", "origin/main"], cwd=repo_root).stdout.strip()


# --- scaffold -----------------------------------------------------------------------------


def test_scaffold_generates_module_and_chart(git_repo):
    repo_root, _ = git_repo
    result = runner.invoke(app, ["scaffold", "hello"])
    assert result.exit_code == 0, result.output

    module_yaml = (repo_root / "src/modules/hello/module.yaml").read_text()
    assert "id: hello" in module_yaml
    assert "displayName: Hello" in module_yaml
    assert "navPath: /hello" in module_yaml
    assert "__MODULE_ID__" not in module_yaml

    chart_yaml = (repo_root / "src/charts/hello/Chart.yaml").read_text()
    assert "name: hello" in chart_yaml
    assert (repo_root / "src/charts/hello/templates/_helpers.tpl").is_file()

    # Deliberately doesn't commit (decision 8) — the scaffold is left for the operator to edit.
    # git collapses an entirely-untracked new directory to one `?? dir/` line by default (no
    # -uall), so check for the directory, not the individual file inside it.
    status = _git(["status", "--porcelain"], cwd=repo_root).stdout
    assert "src/modules/hello/" in status
    assert _head(repo_root) == _remote_head(repo_root)  # nothing pushed either


def test_scaffold_refuses_if_module_already_exists(git_repo):
    repo_root, _ = git_repo
    assert runner.invoke(app, ["scaffold", "dup"]).exit_code == 0
    result = runner.invoke(app, ["scaffold", "dup"])
    assert result.exit_code == 1
    assert "already exists" in result.output


# --- install --------------------------------------------------------------------------------


def _scaffold_and_commit(repo_root: Path, name: str) -> None:
    assert runner.invoke(app, ["scaffold", name]).exit_code == 0
    _commit_all(repo_root, f"add {name} module")


def test_install_writes_commits_and_pushes(git_repo):
    repo_root, origin_url = git_repo
    _scaffold_and_commit(repo_root, "hello")

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 0, result.output

    generated = repo_root / "src/modules-enabled/hello.yaml"
    assert generated.is_file()
    content = generated.read_text()
    assert "name: hello" in content
    assert f"repoURL: {origin_url}" in content
    assert "targetRevision: dev" in content
    assert "path: src/charts/hello" in content
    assert "namespace: hello" in content
    assert "resources-finalizer.argocd.argoproj.io" in content
    # ui-shell-plan.md item 4 (feature/gateway-module-registry, 2026-09-08) — displayName/icon/
    # navPath from module.yaml propagated onto the generated Application as annotations, gateway's
    # GET /modules reads these back out.
    assert 'platform.io/display-name: "Hello"' in content
    assert 'platform.io/icon: "puzzle"' in content
    assert 'platform.io/nav-path: "/hello"' in content

    assert _head(repo_root) == _remote_head(repo_root)  # actually pushed


def test_install_repo_url_override_replaces_discovered_url(git_repo):
    # ui-shell-plan.md item 7's mutation mechanism (feature/gateway-module-lifecycle-dispatch,
    # 2026-09-10) — module-lifecycle.yml passes this explicitly since actions/checkout's own
    # `origin` is the HTTPS form, not the SSH form every self-referencing Application expects.
    repo_root, origin_url = git_repo
    _scaffold_and_commit(repo_root, "hello")

    override = "git@github.com:DougallPercival/opendataplatform.git"
    result = runner.invoke(
        app, ["install", "hello", "--skip-requires-check", "--repo-url", override]
    )
    assert result.exit_code == 0, result.output

    content = (repo_root / "src/modules-enabled/hello.yaml").read_text()
    assert f"repoURL: {override}" in content
    assert origin_url not in content


def test_install_wires_placement_from_module_yaml(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    module_yaml = repo_root / "src/modules/hello/module.yaml"
    module_yaml.write_text(
        module_yaml.read_text()
        + "\nplacement:\n  role: compute\n  tolerations:\n    - key: platform.io/role\n"
        "      operator: Equal\n      value: compute\n      effect: NoSchedule\n"
    )
    _commit_all(repo_root, "add placement")

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 0, result.output
    content = (repo_root / "src/modules-enabled/hello.yaml").read_text()
    assert "role: compute" in content
    assert "value: compute" in content


def test_install_dry_run_writes_nothing(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    before = _head(repo_root)

    result = runner.invoke(app, ["install", "hello", "--dry-run", "--skip-requires-check"])
    assert result.exit_code == 0, result.output
    assert "would write" in result.output
    assert not (repo_root / "src/modules-enabled/hello.yaml").exists()
    assert _head(repo_root) == before


def test_install_rejects_module_yaml_with_unknown_field(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    module_yaml = repo_root / "src/modules/hello/module.yaml"
    module_yaml.write_text(module_yaml.read_text() + "\nsomeTypo: oops\n")
    _commit_all(repo_root, "typo")

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 1
    assert "failed validation" in result.output
    assert not (repo_root / "src/modules-enabled/hello.yaml").exists()


def test_install_requires_clean_worktree(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    (repo_root / "stray.txt").write_text("uncommitted\n")

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 1
    assert "uncommitted changes" in result.output
    assert not (repo_root / "src/modules-enabled/hello.yaml").exists()


def test_install_requires_chart_to_exist(git_repo):
    repo_root, _ = git_repo
    (repo_root / "src/modules/orphan").mkdir(parents=True)
    module_yaml = (repo_root / "src/modules/_template/module.yaml").read_text()
    module_yaml = module_yaml.replace("__MODULE_ID__", "orphan").replace("__Display Name__", "Orphan")
    module_yaml = module_yaml.replace("__module_id__", "orphan").replace("__service__", "orphan")
    module_yaml = module_yaml.replace("__namespace__", "orphan")
    (repo_root / "src/modules/orphan/module.yaml").write_text(module_yaml)
    _commit_all(repo_root, "add orphan descriptor with no chart")

    result = runner.invoke(app, ["install", "orphan", "--skip-requires-check"])
    assert result.exit_code == 1
    assert "chart" in result.output
    assert "scaffold" in result.output


def test_install_skips_helm_check_when_helm_not_on_path(git_repo, monkeypatch):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    monkeypatch.setattr("platform_cli.module.shutil.which", lambda _: None)

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 0, result.output
    assert "helm` not found on PATH" in result.output


def test_install_runs_helm_template_when_available_and_aborts_on_failure(git_repo, monkeypatch):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    monkeypatch.setattr("platform_cli.module.shutil.which", lambda _: "/usr/bin/helm")

    # `platform_cli.module.subprocess` is the real `subprocess` module (import subprocess, not a
    # copy) — patching `.run` on it via that dotted path patches subprocess.run globally, which
    # would also break repo.py's own git calls. So this fake only intercepts the helm invocation
    # and delegates everything else (git) to the real subprocess.run, captured before patching.
    real_run = subprocess.run
    calls = []

    def fake_run(args, **kwargs):
        if args[0] != "/usr/bin/helm":
            return real_run(args, **kwargs)
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom: bad chart")

    monkeypatch.setattr("platform_cli.module.subprocess.run", fake_run)

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 1
    assert "helm template" in result.output
    assert "boom: bad chart" in result.output
    assert calls and calls[0][0] == "/usr/bin/helm"
    assert not (repo_root / "src/modules-enabled/hello.yaml").exists()


def test_install_runs_helm_template_when_available_and_succeeds(git_repo, monkeypatch):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    monkeypatch.setattr("platform_cli.module.shutil.which", lambda _: "/usr/bin/helm")

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[0] != "/usr/bin/helm":
            return real_run(args, **kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("platform_cli.module.subprocess.run", fake_run)

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"])
    assert result.exit_code == 0, result.output
    assert (repo_root / "src/modules-enabled/hello.yaml").is_file()


# --- install: dependency-checking (module-lifecycle-plan.md item 6, 2026-09-03) -------------
#
# install() never constructs its own PlatformClient — it reuses ctx.obj, same as every other
# command in this CLI (workspace.py/dataset.py/function.py; see module.py's own module
# docstring), only when manifest.requires is non-empty. Since this file invokes
# `platform_cli.module.app` directly (not the root `platform` app main.py's callback normally
# builds ctx.obj on), these tests set ctx.obj themselves via CliRunner.invoke's own `obj=` kwarg —
# NOT by monkeypatching a PlatformClient constructor the way test_cli.py's FakeClient does for
# main.py's root callback (there's no constructor call here to intercept).


class _FakeModuleClient:
    """Stands in for ctx.obj's PlatformClient — only check_module_requirements is implemented,
    since that's the only method install()'s dependency check ever calls."""

    def __init__(self, results: list[ModuleRequirementStatus] | None = None) -> None:
        self._results = results or []
        self.calls: list[list[str]] = []

    def check_module_requirements(self, requires: list[str]) -> list[ModuleRequirementStatus]:
        self.calls.append(list(requires))
        return self._results


def _add_requires(repo_root: Path, name: str, requires: list[str]) -> None:
    # Real scaffolded modules start with the template's own `requires: []` default (decision 5,
    # this branch) — this just overwrites that one line, same "edit the real generated file"
    # spirit the rest of this test file already uses (e.g. test_install_wires_placement_...).
    module_yaml = repo_root / f"src/modules/{name}/module.yaml"
    text = module_yaml.read_text()
    assert "requires: []" in text, "expected the template's own requires: [] default"
    module_yaml.write_text(text.replace("requires: []", f"requires: [{', '.join(requires)}]"))


def test_install_blocks_with_clear_message_when_a_required_module_is_not_satisfied(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    _add_requires(repo_root, "hello", ["catalog", "trino"])
    _commit_all(repo_root, "add requires")

    fake = _FakeModuleClient(
        results=[
            ModuleRequirementStatus(module_id="catalog", satisfied=False, status="not installed"),
            ModuleRequirementStatus(module_id="trino", satisfied=False, status="Progressing"),
        ]
    )

    result = runner.invoke(app, ["install", "hello"], obj=fake)

    assert result.exit_code == 1
    assert "catalog" in result.output
    assert "not installed" in result.output
    assert "trino" in result.output
    assert "Progressing" in result.output
    assert not (repo_root / "src/modules-enabled/hello.yaml").exists()
    assert fake.calls == [["catalog", "trino"]]  # checked, then blocked before any write


def test_install_proceeds_when_all_requirements_are_satisfied(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    _add_requires(repo_root, "hello", ["catalog"])
    _commit_all(repo_root, "add requires")

    fake = _FakeModuleClient(
        results=[ModuleRequirementStatus(module_id="catalog", satisfied=True, status="Healthy")]
    )

    result = runner.invoke(app, ["install", "hello"], obj=fake)

    assert result.exit_code == 0, result.output
    assert (repo_root / "src/modules-enabled/hello.yaml").is_file()
    assert fake.calls == [["catalog"]]


def test_install_skip_requires_check_bypasses_the_check_entirely(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")
    _add_requires(repo_root, "hello", ["catalog"])
    _commit_all(repo_root, "add requires")

    # No results queued at all — if this were ever actually called, the empty list would read as
    # "catalog: not installed" and this test would fail on exit_code, catching a regression either
    # way rather than passing vacuously.
    fake = _FakeModuleClient()

    result = runner.invoke(app, ["install", "hello", "--skip-requires-check"], obj=fake)

    assert result.exit_code == 0, result.output
    assert "skip-requires-check" in result.output  # the visible warning
    assert (repo_root / "src/modules-enabled/hello.yaml").is_file()
    assert fake.calls == []  # never even asked


def test_install_with_empty_requires_never_touches_ctx_obj(git_repo):
    # hello's module.yaml keeps the template's own `requires: []` default untouched — this is the
    # common case (hello-module itself, and any freshly-scaffolded module) that must stay exactly
    # as login-free as it was before this branch: zero calls on ctx.obj's client, whatever it is.
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")

    fake = _FakeModuleClient()

    result = runner.invoke(app, ["install", "hello"], obj=fake)

    assert result.exit_code == 0, result.output
    assert fake.calls == []


# --- uninstall ------------------------------------------------------------------------------


def _install(repo_root: Path, name: str) -> None:
    _scaffold_and_commit(repo_root, name)
    assert runner.invoke(app, ["install", name, "--skip-requires-check"]).exit_code == 0


def test_uninstall_removes_commits_and_pushes(git_repo):
    repo_root, _ = git_repo
    _install(repo_root, "hello")

    result = runner.invoke(app, ["uninstall", "hello"])
    assert result.exit_code == 0, result.output
    assert not (repo_root / "src/modules-enabled/hello.yaml").exists()
    assert "PersistentVolumeClaim" in result.output or "survives on purpose" in result.output
    assert _head(repo_root) == _remote_head(repo_root)


def test_uninstall_purge_data_prints_kubectl_command(git_repo):
    repo_root, _ = git_repo
    _install(repo_root, "hello")

    result = runner.invoke(app, ["uninstall", "hello", "--purge-data"])
    assert result.exit_code == 0, result.output
    assert "kubectl delete pvc -n hello -l platform.io/module=hello" in result.output


def test_uninstall_purge_data_warns_to_confirm_application_gone_first(git_repo):
    # Regression test for a real bug found live 2026-09-03 (this branch's own live verification):
    # running the printed delete command before Argo had actually pruned the module's Application
    # got the PVC recreated by that Application's own still-live selfHeal. The printed guidance
    # must tell the operator to check first, not just hand them the delete command — and the check
    # must come before the delete in the output, not after.
    repo_root, _ = git_repo
    _install(repo_root, "hello")

    result = runner.invoke(app, ["uninstall", "hello", "--purge-data"])
    assert result.exit_code == 0, result.output
    assert "do NOT run the delete below yet" in result.output
    assert "kubectl -n argocd get application hello" in result.output
    check_pos = result.output.index("kubectl -n argocd get application hello")
    delete_pos = result.output.index("kubectl delete pvc -n hello -l platform.io/module=hello")
    assert check_pos < delete_pos, "the confirm-it's-gone check must print before the delete command"


def test_uninstall_dry_run_changes_nothing(git_repo):
    repo_root, _ = git_repo
    _install(repo_root, "hello")
    before = _head(repo_root)

    result = runner.invoke(app, ["uninstall", "hello", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (repo_root / "src/modules-enabled/hello.yaml").is_file()
    assert _head(repo_root) == before


def test_uninstall_unknown_module_fails_clearly(git_repo):
    repo_root, _ = git_repo
    _scaffold_and_commit(repo_root, "hello")  # never installed

    result = runner.invoke(app, ["uninstall", "hello"])
    assert result.exit_code == 1
    assert "isn't installed" in result.output
