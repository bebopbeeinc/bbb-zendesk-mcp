"""ZENDESK_MCP_ATTACHMENT_ROOT: every attachment write stays inside one directory, and only
attachments are fetched.

Unset, downloads go where they always have. Set, the default location is <root>/<ticket_id>,
and the download directory, the file, and any archive's unpack directory must resolve
(symlinks resolved) inside the root, or the call is refused before anything is fetched; the URL
must be an attachment's or a Messaging upload's on the account host, and the bearer token goes
to that first hop only.
"""
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from zendesk_mcp.config import ATTACHMENT_ROOT_ENV, attachment_cache_dir
from zendesk_mcp.errors import ToolError


@pytest.fixture(autouse=True)
def _account():
    """The configured account: a confined download only fetches addresses on its host."""
    with patch("zendesk_mcp.tools.attachments.load_config", return_value={"subdomain": "example"}):
        yield


@pytest.fixture
def fetch():
    """The Zendesk download, faked, so a test can assert a refused write fetched nothing."""
    with patch("zendesk_mcp.tools.attachments.auth.request") as request:
        yield request


def _download(fetch, filename="notes.txt", dest_dir=None, content=b"hello", ticket_id=12345):
    from zendesk_mcp.tools.attachments import _download_attachment_data
    fetch.return_value = MagicMock(content=content, raise_for_status=lambda: None)
    return _download_attachment_data(
        "https://example.zendesk.com/sc/attachments/v2/c/" + filename, filename, ticket_id, dest_dir,
    )


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "root"
    monkeypatch.setenv(ATTACHMENT_ROOT_ENV, str(path))
    return Path(os.path.realpath(path))


@pytest.fixture
def outside(tmp_path):
    path = tmp_path / "outside"
    path.mkdir()
    return path


# --- unset: today's behaviour -------------------------------------------------------------

def test_unset_default_location_is_the_configured_cache_dir(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"attachment_cache_dir": str(tmp_path / "cache")}))
    assert attachment_cache_dir(5, config_file) == tmp_path / "cache" / "5"


def test_unset_default_location_falls_back_to_the_user_cache(tmp_path):
    assert attachment_cache_dir(5, tmp_path / "missing.json") == \
        Path("~/.cache/zendesk-mcp/attachments").expanduser() / "5"


def test_unset_dest_dir_anywhere_is_still_allowed(tmp_path, fetch):
    result = json.loads(_download(fetch, dest_dir=str(tmp_path / "anywhere")))
    assert result["cached_path"] == str(tmp_path / "anywhere" / "notes.txt")


# --- set ----------------------------------------------------------------------------------

def test_root_default_location_is_root_slash_ticket(root, fetch):
    result = json.loads(_download(fetch, ticket_id=8838))
    assert result["cached_path"] == str(root / "8838" / "notes.txt")
    assert (root / "8838" / "notes.txt").read_bytes() == b"hello"


def test_root_overrides_a_configured_cache_dir(tmp_path, root):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"attachment_cache_dir": str(tmp_path / "cache")}))
    assert attachment_cache_dir(5, config_file) == root / "5"


def test_root_reached_through_a_symlink_is_resolved(tmp_path, monkeypatch, fetch):
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real)
    monkeypatch.setenv(ATTACHMENT_ROOT_ENV, str(link))
    result = json.loads(_download(fetch, dest_dir=str(link / "7")))
    assert (real / "7" / "notes.txt").read_bytes() == b"hello"
    assert result["cached_path"] == str(link / "7" / "notes.txt")


def test_dest_dir_inside_the_root_is_allowed(root, fetch):
    result = json.loads(_download(fetch, dest_dir=str(root / "work" / "8807")))
    assert result["cached_path"] == str(root / "work" / "8807" / "notes.txt")


@pytest.mark.parametrize("make_dest", [
    lambda root, outside: outside,
    lambda root, outside: root / ".." / "outside",
    lambda root, outside: Path("/"),
])
def test_dest_dir_outside_the_root_is_refused_before_anything_is_fetched(root, outside, make_dest, fetch):
    dest = make_dest(root, outside)
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch, dest_dir=str(dest))
    fetch.assert_not_called()
    assert not (outside / "notes.txt").exists()


def test_a_symlinked_dest_dir_pointing_outside_is_refused(root, outside, fetch):
    root.mkdir(parents=True)
    (root / "escape").symlink_to(outside)
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch, dest_dir=str(root / "escape" / "deeper"))
    fetch.assert_not_called()
    assert list(outside.iterdir()) == []


def test_a_symlinked_ticket_dir_pointing_outside_is_refused(root, outside, fetch):
    root.mkdir(parents=True)
    (root / "12345").symlink_to(outside)
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch)
    assert list(outside.iterdir()) == []


def test_a_file_that_is_a_symlink_pointing_outside_is_refused(root, outside, fetch):
    victim = outside / "victim.txt"
    victim.write_text("keep me")
    (root / "12345").mkdir(parents=True)
    (root / "12345" / "notes.txt").symlink_to(victim)
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch)
    fetch.assert_not_called()
    assert victim.read_text() == "keep me"


@pytest.mark.parametrize("value", ["", "   ", "relative/dir", "./attachments"])
def test_a_blank_or_relative_root_is_refused(monkeypatch, value, fetch):
    monkeypatch.setenv(ATTACHMENT_ROOT_ENV, value)
    with pytest.raises(ToolError, match="must be an absolute directory path"):
        _download(fetch)
    fetch.assert_not_called()


def test_a_zip_unpacks_inside_the_root(root, fetch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("logs/app.log", "boot ok")
    result = json.loads(_download(fetch, "bundle.zip", content=buf.getvalue()))
    assert result["unpack_dir"] == str(root / "12345" / "bundle")
    assert (root / "12345" / "bundle" / "logs" / "app.log").read_text() == "boot ok"


def test_an_unpack_dir_symlinked_outside_is_refused(root, outside, fetch):
    (root / "12345").mkdir(parents=True)
    (root / "12345" / "bundle").symlink_to(outside)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("app.log", "x")
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch, "bundle.zip", content=buf.getvalue())
    assert list(outside.iterdir()) == []


def test_a_tar_link_member_cannot_carry_a_write_outside_the_root(root, outside, fetch):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        link = tarfile.TarInfo("escape")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tf.addfile(link)
        data = b"pwned"
        info = tarfile.TarInfo("escape/planted.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        ok = tarfile.TarInfo("readme.txt")
        ok.size = 2
        tf.addfile(ok, io.BytesIO(b"hi"))
    result = json.loads(_download(fetch, "logs.tar", content=buf.getvalue()))

    assert list(outside.iterdir()) == []
    unpack_dir = root / "12345" / "logs"
    assert not (unpack_dir / "escape").is_symlink()
    assert (unpack_dir / "readme.txt").read_text() == "hi"
    assert result["unpack_dir"] == str(unpack_dir)


# --- set: what is fetched -----------------------------------------------------------------
#
# The bearer token reaches every path on the account host, /api/v2 included, and the caller
# chooses attachment_url. Confined, only an attachment's own address is fetched, and the token
# goes to that first hop alone.

_UPLOAD = "https://example.zendesk.com/sc/attachments/v2/01CONV/IMG_0001.jpeg"
_TOKEN_URL = "https://example.zendesk.com/attachments/token/TESTtoken/?name=IMG_0002.png"


@pytest.mark.parametrize("url", [
    "https://example.zendesk.com/api/v2/users/search.json?query=role:end-user",
    "https://example.zendesk.com/api/v2/users.json",
    "https://example.zendesk.com/sc/attachments/../api/v2/users.json",
    "https://example.zendesk.com/sc/attachments/%2e%2e/api/v2/users.json",
    "https://example.zendesk.com/sc/attachments/v2/%2E%2E/%2E%2E/api/v2/users.json",
    "https://example.zendesk.com/sc/attachments\\..\\..\\api/v2/users.json",
    "https://example.zendesk.com/attachments/12345/a.png",
    "https://example.zendesk.com//sc/attachments/v2/X/a.jpeg",
    "https://other.zendesk.com/sc/attachments/v2/X/a.jpeg",
    "http://example.zendesk.com/sc/attachments/v2/X/a.jpeg",
    "https://p23.zdusercontent.com/attachment/1/a.jpeg",
], ids=["users-search", "users", "dot-dot", "encoded-dot-dot", "encoded-upper", "backslash",
        "attachment-id", "double-slash", "other-account", "http", "signed-host"])
def test_a_non_attachment_address_is_refused_before_anything_is_fetched(root, fetch, url):
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="Refusing to fetch attachment_url"):
        _download_attachment_data(url, "u.json", 8838)
    fetch.assert_not_called()
    assert not (root / "8838" / "u.json").exists()


@pytest.mark.parametrize("url", [_UPLOAD, _TOKEN_URL])
def test_an_attachment_address_is_fetched(root, fetch, url):
    fetch.return_value = MagicMock(content=b"hello", raise_for_status=lambda: None)
    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data(url, "notes.txt", 8838))
    assert result["content"] == "hello"
    fetch.assert_called_once()
    assert fetch.call_args.args[1] == url
    # Redirects are followed by hand, never by the authenticated client.
    assert fetch.call_args.kwargs["follow_redirects"] is False


def test_unset_any_url_on_the_account_is_fetched_as_before(fetch, tmp_path):
    # Interactive use: nothing confined, so nothing about the address is checked here.
    fetch.return_value = MagicMock(content=b'{"users": []}', raise_for_status=lambda: None)
    from zendesk_mcp.tools.attachments import _download_attachment_data
    url = "https://example.zendesk.com/api/v2/users.json"
    result = json.loads(_download_attachment_data(url, "u.json", 8838, str(tmp_path / "anywhere")))
    assert result["content"] == '{"users": []}'
    assert fetch.call_args.args[1] == url
    assert fetch.call_args.kwargs["follow_redirects"] is True


class _Web:
    """httpx.request, faked: answers by URL and records (url, Authorization) for every hop."""

    def __init__(self, pages):
        self.pages, self.hops = pages, []

    def __call__(self, method, url, headers=None, **kwargs):
        url = str(url)
        self.hops.append((url, (headers or {}).get("Authorization")))
        status, extra, body = self.pages[url]
        return httpx.Response(status, headers=extra, content=body, request=httpx.Request(method, url))


@pytest.fixture
def web(tmp_path):
    """The account's config and the network, faked; a test fills in ``web.pages``."""
    config = {"subdomain": "example", "oauth_token": "tok-SECRET"}
    fake = _Web({})
    with patch("zendesk_mcp.auth.load_config", return_value=config), \
            patch("httpx.request", side_effect=fake):
        yield fake


_SIGNED = "https://p23.zdusercontent.com/attachment/1/IMG_0001.jpeg?token=signed"


def test_the_token_goes_to_the_first_hop_only(root, web):
    web.pages = {
        _UPLOAD: (302, {"Location": _SIGNED}, b""),
        _SIGNED: (200, {}, b"hello"),
    }
    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data(_UPLOAD, "notes.txt", 8838))
    assert result["content"] == "hello"
    assert web.hops == [(_UPLOAD, "Bearer tok-SECRET"), (_SIGNED, None)]


def test_a_redirect_back_onto_the_account_carries_no_token(root, web):
    api = "https://example.zendesk.com/api/v2/users.json"
    web.pages = {
        _UPLOAD: (302, {"Location": "/api/v2/users.json"}, b""),
        api: (401, {}, b'{"error": "Couldn\'t authenticate you"}'),
    }
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="Download failed"):
        _download_attachment_data(_UPLOAD, "u.json", 8838)
    assert web.hops == [(_UPLOAD, "Bearer tok-SECRET"), (api, None)]
    assert not (root / "8838" / "u.json").exists()


def test_a_redirect_off_https_is_refused(root, web):
    plain = "http://p23.zdusercontent.com/attachment/1/a.jpeg"
    web.pages = {_UPLOAD: (302, {"Location": plain}, b"")}
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="not https"):
        _download_attachment_data(_UPLOAD, "a.txt", 8838)
    assert [url for url, _ in web.hops] == [_UPLOAD]


def test_a_redirect_loop_is_refused(root, web):
    loop = "https://p23.zdusercontent.com/loop"
    web.pages = {_UPLOAD: (302, {"Location": loop}, b""), loop: (302, {"Location": loop}, b"")}
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="redirects"):
        _download_attachment_data(_UPLOAD, "a.txt", 8838)
