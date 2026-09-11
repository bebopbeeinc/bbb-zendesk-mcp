import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from zendesk_mcp import auth
from zendesk_mcp.config import config_file_lock, load_config, save_config
from zendesk_mcp.setup import (
    _exchange_code,
    _persist_authorization,
    _updated_config,
    _verify_token,
)


def _post_response(**extra):
    body = {
        "access_token": "acc-tok",
        "token_type": "bearer",
        "scope": "read write",
    }
    body.update(extra)
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://acme.zendesk.com/oauth/tokens"),
    )


@patch("zendesk_mcp.setup.httpx.post")
def test_exchange_code_retains_refresh_token_and_expiry(mock_post):
    mock_post.return_value = _post_response(
        refresh_token="ref-tok", expires_in=86400, refresh_token_expires_in=2592000
    )

    result = _exchange_code("acme", "the-code", "cid", "secret", now=1000)

    assert result["access_token"] == "acc-tok"
    assert result["refresh_token"] == "ref-tok"
    assert result["expires_at"] == 1000 + 86400
    assert result["refresh_token_expires_at"] == 1000 + 2592000


@patch("zendesk_mcp.setup.httpx.post")
def test_exchange_code_requests_an_expiry_so_legacy_clients_get_refresh_tokens(mock_post):
    """Zendesk only issues a refresh token for a legacy client if expires_in is passed explicitly."""
    mock_post.return_value = _post_response(refresh_token="ref-tok", expires_in=86400)

    _exchange_code("acme", "the-code", "cid", "secret", now=1000)

    sent = mock_post.call_args.kwargs["json"]
    assert sent["grant_type"] == "authorization_code"
    assert "expires_in" in sent
    assert "refresh_token_expires_in" in sent


@patch("zendesk_mcp.setup.httpx.post")
def test_exchange_code_tolerates_server_omitting_refresh_token(mock_post):
    """A non-expiring legacy token must still configure cleanly, just without refresh."""
    mock_post.return_value = _post_response()

    result = _exchange_code("acme", "the-code", "cid", "secret", now=1000)

    assert result["access_token"] == "acc-tok"
    assert result.get("refresh_token") is None
    assert result.get("expires_at") is None


@patch("zendesk_mcp.setup.httpx.post")
def test_exchange_code_rejects_malformed_subdomain_before_sending_secret(mock_post):
    with pytest.raises(auth.InvalidZendeskSubdomainError):
        _exchange_code(
            "attacker.example/path",
            "authorization-code",
            "cid",
            "client-secret",
        )

    mock_post.assert_not_called()


@patch("zendesk_mcp.setup.httpx.get")
def test_verify_token_rejects_malformed_subdomain_before_sending_token(mock_get):
    with pytest.raises(auth.InvalidZendeskSubdomainError):
        _verify_token("attacker.example/path", "access-token")

    mock_get.assert_not_called()


def test_updated_config_preserves_preferences_and_replaces_stale_credentials():
    existing = {
        "subdomain": "old",
        "oauth_token": "old-token",
        "refresh_token": "old-refresh",
        "client_id": "old-id",
        "client_secret": "old-secret",
        "expires_at": 123,
        "refresh_token_expires_at": 456,
        "knowledge_base_enabled": True,
        "git_zen_field_id": 321,
    }
    tokens = {
        "access_token": "new-token",
        "refresh_token": "new-refresh",
        "expires_at": 1000,
        "refresh_token_expires_at": 2000,
    }

    result = _updated_config(existing, "acme", tokens, "new-id", "new-secret")

    assert result["subdomain"] == "acme"
    assert result["oauth_token"] == "new-token"
    assert result["refresh_token"] == "new-refresh"
    assert result["client_id"] == "new-id"
    assert result["client_secret"] == "new-secret"
    assert result["expires_at"] == 1000
    assert result["refresh_token_expires_at"] == 2000
    assert result["knowledge_base_enabled"] is True
    assert result["git_zen_field_id"] == 321


def test_updated_config_removes_stale_refresh_credentials_when_server_omits_them():
    existing = {
        "subdomain": "acme",
        "oauth_token": "old-token",
        "refresh_token": "old-refresh",
        "client_id": "old-id",
        "client_secret": "old-secret",
        "expires_at": 123,
        "refresh_token_expires_at": 456,
    }

    result = _updated_config(
        existing,
        "acme",
        {"access_token": "legacy-token"},
        "new-id",
        "new-secret",
    )

    assert result["oauth_token"] == "legacy-token"
    for key in (
        "refresh_token",
        "client_id",
        "client_secret",
        "expires_at",
        "refresh_token_expires_at",
    ):
        assert key not in result


def test_persist_authorization_waits_for_refresh_then_wins(tmp_path):
    """A refresh already in flight must not overwrite a newly authorized grant."""
    config_file = tmp_path / "config.json"
    save_config(
        {
            "subdomain": "acme",
            "oauth_token": "old-token",
            "refresh_token": "old-refresh",
            "client_id": "cid",
            "client_secret": "secret",
            "knowledge_base_enabled": True,
        },
        config_file,
    )
    refresh_holds_lock = threading.Event()
    release_refresh = threading.Event()

    def stale_refresh_write():
        with config_file_lock(config_file, timeout=2):
            stale = load_config(config_file)
            refresh_holds_lock.set()
            assert release_refresh.wait(timeout=2)
            stale["oauth_token"] = "old-grant-rotated"
            save_config(stale, config_file)

    new_tokens = {
        "access_token": "new-token",
        "refresh_token": "new-refresh",
        "expires_at": 1000,
        "refresh_token_expires_at": 2000,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(stale_refresh_write)
        assert refresh_holds_lock.wait(timeout=2)
        setup = pool.submit(
            _persist_authorization,
            "acme",
            new_tokens,
            "new-client-id",
            "new-client-secret",
            None,
            False,
            config_file,
        )
        try:
            assert not setup.done()
        finally:
            release_refresh.set()
        refresh.result(timeout=2)
        setup.result(timeout=2)

    saved = json.loads(config_file.read_text())
    assert saved["oauth_token"] == "new-token"
    assert saved["refresh_token"] == "new-refresh"
    assert saved["client_id"] == "new-client-id"
    assert saved["knowledge_base_enabled"] is True


# --- API-token setup ----------------------------------------------------------------

def test_api_token_setup_verifies_before_writing(monkeypatch, tmp_path, capsys):
    """A mistyped token must not cost the working credential.

    setup replaces what the server is currently authenticating with, so writing an
    unverified credential means the mistake surfaces at 4am somewhere that cannot
    explain it.
    """
    import json
    from zendesk_mcp import setup as setup_mod

    cfg = tmp_path / "config.json"
    original = {"subdomain": "acme", "oauth_token": "known-good", "refresh_token": "r"}
    cfg.write_text(json.dumps(original))
    monkeypatch.setattr("zendesk_mcp.config.config_path", lambda: cfg)
    monkeypatch.setattr(setup_mod, "sys", __import__("sys"))
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_EMAIL", "contact@acme.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "wrong")

    def reject(subdomain, email, token):
        raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(setup_mod, "_verify_api_token", reject)

    import pytest
    with pytest.raises(SystemExit):
        setup_mod.run_api_token_setup()

    assert json.loads(cfg.read_text()) == original, "config must be untouched on failure"
    assert "Nothing was changed" in capsys.readouterr().out


def test_oauth_setup_clears_api_token_credentials():
    """Otherwise a successful re-authorisation has no effect.

    api_token_credentials() takes precedence when present, so leaving email/api_token in
    place would send every request out as the old token's account while setup reported
    success.
    """
    from zendesk_mcp.setup import _updated_config

    existing = {"email": "old@acme.com", "api_token": "stale", "attachment_cache_dir": "/keep"}
    updated = _updated_config(existing, "acme", {"access_token": "new"}, "cid", "secret")
    assert "email" not in updated
    assert "api_token" not in updated
    assert updated["oauth_token"] == "new"
    assert updated["attachment_cache_dir"] == "/keep", "unrelated preferences survive"
