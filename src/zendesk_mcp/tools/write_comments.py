from zenpy.lib.api_objects import Comment, Ticket

from zendesk_mcp.client import get_client, ConfigError
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.errors import ToolError


def _post_comment_data(ticket_id: int, body: str, public: bool) -> str:
    try:
        client = get_client()
        ticket = Ticket(id=ticket_id)
        ticket.comment = Comment(body=body, public=public)
        client.tickets.update(ticket)
        label = "Public comment" if public else "Internal note"
        return f"{label} posted successfully on ticket #{ticket_id}."
    except (ConfigError, TokenExpiredError) as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        if "RecordNotFound" in str(e) or "404" in str(e):
            raise ToolError(f"Ticket #{ticket_id} not found or not accessible with current credentials.") from e
        raise ToolError(api_error_message(e)) from e


def register_write_comment_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_post_comment(ticket_id: int, body: str) -> str:
        """Post a public reply on a Zendesk ticket. The reply is visible to the requester. Use for customer-facing responses."""
        return _post_comment_data(ticket_id, body, public=True)

    @mcp.tool()
    def zendesk_post_internal_note(ticket_id: int, body: str) -> str:
        """Post an internal note on a Zendesk ticket. Internal notes are only visible to agents and are not sent to the requester."""
        return _post_comment_data(ticket_id, body, public=False)
