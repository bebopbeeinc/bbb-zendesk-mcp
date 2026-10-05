import pytest
import json
from datetime import datetime
from unittest.mock import patch, MagicMock
from tests.conftest import make_mock_comment, make_mock_attachment, make_mock_user
from zendesk_mcp.client import ConfigError
from zendesk_mcp.errors import ToolError


def _make_client_with_comments(comments):
    mock_client = MagicMock()
    mock_client.tickets.comments.return_value = iter(comments)
    mock_client.users.return_value = make_mock_user()
    return mock_client


@patch("zendesk_mcp.tools.comments.get_client")
def test_get_comments_returns_list(mock_get_client):
    comment = make_mock_comment(comment_id=1, body="Please help.", public=True)
    mock_get_client.return_value = _make_client_with_comments([comment])

    from zendesk_mcp.tools.comments import _get_comments_data
    result = json.loads(_get_comments_data(12345))

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["body"] == "Please help."
    assert result[0]["is_public"] is True


@patch("zendesk_mcp.tools.comments.get_client")
def test_get_comments_includes_attachment_metadata(mock_get_client):
    att = make_mock_attachment("bundle.zip", "application/zip", 2048, "https://cdn.zendesk.com/1")
    comment = make_mock_comment(comment_id=2, attachments=[att])
    mock_get_client.return_value = _make_client_with_comments([comment])

    from zendesk_mcp.tools.comments import _get_comments_data
    result = json.loads(_get_comments_data(12345))

    attachments = result[0]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "bundle.zip"
    assert attachments[0]["content_type"] == "application/zip"
    assert attachments[0]["size_bytes"] == 2048
    assert "content_url" in attachments[0]


@patch("zendesk_mcp.tools.comments.get_client")
def test_get_comments_raises_on_config_error(mock_get_client):
    mock_get_client.side_effect = ConfigError("Zendesk not configured. Run: zendesk-mcp setup")

    from zendesk_mcp.tools.comments import _get_comments_data
    with pytest.raises(ToolError) as err:
        _get_comments_data(12345)
    result = str(err.value)

    assert "zendesk-mcp setup" in result
    assert not result.startswith("[")


@patch("zendesk_mcp.tools.comments.load_config", return_value={"subdomain": "example"})
@patch("zendesk_mcp.tools.comments.get_client")
def test_get_comments_lists_messaging_transcript_uploads(mock_get_client, _config):
    from tests.conftest import MESSENGER_TRANSCRIPT
    comment = make_mock_comment(comment_id=7, body=MESSENGER_TRANSCRIPT)
    mock_get_client.return_value = _make_client_with_comments([comment])

    from zendesk_mcp.tools.comments import _get_comments_data
    [result] = json.loads(_get_comments_data(12345))

    assert result["transcript_uploads"] == [{
        "file_name": "Qx7LmN2pRs4tUv6wXy8zAb1c.jpeg",
        "url": "https://example.zendesk.com/sc/attachments/v2/01J8ZQ4M7K2V9X3B5N6P1R0T2Y/Qx7LmN2pRs4tUv6wXy8zAb1c.jpeg",
        "content_type": "image/jpeg",
        "size": 226766,
        "time": "12:01:36",
        "uploaded_by": "Player One",
    }]
    # Additive: the existing fields are as they were, and a Messaging upload is not an attachment.
    assert result["body"] == MESSENGER_TRANSCRIPT
    assert result["attachments"] == []
    assert set(result) == {"id", "author", "created_at", "is_public", "body", "attachments", "transcript_uploads"}


@patch("zendesk_mcp.tools.comments.load_config", return_value={"subdomain": "example"})
@patch("zendesk_mcp.tools.comments.get_client")
def test_get_comments_transcript_uploads_is_empty_without_uploads(mock_get_client, _config):
    att = make_mock_attachment("bundle.zip", "application/zip", 2048, "https://cdn.zendesk.com/1")
    comment = make_mock_comment(comment_id=2, body="See the attached bundle.", attachments=[att])
    mock_get_client.return_value = _make_client_with_comments([comment])

    from zendesk_mcp.tools.comments import _get_comments_data
    [result] = json.loads(_get_comments_data(12345))

    assert result["transcript_uploads"] == []
    assert len(result["attachments"]) == 1
