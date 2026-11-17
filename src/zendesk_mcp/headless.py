"""Read-only, process-local OAuth for unattended confidential clients.

Client-credentials grants do not rotate refresh tokens, so independent sessions
can authenticate without sharing writable credential files.
"""
import os
import threading
import time

import httpx

_lock = threading.Lock()
_identity: tuple[str, str, str] | None = None
_token: dict = {}


def token_config(now: float | None = None, rejected_token: str | None = None) -> dict:
    # Import lazily: auth also dispatches to this module for headless grants.
    from zendesk_mcp.auth import TokenExpiredError, validate_zendesk_subdomain

    subdomain = validate_zendesk_subdomain(os.environ.get("ZENDESK_SUBDOMAIN", ""))
    client_id = os.environ.get("ZENDESK_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ZENDESK_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise TokenExpiredError(
            "Headless OAuth requires ZENDESK_CLIENT_ID and ZENDESK_CLIENT_SECRET"
        )

    global _identity, _token
    identity = (subdomain, client_id, client_secret)
    with _lock:
        timestamp = time.time() if now is None else now
        if (
            _identity == identity
            and _token.get("expires_at", 0) > timestamp + 120
            and _token.get("oauth_token") != rejected_token
        ):
            return dict(_token)

        try:
            response = httpx.post(
                f"https://{subdomain}.zendesk.com/oauth/tokens",
                json={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "read",
                    "expires_in": 1800,
                },
                timeout=30,
                follow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
            token = body["access_token"]
            lifetime = body["expires_in"]
            if (
                not isinstance(token, str) or not token.strip()
                or not isinstance(lifetime, int) or isinstance(lifetime, bool)
                or not 300 <= lifetime <= 172800
                or body.get("scope", "").split() != ["read"]
                or body.get("token_type", "").lower() != "bearer"
            ):
                raise ValueError("Unexpected OAuth token response")
        except Exception:
            # Do not echo an HTTP response body: it can contain credentials.
            raise TokenExpiredError(
                "Headless OAuth failed; check the Confidential client's credentials "
                "and allowed read scope. No interactive setup was attempted."
            ) from None

        _identity = identity
        _token = {
            "subdomain": subdomain,
            "oauth_token": token.strip(),
            "expires_at": timestamp + lifetime,
        }
        return dict(_token)
