# zendesk-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes Zendesk ticket read and write tools to [Claude Code](https://claude.com/claude-code) and other MCP clients.

## What it does

- Search, list (paginated), and fetch Zendesk tickets, comments, and attachments
- Create new tickets and update existing ticket fields (including group, custom status, and tags)
- Post public replies and internal notes
- Set ticket status and assign tickets to agents
- Browse and apply views and macros
- Look up users, groups, organizations, and custom statuses
- Read and write time-tracking entries
- Format a ticket as a Markdown issue draft for handoff to a tracker (GitLab, GitHub, Jira)
- Two MCP prompts (`analyze-ticket`, `draft-ticket-response`) for ticket analysis and response drafting
- (Optional) Expose Zendesk Help Center articles as an MCP resource
- (Optional) Read linked GitLab issues / MRs / commits via the [Git-Zen](https://www.zendesk.com/marketplace/apps/support/630175/git-zen-zendesk-and-gitlab-integration/) Zendesk app

## Prerequisites

- Python 3.10 or newer
- A Zendesk OAuth client. A Zendesk admin can create one at:
  `https://<your-subdomain>.zendesk.com/admin/apps-integrations/apis/zendesk-api/oauth_clients`
  Use a **Confidential** client, set the redirect URL to
  `http://localhost:8787/callback`, and allow the `read write` scopes.

## Install

Install into a project-local virtualenv. Using a venv keeps `zendesk-mcp` and its dependencies isolated from your system Python and from other projects, and is the recommended path for everything below.

From a clone of this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

For development (also installs pytest):

```bash
.venv/bin/pip install -e ".[dev]"
```

> Throughout this README, commands use the venv's binaries via `.venv/bin/...`. You can instead `source .venv/bin/activate` once per shell and drop the prefix — the result is the same.

## OAuth setup

Run the interactive setup using the venv's Python:

```bash
.venv/bin/python -m zendesk_mcp setup
```

You will be prompted for:

1. Your Zendesk subdomain (e.g. `acme` for `acme.zendesk.com`)
2. The OAuth client ID created by your admin
3. The OAuth client secret
4. (Optional) A Git-Zen integration field ID — see [Optional: Git-Zen integration](#optional-git-zen-integration)
5. (Optional) Whether to enable the Help Center knowledge base resource — see [Optional: Help Center knowledge base](#optional-help-center-knowledge-base)

The setup opens a browser for the OAuth authorization step, then writes a token to `~/.config/zendesk-mcp/config.json` (mode `0600`).

If you have no browser, the URL is printed to the terminal — open it on any device, click **Allow**, and paste the resulting redirect URL back into the prompt.

### Token expiry and refresh

Zendesk access tokens expire. OAuth clients created on or after 2026-04-30 get a
30-minute default lifetime; older clients issue non-expiring tokens unless an expiry is
requested. Setup requests a 24-hour access token and a 90-day refresh token so the
behaviour is the same either way, and the server renews the access token automatically —
before it expires, and again if Zendesk rejects a token mid-request.

To make that possible, the config file also stores `refresh_token`, `expires_at`,
`refresh_token_expires_at`, `client_id`, and `client_secret` alongside the access token.
Credential rotation is serialized across MCP processes and persisted atomically. Keep
the file at mode `0600`; it is the same trust level as the access token itself. If your
OAuth client returns no refresh token, setup says so and the token is used as-is.

Re-run `.venv/bin/python -m zendesk_mcp setup` when:

- the refresh token expires (90 days with no use), or
- you revoke the OAuth grant in Zendesk.

In either case the tools fail with `Zendesk authorization failed: ... Re-run: zendesk-mcp setup`
rather than failing opaquely.

## Register with Claude Code

Register the MCP server using the venv's Python by absolute path. Claude Code launches the server in a fresh shell that does **not** inherit your activated venv, so the absolute path is required — pointing at a bare `python` here will fail to import `zendesk_mcp`.

```bash
ZENDESK_MCP_DIR="$(pwd)"   # run this from the repo root, after install
claude mcp add --scope user zendesk -- "$ZENDESK_MCP_DIR/.venv/bin/python" -m zendesk_mcp
```

Or just inline the absolute path you want:

```bash
claude mcp add --scope user zendesk -- /absolute/path/to/zendesk-mcp/.venv/bin/python -m zendesk_mcp
```

Then add the read tools to `permissions.allow` in `~/.claude/settings.json` to avoid per-call prompts:

```json
{
  "permissions": {
    "allow": [
      "mcp__zendesk__zendesk_get_ticket",
      "mcp__zendesk__zendesk_get_tickets",
      "mcp__zendesk__zendesk_get_comments",
      "mcp__zendesk__zendesk_list_attachments",
      "mcp__zendesk__zendesk_download_attachment",
      "mcp__zendesk__zendesk_search_tickets",
      "mcp__zendesk__zendesk_ticket_to_gitlab_context",
      "mcp__zendesk__zendesk_list_views",
      "mcp__zendesk__zendesk_get_view",
      "mcp__zendesk__zendesk_get_view_tickets",
      "mcp__zendesk__zendesk_list_macros",
      "mcp__zendesk__zendesk_preview_macro",
      "mcp__zendesk__zendesk_search_users",
      "mcp__zendesk__zendesk_get_groups",
      "mcp__zendesk__zendesk_get_group_users",
      "mcp__zendesk__zendesk_get_organization",
      "mcp__zendesk__zendesk_list_custom_statuses"
    ]
  }
}
```

Write tools (`zendesk_post_comment`, `zendesk_post_internal_note`, `zendesk_set_ticket_status`, `zendesk_assign_ticket`, `zendesk_create_ticket`, `zendesk_update_ticket`, `zendesk_log_time`, `zendesk_add_tag`, `zendesk_remove_tag`, `zendesk_apply_macro`) are intentionally not in the default allow-list — Claude will prompt you per call.

## Tools

**A failure is always an error result, never a successful one.** When a call fails — Zendesk
refuses a search, the token is expired, a ticket does not exist, an argument is invalid, an
attachment will not unpack — the tool raises, and the client receives an error result
(`isError: true`) carrying the message behind the SDK's prefix, e.g.

```
Error executing tool zendesk_search_tickets: Zendesk API error: {"error": "invalid", "description": "Invalid search: Error filtering on field: via_id"}
```

So a successful result never reports a failure. Most tools return JSON; a few return prose on
success, by design: `zendesk_set_ticket_status`, `zendesk_assign_ticket`,
`zendesk_post_comment` and `zendesk_post_internal_note` return a one-line confirmation, and
`zendesk_ticket_to_gitlab_context` returns Markdown. `zendesk_create_ticket` is the one success
that can carry a `warning`: when Zendesk creates the ticket but its response has no id, the
result is `{"id": null, "warning": ...}` rather than an error, so a caller does not retry and
create a duplicate.

Every tool that returns ticket records — `zendesk_search_tickets`, `zendesk_get_ticket`,
`zendesk_get_tickets`, `zendesk_get_view_tickets`, `zendesk_create_ticket`,
`zendesk_update_ticket` and `zendesk_apply_macro` — includes `channel`: the channel the ticket
arrived on (Zendesk's `via.channel`: `web`, `email`, `api`, `facebook`, `native_messaging`,
`sunshine_conversations_facebook_messenger`, …). Search accepts it too:
`via:sunshine_conversations_facebook_messenger` finds Facebook Messenger tickets, where
`via:messenger` is an invalid search.

### Tickets

| Tool | What it does |
|---|---|
| `zendesk_search_tickets` | Search tickets by status, priority, type, assignee, requester, tags, or keyword; each result carries its `channel`. A query Zendesk rejects is an error |
| `zendesk_get_tickets` | List tickets with pagination and sorting (page, per_page, sort_by, sort_order), each with its `channel` |
| `zendesk_get_ticket` | Get one ticket's metadata, including its `channel` |
| `zendesk_create_ticket` | Create a new ticket (subject, description, optional priority/type/assignee_id/requester_id/tags/custom_fields); returns it with its `channel` |
| `zendesk_update_ticket` | Update one or more fields on an existing ticket (status, priority, subject, type, assignee_id, requester_id, group_id, custom_status_id, tags, custom_fields, due_at) |
| `zendesk_get_comments` | Get the conversation thread on a ticket; each comment lists its [Messaging uploads](#messaging-uploads) in `transcript_uploads` |
| `zendesk_list_attachments` | List attachments on a ticket, and the files customers sent through [Zendesk Messaging](#messaging-uploads) |
| `zendesk_download_attachment` | Download an attachment or Messaging upload to a [local directory](#where-downloads-are-saved); an image comes back as a picture the model can see |
| `zendesk_ticket_to_gitlab_context` | Format a ticket and its conversation as a Markdown issue draft |
| `zendesk_post_comment` | Post a public reply on a ticket |
| `zendesk_post_internal_note` | Post an agent-only internal note on a ticket |
| `zendesk_set_ticket_status` | Set ticket status (`new`, `open`, `pending`, `hold`, `solved`, `closed`) |
| `zendesk_assign_ticket` | Assign a ticket to an agent by email or `me` |

### Tags

| Tool | What it does |
|---|---|
| `zendesk_add_tag` | Add a tag to a ticket (idempotent) |
| `zendesk_remove_tag` | Remove a tag from a ticket (idempotent) |

### Views & Macros

| Tool | What it does |
|---|---|
| `zendesk_list_views` | List all active views |
| `zendesk_get_view` | Get a view's filter conditions and execution settings |
| `zendesk_get_view_tickets` | Fetch tickets currently matching a view, each with its `channel` |
| `zendesk_list_macros` | List active macros with their actions |
| `zendesk_preview_macro` | Preview what changes a macro would make |
| `zendesk_apply_macro` | Apply a macro to a ticket (applies field changes and posts any comment) |

### Users, Groups & Organizations

| Tool | What it does |
|---|---|
| `zendesk_search_users` | Find users by name or email |
| `zendesk_get_groups` | List all active groups |
| `zendesk_get_group_users` | List the members of a group |
| `zendesk_get_organization` | Fetch an organization including custom fields |
| `zendesk_list_custom_statuses` | List all custom ticket statuses and their IDs |

### Time tracking

| Tool | What it does |
|---|---|
| `zendesk_get_time_tracking` | Read time-tracking entries for a ticket |
| `zendesk_log_time` | Log a time entry against a ticket |

### Git-Zen integration

| Tool | What it does |
|---|---|
| `zendesk_get_git_zen_links` | (Git-Zen only) Get linked GitLab issues / MRs / commits for a ticket |

## Messaging uploads

A picture or file a customer sends through Zendesk Messaging — Facebook Messenger
(`sunshine_conversations_facebook_messenger`), the in-app widget (`native_messaging`) — is
**not** a comment attachment: the comment's `attachments` is empty. The transcript comment's
body names each upload instead:

```
(12:01:36) Jane Doe uploaded: photo.jpeg
URL: https://acme.zendesk.com/sc/attachments/v2/01J8ZQ4M7K2V9X3B5N6P1R0T2Y/photo.jpeg
Type: image/jpeg
Size: 226766
```

The server reads those blocks, additively:

- `zendesk_get_comments` — every comment carries `transcript_uploads`:
  `[{file_name, url, content_type, size, time, uploaded_by}]`, empty when there are none.
  `time` and `uploaded_by` (the customer, an agent or a bot) are as the transcript gives them,
  and `null` when it does not; a missing `Type` or `Size` line is `null` too.
- `zendesk_list_attachments` — lists each one after its comment's own attachments, with
  `"source": "messaging_transcript"`, the `comment_id`, `uploaded_by`, `time`, and the upload
  URL as `download_url`. There is no attachment id: Zendesk does not assign one.
- `zendesk_download_attachment` — fetches that URL like any other attachment. It is on your
  account host and redirects to a signed download.

Only URLs on the configured `https://<subdomain>.zendesk.com` under `/sc/attachments/` are
reported. Anything else in a body is text someone could have typed, and is ignored.

### Images

`zendesk_download_attachment` returns an image as an MCP image block the model can see — a
copy downscaled to a 1568 px long edge, PNG when it has transparency and JPEG otherwise,
under 500 KB — and a short JSON text block: `cached_path` (the original, saved untouched),
`size_bytes`, `width`, `height` and `content_type` of the original, and the `preview`'s own.
Up to 0.1.4 the image came back as base64 inside the JSON text, which at a few hundred KB
overflowed the client's tool-output limit. Other file types are returned as before.

## Where downloads are saved

`zendesk_download_attachment` saves to `dest_dir` when given, otherwise to
`~/.cache/zendesk-mcp/attachments/<ticket_id>` (or `attachment_cache_dir`/`<ticket_id>` from
the config file). Archives are unpacked next to the file.

Set `ZENDESK_MCP_ATTACHMENT_ROOT` to an absolute directory to confine every attachment write
— the download and anything unpacked from an archive — to it:

- the default location becomes `<root>/<ticket_id>`, whatever the config file says;
- a `dest_dir`, a file, or an unpack directory whose real path (symlinks resolved) is outside
  the root is refused with an error; the directory and the file are checked before anything
  is fetched;
- link members of a tar archive are not extracted;
- set but blank or relative is an error, never treated as unset.

Unset, nothing changes. The credentials file is not an attachment and is not affected.

## Prompts

The server exposes two MCP prompts that some clients (e.g. Claude Desktop) surface as slash commands:

| Prompt | Argument | What it does |
|---|---|---|
| `analyze-ticket` | `ticket_id` | Asks the model to fetch the ticket and produce a summary, status/timeline, and key interaction points |
| `draft-ticket-response` | `ticket_id` | Asks the model to fetch the ticket and draft a customer-facing response (with a confirmation step before posting) |

## Optional: Git-Zen integration

If your Zendesk instance uses the [Git-Zen](https://www.zendesk.com/marketplace/apps/support/630175/git-zen-zendesk-and-gitlab-integration/) app, the `zendesk_get_git_zen_links` tool can read its custom-field payload. Find your instance's Git-Zen custom field ID under **Admin → Tickets → Fields** (it is a numeric ID), then either set it during `.venv/bin/python -m zendesk_mcp setup` or edit `~/.config/zendesk-mcp/config.json` to add:

```json
{
  "git_zen_field_id": 12345678901234
}
```

Without this configured, `zendesk_get_git_zen_links` fails with a "not configured" error.

## Optional: Help Center knowledge base

If your Zendesk instance has a published Help Center, you can expose its sections and articles as the `zendesk://knowledge-base` MCP resource. The resource returns a single JSON document covering all sections and articles, cached for one hour.

This is opt-in. Enable it by either answering "y" to the prompt during `.venv/bin/python -m zendesk_mcp setup`, or by adding the following to `~/.config/zendesk-mcp/config.json`:

```json
{
  "knowledge_base_enabled": true
}
```

When the flag is absent or false, the resource is not registered, keeping the server's resource list empty for instances without a Help Center.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests run on Python 3.10, 3.11, and 3.12 in CI (see `.github/workflows/test.yml`).

## License

[Apache-2.0](LICENSE)

<!-- mcp-name: io.github.michaelrice/zendesk-mcp -->
