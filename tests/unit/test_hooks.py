"""Tests for stable hook URL helpers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hooks


@pytest.fixture(autouse=True)
def isolated_appistry_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(hooks.registry, "APPISTRY_DIR", tmp_path)


def test_hook_url_builds_stable_proxy_url():
    assert hooks.hook_url("demo-app", "/api/oauth/callback") == (
        "http://127.0.0.1:47658/hooks/demo-app/api/oauth/callback"
    )


def test_hook_url_preserves_query_string():
    assert hooks.hook_url("my app", "/oauth/callback?code=abc&state=xyz", port=5000) == (
        "http://127.0.0.1:5000/hooks/my%20app/oauth/callback?code=abc&state=xyz"
    )


def test_hook_url_rejects_absolute_targets():
    with pytest.raises(ValueError):
        hooks.hook_url("demo-app", "http://example.com/callback")


def test_hook_port_env_override(monkeypatch):
    monkeypatch.setenv(hooks.HOOK_PORT_ENV, "5001")
    assert hooks.hook_port() == 5001


def test_hook_port_env_rejects_privileged_or_invalid_values(monkeypatch):
    monkeypatch.setenv(hooks.HOOK_PORT_ENV, "80")
    assert hooks.hook_port() == hooks.DEFAULT_HOOK_PORT
    monkeypatch.setenv(hooks.HOOK_PORT_ENV, "not-a-port")
    assert hooks.hook_port() == hooks.DEFAULT_HOOK_PORT


def test_hook_url_prefers_active_port_file(monkeypatch):
    monkeypatch.setenv(hooks.HOOK_PORT_ENV, "5001")
    hooks.hook_port_path().write_text("5002", encoding="utf-8")

    assert hooks.hook_url("demo-app", "/callback") == (
        "http://127.0.0.1:5002/hooks/demo-app/callback"
    )


def test_active_hook_port_ignores_invalid_port_file(monkeypatch):
    monkeypatch.setenv(hooks.HOOK_PORT_ENV, "5001")
    hooks.hook_port_path().write_text("not-a-port", encoding="utf-8")

    assert hooks.active_hook_port() == 5001
