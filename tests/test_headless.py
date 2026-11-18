"""Unattended OAuth must never inherit the interactive user's broader grant."""
import asyncio
from unittest.mock import Mock, patch

import httpx
import pytest

from zendesk_mcp import auth, config, headless
from zendesk_mcp.client import RefreshingZendeskSession


@pytest.fixture
def unattended(monkeypatch):
    monkeypatch.setenv("ZENDESK_AUTH_MODE", "client_credentials")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
    monkeypatch.setenv("ZENDESK_CLIENT_ID", "community-readonly")
    monkeypatch.setenv("ZENDESK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(headless, "_identity", None)
    monkeypatch.setattr(headless, "_token", {})
    with patch("zendesk_mcp.headless.httpx.post") as grant:
        grant.return_value = response()
        yield grant


def response(token="first-token", **changes):
    payload = {"access_token": token, "expires_in": 1800,
               "token_type": "bearer", "scope": "read"}
    payload.update(changes)
    return httpx.Response(200, json=payload,
                          request=httpx.Request("POST", "https://example.zendesk.com/oauth/tokens"))


def test_caches_and_renews_without_files_or_refresh_tokens(unattended, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"subdomain":"other", "oauth_token":"legacy-write-grant"}')
    assert config.load_config(cfg) == {"subdomain": "example"}
    assert auth.valid_token(cfg, now=100) == ("example", "first-token")
    assert auth.valid_token(cfg, now=200) == ("example", "first-token")
    assert unattended.call_count == 1
    args = unattended.call_args
    assert args.args == ("https://example.zendesk.com/oauth/tokens",)
    assert args.kwargs["json"] == {
        "grant_type": "client_credentials", "client_id": "community-readonly",
        "client_secret": "test-client-secret", "scope": "read", "expires_in": 1800,
    }
    unattended.return_value = response("renewed-token")
    assert auth.valid_token(cfg, now=1780) == ("example", "renewed-token")
    assert unattended.call_count == 2
    assert cfg.read_text() == '{"subdomain":"other", "oauth_token":"legacy-write-grant"}'
    assert list(tmp_path.iterdir()) == [cfg]


def test_rejection_renews_once_and_reuses_new_token(unattended):
    auth.valid_token(now=100)
    unattended.return_value = response("replacement")
    assert auth.refresh_rejected_token("first-token", now=101)["oauth_token"] == "replacement"
    assert auth.refresh_rejected_token("first-token", now=102)["oauth_token"] == "replacement"
    assert unattended.call_count == 2


@pytest.mark.parametrize("changes", [
    {"scope": "read write"}, {"scope": ""}, {"expires_in": 0},
    {"expires_in": True}, {"expires_in": "1800"}, {"access_token": ""},
    {"token_type": "basic"},
])
def test_rejects_unexpected_or_write_capable_grants(unattended, changes):
    unattended.return_value = response(**changes)
    with pytest.raises(auth.TokenExpiredError, match="Headless OAuth failed"):
        auth.valid_token(now=100)


def test_grant_failure_does_not_echo_sensitive_body(unattended):
    unattended.return_value = httpx.Response(
        400, json={"error": "test-client-secret"},
        request=httpx.Request("POST", "https://example.zendesk.com/oauth/tokens"),
    )
    with pytest.raises(auth.TokenExpiredError) as caught:
        auth.valid_token(now=100)
    assert "test-client-secret" not in str(caught.value)
    assert headless._token == {}


def test_missing_credentials_never_falls_back(unattended, monkeypatch):
    monkeypatch.delenv("ZENDESK_CLIENT_SECRET")
    with pytest.raises(auth.TokenExpiredError, match="ZENDESK_CLIENT_SECRET"):
        auth.valid_token()
    unattended.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "put", "PATCH", "DELETE"])
def test_both_http_paths_refuse_writes_before_authentication(unattended, method):
    url = "https://example.zendesk.com/api/v2/tickets.json"
    with patch("httpx.request") as direct, patch("requests.Session.request") as zenpy:
        with pytest.raises(ValueError, match="read-only"):
            auth.request(method, url)
        with pytest.raises(ValueError, match="read-only"):
            RefreshingZendeskSession("example").request(method, url)
    unattended.assert_not_called()
    direct.assert_not_called()
    zenpy.assert_not_called()


def test_origin_is_checked_before_minting_a_grant(unattended):
    with pytest.raises(auth.UnsafeZendeskUrlError):
        auth.request("GET", "https://untrusted.example/tickets.json")
    unattended.assert_not_called()


def test_rejected_api_token_is_retried_with_new_client_credentials(unattended):
    rejected = httpx.Response(401, json={"error": "invalid_token"},
                              request=httpx.Request("GET", "https://example.zendesk.com/api/v2/tickets.json"))
    accepted = httpx.Response(200, json={"tickets": []})
    unattended.side_effect = [response(), response("replacement")]
    answers = iter([rejected, accepted])
    sent_tokens = []

    def send(*args, **kwargs):
        # auth.request updates its headers for the retry; snapshot at send time.
        sent_tokens.append(kwargs["headers"]["Authorization"])
        return next(answers)

    with patch("httpx.request", side_effect=send):
        assert auth.request("GET", str(rejected.url), now=100) is accepted
    assert sent_tokens == ["Bearer first-token", "Bearer replacement"]
    assert unattended.call_count == 2


def test_zenpy_read_path_uses_headless_grant(unattended):
    with patch("requests.Session.request", return_value=Mock(status_code=200)) as request:
        RefreshingZendeskSession("example").request("GET", "https://example.zendesk.com/api/v2/tickets.json")
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer first-token"


@pytest.mark.parametrize("transport", ["httpx", "zenpy"])
def test_repeated_rejection_gives_headless_recovery_in_both_paths(unattended, transport):
    url = "https://example.zendesk.com/api/v2/tickets.json"
    rejected = httpx.Response(401, json={"error": "invalid_token"},
                              request=httpx.Request("GET", url))
    unattended.side_effect = [response(), response("replacement")]
    target = "httpx.request" if transport == "httpx" else "requests.Session.request"
    with patch(target, side_effect=[rejected, rejected]) as send:
        with pytest.raises(auth.TokenExpiredError) as caught:
            if transport == "httpx":
                auth.request("GET", url)
            else:
                RefreshingZendeskSession("example").request("GET", url)
    message = str(caught.value)
    assert "ZENDESK_CLIENT_ID" in message
    assert "ZENDESK_CLIENT_SECRET" in message
    assert "allowed read scope" in message
    assert "restart" in message
    assert "zendesk-mcp setup" not in message
    assert "test-client-secret" not in message
    assert "first-token" not in message
    assert "replacement" not in message
    assert unattended.call_count == 2
    assert send.call_count == 2


def test_only_read_tools_are_advertised_without_minting_tokens(unattended):
    from zendesk_mcp.server import FastMCP, register_all

    server = FastMCP("unattended-test")
    register_all(server)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"zendesk_get_ticket", "zendesk_get_comments", "zendesk_search_tickets",
            "zendesk_get_tickets", "zendesk_search_users", "zendesk_preview_macro",
            "zendesk_list_attachments"} <= names
    assert not names & {
        "zendesk_post_comment", "zendesk_post_internal_note", "zendesk_set_ticket_status",
        "zendesk_assign_ticket", "zendesk_update_ticket", "zendesk_create_ticket",
        "zendesk_log_time", "zendesk_add_tag", "zendesk_remove_tag", "zendesk_apply_macro",
        "zendesk_download_attachment", "zendesk_get_git_zen_links",
    }
    unattended.assert_not_called()


def test_unknown_auth_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("ZENDESK_AUTH_MODE", "client_credential_typo")
    with pytest.raises(ValueError, match="ZENDESK_AUTH_MODE"):
        config.load_config()
