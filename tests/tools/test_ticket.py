import pytest
import json
from unittest.mock import patch, MagicMock
from tests.conftest import make_mock_ticket
from zendesk_mcp.client import ConfigError
from zendesk_mcp.errors import ToolError


@patch("zendesk_mcp.tools.ticket.get_client")
def test_get_ticket_returns_structured_fields(mock_get_client):
    mock_client = MagicMock()
    mock_client.tickets.return_value = make_mock_ticket()
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _get_ticket_data
    result = json.loads(_get_ticket_data(12345))

    assert result["id"] == 12345
    assert result["subject"] == "Login fails after password reset"
    assert result["status"] == "open"
    assert result["priority"] == "high"
    assert result["requester"]["email"] == "jane@customer.com"
    assert result["assignee"]["name"] == "Test Agent"
    assert result["group"] == "Support"
    assert "auth" in result["tags"]
    assert "zendesk.com" in result["ticket_url"]


@patch("zendesk_mcp.tools.ticket.get_client")
def test_get_ticket_returns_custom_fields(mock_get_client):
    """custom_fields carries the per-game metadata (player profile uid, game, tier),
    which is the join key into analytics. Dropping it makes the ticket unattributable."""
    mock_client = MagicMock()
    mock_client.tickets.return_value = make_mock_ticket()
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _get_ticket_data
    result = json.loads(_get_ticket_data(12345))

    assert "custom_fields" in result
    by_id = {f["id"]: f["value"] for f in result["custom_fields"]}
    assert by_id[30481513007639] == "d5ef2ec1-ea9e-40c1-8da5-dbc5dd877b9e"
    assert by_id[30390717200919] == "Travel Crush 2.0"
    # unset fields are preserved as None rather than filtered out, so callers can
    # distinguish "not set on this ticket" from "not returned by the API"
    assert 30390695540503 in by_id and by_id[30390695540503] is None


@patch("zendesk_mcp.tools.ticket.get_client")
def test_get_ticket_custom_fields_defaults_to_empty_list(mock_get_client):
    mock_client = MagicMock()
    ticket = make_mock_ticket()
    ticket.custom_fields = None
    mock_client.tickets.return_value = ticket
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _get_ticket_data
    result = json.loads(_get_ticket_data(12345))

    assert result["custom_fields"] == []


@patch("zendesk_mcp.tools.ticket.get_client")
def test_get_ticket_raises_on_config_error(mock_get_client):
    mock_get_client.side_effect = ConfigError("Zendesk not configured. Run: zendesk-mcp setup")

    from zendesk_mcp.tools.ticket import _get_ticket_data
    with pytest.raises(ToolError) as err:
        _get_ticket_data(12345)
    result = str(err.value)

    assert "zendesk-mcp setup" in result
    assert not result.startswith("{")


@patch("zendesk_mcp.tools.ticket.get_client")
def test_get_ticket_raises_on_not_found(mock_get_client):
    mock_client = MagicMock()
    mock_client.tickets.side_effect = Exception("RecordNotFound: Couldn't find Ticket with id=99999")
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _get_ticket_data
    with pytest.raises(ToolError) as err:
        _get_ticket_data(99999)
    result = str(err.value)

    assert "99999" in result
    assert "not found" in result.lower()


@patch("zendesk_mcp.tools.ticket.get_client")
def test_search_tickets_with_keywords_includes_keyword_in_query(mock_get_client):
    mock_client = MagicMock()
    mock_client.search.return_value = iter([])
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _search_tickets_data
    _search_tickets_data(keywords="login failure", status=None, limit=10)

    call_args = mock_client.search.call_args
    assert "login failure" in call_args.kwargs["query"]
    assert "type:ticket" in call_args.kwargs["query"]


@patch("zendesk_mcp.tools.ticket.get_client")
def test_search_tickets_with_keywords_and_status(mock_get_client):
    mock_client = MagicMock()
    mock_client.search.return_value = iter([])
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _search_tickets_data
    _search_tickets_data(keywords="LDAP auth", status="open", limit=10)

    query = mock_client.search.call_args.kwargs["query"]
    assert "LDAP auth" in query
    assert "status:open" in query


@patch("zendesk_mcp.tools.ticket.get_client")
def test_search_tickets_no_keywords_behaves_as_before(mock_get_client):
    mock_client = MagicMock()
    mock_client.search.return_value = iter([])
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _search_tickets_data
    _search_tickets_data(keywords=None, status=None, limit=5)

    query = mock_client.search.call_args.kwargs["query"]
    assert query == "type:ticket"


@patch("zendesk_mcp.tools.ticket.get_client")
def test_search_and_get_report_the_channel_each_ticket_arrived_on(mock_get_client):
    from zenpy.lib.api_objects import Via
    ticket = make_mock_ticket()
    ticket.via = Via(channel="facebook")
    mock_client = MagicMock()
    mock_client.search.return_value = [ticket]
    mock_client.tickets.return_value = ticket
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.ticket import _search_tickets_data, _get_ticket_data
    assert json.loads(_search_tickets_data(keywords=None, status=None, limit=5))[0]["channel"] == "facebook"
    assert json.loads(_get_ticket_data(12345))["channel"] == "facebook"


@patch("zendesk_mcp.tools.ticket.get_client")
def test_search_rejected_by_zendesk_raises_instead_of_returning_prose(mock_get_client):
    mock_get_client.return_value.search.side_effect = Exception(
        '{"error": "invalid", "description": "Invalid search: Error filtering on field: via_id"}')

    from zendesk_mcp.tools.ticket import _search_tickets_data
    with pytest.raises(ToolError, match="Zendesk API error.*Invalid search"):
        _search_tickets_data(keywords="via:messenger", status=None, limit=5)
