from pathlib import Path
from zenpy import Zenpy
from zendesk_mcp.auth import valid_token


class ConfigError(Exception):
    pass


def get_client(config_file: Path | None = None) -> Zenpy:
    subdomain, token = valid_token(config_file)
    if not subdomain or not token:
        raise ConfigError("Zendesk not configured. Run: zendesk-mcp setup")
    return Zenpy(subdomain=subdomain, oauth_token=token)


def get_oauth_session() -> tuple[str, str]:
    """Return (subdomain, oauth_token) for direct API calls, refreshing an expired token.

    Raises ConfigError if unconfigured, TokenExpiredError if the token cannot be renewed.
    """
    subdomain, token = valid_token()
    if not subdomain or not token:
        raise ConfigError("Zendesk not configured. Run: zendesk-mcp setup")
    return subdomain, token
