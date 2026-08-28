import json
from zendesk_mcp.client import get_oauth_session, ConfigError
from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError


def _get_organization_data(organization_id: int) -> str:
    try:
        subdomain, _ = get_oauth_session()
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    url = f"https://{subdomain}.zendesk.com/api/v2/organizations/{organization_id}.json"
    try:
        response = auth.request("GET", url, timeout=30)
        response.raise_for_status()
        org = response.json().get("organization", {})
        return json.dumps({
            "id": org.get("id"),
            "name": org.get("name"),
            "organization_fields": org.get("organization_fields"),
            "tags": org.get("tags"),
            "created_at": org.get("created_at"),
            "updated_at": org.get("updated_at"),
        }, indent=2)
    except Exception as e:
        if "404" in str(e):
            return f"Organization #{organization_id} not found or not accessible with current credentials."
        return api_error_message(e)


def register_organization_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_get_organization(organization_id: int) -> str:
        """Fetch a Zendesk organization including its custom fields and tags. Returns JSON with id, name, organization_fields, tags, created_at, updated_at."""
        return _get_organization_data(organization_id)
