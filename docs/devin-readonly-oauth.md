# Read-only Zendesk OAuth for headless Devin sessions

## Recommended connection

Use this server's opt-in **client-credentials** mode with a new Zendesk
**Confidential** OAuth client whose **Allowed scopes** are exactly `read`.
The server also requests exactly `read`, refuses write methods locally, and
advertises only read tools. It never runs browser setup, reads an existing
interactive grant, or saves credentials/tokens to disk in this mode.

Zendesk explicitly recommends client credentials for background jobs. These
grants have **no refresh token**: the server caches the short-lived access token
in memory and obtains another using the client credentials before expiry or
after one trusted `invalid_token` response. Each Devin process gets its own grant;
sessions do not race to rotate a shared refresh token.

This mode is **not present at historical revision
`50d8f9a96c6dce86f876efb275b5a8f817d823f3`**. That revision reads the three
`ZENDESK_*` variables only during interactive `setup`, requests `read write`,
and subsequently reads its grant from `~/.config/zendesk-mcp/config.json`.
Installing that revision with only the environment variables is insufficient.
Use the reviewed commit containing this document instead.

## What Marcelo must do once

1. Sign in as the Zendesk admin who will own this integration at
   [bebopbeehelp's OAuth clients](https://bebopbeehelp.zendesk.com/admin/apps-integrations/apis/zendesk-api/oauth_clients).
   The account is `bebopbeehelp`, as configured by `bbb-agents`' community-desk
   client; `wheres-chonky` in its games file is a Help Center host, not the API account.
2. Add a **separate** OAuth client; do not change or copy the current build Mac's
   OAuth client or token file.

   | Setting | Value |
   | --- | --- |
   | Name | `Devin Community Read Only` |
   | Identifier | `devin-community-readonly` (or the exact unique identifier Zendesk accepts) |
   | Client kind | **Confidential** |
   | Allowed scopes / Scopes | **`read` only**; do not leave blank or add `write` / `impersonate` |
   | Redirect URLs | Empty; client credentials do not use a redirect |
   | Token expiration | Keep the new-client default; the server explicitly requests `expires_in=1800` seconds |
   | Description | Read-only community pilot; no ticket changes or player replies |

3. Save and copy the one-time client secret through Devin's secure secret entry,
   never ordinary chat. Save these MCP-specific protected secrets for future sessions:

   | MCP secret name | Value | Server environment variable |
   | --- | --- | --- |
   | `MCP_ZENDESK_CLIENT_ID` | The **Identifier** string above, not the numeric OAuth client record ID | `ZENDESK_CLIENT_ID` |
   | `MCP_ZENDESK_CLIENT_SECRET` | The OAuth client's one-time Secret | `ZENDESK_CLIENT_SECRET` |
   | `MCP_ZENDESK_SUBDOMAIN` | `bebopbeehelp` (no URL or `.zendesk.com`) | `ZENDESK_SUBDOMAIN` |

There is **no separate browser Allow/consent step** for client credentials,
no API token, no access-token secret to copy, and no refresh-token secret to rotate.
The client secret remains a long-lived sensitive credential: rotate/revoke it
in Zendesk and replace the protected MCP secret when needed.

Tokens act as the admin who created the client, bounded by the client's allowed
scopes and the token's `read` scope. An existing authorized admin can own this
pilot; a dedicated integration admin is optional for a stable identity. If the
creator is deleted or loses the required admin permissions, these grants stop
working. Creating a client and then downgrading its creator is not a supported
way to limit this connection; the `read` scope is the write restriction.

## Install command and Bot attachment (do not run until authorized)

Replace `REVIEWED_COMMIT_SHA` with the immutable reviewed commit containing
headless mode; it must **not** be the historical revision above. On the current
Linux Devin image, the STDIO launch command is:

```sh
/usr/bin/env ZENDESK_AUTH_MODE=client_credentials /home/ubuntu/.local/bin/uvx --from git+https://github.com/bebopbeeinc/bbb-zendesk-mcp@REVIEWED_COMMIT_SHA zendesk-mcp
```

After the code is reviewed and the three protected secrets exist, the Bot can
request this **custom STDIO** installation with `devin_mcp_server_manage`:

```json
{
  "action": "install",
  "name": "Zendesk Read Only",
  "slug": "zendesk-readonly",
  "short_description": "Read-only Zendesk OAuth for the community pilot",
  "transport": "STDIO",
  "command": "/usr/bin/env ZENDESK_AUTH_MODE=client_credentials /home/ubuntu/.local/bin/uvx --from git+https://github.com/bebopbeeinc/bbb-zendesk-mcp@REVIEWED_COMMIT_SHA zendesk-mcp",
  "auth_method": "none",
  "env_secret_names": {
    "ZENDESK_CLIENT_ID": "MCP_ZENDESK_CLIENT_ID",
    "ZENDESK_CLIENT_SECRET": "MCP_ZENDESK_CLIENT_SECRET",
    "ZENDESK_SUBDOMAIN": "MCP_ZENDESK_SUBDOMAIN"
  }
}
```

`auth_method: none` describes local STDIO transport only. The Python server
authenticates to Zendesk with OAuth; this is **not** anonymous Zendesk access.
Run the install request in this Bot's conversation/session so it attaches to
`bot-a586179aaf2040d69081a243f69ab403`, not an unrelated organization connection.
The launch executable must exist by absolute path on each session image.

The only authentication request made by the server is:

```text
POST https://bebopbeehelp.zendesk.com/oauth/tokens
Content-Type: application/json

{"grant_type":"client_credentials","client_id":"<Identifier>",
 "client_secret":"<protected-secret>","scope":"read","expires_in":1800}
```

Do not run `zendesk-mcp setup` in this mode. Do not seed `config.json` from the
build Mac, or copy an access/refresh token into multiple VMs. Revoking this
dedicated client can disable the pilot without interrupting the current agent.

## Does Zendesk host its own MCP?

**Yes: Zendesk Bridge / External AI is documented as an early-access program.**
Account enrollment, available tools, OAuth metadata, and read-only scope
compatibility were not verified for bebopbeehelp; no Zendesk account was accessed.
There is no Zendesk entry in Devin's MCP marketplace as checked for this work.

If Marcelo already has External AI enabled, a hosted option is:

1. In Admin Center, open **AI > External AI > Add connection > Bring your own provider**.
   Create a separate OAuth client first. Zendesk's EAP instructions say to leave
   its **Allowed scopes blank**, unlike the strictly `read`-capped local recipe above.
2. Use `https://api.devin.ai/mcp/oauth/callback` as the redirect URL for a
   pre-registered Devin OAuth client. Select only read/lookup tools in Zendesk;
   add no write tools or delegating AI agents. Limit access to the intended admins/group.
3. Copy the **actual MCP URL from the connection's Setup > Tools tab**. Zendesk
   gives `https://example.zendesk.com/mcp` as an example, not an account-specific
   guarantee. Do not assume `/api/mcp` from third-party blog posts.
4. Request a custom `HTTP` MCP installation with that copied URL,
   `auth_method="oauth"`, `oauth_client_id=<Identifier>`, and
   `oauth_scope="read"`. Enter the client secret in Devin's OAuth credentials
   settings, not in the install tool call or session text; if a named protected
   secret is needed for handoff, use a distinct `MCP_ZENDESK_HOSTED_CLIENT_SECRET`.
5. Complete browser sign-in as the intended Zendesk user once. Devin's managed
   OAuth connection handles later sessions; if reauthorization becomes necessary,
   the user must sign in again.

This is a **conditional alternative**, not an installation-ready verified route:
the EAP's public docs do not promise that its MCP accepts `read` alone or list
the required scopes for every tool. If it demands `write`, do not broaden the
pilot grant; use the local client-credentials/read-only setup instead. No local
MCP secret mapping is needed for managed HTTP OAuth credentials.

## Verification boundary

Tests for headless mode mock OAuth and Zendesk responses. They cover renewal,
retry, scope rejection, no credential-file use, read-only tool discovery, and
both direct and Zenpy HTTP write prevention. Live authentication and ticket
reads require separate authorization after setup. This work does not install
or connect any MCP, create an OAuth client, read Zendesk data, or modify tickets.
The build Mac's existing community-desk remains unchanged. Pilot replies remain
drafts only, and this connection does not post to Slack or `#community`.

## References

- [Zendesk API-token migration and April 30, 2027 retirement](https://developer.zendesk.com/documentation/authentication/oauth-migration/)
- [Zendesk client credentials, scope, and token lifetime contract](https://developer.zendesk.com/api-reference/ticketing/oauth/grant_type_tokens/)
- [Zendesk no-redirect client-credentials setup](https://developer.zendesk.com/documentation/authentication/api-tokens-to-oauth/)
- [Zendesk OAuth registration, scope ceiling, and creator permissions](https://support.zendesk.com/hc/en-us/articles/4408845965210-Using-OAuth-authentication-with-your-application)
- [Zendesk Bridge / External AI hosted MCP EAP](https://support.zendesk.com/hc/en-us/articles/11400026079130-Connecting-external-AI-agents-to-Zendesk-EAP)
- [Devin custom MCP and exact OAuth callback](https://docs.devin.ai/work-with-devin/mcp)
- [Historical setup implementation](https://github.com/bebopbeeinc/bbb-zendesk-mcp/blob/50d8f9a96c6dce86f876efb275b5a8f817d823f3/src/zendesk_mcp/setup.py)
- [Current agent's separate OAuth usage](https://github.com/bebopbeeinc/bbb-agents/tree/main/pipelines/community-desk)
