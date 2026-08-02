# Hosted cp — connect Claude to shared team memory

**What this is:** the cp-engine MCP server, hosted, reachable from claude.ai,
Claude mobile, and Claude Code — no local install, no git checkout. You get
live access to the spine, commitments, project sources, meetings, semantic
search, and the tenant tree (Exec Summaries, sprint files), plus the ability
to add spine elements, commitments, and notes from anywhere. Everything runs
under **your own identity**: Postgres row-level security is the authorization
boundary, and every call is audit-logged.

## Setup (once, ~2 minutes)

1. In claude.ai: **Settings → Connectors → Add custom connector**
2. URL: `https://cp.mc-2.1p.is/mcp`
3. Under **Advanced settings**, OAuth Client ID:
   `e6dcd894-7d04-461c-a84f-bf8bbec13b6d` — leave the client secret **blank**
   (it's a public PKCE client).
4. Click Connect. You'll be bounced to Mission Control (`mc2.1p.is`) — sign in
   with Google if you aren't already — and land on a consent screen. **Approve.**
5. Done. The same connector works on Claude mobile automatically.

## Requirements (already true for all partners)

- You must be on the **team roster** (`profiles` row in MC-2). Not just "able
  to log in" — an account without a roster row authenticates fine but reads
  **zero rows everywhere**. That's the security model working, not a bug.
- For `create_note` specifically you also need an **entities row** with your
  email (the Notes feature's people registry). Partners all have one.

## What you can do

Ask naturally — "where are we on ibx-5153?", "search the Infoblox material
for the pillar rewrite", "what commitments are open on sap-5174?", "add a
spine note that the client confirmed the Q4 date". Under the hood:

| | Tools |
|---|---|
| Project state | `get_project_state` (Exec Summary + current sprint file), `read_project_file` |
| Spine | `list_spine_elements`, `pull_spine_element`, `create_spine_element` |
| Commitments | `list_commitments`, `create_commitment` |
| Sources & meetings | `list_project_sources`, `pull_project_source`, `list_project_meetings` |
| Search | `semantic_search` (vector search over the ingested corpus) |
| Notes | `create_note` (self-note, or `recipient_email` to ping a partner) |

Not available (by design, for now): editing existing spine versions
(`add_spine_version` — see cp-engine #142), long-form document authoring
(that stays in Claude Code where diffs and review work).

## Troubleshooting

- **Every tool returns 0 rows** → you're authenticated but not on the roster;
  ask Drew to add you to `profiles`.
- **`create_note` says "no entities row"** → ask a partner to add you to the
  entities registry in MC-2.
- **Connector won't finish OAuth** → make sure you completed the Google
  sign-in on mc2.1p.is before the consent screen; then retry Connect.
- Something else → the server audit-logs every call; tell Drew roughly when
  you tried and the failing tool.

---
*Server: Railway service `hosted-mcp` (Mission Control project), canonical
URL `https://cp.mc-2.1p.is/mcp` (alias: `mcp.mc-2.1p.is`). Source:
`cp-engine/prototypes/hosted-mcp/`. Auth: Supabase OAuth 2.1 (PKCE), ES256
JWTs verified against the project JWKS. No service-role credentials anywhere
in the hosted path.*
