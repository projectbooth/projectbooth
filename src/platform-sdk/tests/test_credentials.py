"""Unit tests for platform_sdk.credentials — real filesystem, but scoped to
a pytest tmp_path via XDG_CONFIG_HOME (monkeypatched per test), never the
real ~/.config/platform/credentials.json. No mocking needed beyond that:
this module's whole job is "read/write one small JSON file correctly and
with the right permissions," which is cheap and safe to test for real.
"""
from __future__ import annotations

import stat
from datetime import UTC, datetime

import pytest

from platform_sdk.credentials import clear_credentials, credentials_path, load_credentials, save_credentials
from platform_sdk.models import TokenSet


@pytest.fixture(autouse=True)
def _isolated_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _token_set(**overrides) -> TokenSet:
    kwargs = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC),
        "preferred_username": "alice",
    }
    kwargs.update(overrides)
    return TokenSet(**kwargs)


def test_credentials_path_respects_xdg_config_home(tmp_path):
    assert credentials_path() == tmp_path / "config" / "platform" / "credentials.json"


def test_load_credentials_returns_none_when_no_file_exists():
    assert load_credentials() is None


def test_save_then_load_round_trips_every_field():
    token_set = _token_set()
    save_credentials(token_set)

    loaded = load_credentials()

    assert loaded == token_set


def test_save_creates_parent_directory():
    save_credentials(_token_set())
    assert credentials_path().parent.is_dir()


def test_save_writes_file_with_0600_permissions():
    save_credentials(_token_set())
    mode = stat.S_IMODE(credentials_path().stat().st_mode)
    assert mode == 0o600


def test_save_overwrites_a_previous_shorter_or_longer_file_cleanly():
    save_credentials(_token_set(access_token="a" * 500))  # write something long first
    save_credentials(_token_set(access_token="short"))  # then something short

    loaded = load_credentials()

    assert loaded.access_token == "short"


def test_load_credentials_returns_none_for_corrupt_json():
    path = credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")

    assert load_credentials() is None


def test_load_credentials_returns_none_for_valid_json_missing_required_fields():
    path = credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"access_token": "at-1"}')  # no expires_at

    assert load_credentials() is None


def test_clear_credentials_removes_the_file():
    save_credentials(_token_set())
    assert credentials_path().exists()

    clear_credentials()

    assert not credentials_path().exists()


def test_clear_credentials_is_safe_when_nothing_exists():
    clear_credentials()  # must not raise
    assert load_credentials() is None
