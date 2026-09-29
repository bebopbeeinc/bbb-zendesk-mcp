"""Every failure path ends in ToolError (or ResourceError) with its message.

On mcp 2.x an exception that is not a ToolError reaches the client as "Error executing tool
<name>", its text withheld: a bare OSError from a token refresh, or a RuntimeError from an
encrypted zip, would tell the caller nothing. And an expired token must fail the call, never
come back as a successful answer with "Unknown" authors in it.
"""
import asyncio
import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from zenpy.lib.exception import APIException

from tests.conftest import make_mock_comment, make_mock_ticket
from zendesk_mcp.auth import InvalidZendeskSubdomainError, TokenExpiredError
from zendesk_mcp.errors import ResourceError, ToolError

# The tools that refresh the token with get_oauth_session() before their own try block.
OAUTH_HELPERS = [
    ("custom_statuses", "_list_custom_statuses_data", ()),
    ("groups", "_get_groups_data", ()),
    ("groups", "_get_group_users_data", (1,)),
    ("list_tickets", "_get_tickets_data", ()),
    ("macros", "_preview_macro_data", (1,)),
    ("macros", "_apply_macro_data", (1, 2)),
    ("organizations", "_get_organization_data", (1,)),
    ("users", "_search_users_data", ("x",)),
    ("views", "_get_view_data", (1,)),
]


@pytest.mark.parametrize("error", [
    InvalidZendeskSubdomainError("Zendesk subdomain 'a b' is not a valid DNS label"),
    PermissionError("[Errno 13] Permission denied: '/home/x/.config/zendesk-mcp/config.json'"),
    OSError("disk full"),
])
@pytest.mark.parametrize("module,name,args", OAUTH_HELPERS)
def test_a_token_refresh_that_fails_any_way_is_a_tool_error_with_its_message(module, name, args, error):
    import importlib
    mod = importlib.import_module(f"zendesk_mcp.tools.{module}")
    with patch(f"zendesk_mcp.tools.{module}.get_oauth_session", side_effect=error):
        with pytest.raises(ToolError) as err:
            getattr(mod, name)(*args)
    assert str(error) in str(err.value)


def test_git_zen_config_that_will_not_load_is_a_tool_error():
    from zendesk_mcp.tools.git_zen import _get_git_zen_links_data
    with patch("zendesk_mcp.tools.git_zen.load_config", side_effect=OSError("unreadable config")):
        with pytest.raises(ToolError, match="unreadable config"):
            _get_git_zen_links_data(1)


def _download(tmp_path, content, filename, **kw):
    from zendesk_mcp.tools.attachments import _download_attachment_data
    with patch("zendesk_mcp.tools.attachments.attachment_cache_dir",
               return_value=tmp_path / "attachments" / "1"), \
            patch("zendesk_mcp.tools.attachments.auth.request",
                  return_value=MagicMock(content=content, raise_for_status=lambda: None)):
        return _download_attachment_data("https://cdn.zendesk.com/f", filename, 1, **kw)


def test_download_dir_that_cannot_be_created_is_a_tool_error(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(ToolError, match="Cannot create the download directory"):
        _download(tmp_path, b"hi", "a.log", dest_dir=str(blocker / "sub"))


def test_encrypted_zip_is_a_tool_error_naming_the_cached_file(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("secret.txt", "x")
    # zipfile cannot write encrypted archives; make extraction behave as it does on one.
    monkeypatch.setattr(zipfile.ZipFile, "extract", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("File 'secret.txt' is encrypted, password required for extraction")))
    with pytest.raises(ToolError) as err:
        _download(tmp_path, buf.getvalue(), "a.zip")
    assert "encrypted" in str(err.value) and "cached_path" in str(err.value)


def test_unsupported_zip_compression_is_a_tool_error(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "x")
    monkeypatch.setattr(zipfile.ZipFile, "extract", lambda *a, **k: (_ for _ in ()).throw(
        NotImplementedError("That compression method is not supported")))
    with pytest.raises(ToolError, match="compression method is not supported"):
        _download(tmp_path, buf.getvalue(), "a.zip")


def test_truncated_tar_gz_is_a_tool_error(tmp_path):
    import gzip
    import tarfile
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        data = b"x" * 5000
        info = tarfile.TarInfo("a.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    truncated = gzip.compress(raw.getvalue())[:60]
    with pytest.raises(ToolError, match="Failed to unpack tar"):
        _download(tmp_path, truncated, "a.tar.gz")


def test_expired_token_during_an_author_lookup_fails_the_comments_call():
    from zendesk_mcp.tools.comments import _get_comments_data
    client = MagicMock()
    client.tickets.comments.return_value = iter([make_mock_comment()])
    client.users.side_effect = TokenExpiredError("Zendesk OAuth token expired. Re-run: zendesk-mcp setup")
    with patch("zendesk_mcp.tools.comments.get_client", return_value=client):
        with pytest.raises(ToolError, match="zendesk-mcp setup"):
            _get_comments_data(1)


def test_revoked_token_401_during_an_author_lookup_fails_the_comments_call():
    from zendesk_mcp.tools.comments import _get_comments_data
    client = MagicMock()
    client.tickets.comments.return_value = iter([make_mock_comment()])
    client.users.side_effect = APIException(json.dumps({"error": "invalid_token",
                                                        "error_description": "The access token provided is expired"}))
    with patch("zendesk_mcp.tools.comments.get_client", return_value=client):
        with pytest.raises(ToolError, match="zendesk-mcp setup"):
            _get_comments_data(1)


def test_a_deleted_author_is_still_unknown_not_an_error():
    from zendesk_mcp.tools.comments import _get_comments_data
    client = MagicMock()
    client.tickets.comments.return_value = iter([make_mock_comment()])
    client.users.side_effect = Exception("RecordNotFound: user 101")
    with patch("zendesk_mcp.tools.comments.get_client", return_value=client):
        assert json.loads(_get_comments_data(1))[0]["author"]["name"] == "Unknown"


def test_expired_token_during_an_author_lookup_fails_the_gitlab_context():
    from zendesk_mcp.tools.gitlab_context import _get_gitlab_context
    client = MagicMock()
    client.tickets.return_value = make_mock_ticket()
    client.tickets.comments.return_value = iter([make_mock_comment()])
    client.users.side_effect = TokenExpiredError("Zendesk OAuth token expired. Re-run: zendesk-mcp setup")
    with patch("zendesk_mcp.tools.gitlab_context.get_client", return_value=client), \
            patch("zendesk_mcp.tools.gitlab_context.load_config", return_value={"subdomain": "x"}):
        with pytest.raises(ToolError, match="zendesk-mcp setup"):
            _get_gitlab_context(1)


def test_knowledge_base_read_failure_is_a_resource_error_with_its_message():
    from zendesk_mcp.tools import knowledge_base as kb
    registered = {}

    class FakeServer:
        def resource(self, *a, **k):
            def deco(f):
                registered["read"] = f
                return f
            return deco

    with patch.object(kb, "load_config", return_value={"knowledge_base_enabled": True}):
        kb.register_knowledge_base_resource(FakeServer())
    kb._get_knowledge_base_data_cached.cache_clear()
    with patch.object(kb, "get_client", side_effect=OSError("Help Center unreachable")):
        with pytest.raises(ResourceError, match="Help Center unreachable"):
            registered["read"]()
