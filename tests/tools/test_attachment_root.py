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
import stat
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from PIL import Image

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
def headless_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ZENDESK_AUTH_MODE", "client_credentials")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".cache" / "zendesk-mcp" / "attachments"


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
    result = json.loads(_download(fetch, ticket_id=1001))
    assert result["cached_path"] == str(root / "1001" / "notes.txt")
    assert (root / "1001" / "notes.txt").read_bytes() == b"hello"


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
    assert result["cached_path"] == str(real / "7" / "notes.txt")


def test_dest_dir_inside_the_root_is_allowed(root, fetch):
    result = json.loads(_download(fetch, dest_dir=str(root / "work" / "1002")))
    assert result["cached_path"] == str(root / "work" / "1002" / "notes.txt")


def test_dest_dir_only_creates_directories_on_the_resolved_path(root, outside, fetch):
    dest_dir = outside / "unneeded" / ".." / ".." / root.name / "1002"
    result = json.loads(_download(fetch, dest_dir=str(dest_dir)))
    assert result["cached_path"] == str(root / "1002" / "notes.txt")
    assert list(outside.iterdir()) == []


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


def test_headless_refuses_directory_swapped_to_a_symlink_during_mkdir(headless_cache, outside, fetch, monkeypatch):
    headless_cache.mkdir(parents=True)
    target = headless_cache / "12345"
    mkdir = Path.mkdir

    def mkdir_after_swap(path, *args, **kwargs):
        if path == target:
            path.symlink_to(outside, target_is_directory=True)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir_after_swap)
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch)

    assert target.is_symlink()
    fetch.assert_not_called()
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(
    not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "fchmod")),
    reason="requires POSIX directory descriptors",
)
@pytest.mark.parametrize("swap_after_open", [False, True])
def test_headless_directory_swap_cannot_change_outside_mode(
    headless_cache, outside, fetch, monkeypatch, swap_after_open,
):
    headless_cache.mkdir(parents=True)
    target = headless_cache / "12345"
    outside.chmod(0o755)
    open_dir = os.open

    def open_with_swap(path, *args, **kwargs):
        if path != target:
            return open_dir(path, *args, **kwargs)
        fd = open_dir(path, *args, **kwargs) if swap_after_open else None
        target.rmdir()
        target.symlink_to(outside, target_is_directory=True)
        return open_dir(path, *args, **kwargs) if fd is None else fd

    monkeypatch.setattr(os, "open", open_with_swap)
    with pytest.raises(ToolError):
        _download(fetch)

    assert target.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    fetch.assert_not_called()


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


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.parametrize("root_mode", [0o755, 0o700])
def test_headless_download_keeps_existing_modes_and_creates_private_paths(headless_cache, root, fetch, umask_022, root_mode):
    root.mkdir(mode=root_mode)
    batch = root / "batch"
    batch.mkdir(mode=0o755)
    dest_dir = batch / "nested" / "ticket"
    result = json.loads(_download(fetch, dest_dir=str(dest_dir)))
    saved = Path(result["cached_path"])
    saved.chmod(0o644)
    result = json.loads(_download(fetch, dest_dir=str(dest_dir), content=b"updated"))

    assert Path(result["cached_path"]) == saved
    assert saved.read_bytes() == b"updated"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == root_mode
    assert stat.S_IMODE(batch.stat().st_mode) == 0o755
    for directory in (dest_dir.parent, dest_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory permissions")
@pytest.mark.parametrize("relative_dir", [".", "batch", "batch/ticket"])
@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_headless_refuses_writable_cache_paths(headless_cache, root, fetch, relative_dir, mode):
    unsafe = root / relative_dir
    unsafe.mkdir(parents=True)
    unsafe.chmod(mode)
    dest_dir = root / "batch" / "ticket"

    with pytest.raises(ToolError, match="group- or world-writable without the sticky bit") as error:
        _download(fetch, dest_dir=str(dest_dir))

    assert str(unsafe) in str(error.value)
    assert stat.S_IMODE(unsafe.stat().st_mode) == mode
    assert not list(unsafe.iterdir())
    fetch.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory ownership")
def test_headless_refuses_cache_root_owned_by_another_user(headless_cache, root, fetch, monkeypatch):
    root.mkdir(mode=0o700)
    uid = root.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)

    with pytest.raises(ToolError, match="not owned by the server user") as error:
        _download(fetch)

    assert str(root) in str(error.value)
    assert not list(root.iterdir())
    fetch.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory permissions")
def test_headless_accepts_owned_writable_cache_root_with_sticky_bit(headless_cache, root, fetch):
    root.mkdir()
    root.chmod(0o1777)

    saved = Path(json.loads(_download(fetch))["cached_path"])

    assert saved.read_bytes() == b"hello"
    assert stat.S_IMODE(root.stat().st_mode) == 0o1777


def test_headless_download_without_posix_permission_primitives(headless_cache, fetch, monkeypatch):
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "fchmod"):
        monkeypatch.delattr(os, name, raising=False)

    result = json.loads(_download(fetch))
    saved = Path(result["cached_path"])
    assert saved.parent == headless_cache / "12345"
    assert saved.read_bytes() == b"hello"
    if os.name == "posix":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX umask and permission bits")
@pytest.mark.parametrize("mask", [0o177, 0o377, 0o577])
def test_headless_download_restores_directory_search_permission_under_restrictive_umask(headless_cache, fetch, mask):
    dest_dir = headless_cache / "nested" / "ticket"
    previous = os.umask(mask)
    try:
        result = json.loads(_download(fetch, dest_dir=str(dest_dir)))
        assert os.umask(mask) == mask
    finally:
        os.umask(previous)

    saved = Path(result["cached_path"])
    assert saved.parent == dest_dir
    assert saved.read_bytes() == b"hello"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    for directory in (headless_cache, dest_dir.parent, dest_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("extension", ["zip", "tar.gz"])
def test_headless_archives_unpack_private_files_and_directories(headless_cache, fetch, umask_022, extension):
    buf = io.BytesIO()
    filename = "explicit/implicit/notes.txt"
    if extension == "zip":
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("explicit/", b"")
            zf.writestr(filename, b"customer data")
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            directory = tarfile.TarInfo("explicit")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o777
            tf.addfile(directory)
            file = tarfile.TarInfo(filename)
            file.mode = 0o777
            file.size = len(b"customer data")
            tf.addfile(file, io.BytesIO(b"customer data"))

    result = json.loads(_download(fetch, f"bundle.{extension}", content=buf.getvalue()))
    unpack_dir = Path(result["unpack_dir"])
    cached = Path(result["cached_path"])
    assert unpack_dir.parent == cached.parent == headless_cache / "12345"
    assert (unpack_dir / filename).read_bytes() == b"customer data"
    for directory in (unpack_dir, unpack_dir / "explicit", unpack_dir / "explicit" / "implicit"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in (cached, unpack_dir / filename):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


@pytest.mark.parametrize("extension", ["zip", "tar.gz"])
@pytest.mark.parametrize("limit", ["expanded-byte", "entry"])
def test_headless_archive_over_limit_is_not_unpacked(headless_cache, fetch, monkeypatch, extension, limit):
    if limit == "expanded-byte":
        monkeypatch.setattr("zendesk_mcp.tools.attachments._HEADLESS_ARCHIVE_MAX_BYTES", 5)
    else:
        monkeypatch.setattr("zendesk_mcp.tools.attachments._HEADLESS_ARCHIVE_MAX_ENTRIES", 1)
    files = {"first.txt": b"abc", "second.txt": b"def"}
    buf = io.BytesIO()
    if extension == "zip":
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ToolError, match=f"{limit} limit"):
        _download(fetch, f"bundle.{extension}", content=buf.getvalue())

    cached, = (headless_cache / "12345").iterdir()
    assert cached.read_bytes() == buf.getvalue()


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
@pytest.mark.parametrize("root_fixture", ["root", "headless_cache"])
def test_a_non_attachment_address_is_refused_before_anything_is_fetched(request, root_fixture, fetch, url):
    from zendesk_mcp.tools.attachments import _download_attachment_data
    root = request.getfixturevalue(root_fixture)
    with pytest.raises(ToolError, match="Refusing to fetch attachment_url"):
        _download_attachment_data(url, "u.json", 1001)
    fetch.assert_not_called()
    assert not (root / "1001" / "u.json").exists()


def test_headless_default_root_refuses_a_destination_outside_it(headless_cache, outside, fetch):
    with pytest.raises(ToolError, match=f"outside {ATTACHMENT_ROOT_ENV}"):
        _download(fetch, dest_dir=str(outside))
    fetch.assert_not_called()
    assert not headless_cache.exists()
    assert not list(outside.iterdir())


def test_headless_honors_an_explicit_root(headless_cache, root, fetch):
    result = json.loads(_download(fetch, ticket_id=1001))
    saved = Path(result["cached_path"])
    assert saved.parent == root / "1001"
    assert saved.name.endswith("-notes.txt")
    assert not headless_cache.exists()


@pytest.mark.parametrize("url", [_UPLOAD, _TOKEN_URL])
def test_an_attachment_address_is_fetched(root, fetch, url):
    fetch.return_value = MagicMock(content=b"hello", raise_for_status=lambda: None)
    from zendesk_mcp.tools.attachments import _download_attachment_data
    result = json.loads(_download_attachment_data(url, "notes.txt", 1001))
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
    result = json.loads(_download_attachment_data(url, "u.json", 1001, str(tmp_path / "anywhere")))
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
    result = json.loads(_download_attachment_data(_UPLOAD, "notes.txt", 1001))
    assert result["content"] == "hello"
    assert web.hops == [(_UPLOAD, "Bearer tok-SECRET"), (_SIGNED, None)]


def test_headless_messaging_image_follows_https_redirect_without_forwarding_token(headless_cache, web):
    from zendesk_mcp.tools.attachments import _download_attachment_data

    jpeg = io.BytesIO()
    Image.new("RGB", (16, 16)).save(jpeg, "JPEG")
    web.pages = {
        _UPLOAD: (302, {"Location": _SIGNED}, b""),
        _SIGNED: (200, {}, jpeg.getvalue()),
    }
    with patch("zendesk_mcp.headless.token_config",
               return_value={"subdomain": "example", "oauth_token": "tok-SECRET"}):
        result = _download_attachment_data(_UPLOAD, "IMG_0001.jpeg", 1001)

    meta = json.loads(result.content[0].text)
    saved = Path(meta["cached_path"])
    assert result.content[1].type == "image"
    assert saved.parent == headless_cache / "1001"
    assert saved.name.endswith("-IMG_0001.jpeg")
    assert saved.read_bytes() == jpeg.getvalue()
    assert web.hops == [(_UPLOAD, "Bearer tok-SECRET"), (_SIGNED, None)]


def test_headless_preserves_both_uploads_when_filenames_match(headless_cache, fetch):
    from zendesk_mcp.tools.attachments import _download_attachment_data

    filename = "pickedMedia.jpg"
    paths = []
    originals = []
    for upload_id, color in [("one", "red"), ("two", "blue")]:
        url = f"https://example.zendesk.com/sc/attachments/v2/{upload_id}/{filename}"
        jpeg = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(jpeg, "JPEG")
        originals.append(jpeg.getvalue())
        fetch.return_value = MagicMock(content=originals[-1])
        result = _download_attachment_data(url, filename, 1001)
        paths.append(Path(json.loads(result.content[0].text)["cached_path"]))

    assert paths[0] != paths[1]
    assert all(path.parent == headless_cache / "1001" for path in paths)
    assert [path.read_bytes() for path in paths] == originals


def test_headless_truncates_a_long_utf8_stem_without_losing_the_extension(headless_cache, fetch):
    filename = "界" * 82 + ".txt"
    assert len(filename.encode()) == 250
    result = json.loads(_download(fetch, filename))
    saved = Path(result["cached_path"])
    prefix, stem = saved.stem.split("-", 1)
    assert saved.parent == headless_cache / "12345"
    assert len(prefix) == 24
    assert stem == "界" * 75
    assert len(saved.name.encode()) == 254
    assert saved.suffix == ".txt"
    assert saved.read_bytes() == b"hello"


def test_a_redirect_back_onto_the_account_carries_no_token(root, web):
    api = "https://example.zendesk.com/api/v2/users.json"
    web.pages = {
        _UPLOAD: (302, {"Location": "/api/v2/users.json"}, b""),
        api: (401, {}, b'{"error": "Couldn\'t authenticate you"}'),
    }
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="Download failed"):
        _download_attachment_data(_UPLOAD, "u.json", 1001)
    assert web.hops == [(_UPLOAD, "Bearer tok-SECRET"), (api, None)]
    assert not (root / "1001" / "u.json").exists()


def test_a_redirect_off_https_is_refused(root, web):
    plain = "http://p23.zdusercontent.com/attachment/1/a.jpeg"
    web.pages = {_UPLOAD: (302, {"Location": plain}, b"")}
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="not https"):
        _download_attachment_data(_UPLOAD, "a.txt", 1001)
    assert [url for url, _ in web.hops] == [_UPLOAD]


def test_a_redirect_loop_is_refused(root, web):
    loop = "https://p23.zdusercontent.com/loop"
    web.pages = {_UPLOAD: (302, {"Location": loop}, b""), loop: (302, {"Location": loop}, b"")}
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with pytest.raises(ToolError, match="redirects"):
        _download_attachment_data(_UPLOAD, "a.txt", 1001)
