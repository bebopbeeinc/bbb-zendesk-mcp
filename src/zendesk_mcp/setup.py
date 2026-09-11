from getpass import getpass
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx
from filelock import Timeout as FileLockTimeout

from zendesk_mcp.auth import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_LOCK_TIMEOUT_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    InvalidZendeskSubdomainError,
    validate_zendesk_subdomain,
)
from zendesk_mcp.config import config_file_lock, config_path, load_config, save_config

REDIRECT_URI = "http://localhost:8787/callback"
CALLBACK_PORT = 8787
CALLBACK_TIMEOUT_SECONDS = 90


def _extract_code(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if "?" in raw or raw.startswith("http"):
        params = parse_qs(urlparse(raw).query)
        return params.get("code", [None])[0]
    return raw


def _exchange_code(
    subdomain: str,
    code: str,
    client_id: str,
    client_secret: str,
    now: float | None = None,
) -> dict:
    """Exchange the authorization code for a token pair.

    ``expires_in`` is requested explicitly: without it, a legacy OAuth client (created
    before 2026-04-30) returns a non-expiring access token and no refresh token, leaving
    nothing to renew when Zendesk later applies expiry.
    """
    subdomain = validate_zendesk_subdomain(subdomain)
    response = httpx.post(
        f"https://{subdomain}.zendesk.com/oauth/tokens",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "scope": "read write",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token_expires_in": REFRESH_TOKEN_TTL_SECONDS,
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    now = now if now is not None else time.time()
    result = {"access_token": body["access_token"]}
    if body.get("refresh_token"):
        result["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        result["expires_at"] = int(now + body["expires_in"])
    if body.get("refresh_token_expires_in"):
        result["refresh_token_expires_at"] = int(
            now + body["refresh_token_expires_in"]
        )
    return result


def _updated_config(
    existing: dict,
    subdomain: str,
    tokens: dict,
    client_id: str,
    client_secret: str,
) -> dict:
    """Replace OAuth credentials without discarding unrelated local preferences."""
    subdomain = validate_zendesk_subdomain(subdomain)
    updated = dict(existing)
    for key in (
        "oauth_token",
        "refresh_token",
        "expires_at",
        "refresh_token_expires_at",
        "client_id",
        "client_secret",
        # And the API-token credentials. They take precedence when present, so leaving
        # them here would let a successful OAuth re-authorisation appear to work while
        # every request still went out as the old token's account.
        "email",
        "api_token",
    ):
        updated.pop(key, None)

    updated["subdomain"] = subdomain
    updated["oauth_token"] = tokens["access_token"]
    updated.setdefault(
        "attachment_cache_dir", "~/.cache/zendesk-mcp/attachments"
    )

    if tokens.get("refresh_token"):
        updated["refresh_token"] = tokens["refresh_token"]
        updated["client_id"] = client_id
        updated["client_secret"] = client_secret
    if tokens.get("expires_at"):
        updated["expires_at"] = tokens["expires_at"]
    if tokens.get("refresh_token_expires_at"):
        updated["refresh_token_expires_at"] = tokens[
            "refresh_token_expires_at"
        ]
    return updated


def _persist_authorization(
    subdomain: str,
    tokens: dict,
    client_id: str,
    client_secret: str,
    git_zen_field_id: int | None = None,
    knowledge_base_enabled: bool = False,
    config_file: Path | None = None,
) -> None:
    """Persist a new grant without racing a rotating-token refresh."""
    with config_file_lock(config_file, timeout=REFRESH_LOCK_TIMEOUT_SECONDS):
        config_data = _updated_config(
            load_config(config_file),
            subdomain,
            tokens,
            client_id,
            client_secret,
        )
        if git_zen_field_id is not None:
            config_data["git_zen_field_id"] = git_zen_field_id
        if knowledge_base_enabled:
            config_data["knowledge_base_enabled"] = True
        save_config(config_data, config_file)


def _verify_token(subdomain: str, token: str) -> dict:
    subdomain = validate_zendesk_subdomain(subdomain)
    response = httpx.get(
        f"https://{subdomain}.zendesk.com/api/v2/users/me.json",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["user"]


def _verify_api_token(subdomain: str, email: str, token: str) -> dict:
    """Prove the credential works before anything is written.

    The OAuth path verifies with _verify_token() for the same reason: setup replaces the
    credential the server is currently using, so accepting an unverified one on trust
    means a typo silently destroys a working grant and reports success. The mistake then
    surfaces at 4am, in a place that cannot explain it.
    """
    import base64 as _b64
    subdomain = validate_zendesk_subdomain(subdomain)
    raw = _b64.b64encode(f"{email}/token:{token}".encode()).decode()
    response = httpx.get(
        f"https://{subdomain}.zendesk.com/api/v2/users/me.json",
        headers={"Authorization": f"Basic {raw}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["user"]


def run_api_token_setup() -> None:
    """Configure an API token instead of an OAuth grant.

    OAuth here is created by a person signing in, so the integration inherits that
    person's account and stops working when they leave -- and because refresh tokens
    rotate, only one machine can hold a working grant at a time. An API token issued to a
    shared service account (contact@, not a named human) has neither property.

    Create one at: Admin Center > Apps and integrations > APIs > Zendesk API >
    Settings > Token access > Add API token, while signed in AS that account.
    """
    import os
    print("\n  zendesk-mcp setup (API token)\n")

    subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "") or input(
        "  Zendesk subdomain (e.g. 'acme' for acme.zendesk.com): ").strip()
    try:
        subdomain = validate_zendesk_subdomain(subdomain)
    except InvalidZendeskSubdomainError as exc:
        print(f"\n  Invalid Zendesk subdomain: {exc}\n")
        sys.exit(1)

    email = os.environ.get("ZENDESK_EMAIL", "") or input(
        "  Zendesk account the token belongs to (use a shared one, not a person): ").strip()
    token = os.environ.get("ZENDESK_API_TOKEN", "") or getpass("  API token: ").strip()
    if not email or not token:
        print("\n  Both an account email and an API token are required.\n")
        sys.exit(1)

    # Verify BEFORE touching the config, so a mistyped token cannot cost the working one.
    try:
        who = _verify_api_token(subdomain, email, token)
    except Exception as exc:                                  # noqa: BLE001
        print(f"\n  Zendesk rejected those credentials: {exc}")
        print("  Nothing was changed; the existing configuration is untouched.")
        print("  Check the token in Admin Center > Apps and integrations > APIs > "
              "Zendesk API > Settings > Token access,")
        print("  and that the email is the account the token was created under.\n")
        sys.exit(1)

    from zendesk_mcp.config import config_file_lock, load_config, save_config
    with config_file_lock():
        cfg = load_config()
        cfg["subdomain"] = subdomain
        cfg["email"] = email
        cfg["api_token"] = token
        # Remove the OAuth credentials so there is exactly one answer to "what is this
        # authenticating as". Leaving both would make the effective identity depend on
        # which branch of the code happened to run.
        for key in ("oauth_token", "refresh_token", "client_id", "client_secret",
                    "expires_at"):
            cfg.pop(key, None)
        save_config(cfg)

    role = who.get("role", "unknown")
    print(f"\n  Saved. Authenticating as {who.get('name', email)} <{email}> "
          f"on {subdomain}.zendesk.com (role: {role}).")
    if role == "end-user":
        # An end-user cannot read the agent-side API at all, so this would fail on the
        # first real call rather than here.
        print("  WARNING: that account is an end-user, not an agent — most tools will "
              "return nothing.")
    print("  This credential does not expire and is not tied to anyone's login session.\n")


def run_setup() -> None:
    import os
    if "--api-token" in sys.argv:
        run_api_token_setup()
        return
    print("\n  zendesk-mcp setup\n")

    env_subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "")
    env_client_id = os.environ.get("ZENDESK_CLIENT_ID", "")
    env_client_secret = os.environ.get("ZENDESK_CLIENT_SECRET", "")

    if env_subdomain:
        subdomain = env_subdomain
        print(f"  Zendesk subdomain: {subdomain} (from ZENDESK_SUBDOMAIN)")
    else:
        subdomain = input("  Zendesk subdomain (e.g. 'acme' for acme.zendesk.com): ").strip()

    try:
        subdomain = validate_zendesk_subdomain(subdomain)
    except InvalidZendeskSubdomainError as exc:
        print(f"\n  Invalid Zendesk subdomain: {exc}\n")
        sys.exit(1)

    if env_client_id:
        client_id = env_client_id
        print(f"  OAuth client_id: {client_id} (from ZENDESK_CLIENT_ID)")
    else:
        client_id = input("  OAuth client_id: ").strip()

    if env_client_secret:
        client_secret = env_client_secret
        print("  OAuth client_secret: *** (from ZENDESK_CLIENT_SECRET)")
    else:
        client_secret = getpass("  OAuth client_secret: ").strip()

    auth_url = (
        f"https://{subdomain}.zendesk.com/oauth/authorizations/new"
        f"?response_type=code"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        f"&client_id={quote(client_id, safe='')}"
        f"&scope=read%20write"
    )

    code_holder: dict = {"code": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authorization successful! You can close this tab.")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"No authorization code received.")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    server.timeout = 1

    def _serve():
        import time
        deadline = time.time() + CALLBACK_TIMEOUT_SECONDS
        while code_holder["code"] is None and time.time() < deadline:
            server.handle_request()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    print(f"\n  Opening browser for Zendesk authorization...")
    print(f"  Waiting up to {CALLBACK_TIMEOUT_SECONDS}s for callback on {REDIRECT_URI}\n")
    print(f"  If your browser is on a different machine, open this URL manually:")
    print(f"    {auth_url}\n")

    webbrowser.open(auth_url)

    thread.join(timeout=CALLBACK_TIMEOUT_SECONDS)

    if code_holder["code"] is None:
        print("  Callback not received automatically.")
        pasted = input("  Paste the full redirect URL (or just the code value) here: ").strip()
        code_holder["code"] = _extract_code(pasted)

    if not code_holder["code"]:
        print("\n  No authorization code received. Setup failed.\n")
        sys.exit(1)

    print("\n  Exchanging code for access token...")
    try:
        tokens = _exchange_code(subdomain, code_holder["code"], client_id, client_secret)
    except Exception as e:
        print(f"\n  Token exchange failed: {e}\n")
        sys.exit(1)

    token = tokens["access_token"]

    print("  Verifying token...")
    try:
        user = _verify_token(subdomain, token)
    except Exception as e:
        print(f"\n  Token verification failed: {e}\n")
        sys.exit(1)

    role = user.get("role", "unknown")
    email = user.get("email", "unknown")

    git_zen_input = input(
        "  Git-Zen integration field ID (optional, press Enter to skip): "
    ).strip()
    git_zen_field_id = None
    if git_zen_input:
        try:
            git_zen_field_id = int(git_zen_input)
        except ValueError:
            print(f"  Warning: '{git_zen_input}' is not a valid integer; skipping Git-Zen field ID.")

    kb_input = input(
        "  Enable Help Center knowledge base resource? (y/N): "
    ).strip().lower()
    try:
        _persist_authorization(
            subdomain,
            tokens,
            client_id,
            client_secret,
            git_zen_field_id=git_zen_field_id,
            knowledge_base_enabled=kb_input in {"y", "yes"},
        )
    except FileLockTimeout:
        print("\n  Setup failed: timed out waiting to save OAuth credentials.\n")
        sys.exit(1)

    cfg_path = config_path()
    if role == "admin":
        print(f"\n  Warning: connected as {email} (role: admin).")
        print("     Consider using a dedicated agent-role account for least-privilege access.")
    else:
        print(f"\n  Authorization successful.")
        print(f"  Verified: connected as {email} (role: {role})")

    print(f"  Token saved to {cfg_path}\n")

    if tokens.get("refresh_token"):
        print("  Access token renews automatically; no action needed until the refresh")
        print(f"  token expires ({REFRESH_TOKEN_TTL_SECONDS // 86400} days of no use).\n")
    else:
        print("  Note: this OAuth client issued a non-expiring token and no refresh token.")
        print("     If Zendesk later applies expiry to it, re-run this setup.\n")
