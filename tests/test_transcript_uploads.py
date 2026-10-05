"""Messaging uploads parsed out of transcript comment bodies.

A picture sent over Facebook Messenger or the in-app chat is not a comment attachment: the
transcript body names it in a "<who> uploaded: <file>" block. Only URLs on the configured
Zendesk origin under /sc/attachments/ come out; everything else is text a player could type.
"""
import pytest

from tests.conftest import IN_GAME_TRANSCRIPT, MESSENGER_TRANSCRIPT
from zendesk_mcp.transcript_uploads import is_transcript_upload_url, parse_transcript_uploads

BASE = "https://example.zendesk.com/sc/attachments/v2/01J8ZQ4M7K2V9X3B5N6P1R0T2Y"


def test_messenger_transcript_yields_its_upload():
    assert parse_transcript_uploads(MESSENGER_TRANSCRIPT, "example") == [{
        "file_name": "Qx7LmN2pRs4tUv6wXy8zAb1c.jpeg",
        "url": f"{BASE}/Qx7LmN2pRs4tUv6wXy8zAb1c.jpeg",
        "content_type": "image/jpeg",
        "size": 226766,
        "time": "12:01:36",
        "uploaded_by": "Player One",
    }]


def test_several_uploads_in_one_comment_in_order_including_the_agent_side():
    uploads = parse_transcript_uploads(IN_GAME_TRANSCRIPT, "example")
    assert [u["file_name"] for u in uploads] == [
        "pickedMedia.jpg", "Screenshot_20260101-120000.jpg", "how-to-restore.png",
    ]
    assert [u["uploaded_by"] for u in uploads] == ["Player Two", "Player Two", "Support Agent"]
    assert [u["time"] for u in uploads] == ["09:14:20", "09:14:40", "09:15:03"]
    assert uploads[2]["content_type"] == "image/png"
    assert uploads[1]["size"] == 412001


def test_crlf_line_endings_parse_the_same():
    crlf = IN_GAME_TRANSCRIPT.replace("\n", "\r\n")
    assert parse_transcript_uploads(crlf, "example") == parse_transcript_uploads(IN_GAME_TRANSCRIPT, "example")
    bare_cr = MESSENGER_TRANSCRIPT.replace("\n", "\r")
    assert len(parse_transcript_uploads(bare_cr, "example")) == 1


def test_extra_spaces_are_tolerated():
    body = (
        "   (12:01:36)    Player One    uploaded:    photo one.jpeg   \n"
        f"  URL :   {BASE}/photo%20one.jpeg   \n"
        "Type:image/jpeg\n"
        "  size :  1,024  \n"
    )
    assert parse_transcript_uploads(body, "example") == [{
        "file_name": "photo one.jpeg",
        "url": f"{BASE}/photo%20one.jpeg",
        "content_type": "image/jpeg",
        "size": 1024,
        "time": "12:01:36",
        "uploaded_by": "Player One",
    }]


@pytest.mark.parametrize("missing", ["Type", "Size"])
def test_a_missing_type_or_size_line_is_none(missing):
    lines = [
        "(12:01:36) Player One uploaded: a.jpeg",
        f"URL: {BASE}/a.jpeg",
        "Type: image/jpeg",
        "Size: 10",
    ]
    body = "\n".join(line for line in lines if not line.startswith(missing))
    [upload] = parse_transcript_uploads(body, "example")
    assert upload["url"] == f"{BASE}/a.jpeg"
    key = {"Type": "content_type", "Size": "size"}[missing]
    assert upload[key] is None


def test_an_uploader_or_time_the_transcript_does_not_give_is_never_invented():
    body = (
        f"(12:01:36) uploaded: a.jpeg\nURL: {BASE}/a.jpeg\n\n"
        f"Player One uploaded: b.jpeg\nURL: {BASE}/b.jpeg\n"
    )
    a, b = parse_transcript_uploads(body, "example")
    assert a["uploaded_by"] is None and a["time"] == "12:01:36"
    assert b["uploaded_by"] == "Player One" and b["time"] is None


def test_a_header_without_a_file_name_falls_back_to_the_url_file():
    body = f"(12:01:36) Player One uploaded:\nURL: {BASE}/c%20d.png\n"
    [upload] = parse_transcript_uploads(body, "example")
    assert upload["file_name"] == "c d.png"


def test_unparseable_size_is_none():
    body = f"(12:01:36) P uploaded: a.jpeg\nURL: {BASE}/a.jpeg\nSize: big\n"
    assert parse_transcript_uploads(body, "example")[0]["size"] is None


def test_url_in_angle_brackets_is_accepted():
    body = f"(12:01:36) P uploaded: a.jpeg\nURL: <{BASE}/a.jpeg>\n"
    assert parse_transcript_uploads(body, "example")[0]["url"] == f"{BASE}/a.jpeg"


@pytest.mark.parametrize("url", [
    "https://evil.example.com/sc/attachments/v2/x/a.jpeg",
    "https://other.zendesk.com/sc/attachments/v2/x/a.jpeg",
    "https://example.zendesk.com.evil.com/sc/attachments/v2/x/a.jpeg",
    "http://example.zendesk.com/sc/attachments/v2/x/a.jpeg",
    "https://example.zendesk.com:8443/sc/attachments/v2/x/a.jpeg",
    "https://user:pass@example.zendesk.com/sc/attachments/v2/x/a.jpeg",
    "https://example.zendesk.com/api/v2/users/me.json",
    "https://example.zendesk.com/attachments/token/abc/?name=a.jpeg",
    "https://example.zendesk.com/sc/attachmentsX/a.jpeg",
    "https://example.zendesk.com/sc/attachments/../../api/v2/users/me.json",
    "https://example.zendesk.com/sc/attachments/%2e%2e/%2e%2e/api/v2/users/me.json",
    "javascript:alert(1)",
    "",
])
def test_urls_off_the_configured_upload_path_are_ignored(url):
    body = f"(12:01:36) Player One uploaded: a.jpeg\nURL: {url}\nType: image/jpeg\nSize: 10\n"
    assert parse_transcript_uploads(body, "example") == []
    assert not is_transcript_upload_url(url, "example")


@pytest.mark.parametrize("subdomain", ["", "not a label", "example.zendesk.com"])
def test_an_unusable_subdomain_yields_nothing(subdomain):
    assert parse_transcript_uploads(MESSENGER_TRANSCRIPT, subdomain) == []


def test_the_subdomain_is_compared_case_insensitively():
    assert len(parse_transcript_uploads(MESSENGER_TRANSCRIPT, "Example")) == 1


@pytest.mark.parametrize("body", [None, 42, "", "Customer reported login failure."])
def test_bodies_without_uploads_yield_nothing(body):
    assert parse_transcript_uploads(body, "example") == []


def test_a_chat_line_mentioning_uploaded_is_not_an_upload():
    body = "(12:00:58) Player One: I uploaded: a screenshot yesterday\n(12:01:00) Player One: did you get it?\n"
    assert parse_transcript_uploads(body, "example") == []


def test_a_url_line_after_an_intervening_message_is_not_attributed_to_the_upload():
    body = (
        "(12:01:36) Player One uploaded: a.jpeg\n"
        "(12:01:40) Player One: one sec\n"
        f"URL: {BASE}/a.jpeg\n"
    )
    assert parse_transcript_uploads(body, "example") == []


def test_a_second_url_line_ends_the_block():
    body = (
        "(12:01:36) Player One uploaded: a.jpeg\n"
        f"URL: {BASE}/a.jpeg\n"
        f"URL: {BASE}/b.jpeg\n"
    )
    assert [u["url"] for u in parse_transcript_uploads(body, "example")] == [f"{BASE}/a.jpeg"]
