import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from zenpy import Zenpy

from zendesk_mcp import auth
from zendesk_mcp.client import RefreshingZendeskSession


def _response(
    status_code,
    body,
    url="https://acme.zendesk.com/api/v2/tickets.json",
):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.url = url
    response.json.return_value = body
    return response


def test_get_client_raises_config_error_when_token_missing(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"subdomain": "example"}))
    from zendesk_mcp.client import get_client, ConfigError
    with pytest.raises(ConfigError, match="Run: zendesk-mcp setup"):
        get_client(cfg_file)


def test_get_client_raises_config_error_when_subdomain_missing(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"oauth_token": "tok"}))
    from zendesk_mcp.client import get_client, ConfigError
    with pytest.raises(ConfigError, match="Run: zendesk-mcp setup"):
        get_client(cfg_file)


def test_get_client_returns_zenpy_instance(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "subdomain": "example",
        "oauth_token": "tok123",
    }))
    with patch("zendesk_mcp.client.Zenpy") as mock_zenpy:
        mock_instance = MagicMock()
        mock_zenpy.return_value = mock_instance
        from zendesk_mcp.client import get_client
        result = get_client(cfg_file)
        mock_zenpy.assert_called_once()
        assert mock_zenpy.call_args.kwargs["subdomain"] == "example"
        session = mock_zenpy.call_args.kwargs["session"]
        assert isinstance(session, RefreshingZendeskSession)
        assert "Authorization" not in session.headers
        assert result is mock_instance


def test_refreshing_session_preserves_zenpy_retry_adapter():
    session = RefreshingZendeskSession("acme")

    expected = Zenpy.http_adapter_kwargs()["max_retries"]
    actual = session.adapters["https://"].max_retries
    assert actual.total == expected.total
    assert actual.connect == expected.connect
    assert actual.read == expected.read


def test_refreshing_session_uses_valid_token_and_managed_authorization(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg = {"subdomain": "acme", "oauth_token": "stored-token"}
    cfg_file.write_text(json.dumps(cfg))
    response = _response(200, {})
    session = RefreshingZendeskSession("acme", cfg_file)

    with (
        patch("zendesk_mcp.client.auth.valid_token", return_value=("acme", "fresh-token")) as valid,
        patch.object(requests.Session, "request", autospec=True, return_value=response) as send,
    ):
        result = session.get(
            "https://acme.zendesk.com/api/v2/users/me.json",
            headers={"authorization": "caller-token", "X-Test": "yes"},
        )

    assert result is response
    valid.assert_called_once_with(cfg_file, cfg_snapshot=cfg)
    assert send.call_count == 1
    sent_headers = send.call_args.kwargs["headers"]
    assert sent_headers == {
        "X-Test": "yes",
        "Authorization": "Bearer fresh-token",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.zendesk.com/api/v2/users/me.json",
        "https://other.zendesk.com/api/v2/users/me.json",
        "https://acme.zendesk.com.evil.test/api/v2/users/me.json",
        "https://acme.zendesk.com:444/api/v2/users/me.json",
        "https://user@acme.zendesk.com/api/v2/users/me.json",
    ],
)
def test_refreshing_session_rejects_untrusted_origins_before_loading_token(url):
    session = RefreshingZendeskSession("acme")

    with (
        patch("zendesk_mcp.client.auth.valid_token") as valid,
        patch.object(requests.Session, "request", autospec=True) as send,
        pytest.raises(auth.UnsafeZendeskUrlError),
    ):
        session.get(url)

    valid.assert_not_called()
    send.assert_not_called()


def test_refreshing_session_strips_authorization_on_external_redirect():
    session = RefreshingZendeskSession("acme")
    original = requests.Request(
        "GET",
        "https://acme.zendesk.com/api/v2/attachments/1/content",
        headers={"Authorization": "Bearer token"},
    ).prepare()
    redirected = requests.Request(
        "GET",
        "https://uploads.example.test/temporary-file",
        headers={"Authorization": "Bearer token"},
    ).prepare()
    response = requests.Response()
    response.request = original

    session.rebuild_auth(redirected, response)

    assert "Authorization" not in redirected.headers


def test_refreshing_session_keeps_authorization_on_same_origin_redirect():
    session = RefreshingZendeskSession("acme")
    original = requests.Request(
        "GET",
        "https://acme.zendesk.com/api/v2/tickets",
        headers={"Authorization": "Bearer token"},
    ).prepare()
    redirected = requests.Request(
        "GET",
        "https://acme.zendesk.com/api/v2/tickets.json",
        headers={"Authorization": "Bearer token"},
    ).prepare()
    response = requests.Response()
    response.request = original

    session.rebuild_auth(redirected, response)

    assert redirected.headers["Authorization"] == "Bearer token"


def test_refreshing_session_refreshes_exact_invalid_token_once(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg = {
        "subdomain": "acme",
        "oauth_token": "old-token",
        "refresh_token": "refresh-token",
        "client_id": "client-id",
    }
    cfg_file.write_text(json.dumps(cfg))
    rejected = _response(401, {"error": "invalid_token"})
    accepted = _response(200, {"ok": True})
    session = RefreshingZendeskSession("acme", cfg_file)

    with (
        patch("zendesk_mcp.client.auth.valid_token", return_value=("acme", "old-token")),
        patch.object(
            requests.Session,
            "request",
            autospec=True,
            side_effect=[rejected, accepted],
        ) as send,
        patch(
            "zendesk_mcp.client.auth.refresh_rejected_token",
            return_value={**cfg, "oauth_token": "new-token"},
        ) as refresh,
    ):
        result = session.get("https://acme.zendesk.com/api/v2/tickets.json")

    assert result is accepted
    refresh.assert_called_once_with(
        "old-token",
        config_file=cfg_file,
        cfg_snapshot=cfg,
    )
    assert send.call_count == 2
    assert send.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer old-token"
    assert send.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer new-token"


@pytest.mark.parametrize(
    "status_code,body",
    [
        (401, {"error": "unauthorized"}),
        (401, {"description": "invalid_token"}),
        (403, {"error": "invalid_token"}),
    ],
)
def test_refreshing_session_does_not_refresh_unrelated_responses(
    tmp_path, status_code, body
):
    cfg_file = tmp_path / "config.json"
    cfg = {"subdomain": "acme", "oauth_token": "token"}
    cfg_file.write_text(json.dumps(cfg))
    response = _response(status_code, body)
    session = RefreshingZendeskSession("acme", cfg_file)

    with (
        patch("zendesk_mcp.client.auth.valid_token", return_value=("acme", "token")),
        patch.object(requests.Session, "request", autospec=True, return_value=response) as send,
        patch("zendesk_mcp.client.auth.refresh_rejected_token") as refresh,
    ):
        result = session.get("https://acme.zendesk.com/api/v2/tickets.json")

    assert result is response
    assert send.call_count == 1
    refresh.assert_not_called()


def test_refreshing_session_does_not_refresh_for_external_invalid_token_response(
    tmp_path,
):
    cfg_file = tmp_path / "config.json"
    cfg = {"subdomain": "acme", "oauth_token": "token"}
    cfg_file.write_text(json.dumps(cfg))
    response = _response(
        401,
        {"error": "invalid_token"},
        url="https://uploads.example.test/temporary-file",
    )
    session = RefreshingZendeskSession("acme", cfg_file)

    with (
        patch("zendesk_mcp.client.auth.valid_token", return_value=("acme", "token")),
        patch.object(requests.Session, "request", autospec=True, return_value=response) as send,
        patch("zendesk_mcp.client.auth.refresh_rejected_token") as refresh,
    ):
        result = session.get(
            "https://acme.zendesk.com/api/v2/attachments/1/content"
        )

    assert result is response
    assert send.call_count == 1
    refresh.assert_not_called()


def test_refreshing_session_never_retries_invalid_token_more_than_once(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg = {
        "subdomain": "acme",
        "oauth_token": "old-token",
        "refresh_token": "refresh-token",
        "client_id": "client-id",
    }
    cfg_file.write_text(json.dumps(cfg))
    rejected = _response(401, {"error": "invalid_token"})
    session = RefreshingZendeskSession("acme", cfg_file)

    with (
        patch("zendesk_mcp.client.auth.valid_token", return_value=("acme", "old-token")),
        patch.object(
            requests.Session,
            "request",
            autospec=True,
            side_effect=[rejected, rejected],
        ) as send,
        patch(
            "zendesk_mcp.client.auth.refresh_rejected_token",
            return_value={**cfg, "oauth_token": "new-token"},
        ) as refresh,
        pytest.raises(auth.TokenExpiredError, match="still rejected after refresh"),
    ):
        session.get("https://acme.zendesk.com/api/v2/tickets.json")

    assert send.call_count == 2
    refresh.assert_called_once()


def test_refreshing_session_rejects_subdomain_change_during_refresh(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg = {
        "subdomain": "acme",
        "oauth_token": "old-token",
        "refresh_token": "refresh-token",
        "client_id": "client-id",
    }
    cfg_file.write_text(json.dumps(cfg))
    rejected = _response(401, {"error": "invalid_token"})
    session = RefreshingZendeskSession("acme", cfg_file)

    with (
        patch("zendesk_mcp.client.auth.valid_token", return_value=("acme", "old-token")),
        patch.object(
            requests.Session,
            "request",
            autospec=True,
            return_value=rejected,
        ) as send,
        patch(
            "zendesk_mcp.client.auth.refresh_rejected_token",
            return_value={**cfg, "subdomain": "other", "oauth_token": "new-token"},
        ),
        pytest.raises(auth.UnsafeZendeskUrlError, match="changed while refreshing"),
    ):
        session.get("https://acme.zendesk.com/api/v2/tickets.json")

    assert send.call_count == 1


def test_get_oauth_session_returns_subdomain_and_token():
    with patch("zendesk_mcp.auth.load_config", return_value={"subdomain": "acme", "oauth_token": "tok123"}):
        from zendesk_mcp.client import get_oauth_session
        subdomain, token = get_oauth_session()
        assert subdomain == "acme"
        assert token == "tok123"


def test_get_oauth_session_raises_config_error_when_missing_token():
    with patch("zendesk_mcp.auth.load_config", return_value={"subdomain": "acme"}):
        from zendesk_mcp.client import get_oauth_session, ConfigError
        with pytest.raises(ConfigError, match="Run: zendesk-mcp setup"):
            get_oauth_session()


def test_get_oauth_session_raises_config_error_when_missing_subdomain():
    with patch("zendesk_mcp.auth.load_config", return_value={"oauth_token": "tok"}):
        from zendesk_mcp.client import get_oauth_session, ConfigError
        with pytest.raises(ConfigError, match="Run: zendesk-mcp setup"):
            get_oauth_session()
