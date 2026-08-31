"""End-to-end: an expired OAuth token must produce an actionable message, not an opaque 401.

Regression cover for the reported failure where the server "just stops working"
once the Zendesk access token expires.
"""
import json
from unittest.mock import patch, MagicMock

import httpx
from zenpy.lib.exception import APIException

from zendesk_mcp.auth import TokenExpiredError

EXPIRED_401 = {
    "error": "invalid_token",
    "error_description": "The access token provided is expired, revoked, malformed or invalid for other reasons.",
}


def unauthorized(url="https://acme.zendesk.com/api/v2/users/search.json"):
    return httpx.Response(401, json=EXPIRED_401, request=httpx.Request("GET", url))


@patch("zendesk_mcp.tools.users.auth.request")
def test_httpx_tool_reports_expired_token_actionably(mock_request):
    """Previously returned: "Client error '401 Unauthorized' ... check MDN" — no remediation."""
    mock_request.side_effect = TokenExpiredError(
        "Zendesk OAuth token expired and could not be refreshed. Re-run: zendesk-mcp setup"
    )
    from zendesk_mcp.tools.users import _search_users_data

    result = _search_users_data("jane@customer.com")

    assert "zendesk-mcp setup" in result
    assert "developer.mozilla.org" not in result


@patch("zendesk_mcp.tools.ticket.get_client")
def test_zenpy_tool_reports_expired_token_actionably(mock_get_client):
    client = MagicMock()
    client.tickets.side_effect = APIException(json.dumps(EXPIRED_401))
    mock_get_client.return_value = client
    from zendesk_mcp.tools.ticket import _get_ticket_data

    result = _get_ticket_data(12345)

    assert "zendesk-mcp setup" in result


@patch("zendesk_mcp.tools.ticket.get_client")
def test_zenpy_tool_surfaces_unrefreshable_token(mock_get_client):
    mock_get_client.side_effect = TokenExpiredError(
        "Zendesk OAuth token expired and could not be refreshed. Re-run: zendesk-mcp setup"
    )
    from zendesk_mcp.tools.ticket import _get_ticket_data

    result = _get_ticket_data(12345)

    assert "zendesk-mcp setup" in result


@patch("zendesk_mcp.tools.ticket.get_client")
def test_non_auth_errors_are_not_mislabelled_as_expiry(mock_get_client):
    client = MagicMock()
    client.tickets.side_effect = Exception("500 Internal Server Error")
    mock_get_client.return_value = client
    from zendesk_mcp.tools.ticket import _get_ticket_data

    result = _get_ticket_data(12345)

    assert "zendesk-mcp setup" not in result
    assert "500" in result


@patch("zendesk_mcp.tools.ticket.get_client")
def test_not_found_still_reported_as_not_found(mock_get_client):
    client = MagicMock()
    client.tickets.side_effect = Exception("RecordNotFound")
    mock_get_client.return_value = client
    from zendesk_mcp.tools.ticket import _get_ticket_data

    result = _get_ticket_data(999)

    assert "not found" in result.lower()
    assert "zendesk-mcp setup" not in result
