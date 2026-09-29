import json
from zendesk_mcp.client import get_oauth_session, ConfigError
from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.errors import ToolError


def _search_users_data(query: str) -> str:
    try:
        subdomain, _ = get_oauth_session()
    except (ConfigError, TokenExpiredError) as e:
        raise ToolError(str(e)) from e
    except Exception as e:
        # a refresh that fails another way: a bad subdomain, a config file it cannot write
        raise ToolError(api_error_message(e)) from e
    url = f"https://{subdomain}.zendesk.com/api/v2/users/search.json"
    try:
        response = auth.request(
            "GET",
            url,
            params={"query": query},
            timeout=30,
        )
        response.raise_for_status()
        users = response.json().get("users", [])
        return json.dumps([{
            "id": u.get("id"),
            "name": u.get("name"),
            "email": u.get("email"),
            "role": u.get("role"),
        } for u in users], indent=2)
    except Exception as e:
        raise ToolError(api_error_message(e)) from e


def register_user_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_search_users(query: str) -> str:
        """Search Zendesk users by name or email. Returns JSON array of {id, name, email, role}."""
        return _search_users_data(query)
