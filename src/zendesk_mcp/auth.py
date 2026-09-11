"""OAuth access-token lifecycle for the Zendesk API.

Zendesk applies a default expiry to access tokens issued by OAuth clients created on or
after 2026-04-30 (30 minutes, with a 30-day refresh token). Clients created earlier still
issue non-expiring tokens unless ``expires_in`` is requested explicitly. Both shapes have
to keep working, so a config without ``expires_at`` is treated as non-expiring and is
never refreshed.

Refreshing rotates the refresh token and invalidates the previous pair, so the new
credentials must be persisted on every refresh.
"""
import base64
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from filelock import Timeout as FileLockTimeout

from zendesk_mcp.config import config_file_lock, load_config, save_config

# Refresh slightly ahead of the deadline so a request already in flight does not
# land on a token that expired in transit.
REFRESH_SKEW_SECONDS = 120
REFRESH_TIMEOUT_SECONDS = 30
REFRESH_LOCK_TIMEOUT_SECONDS = 30

# Zendesk accepts these lifetime requests on both authorization-code and refresh grants.
ACCESS_TOKEN_TTL_SECONDS = 86400       # 24 hours
REFRESH_TOKEN_TTL_SECONDS = 7776000    # 90 days

REAUTH_HINT = "Re-run: zendesk-mcp setup"


class TokenExpiredError(Exception):
    """The access token is unusable and could not be renewed automatically."""


class UnsafeZendeskUrlError(ValueError):
    """A request attempted to send Zendesk credentials to an untrusted origin."""


class InvalidZendeskSubdomainError(ValueError):
    """The configured Zendesk subdomain is not one safe DNS label."""


def expired_message(detail: str = "") -> str:
    base = (
        "Zendesk authorization failed: the OAuth access token is expired, revoked, or "
        "invalid, and could not be refreshed automatically."
    )
    if detail:
        base = f"{base} ({detail})"
    return f"{base} {REAUTH_HINT}"


def api_token_credentials(cfg: dict) -> tuple[str, str] | None:
    """``(email, api_token)`` when the config authenticates with a Zendesk API token.

    An API token belongs to a Zendesk USER, so the identity it carries is whichever
    account the token was issued for. Pointing it at a shared service account --
    contact@ rather than a named person -- is what makes an unattended integration
    survive that person leaving, which OAuth here cannot: an OAuth grant is created by
    somebody signing in, and it dies with their account.

    It also removes the single-holder constraint. Refresh tokens rotate, so exactly one
    machine can hold a working OAuth grant and re-authorising on a second one silently
    invalidates the first. API tokens do not rotate, so a laptop and a build box can use
    the same credential without fighting.
    """
    email = str(cfg.get("email") or "").strip()
    token = str(cfg.get("api_token") or "").strip()
    return (email, token) if email and token else None


def authorization_header(cfg: dict, oauth_token: str | None = None) -> str:
    """The Authorization header value for whichever credential is configured."""
    creds = api_token_credentials(cfg)
    if creds:
        email, token = creds
        # Zendesk's API-token scheme: basic auth with "<email>/token" as the username.
        raw = base64.b64encode(f"{email}/token:{token}".encode()).decode()
        return f"Basic {raw}"
    return f"Bearer {oauth_token}"


def is_expired(cfg: dict, now: float | None = None) -> bool:
    """True if the stored token has passed (or is about to pass) its expiry.

    A config with no ``expires_at`` came from a non-expiring OAuth client and is
    reported as valid.
    """
    if api_token_credentials(cfg):
        return False                     # API tokens do not expire and cannot be refreshed
    expires_at = cfg.get("expires_at")
    if not expires_at:
        return False
    return (now if now is not None else time.time()) >= expires_at - REFRESH_SKEW_SECONDS


def can_refresh(cfg: dict) -> bool:
    return bool(cfg.get("refresh_token") and cfg.get("client_id"))


_SUBDOMAIN_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)


def validate_zendesk_subdomain(subdomain: str) -> str:
    """Return a canonical Zendesk subdomain after enforcing one DNS label."""
    normalized = subdomain.strip().lower()
    if not _SUBDOMAIN_RE.fullmatch(normalized):
        raise InvalidZendeskSubdomainError(
            "Zendesk subdomain must be a single DNS label containing only letters, "
            "numbers, and internal hyphens"
        )
    return normalized


def validate_zendesk_url(url: str, subdomain: str) -> None:
    """Reject any destination that is not the configured Zendesk HTTPS origin."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeZendeskUrlError("Invalid Zendesk request URL") from exc

    expected_host = f"{validate_zendesk_subdomain(subdomain)}.zendesk.com"
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != expected_host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeZendeskUrlError(
            f"Refusing to send Zendesk credentials outside https://{expected_host}"
        )


def is_invalid_token_response(response) -> bool:
    if response.status_code != 401:
        return False
    try:
        body = response.json()
    except (TypeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("error") == "invalid_token"


def is_zendesk_invalid_token_response(response, subdomain: str) -> bool:
    """True only for Zendesk's invalid-token response from the trusted origin."""
    if not is_invalid_token_response(response):
        return False
    try:
        response_url = getattr(response, "url", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    if response_url is None:
        return False
    try:
        validate_zendesk_url(str(response_url), subdomain)
    except UnsafeZendeskUrlError:
        return False
    return True


def refresh_access_token(
    cfg: dict, config_file: Path | None = None, now: float | None = None
) -> dict:
    """Exchange the stored refresh token for a new token pair and persist it.

    Returns the updated config. Raises TokenExpiredError if the refresh is rejected.
    """
    if not can_refresh(cfg):
        raise TokenExpiredError(expired_message("no refresh token stored"))

    now = now if now is not None else time.time()
    subdomain = validate_zendesk_subdomain(cfg.get("subdomain", ""))
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
        "client_id": cfg["client_id"],
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token_expires_in": REFRESH_TOKEN_TTL_SECONDS,
    }
    if cfg.get("client_secret"):
        payload["client_secret"] = cfg["client_secret"]

    try:
        response = httpx.post(
            f"https://{subdomain}.zendesk.com/oauth/tokens",
            json=payload,
            timeout=REFRESH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as e:
        # invalid_grant => the refresh token itself is expired (30d default) or revoked.
        raise TokenExpiredError(
            expired_message(f"refresh rejected with HTTP {e.response.status_code}")
        ) from e
    except Exception as e:
        raise TokenExpiredError(expired_message(f"refresh failed: {e}")) from e

    access_token = body.get("access_token")
    if not access_token:
        raise TokenExpiredError(expired_message("refresh response had no access_token"))

    updated = dict(cfg)
    updated["oauth_token"] = access_token
    # Zendesk invalidates the old refresh token on use; keep the rotated one.
    if body.get("refresh_token"):
        updated["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        updated["expires_at"] = int(now + body["expires_in"])
    if body.get("refresh_token_expires_in"):
        updated["refresh_token_expires_at"] = int(
            now + body["refresh_token_expires_in"]
        )
    save_config(updated, config_file)
    return updated


def _refresh_under_lock(
    config_file: Path | None,
    now: float | None,
    fallback: dict,
    rejected_token: str | None = None,
) -> dict:
    """Serialize rotating-token redemption and prefer credentials refreshed elsewhere."""
    try:
        with config_file_lock(config_file, timeout=REFRESH_LOCK_TIMEOUT_SECONDS):
            current = load_config(config_file) or fallback
            current_token = current.get("oauth_token", "").strip()

            if rejected_token is None:
                if current_token and not is_expired(current, now):
                    return current
            elif current_token and current_token != rejected_token:
                # Another process already rotated the rejected access token while this
                # process was waiting for the lock. Reuse its complete persisted state.
                return current

            return refresh_access_token(current, config_file, now)
    except FileLockTimeout as exc:
        raise TokenExpiredError(expired_message("timed out waiting to refresh")) from exc


def refresh_rejected_token(
    rejected_token: str,
    config_file: Path | None = None,
    now: float | None = None,
    cfg_snapshot: dict | None = None,
) -> dict:
    """Refresh a token rejected by Zendesk, or reuse a newer persisted token.

    Keeping this operation centralized ensures both the direct-httpx and Zenpy request
    paths serialize Zendesk's single-use refresh-token rotation in the same way.
    """
    cfg = load_config(config_file) or cfg_snapshot or {}
    if not can_refresh(cfg):
        raise TokenExpiredError(expired_message("server rejected the token"))
    return _refresh_under_lock(
        config_file,
        now,
        cfg,
        rejected_token=rejected_token,
    )


def valid_token(
    config_file: Path | None = None,
    now: float | None = None,
    cfg_snapshot: dict | None = None,
) -> tuple[str, str]:
    """Return (subdomain, access_token), refreshing first if the token has expired.

    Raises TokenExpiredError when the token is expired and cannot be renewed.
    """
    cfg = cfg_snapshot if cfg_snapshot is not None else load_config(config_file)
    subdomain = cfg.get("subdomain", "").strip()
    creds = api_token_credentials(cfg)
    if creds:
        # No expiry and nothing to refresh; the token IS the credential.
        return validate_zendesk_subdomain(subdomain) if subdomain else subdomain, creds[1]
    token = cfg.get("oauth_token", "").strip()
    if not subdomain or not token:
        # Caller maps this to the existing "not configured" ConfigError.
        return subdomain, token

    subdomain = validate_zendesk_subdomain(subdomain)
    if not is_expired(cfg, now):
        return subdomain, token

    cfg = _refresh_under_lock(config_file, now, cfg)
    return cfg.get("subdomain", "").strip(), cfg["oauth_token"].strip()


def request(
    method: str,
    url: str,
    config_file: Path | None = None,
    now: float | None = None,
    **kwargs,
) -> httpx.Response:
    """httpx.request with the bearer token injected, refreshing once on a 401.

    Pre-emptive expiry checking covers the normal case; this also handles a token
    revoked server-side or a clock skew that made it look valid.
    """
    kwargs.setdefault("timeout", 30)
    snapshot = load_config(config_file)
    subdomain = validate_zendesk_subdomain(snapshot.get("subdomain", ""))
    validate_zendesk_url(url, subdomain)
    current_subdomain, token = valid_token(config_file, now, cfg_snapshot=snapshot)
    if current_subdomain.lower() != subdomain:
        raise UnsafeZendeskUrlError(
            "Zendesk subdomain changed before the request could be sent"
        )
    headers = {**kwargs.pop("headers", {}),
               "Authorization": authorization_header(snapshot, token)}
    response = httpx.request(method, url, headers=headers, **kwargs)

    if not is_zendesk_invalid_token_response(response, subdomain):
        return response

    if api_token_credentials(snapshot):
        # There is no refresh path for an API token. Retrying would repeat the same
        # rejected credential and then report it as a refresh failure, which sends
        # whoever reads it looking for the wrong problem.
        raise TokenExpiredError(
            "Zendesk rejected the API token. Check that it is still active in Admin "
            "Center > Apps and integrations > Zendesk API, and that the account it "
            "belongs to is still an active agent."
        )

    cfg = refresh_rejected_token(
        token,
        config_file=config_file,
        now=now,
        cfg_snapshot=snapshot,
    )
    if validate_zendesk_subdomain(cfg.get("subdomain", "")) != subdomain:
        raise UnsafeZendeskUrlError(
            "Zendesk subdomain changed while refreshing the token"
        )
    headers["Authorization"] = f"Bearer {cfg['oauth_token']}"
    response = httpx.request(method, url, headers=headers, **kwargs)
    if is_zendesk_invalid_token_response(response, subdomain):
        raise TokenExpiredError(expired_message("still rejected after refresh"))
    return response


_AUTH_KEYWORDS = ("invalid_token", "invalid_grant", "unauthorized", "couldn't authenticate")


def is_auth_error(exc: Exception) -> bool:
    """True if an exception from a Zendesk call means the credentials were rejected."""
    if isinstance(exc, TokenExpiredError):
        return True
    # Structured signals first: an httpx HTTPStatusError or a zenpy APIException both
    # carry the originating response.
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        if status_code != 401:
            return False
        subdomain = load_config().get("subdomain", "")
        if not subdomain:
            return False
        try:
            return is_zendesk_invalid_token_response(response, subdomain)
        except InvalidZendeskSubdomainError:
            return False

    text = str(exc).lower()
    if any(keyword in text for keyword in _AUTH_KEYWORDS):
        return True
    # Fall back to the status code in a stringified error, but keep it word-bounded so
    # "4013" or a ticket numbered 401 is not mistaken for an auth failure.
    if "not found" in text:
        return False
    return re.search(r"(?<!\d)401(?!\d)", text) is not None


def api_error_message(exc: Exception) -> str:
    """Render an exception from a Zendesk call as a message for the caller.

    Authorization failures get remediation; everything else keeps the existing format.
    """
    if isinstance(exc, TokenExpiredError):
        return str(exc)
    if is_auth_error(exc):
        return expired_message()
    return f"Zendesk API error: {exc}"
