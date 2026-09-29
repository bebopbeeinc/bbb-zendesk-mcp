import json
from zendesk_mcp.client import get_client, ConfigError
from zendesk_mcp.auth import api_error_message, is_auth_error, TokenExpiredError
from zendesk_mcp.errors import ToolError


def _get_comments_data(ticket_id: int) -> str:
    try:
        client = get_client()
        comments = client.tickets.comments(ticket_id)
        result = []
        for comment in comments:
            try:
                author = client.users(id=comment.author_id)
                author_info = {
                    "name": author.name,
                    "email": author.email,
                    "role": author.role,
                }
            except TokenExpiredError:
                raise
            except Exception as e:
                # An expired or revoked token is the whole call failing, never an "Unknown"
                # author on an otherwise successful answer.
                if is_auth_error(e):
                    raise
                author_info = {"name": "Unknown", "email": "", "role": ""}

            attachments = [
                {
                    "filename": att.file_name,
                    "content_type": att.content_type,
                    "size_bytes": att.size,
                    "content_url": att.content_url,
                }
                for att in (comment.attachments or [])
            ]

            result.append({
                "id": comment.id,
                "author": author_info,
                "created_at": str(comment.created_at),
                "is_public": comment.public,
                "body": comment.body,
                "attachments": attachments,
            })
        return json.dumps(result, indent=2)
    except (ConfigError, TokenExpiredError) as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        if "RecordNotFound" in str(e) or "404" in str(e):
            raise ToolError(f"Ticket #{ticket_id} not found or not accessible with current credentials.") from e
        raise ToolError(api_error_message(e)) from e


def register_comments_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_get_comments(ticket_id: int) -> str:
        """Get all comments (public replies and internal notes) for a Zendesk ticket. Includes attachment metadata but does not download files."""
        return _get_comments_data(ticket_id)
