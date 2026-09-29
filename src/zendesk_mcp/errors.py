"""Tool failures reach the client as errors, never as a successful answer.

A tool that RETURNS its failure as text hands the caller a success whose body is prose: the
client cannot tell "Zendesk refused this search" from a ticket listing, and a caller that
parses the body reads the refusal as data. Every tool raises ToolError instead, so the MCP
result carries isError and the message.
"""
try:
    # mcp >= 2.0 (MCPServer). A ToolError's message reaches the client; any other exception is
    # reported only as "Error executing tool <name>", its text withheld -- so it must be this.
    # ResourceError is the same for a resource read.
    from mcp.server.mcpserver.exceptions import ResourceError, ToolError
except ImportError:  # pragma: no cover - exercised only on mcp 1.x
    from mcp.server.fastmcp.exceptions import ResourceError, ToolError

__all__ = ["ResourceError", "ToolError"]
