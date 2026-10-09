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

Every line is a player's text, so nothing here may cost more than linear time in it: Python's
``re`` holds the GIL while it matches, and a backtracking pattern over a long run of spaces
stalls the whole server, not only the call. The lines are stripped and split in Python, and
each pattern left starts at a literal and has a single quantifier, so none can backtrack.
"""
import re
from urllib.parse import unquote, urlparse

from zendesk_mcp.auth import validate_zendesk_url

UPLOAD_PATH_PREFIX = "/sc/attachments/"

# "uploaded:" as a word; the first one on a line splits it into "(time) who" and the file.
_UPLOADED_RE = re.compile(r"\buploaded\s*:", re.IGNORECASE)
# "(12:01:36)" at the start of the uploader part. No nested parentheses, as before.
_TIME_RE = re.compile(r"\(([^()]*)\)")
_FIELDS = ("url", "type", "size")
# A byte count. Longer than this is not a file Zendesk took, and int() of an unbounded digit
# string is itself an error (sys.int_info.default_max_str_digits).
_SIZE_RE = re.compile(r"[0-9]{1,15}")


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
    return not has_dot_segment(path)


def has_dot_segment(path: str) -> bool:
    """True when a URL path holds a "." or ".." segment, percent-encoded or not."""
    return any(segment in (".", "..") for segment in unquote(path).split("/"))


def _header(line: str) -> dict | None:
    """``{time, who, file}`` of an "(time) who uploaded: file" line, else None."""
    if "uploaded" not in line.lower():
        return None
    line = line.strip()
    marker = _UPLOADED_RE.search(line)
    if not marker:
        return None
    who = line[:marker.start()].strip()
    time = None
    stamp = _TIME_RE.match(who)
    if stamp:
        time = stamp.group(1).strip()
        who = who[stamp.end():].strip()
    return {"time": time, "who": who, "file": line[marker.end():].strip()}


def _field(line: str) -> tuple[str, str] | None:
    """``(key, value)`` of a "URL: ..." / "Type: ..." / "Size: ..." line, else None."""
    key, colon, value = line.strip().partition(":")
    key = key.strip().lower()
    if not colon or key not in _FIELDS:
        return None
    return key, value.strip()


def _record(header: dict, fields: dict, subdomain: str) -> dict | None:
    tokens = fields.get("url", "").split()
    url = tokens[0].strip("<>") if tokens else ""
    if not url or not is_transcript_upload_url(url, subdomain):
        return None

    size_text = fields.get("size", "").replace(",", "")
    file_name = header["file"] or unquote(urlparse(url).path.rsplit("/", 1)[-1]) or None
    return {
        "file_name": file_name,
        "url": url,
        "content_type": fields.get("type") or None,
        # ASCII digits only: str.isdigit() also passes "²", which int() rejects.
        "size": int(size_text) if _SIZE_RE.fullmatch(size_text) else None,
        "time": header["time"] or None,
        "uploaded_by": header["who"] or None,
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
        header = _header(lines[i])
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
            field = _field(line)
            if not field or field[0] in fields:
                break
            fields[field[0]] = field[1]
            i += 1
        record = _record(header, fields, subdomain)
        if record:
            uploads.append(record)
    return uploads
