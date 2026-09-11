from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from zenpy import Zenpy

from zendesk_mcp import auth
from zendesk_mcp.config import load_config


class ConfigError(Exception):
    pass


class RefreshingZendeskSession(requests.Session):
    """Requests session that keeps Zenpy on the shared OAuth lifecycle."""

    # Zenpy treats an authorized session as responsible for its own credentials.
    # Keeping the bearer token out of ``session.headers`` also lets us validate every
    # destination before attaching it to an individual request.
    authorized = True

    def __init__(self, subdomain: str, config_file: Path | None = None):
        super().__init__()
        self._subdomain = subdomain.strip().lower()
        self._config_file = config_file
        # Match the adapter Zenpy installs for its own requests.Session, including its
        # connect/read retry policy.
        self.mount("https://", HTTPAdapter(**Zenpy.http_adapter_kwargs()))

    @staticmethod
    def _authorization_headers(headers, token: str, cfg: dict | None = None) -> dict:
        # Do not allow a caller-supplied Authorization spelling to survive alongside
        # the managed token in requests' case-insensitive header collection.
        managed = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() != "authorization"
        }
        managed["Authorization"] = auth.authorization_header(cfg or {}, token)
        return managed

    def rebuild_auth(
        self,
        prepared_request: requests.PreparedRequest,
        response: requests.Response,
    ) -> None:
        """Strip credentials from every redirect outside the configured origin."""
        try:
            auth.validate_zendesk_url(prepared_request.url, self._subdomain)
        except auth.UnsafeZendeskUrlError:
            prepared_request.headers.pop("Authorization", None)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        # Validate before valid_token() can perform a network refresh. More
        # importantly, the bearer token is never attached to an arbitrary URL passed
        # through the otherwise-general requests.Session API.
        auth.validate_zendesk_url(url, self._subdomain)

        snapshot = load_config(self._config_file)
        subdomain, token = auth.valid_token(
            self._config_file,
            cfg_snapshot=snapshot,
        )
        if not subdomain or not token:
            raise ConfigError("Zendesk not configured. Run: zendesk-mcp setup")
        if subdomain.lower() != self._subdomain:
            raise auth.UnsafeZendeskUrlError(
                "Zendesk subdomain changed after the client was created"
            )

        headers = self._authorization_headers(kwargs.pop("headers", None), token, snapshot)
        response = super().request(method, url, headers=headers, **kwargs)

        if auth.api_token_credentials(snapshot):
            # Before the OAuth guard: nothing to refresh, and a rejected API token does
            # not answer with the invalid_token error that guard looks for.
            if auth.is_zendesk_auth_rejection(response, self._subdomain):
                raise auth.TokenExpiredError(auth.API_TOKEN_REJECTED)
            return response

        if not auth.is_zendesk_invalid_token_response(response, self._subdomain):
            return response

        refreshed = auth.refresh_rejected_token(
            token,
            config_file=self._config_file,
            cfg_snapshot=snapshot,
        )
        if refreshed.get("subdomain", "").strip().lower() != self._subdomain:
            raise auth.UnsafeZendeskUrlError(
                "Zendesk subdomain changed while refreshing the token"
            )
        retry_headers = {
            **headers,
            "Authorization": f"Bearer {refreshed['oauth_token']}",
        }
        response = super().request(method, url, headers=retry_headers, **kwargs)
        if auth.is_zendesk_invalid_token_response(response, self._subdomain):
            raise auth.TokenExpiredError(
                auth.expired_message("still rejected after refresh")
            )
        return response


def get_client(config_file: Path | None = None) -> Zenpy:
    subdomain, token = auth.valid_token(config_file)
    if not subdomain or not token:
        raise ConfigError("Zendesk not configured. Run: zendesk-mcp setup")
    session = RefreshingZendeskSession(subdomain, config_file)
    return Zenpy(subdomain=subdomain, session=session)


def get_oauth_session() -> tuple[str, str]:
    """Return (subdomain, oauth_token) for direct API calls, refreshing an expired token.

    Raises ConfigError if unconfigured, TokenExpiredError if the token cannot be renewed.
    """
    subdomain, token = auth.valid_token()
    if not subdomain or not token:
        raise ConfigError("Zendesk not configured. Run: zendesk-mcp setup")
    return subdomain, token
