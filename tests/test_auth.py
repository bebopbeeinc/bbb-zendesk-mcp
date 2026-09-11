import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from zendesk_mcp import auth
from zendesk_mcp.auth import TokenExpiredError

NOW = 1_760_000_000

EXPIRED_401 = {
    "error": "invalid_token",
    "error_description": "The access token provided is expired, revoked, malformed or invalid for other reasons.",
}


def write_config(tmp_path, **overrides):
    cfg = {"subdomain": "acme", "oauth_token": "old-tok"}
    cfg.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return path


def token_response(access="new-tok", refresh="new-refresh", expires_in=86400):
    body = {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": expires_in,
        "refresh_token": refresh,
        "refresh_token_expires_in": 2592000,
        "scope": "read write",
    }
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://acme.zendesk.com/oauth/tokens"),
    )


# --- origin validation ----------------------------------------------------


@pytest.mark.parametrize(
    "subdomain",
    [
        "",
        "-acme",
        "acme-",
        "acme.example",
        "attacker.example/path",
        "acme:443",
        "acme@evil",
        "a" * 64,
    ],
)
def test_validate_zendesk_subdomain_rejects_non_dns_label(subdomain):
    with pytest.raises(auth.InvalidZendeskSubdomainError):
        auth.validate_zendesk_subdomain(subdomain)


@pytest.mark.parametrize("subdomain", ["acme", "ACME-2", " bEbopBeeHelp "])
def test_validate_zendesk_subdomain_returns_canonical_label(subdomain):
    assert auth.validate_zendesk_subdomain(subdomain) == subdomain.strip().lower()


# --- expiry detection ------------------------------------------------------


def test_is_expired_true_when_past():
    assert auth.is_expired({"expires_at": NOW - 1}, now=NOW) is True


def test_is_expired_false_when_far_future():
    assert auth.is_expired({"expires_at": NOW + 100_000}, now=NOW) is False


def test_is_expired_false_without_expires_at():
    """Legacy configs from a non-expiring OAuth client must not be treated as expired."""
    assert auth.is_expired({"oauth_token": "tok"}, now=NOW) is False


def test_is_expired_true_inside_skew_window():
    """Refresh slightly early so a call in flight does not land on a dead token."""
    assert auth.is_expired({"expires_at": NOW + 5}, now=NOW) is True


# --- valid_token -----------------------------------------------------------


@patch("zendesk_mcp.auth.httpx.post")
def test_valid_token_legacy_config_makes_no_network_call(mock_post, tmp_path):
    cfg = write_config(tmp_path)
    subdomain, token = auth.valid_token(cfg, now=NOW)
    assert (subdomain, token) == ("acme", "old-tok")
    mock_post.assert_not_called()


@patch("zendesk_mcp.auth.httpx.post")
def test_valid_token_unexpired_makes_no_network_call(mock_post, tmp_path):
    cfg = write_config(tmp_path, refresh_token="r", expires_at=NOW + 100_000)
    subdomain, token = auth.valid_token(cfg, now=NOW)
    assert token == "old-tok"
    mock_post.assert_not_called()


@patch("zendesk_mcp.auth.httpx.post")
def test_valid_token_refreshes_when_expired(mock_post, tmp_path):
    mock_post.return_value = token_response()
    cfg = write_config(
        tmp_path,
        refresh_token="the-refresh",
        expires_at=NOW - 10,
        client_id="cid",
        client_secret="secret",
    )

    subdomain, token = auth.valid_token(cfg, now=NOW)

    assert token == "new-tok"
    sent = mock_post.call_args.kwargs["json"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "the-refresh"
    assert sent["client_id"] == "cid"
    assert sent["client_secret"] == "secret"
    assert sent["expires_in"] == auth.ACCESS_TOKEN_TTL_SECONDS
    assert sent["refresh_token_expires_in"] == auth.REFRESH_TOKEN_TTL_SECONDS
    assert "acme.zendesk.com/oauth/tokens" in mock_post.call_args.args[0]


@patch("zendesk_mcp.auth.httpx.post")
def test_refresh_persists_rotated_credentials(mock_post, tmp_path):
    """Zendesk invalidates the old refresh token on use, so the new one must be saved."""
    mock_post.return_value = token_response(access="a2", refresh="r2", expires_in=3600)
    cfg = write_config(
        tmp_path, refresh_token="r1", expires_at=NOW - 10, client_id="cid", client_secret="s"
    )

    auth.valid_token(cfg, now=NOW)

    saved = json.loads(cfg.read_text())
    assert saved["oauth_token"] == "a2"
    assert saved["refresh_token"] == "r2"
    assert saved["expires_at"] == NOW + 3600
    assert saved["refresh_token_expires_at"] == NOW + 2592000
    assert saved["subdomain"] == "acme"


@patch("zendesk_mcp.auth.httpx.post")
def test_refresh_preserves_unrelated_config_keys(mock_post, tmp_path):
    mock_post.return_value = token_response()
    cfg = write_config(
        tmp_path,
        refresh_token="r1",
        expires_at=NOW - 10,
        client_id="cid",
        git_zen_field_id=4321,
        attachment_cache_dir="~/.cache/zendesk-mcp/attachments",
    )

    auth.valid_token(cfg, now=NOW)

    saved = json.loads(cfg.read_text())
    assert saved["git_zen_field_id"] == 4321
    assert saved["attachment_cache_dir"] == "~/.cache/zendesk-mcp/attachments"


def test_valid_token_expired_without_refresh_token_tells_user_to_rerun_setup(tmp_path):
    """The currently-broken population: token expires but nothing on disk can renew it."""
    cfg = write_config(tmp_path, expires_at=NOW - 10)
    with pytest.raises(TokenExpiredError) as exc:
        auth.valid_token(cfg, now=NOW)
    assert "zendesk-mcp setup" in str(exc.value)


@patch("zendesk_mcp.auth.httpx.post")
def test_valid_token_raises_when_refresh_token_rejected(mock_post, tmp_path):
    """Refresh token expired (30d) or revoked -> user must re-authorize."""
    mock_post.return_value = httpx.Response(
        status_code=400,
        json={"error": "invalid_grant", "error_description": "The refresh token is invalid."},
        request=httpx.Request("POST", "https://acme.zendesk.com/oauth/tokens"),
    )
    cfg = write_config(
        tmp_path, refresh_token="dead", expires_at=NOW - 10, client_id="cid", client_secret="s"
    )

    with pytest.raises(TokenExpiredError) as exc:
        auth.valid_token(cfg, now=NOW)
    assert "zendesk-mcp setup" in str(exc.value)


@patch("zendesk_mcp.auth.httpx.post")
def test_refresh_rejects_malformed_subdomain_before_sending_credentials(
    mock_post, tmp_path
):
    cfg = write_config(
        tmp_path,
        subdomain="attacker.example/path",
        refresh_token="refresh-secret",
        expires_at=NOW - 10,
        client_id="cid",
        client_secret="client-secret",
    )

    with pytest.raises(auth.InvalidZendeskSubdomainError):
        auth.valid_token(cfg, now=NOW)

    mock_post.assert_not_called()


@patch("zendesk_mcp.auth.httpx.post")
def test_refresh_does_not_clobber_a_token_another_process_already_renewed(mock_post, tmp_path):
    """Two MCP servers share one config file; re-read before refreshing to avoid a rotation race."""
    cfg = write_config(tmp_path, refresh_token="r1", expires_at=NOW - 10, client_id="cid")
    stale = json.loads(cfg.read_text())
    # Another process refreshed after we loaded our copy.
    cfg.write_text(json.dumps({**stale, "oauth_token": "fresh-from-other", "expires_at": NOW + 5000}))

    subdomain, token = auth.valid_token(cfg, now=NOW, cfg_snapshot=stale)

    assert token == "fresh-from-other"
    mock_post.assert_not_called()


@patch("zendesk_mcp.auth.httpx.post")
def test_concurrent_refresh_uses_rotating_refresh_token_once(mock_post, tmp_path):
    """Only one process may redeem Zendesk's single-use refresh token."""
    first_post_started = threading.Event()
    second_worker_started = threading.Event()
    second_post_started = threading.Event()
    release_first_post = threading.Event()

    def delayed_response(*args, **kwargs):
        if mock_post.call_count == 1:
            first_post_started.set()
        else:
            second_post_started.set()
        assert release_first_post.wait(timeout=2)
        return token_response(access="shared-new-token", refresh="rotated-refresh")

    def second_refresh():
        second_worker_started.set()
        return auth.valid_token(cfg, NOW, stale)

    mock_post.side_effect = delayed_response
    cfg = write_config(
        tmp_path,
        refresh_token="single-use-refresh",
        expires_at=NOW - 10,
        client_id="cid",
        client_secret="s",
    )
    stale = json.loads(cfg.read_text())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(auth.valid_token, cfg, NOW, stale)
        assert first_post_started.wait(timeout=2)
        second = pool.submit(second_refresh)

        try:
            assert second_worker_started.wait(timeout=2)
            # If the file lock is missing, the second worker reaches the token endpoint
            # while the first request is deliberately held open.
            assert not second_post_started.wait(timeout=0.1)
            assert not second.done()
            assert mock_post.call_count == 1
        finally:
            release_first_post.set()

        assert first.result(timeout=2)[1] == "shared-new-token"
        assert second.result(timeout=2)[1] == "shared-new-token"

    mock_post.assert_called_once()


# --- error classification --------------------------------------------------


@patch("zendesk_mcp.auth.load_config", return_value={"subdomain": "acme"})
def test_api_error_message_upgrades_httpx_401(_mock_load):
    request = httpx.Request(
        "GET", "https://acme.zendesk.com/api/v2/users/search.json"
    )
    exc = httpx.HTTPStatusError(
        "Client error '401 Unauthorized' for url 'https://acme.zendesk.com/api/v2/users/search.json'",
        request=request,
        response=httpx.Response(401, json=EXPIRED_401, request=request),
    )
    msg = auth.api_error_message(exc)
    assert "zendesk-mcp setup" in msg
    assert "expired" in msg.lower()


def test_api_error_message_upgrades_zenpy_invalid_token():
    from zenpy.lib.exception import APIException

    msg = auth.api_error_message(APIException(json.dumps(EXPIRED_401)))
    assert "zendesk-mcp setup" in msg


def test_api_error_message_leaves_other_errors_alone():
    msg = auth.api_error_message(Exception("404 Not Found"))
    assert msg == "Zendesk API error: 404 Not Found"
    assert "zendesk-mcp setup" not in msg


def test_api_error_message_does_not_flag_rate_limit():
    msg = auth.api_error_message(Exception("429 Too Many Requests"))
    assert "zendesk-mcp setup" not in msg


def test_api_error_message_does_not_flag_a_ticket_numbered_401():
    msg = auth.api_error_message(Exception("Ticket #401 not found"))
    assert "zendesk-mcp setup" not in msg


def test_api_error_message_does_not_flag_a_status_code_containing_401():
    msg = auth.api_error_message(Exception("upstream returned 4013"))
    assert "zendesk-mcp setup" not in msg


def test_api_error_message_flags_unauthorized_wording():
    msg = auth.api_error_message(Exception("Client error '401 Unauthorized' for url ..."))
    assert "zendesk-mcp setup" in msg


@patch("zendesk_mcp.auth.load_config", return_value={"subdomain": "acme"})
def test_api_error_message_does_not_flag_same_origin_unrelated_401(_mock_load):
    exc = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=httpx.Request("GET", "https://acme.zendesk.com/api/v2/users/me.json"),
        response=httpx.Response(
            401,
            json={"error": "unauthorized", "description": "missing permission"},
            request=httpx.Request(
                "GET", "https://acme.zendesk.com/api/v2/users/me.json"
            ),
        ),
    )

    msg = auth.api_error_message(exc)

    assert msg.startswith("Zendesk API error:")
    assert "zendesk-mcp setup" not in msg


@patch("zendesk_mcp.auth.load_config", return_value={"subdomain": "acme"})
def test_api_error_message_does_not_flag_external_redirect_401(_mock_load):
    request = httpx.Request("GET", "https://uploads.example.test/temporary-file")
    exc = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=request,
        response=httpx.Response(401, json=EXPIRED_401, request=request),
    )

    msg = auth.api_error_message(exc)

    assert msg.startswith("Zendesk API error:")
    assert "zendesk-mcp setup" not in msg


@patch("zendesk_mcp.auth.load_config", return_value={"subdomain": "acme"})
def test_api_error_message_does_not_flag_structured_non_401_invalid_token(
    _mock_load,
):
    request = httpx.Request("GET", "https://acme.zendesk.com/api/v2/tickets.json")
    exc = httpx.HTTPStatusError(
        "403 Forbidden",
        request=request,
        response=httpx.Response(403, json=EXPIRED_401, request=request),
    )

    msg = auth.api_error_message(exc)

    assert msg.startswith("Zendesk API error:")
    assert "zendesk-mcp setup" not in msg


# --- reactive refresh on 401 ----------------------------------------------


@patch("zendesk_mcp.auth.httpx.request")
@patch("zendesk_mcp.auth.httpx.post")
def test_request_refreshes_and_retries_once_on_401(mock_post, mock_request, tmp_path):
    """Server-side revocation / clock skew: a 401 should trigger one refresh + retry."""
    mock_post.return_value = token_response(access="tok2")
    ok = httpx.Response(
        200, json={"users": []}, request=httpx.Request("GET", "https://acme.zendesk.com/x")
    )
    unauthorized = httpx.Response(
        401, json=EXPIRED_401, request=httpx.Request("GET", "https://acme.zendesk.com/x")
    )
    mock_request.side_effect = [unauthorized, ok]
    cfg = write_config(
        tmp_path,
        refresh_token="r1",
        expires_at=NOW + 100_000,  # looks valid, but server disagrees
        client_id="cid",
        client_secret="s",
    )

    response = auth.request("GET", "https://acme.zendesk.com/x", config_file=cfg, now=NOW)

    assert response.status_code == 200
    assert mock_request.call_count == 2
    mock_post.assert_called_once()
    # retry must carry the refreshed bearer token
    assert mock_request.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer tok2"


@patch("zendesk_mcp.auth.httpx.request")
@patch("zendesk_mcp.auth.httpx.post")
def test_request_does_not_retry_more_than_once(mock_post, mock_request, tmp_path):
    mock_post.return_value = token_response()
    unauthorized = httpx.Response(
        401, json=EXPIRED_401, request=httpx.Request("GET", "https://acme.zendesk.com/x")
    )
    mock_request.side_effect = [unauthorized, unauthorized]
    cfg = write_config(
        tmp_path, refresh_token="r1", expires_at=NOW + 100_000, client_id="cid", client_secret="s"
    )

    with pytest.raises(TokenExpiredError):
        auth.request("GET", "https://acme.zendesk.com/x", config_file=cfg, now=NOW)

    assert mock_request.call_count == 2


@patch("zendesk_mcp.auth.httpx.request")
def test_request_injects_bearer_token(mock_request, tmp_path):
    mock_request.return_value = httpx.Response(
        200, json={}, request=httpx.Request("GET", "https://acme.zendesk.com/x")
    )
    cfg = write_config(tmp_path)

    auth.request("GET", "https://acme.zendesk.com/x", config_file=cfg, now=NOW)

    assert mock_request.call_args.kwargs["headers"]["Authorization"] == "Bearer old-tok"


@patch("zendesk_mcp.auth.httpx.request")
@patch("zendesk_mcp.auth.valid_token", return_value=("other", "other-token"))
def test_request_rejects_subdomain_change_before_initial_send(
    _mock_valid_token, mock_request, tmp_path
):
    cfg = write_config(tmp_path)

    with pytest.raises(auth.UnsafeZendeskUrlError, match="changed before"):
        auth.request("GET", "https://acme.zendesk.com/x", config_file=cfg, now=NOW)

    mock_request.assert_not_called()


@patch("zendesk_mcp.auth.refresh_rejected_token")
@patch("zendesk_mcp.auth.httpx.request")
def test_request_rejects_subdomain_change_before_retry(
    mock_request, mock_refresh, tmp_path
):
    mock_request.return_value = httpx.Response(
        401,
        json=EXPIRED_401,
        request=httpx.Request("GET", "https://acme.zendesk.com/x"),
    )
    mock_refresh.return_value = {
        "subdomain": "other",
        "oauth_token": "other-token",
        "refresh_token": "other-refresh",
        "client_id": "cid",
    }
    cfg = write_config(
        tmp_path,
        refresh_token="r1",
        expires_at=NOW + 100_000,
        client_id="cid",
    )

    with pytest.raises(auth.UnsafeZendeskUrlError, match="changed while refreshing"):
        auth.request("GET", "https://acme.zendesk.com/x", config_file=cfg, now=NOW)

    mock_request.assert_called_once()


@patch("zendesk_mcp.auth.httpx.request")
@patch("zendesk_mcp.auth.httpx.post")
def test_request_rejects_untrusted_url_before_sending_or_refreshing(
    mock_post, mock_request, tmp_path
):
    cfg = write_config(
        tmp_path,
        refresh_token="r1",
        expires_at=NOW - 10,
        client_id="cid",
        client_secret="s",
    )

    with pytest.raises(auth.UnsafeZendeskUrlError):
        auth.request("GET", "https://attacker.example/file", config_file=cfg, now=NOW)

    mock_post.assert_not_called()
    mock_request.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.zendesk.com/x",
        "https://acme.zendesk.com.evil.example/x",
        "https://acme.zendesk.com@evil.example/x",
        "https://acme.zendesk.com:444/x",
    ],
)
def test_validate_zendesk_url_rejects_lookalike_origins(url):
    with pytest.raises(auth.UnsafeZendeskUrlError):
        auth.validate_zendesk_url(url, "acme")


@patch("zendesk_mcp.auth.httpx.request")
@patch("zendesk_mcp.auth.httpx.post")
def test_request_does_not_refresh_on_unrelated_401(mock_post, mock_request, tmp_path):
    mock_request.return_value = httpx.Response(
        401,
        json={"error": "unauthorized", "description": "missing permission"},
        request=httpx.Request("GET", "https://acme.zendesk.com/x"),
    )
    cfg = write_config(
        tmp_path,
        refresh_token="r1",
        expires_at=NOW + 100_000,
        client_id="cid",
        client_secret="s",
    )

    response = auth.request("GET", "https://acme.zendesk.com/x", config_file=cfg, now=NOW)

    assert response.status_code == 401
    mock_post.assert_not_called()
    mock_request.assert_called_once()


@patch("zendesk_mcp.auth.httpx.request")
@patch("zendesk_mcp.auth.httpx.post")
def test_request_does_not_refresh_for_external_redirect_response(
    mock_post, mock_request, tmp_path
):
    mock_request.return_value = httpx.Response(
        401,
        json=EXPIRED_401,
        request=httpx.Request("GET", "https://uploads.example.test/temporary-file"),
    )
    cfg = write_config(
        tmp_path,
        refresh_token="r1",
        expires_at=NOW + 100_000,
        client_id="cid",
        client_secret="s",
    )

    response = auth.request(
        "GET",
        "https://acme.zendesk.com/attachments/token/file",
        config_file=cfg,
        now=NOW,
        follow_redirects=True,
    )

    assert response.status_code == 401
    mock_post.assert_not_called()
    mock_request.assert_called_once()


# --- API-token authentication -------------------------------------------------------
# An OAuth grant is created by a person signing in and dies with their account, which is
# how an unattended integration ends up depending on one employee. An API token issued to
# a shared service account does not.

import base64 as _base64


def test_api_token_credentials_requires_both_halves():
    from zendesk_mcp import auth
    assert auth.api_token_credentials({"email": "a@b.com", "api_token": "t"}) == ("a@b.com", "t")
    assert auth.api_token_credentials({"email": "a@b.com"}) is None
    assert auth.api_token_credentials({"api_token": "t"}) is None
    assert auth.api_token_credentials({"email": " ", "api_token": "t"}) is None
    assert auth.api_token_credentials({}) is None


def test_authorization_header_uses_basic_for_api_token():
    from zendesk_mcp import auth
    header = auth.authorization_header({"email": "contact@bebopbee.com", "api_token": "tok"})
    assert header.startswith("Basic ")
    decoded = _base64.b64decode(header.split(" ", 1)[1]).decode()
    # Zendesk's scheme: the username is "<email>/token".
    assert decoded == "contact@bebopbee.com/token:tok"


def test_authorization_header_falls_back_to_bearer():
    from zendesk_mcp import auth
    assert auth.authorization_header({}, "abc123") == "Bearer abc123"


def test_api_token_never_looks_expired():
    from zendesk_mcp import auth
    # An expires_at left behind by a previous OAuth setup must not make a token config
    # look stale and send the client into a refresh it cannot perform.
    cfg = {"email": "a@b.com", "api_token": "t", "expires_at": 1}
    assert auth.is_expired(cfg, now=10_000_000_000) is False


def test_valid_token_returns_api_token_without_refreshing(tmp_path, monkeypatch):
    from zendesk_mcp import auth
    cfg = {"subdomain": "bebopbeehelp", "email": "a@b.com", "api_token": "tok",
           "expires_at": 1, "refresh_token": "r", "client_id": "c"}
    def explode(*a, **k):
        raise AssertionError("must not attempt a refresh for an API token")
    monkeypatch.setattr(auth, "_refresh_under_lock", explode)
    assert auth.valid_token(cfg_snapshot=cfg) == ("bebopbeehelp", "tok")


def test_request_sends_basic_auth_for_api_token(monkeypatch, tmp_path):
    """The wire format, not just the helper — this is what Zendesk actually receives."""
    import json as _json
    from zendesk_mcp import auth

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(_json.dumps(
        {"subdomain": "bebopbeehelp", "email": "contact@bebopbee.com", "api_token": "tok"}))

    seen = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"
        def json(self): return {}

    def fake_request(method, url, **kwargs):
        seen["headers"] = kwargs.get("headers", {})
        return _Resp()

    monkeypatch.setattr(auth.httpx, "request", fake_request)
    auth.request("GET", "https://bebopbeehelp.zendesk.com/api/v2/groups.json",
                 config_file=cfg_file)
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert _base64.b64decode(seen["headers"]["Authorization"].split()[1]).decode() \
        == "contact@bebopbee.com/token:tok"
