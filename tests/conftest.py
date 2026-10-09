import pytest
from datetime import datetime
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _no_attachment_root(monkeypatch):
    """A developer's own ZENDESK_MCP_ATTACHMENT_ROOT must not change what the suite tests."""
    monkeypatch.delenv("ZENDESK_MCP_ATTACHMENT_ROOT", raising=False)


# Anonymised copies of real Zendesk Messaging transcript comments: the shape is the live one
# (a Facebook Messenger ticket and an in-app native_messaging one), the names, ids and file
# tokens are made up. The account host is example.zendesk.com, so tests configure "example".
MESSENGER_TRANSCRIPT = """Conversation with Player One

(12:00:58) Player One: hi my streak reset after the update
(12:01:36) Player One uploaded: Qx7LmN2pRs4tUv6wXy8zAb1c.jpeg
URL: https://example.zendesk.com/sc/attachments/v2/01J8ZQ4M7K2V9X3B5N6P1R0T2Y/Qx7LmN2pRs4tUv6wXy8zAb1c.jpeg
Type: image/jpeg
Size: 226766

(12:02:15) Support Bot: Thanks, we are looking into it.
"""

IN_GAME_TRANSCRIPT = """(09:14:02) Player Two: the shop took my coins
(09:14:20) Player Two uploaded: pickedMedia.jpg
URL: https://example.zendesk.com/sc/attachments/v2/01J8ZR5D3F7H9K1M3P5R7T9V1X/pickedMedia.jpg
Type: image/jpeg
Size: 98304
(09:14:40) Player Two uploaded: Screenshot_20260101-120000.jpg
URL: https://example.zendesk.com/sc/attachments/v2/01J8ZR5D3F7H9K1M3P5R7T9V1X/Screenshot_20260101-120000.jpg
Type: image/jpeg
Size: 412001
(09:15:03) Support Agent uploaded: how-to-restore.png
URL: https://example.zendesk.com/sc/attachments/v2/01J8ZR5D3F7H9K1M3P5R7T9V1X/how-to-restore.png
Type: image/png
Size: 51200
"""


def make_mock_user(name="Jane Smith", email="jane@customer.com", role="end-user", user_id=101):
    user = MagicMock()
    user.id = user_id
    user.name = name
    user.email = email
    user.role = role
    return user


def make_mock_attachment(filename="debug.log", content_type="text/plain", size=1024, url="https://cdn.zendesk.com/attachments/1"):
    att = MagicMock()
    att.file_name = filename
    att.content_type = content_type
    att.size = size
    att.content_url = url
    return att


def make_mock_comment(comment_id=1, body="Customer reported login failure.", public=True, attachments=None):
    comment = MagicMock()
    comment.id = comment_id
    comment.body = body
    comment.public = public
    comment.author_id = 101
    comment.created_at = datetime(2026, 4, 20, 10, 0, 0)
    comment.via = MagicMock()
    comment.attachments = attachments or []
    return comment


def make_mock_ticket(ticket_id=12345, subject="Login fails after password reset"):
    ticket = MagicMock()
    ticket.id = ticket_id
    ticket.subject = subject
    ticket.status = "open"
    ticket.priority = "high"
    ticket.type = "problem"
    ticket.tags = ["auth", "login"]
    ticket.created_at = datetime(2026, 4, 20, 10, 0, 0)
    ticket.updated_at = datetime(2026, 4, 27, 9, 0, 0)
    ticket.description = "User cannot log in after resetting password."
    ticket.url = "https://example.zendesk.com/api/v2/tickets/12345.json"

    # Shape mirrors the live API: every field defined on the form is returned,
    # with value None for the ones that are not set on this ticket.
    ticket.custom_fields = [
        {"id": 30481513007639, "value": "d5ef2ec1-ea9e-40c1-8da5-dbc5dd877b9e"},
        {"id": 30390717200919, "value": "Travel Crush 2.0"},
        {"id": 30390677271959, "value": "Non Payer"},
        {"id": 360037612934, "value": "2.5.2"},
        {"id": 30390695540503, "value": None},
    ]

    ticket.requester = make_mock_user("Jane Smith", "jane@customer.com", "end-user")
    ticket.assignee = make_mock_user("Test Agent", "agent@example.com", "agent", 202)

    group = MagicMock()
    group.name = "Support"
    ticket.group = group

    return ticket


@pytest.fixture
def mock_ticket():
    return make_mock_ticket()


@pytest.fixture
def mock_comment():
    return make_mock_comment()
