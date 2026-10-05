"""Pictures and files sent through Zendesk Messaging, read out of the transcript comment.

A file sent over a Messaging channel (Facebook Messenger, the in-app ``native_messaging``
widget, ...) is not a Zendesk comment attachment: ``comment.attachments`` is empty. The
transcript comment's body carries one block per upload instead::

    (12:01:36) Player One uploaded: photo.jpeg
    URL: https://acme.zendesk.com/sc/attachments/v2/<conversation>/photo.jpeg
    Type: image/jpeg
    Size: 226766

The URL is on the account host and redirects to a signed download, so
zendesk_download_attachment can fetch it like any other attachment. Only URLs on the
configured Zendesk origin under ``/sc/attachments/`` are reported; anything else in a body
is text a player could have typed, and is ignored.
"""
import re
from urllib.parse import unquote, urlparse

from zendesk_mcp.auth import validate_zendesk_url

UPLOAD_PATH_PREFIX = "/sc/attachments/"

# "(12:01:36) Player One uploaded: photo.jpeg". The time and the uploader are optional, so a
# block whose header lacks either still parses; neither is ever filled in when absent.
_HEADER_RE = re.compile(
    r"^\s*(?:\(\s*(?P<time>[^()]*?)\s*\)\s*)?(?P<who>.*?)\s*\buploaded\s*:\s*(?P<file>.*?)\s*$",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(r"^\s*(?P<key>url|type|size)\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)


def is_transcript_upload_url(url: str, subdomain: str) -> bool:
    """True for a Messaging upload URL on the configured Zendesk origin."""
    try:
        validate_zendesk_url(url, subdomain)
    except ValueError:
        # UnsafeZendeskUrlError for a foreign origin, InvalidZendeskSubdomainError for an
        # unset or malformed subdomain: neither yields a URL worth reporting.
        return False
    path = urlparse(url).path
    if not path.startswith(UPLOAD_PATH_PREFIX):
        return False
    # "/sc/attachments/../api/v2/..." starts with the prefix but is normalised elsewhere.
    return not any(segment in (".", "..") for segment in unquote(path).split("/"))


def _record(header: re.Match, fields: dict, subdomain: str) -> dict | None:
    tokens = fields.get("url", "").split()
    url = tokens[0].strip("<>") if tokens else ""
    if not url or not is_transcript_upload_url(url, subdomain):
        return None

    size_text = fields.get("size", "").replace(",", "")
    file_name = header.group("file") or unquote(urlparse(url).path.rsplit("/", 1)[-1]) or None
    return {
        "file_name": file_name,
        "url": url,
        "content_type": fields.get("type") or None,
        "size": int(size_text) if size_text.isdigit() else None,
        "time": header.group("time") or None,
        "uploaded_by": header.group("who") or None,
    }


def parse_transcript_uploads(body, subdomain: str) -> list[dict]:
    """Every upload block in a Messaging transcript body, in order.

    Each is ``{file_name, url, content_type, size, time, uploaded_by}``; a missing Type or
    Size line, time, or uploader is None. A block without a valid upload URL is dropped.
    """
    if not isinstance(body, str) or "uploaded" not in body.lower():
        return []

    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    uploads = []
    i = 0
    while i < len(lines):
        header = _HEADER_RE.match(lines[i])
        i += 1
        if not header:
            continue
        fields: dict[str, str] = {}
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                if fields:
                    break
                i += 1
                continue
            field = _FIELD_RE.match(line)
            if not field or field.group("key").lower() in fields:
                break
            fields[field.group("key").lower()] = field.group("value")
            i += 1
        record = _record(header, fields, subdomain)
        if record:
            uploads.append(record)
    return uploads
