import json
from zendesk_mcp.client import get_oauth_session, ConfigError
from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError


def _list_custom_statuses_data() -> str:
    try:
        subdomain, _ = get_oauth_session()
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    url = f"https://{subdomain}.zendesk.com/api/v2/custom_statuses"
    try:
        response = auth.request("GET", url, timeout=30)
        response.raise_for_status()
        statuses = response.json().get("custom_statuses", [])
        return json.dumps([{
            "id": s.get("id"),
            "agent_label": s.get("agent_label"),
            "end_user_label": s.get("end_user_label"),
            "status_category": s.get("status_category"),
            "active": s.get("active"),
            "default": s.get("default"),
        } for s in statuses], indent=2)
    except Exception as e:
        return api_error_message(e)


def register_custom_status_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_list_custom_statuses() -> str:
        """List all custom ticket statuses defined in Zendesk. Returns JSON array of {id, agent_label, end_user_label, status_category, active, default}. Use the id when setting custom_status_id on a ticket."""
        return _list_custom_statuses_data()
