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
