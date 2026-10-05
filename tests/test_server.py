"""Guards against MCP SDK breaking changes reaching users instead of CI.

mcp 2.0 renamed FastMCP to MCPServer and dropped mcp.server.fastmcp, which broke
every install that resolved to the new SDK (issue #11). These tests exercise the
import and the full decorator surface against whichever mcp version is installed.
"""
import asyncio
from unittest.mock import patch

import pytest


def test_server_module_imports():
    import zendesk_mcp.server as server
    assert server.mcp is not None


def test_server_exposes_decorator_api():
    from zendesk_mcp.server import mcp
    for attr in ("tool", "resource", "prompt", "run"):
        assert callable(getattr(mcp, attr)), f"mcp.{attr} is missing from the SDK"


@pytest.fixture(scope="module")
def registered_server():
    from zendesk_mcp.server import FastMCP, register_all
    srv = FastMCP("zendesk-mcp-test")
    register_all(srv)
    return srv


def test_register_all_registers_tools(registered_server):
    tools = asyncio.run(registered_server.list_tools())
    names = {t.name for t in tools}
    assert "zendesk_get_ticket" in names
    assert len(names) > 20


def test_register_all_registers_prompts(registered_server):
    prompts = asyncio.run(registered_server.list_prompts())
    names = {p.name for p in prompts}
    assert {"analyze-ticket", "draft-ticket-response"} <= names


def test_knowledge_base_resource_registers_when_enabled():
    from zendesk_mcp.server import FastMCP
    from zendesk_mcp.tools.knowledge_base import register_knowledge_base_resource

    srv = FastMCP("zendesk-mcp-test-kb")
    with patch("zendesk_mcp.tools.knowledge_base.load_config",
               return_value={"knowledge_base_enabled": True}):
        register_knowledge_base_resource(srv)

    resources = asyncio.run(srv.list_resources())
    uris = {str(r.uri) for r in resources}
    assert "zendesk://knowledge-base" in uris


async def _call_tool(srv, name, arguments):
    """Call a tool the way a client does, on whichever mcp version is installed: 2.x's in-memory
    Client, or 1.x's connected server/client session pair."""
    try:
        from mcp.client.client import Client
    except ImportError:  # mcp 1.x
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(srv._mcp_server) as session:
            return await session.call_tool(name, arguments)
    async with Client(srv) as client:
        return await client.call_tool(name, arguments)


def _is_error(result):
    # 2.x names the field is_error; 1.x's pydantic model isError.
    return getattr(result, "is_error", getattr(result, "isError", None))


def test_a_tool_failure_reaches_the_client_as_an_error_with_its_message(registered_server):
    """The point of raising: a failure must not arrive as a successful result.

    Zendesk refuses `via:messenger` ("Invalid search: Error filtering on field: via_id"). When
    the tools RETURNED that text, a client received a success whose body was prose, and a
    caller that parsed tool results read the refusal as a malformed ticket list. Through the
    real tool-call path it must be an error result that still carries Zendesk's words.
    """
    rejected = Exception('{"error": "invalid", "description": "Invalid search: Error filtering on field: via_id"}')

    async def call():
        with patch("zendesk_mcp.tools.ticket.get_client") as get_client:
            get_client.return_value.search.side_effect = rejected
            return await _call_tool(registered_server, "zendesk_search_tickets", {"keywords": "via:messenger"})

    result = asyncio.run(call())
    assert _is_error(result) is True
    text = " ".join(getattr(c, "text", "") for c in result.content)
    assert "Error executing tool zendesk_search_tickets" in text
    assert "Zendesk API error" in text and "Invalid search" in text


def test_a_successful_tool_call_is_not_an_error(registered_server):
    async def call():
        with patch("zendesk_mcp.tools.ticket.get_client") as get_client:
            get_client.return_value.search.return_value = []
            return await _call_tool(registered_server, "zendesk_search_tickets", {"keywords": "created>2026-09-25"})

    result = asyncio.run(call())
    assert not _is_error(result)


def _field(model, snake, camel):
    # mcp 2.x's models name fields in snake_case, 1.x's in camelCase.
    return getattr(model, snake, getattr(model, camel, None))


def _download_through_the_sdk(srv, tmp_path, monkeypatch, filename, content):
    """Call zendesk_download_attachment the way a client does, saving under tmp_path."""
    from unittest.mock import MagicMock
    monkeypatch.setenv("ZENDESK_MCP_ATTACHMENT_ROOT", str(tmp_path))

    async def call():
        with patch("zendesk_mcp.tools.attachments.auth.request",
                   return_value=MagicMock(content=content, raise_for_status=lambda: None)), \
                patch("zendesk_mcp.tools.attachments.load_config", return_value={"subdomain": "example"}):
            return await _call_tool(srv, "zendesk_download_attachment", {
                "attachment_url": f"https://example.zendesk.com/sc/attachments/v2/c/{filename}",
                "filename": filename,
                "ticket_id": 8838,
            })

    return asyncio.run(call())


def test_an_image_download_reaches_the_client_as_an_image_block(registered_server, tmp_path, monkeypatch):
    """The picture must arrive as image content the model can see, not as base64 in text,
    which overflowed Claude Code's tool-output limit at a few hundred KB."""
    import base64
    import io
    import json
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (10, 120, 200)).save(buf, format="JPEG")
    result = _download_through_the_sdk(registered_server, tmp_path, monkeypatch, "pickedMedia.jpg", buf.getvalue())

    assert not _is_error(result)
    kinds = [block.type for block in result.content]
    assert kinds == ["text", "image"]
    text, image = result.content
    assert _field(image, "mime_type", "mimeType") == "image/jpeg"
    preview = Image.open(io.BytesIO(base64.b64decode(image.data)))
    assert max(preview.size) == 1568
    meta = json.loads(text.text)
    assert meta["cached_path"].endswith("8838/pickedMedia.jpg")
    assert (meta["width"], meta["height"]) == (3000, 2000)
    assert len(text.text) < 1000


def test_a_non_image_download_reaches_the_client_as_before(registered_server, tmp_path, monkeypatch):
    import json
    result = _download_through_the_sdk(registered_server, tmp_path, monkeypatch, "debug.log", b"ERROR: disk full")

    assert not _is_error(result)
    [block] = result.content
    assert block.type == "text"
    payload = json.loads(block.text)
    assert payload["type"] == "text" and payload["content"] == "ERROR: disk full"
    # Same structured output as every other str tool: {"result": <the JSON text>}.
    assert _field(result, "structured_content", "structuredContent") == {"result": block.text}


def test_download_attachment_declares_the_same_output_schema_as_before(registered_server):
    tools = {t.name: t for t in asyncio.run(registered_server.list_tools())}
    download = _field(tools["zendesk_download_attachment"], "output_schema", "outputSchema")
    plain_str = _field(tools["zendesk_list_attachments"], "output_schema", "outputSchema")
    # {"result": string}, as a `-> str` tool declares; only the generated title names the tool.
    assert {k: v for k, v in download.items() if k != "title"} == \
        {k: v for k, v in plain_str.items() if k != "title"}
    assert download["properties"]["result"]["type"] == "string"
