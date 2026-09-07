import pytest
from datetime import datetime
from unittest.mock import MagicMock


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
