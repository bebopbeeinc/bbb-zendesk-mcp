from unittest.mock import patch

import httpx

from zendesk_mcp.setup import _exchange_code


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
