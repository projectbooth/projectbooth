"""Unit tests for `extract_platform_ca_cert()`/`cleanup_ca_cert()` — all
that's left of `_keycloak_connection.py` after the platform-ingress branch
(2026-09-02) removed `_PortForward`/`_ResolvePatch` outright. See that
module's own docstring for why: `keycloak.platform.local` (and
`gateway.platform.local`) now resolve for real, off-cluster, via a real
`Ingress` plus a per-device `/etc/hosts` entry, so nothing needs a
`kubectl port-forward` or a `socket.getaddrinfo` patch to reach either one
anymore.

This file used to also cover `_PortForward._wait_ready()`'s TLS-handshake-
readiness fix and `start()`'s constructed kubectl command — both real bugs
found live on 2026-09-02, the day before this simplification — but that
whole class is gone, so those tests went with it rather than being kept
around testing dead code. `extract_platform_ca_cert()`'s logic is exactly
what `_PortForward._extract_ca_cert()` used to do, unchanged, so it still
deserves its own direct coverage: these tests mock `subprocess.run` the
same way the old CA-extraction path was exercised in practice (never
actually unit-tested on its own before — it always ran as a side effect of
`start()`, which needed a live cluster to test at all).
"""
from __future__ import annotations

import base64
import subprocess

import pytest

from platform_sdk._keycloak_connection import cleanup_ca_cert, extract_platform_ca_cert
from platform_sdk.exceptions import KeycloakAdminError


def _fake_run(stdout: str):
    def _run(cmd, **_kwargs):
        class _Result:
            pass

        result = _Result()
        result.stdout = stdout
        return result

    return _run


def test_extract_platform_ca_cert_writes_the_decoded_secret_to_a_temp_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n").decode()
    monkeypatch.setattr(subprocess, "run", _fake_run(encoded))

    path = extract_platform_ca_cert("kubectl")
    try:
        with open(path, "rb") as f:
            assert f.read() == base64.b64decode(encoded)
    finally:
        cleanup_ca_cert(path)


def test_extract_platform_ca_cert_passes_the_kubectl_cmd_prefix_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    def _run(cmd, **_kwargs):
        captured["cmd"] = cmd

        class _Result:
            stdout = base64.b64encode(b"test").decode()

        return _Result()

    monkeypatch.setattr(subprocess, "run", _run)

    extract_platform_ca_cert("sudo /usr/local/bin/kubectl")
    # A multi-word kubectl_cmd (the sudo-prefixed default every caller in
    # this repo actually uses) has to be split into separate argv entries,
    # not passed as one literal string subprocess.run would try to exec.
    assert captured["cmd"][:3] == ["sudo", "/usr/local/bin/kubectl", "get"]


def test_extract_platform_ca_cert_raises_a_clear_error_on_kubectl_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="secrets \"platform-ca-secret\" not found")

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(KeycloakAdminError, match="Couldn't read platform-ca-secret"):
        extract_platform_ca_cert("kubectl")


def test_extract_platform_ca_cert_raises_when_the_secret_decodes_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(""))

    with pytest.raises(KeycloakAdminError, match="came back empty"):
        extract_platform_ca_cert("kubectl")


def test_cleanup_ca_cert_removes_the_file() -> None:
    import tempfile
    from pathlib import Path

    fd = tempfile.NamedTemporaryFile(delete=False)
    fd.write(b"x")
    fd.close()
    assert Path(fd.name).exists()

    cleanup_ca_cert(fd.name)
    assert not Path(fd.name).exists()


def test_cleanup_ca_cert_is_a_no_op_for_none() -> None:
    cleanup_ca_cert(None)  # should not raise


def test_cleanup_ca_cert_is_a_no_op_for_an_already_missing_file() -> None:
    # Regression-shaped: cleanup running twice (or against a path that was
    # never actually created) shouldn't be an error — same tolerance
    # _PortForward.stop() used to have for its own CA-file cleanup.
    cleanup_ca_cert("/tmp/definitely-does-not-exist-platform-ca-test.crt")
