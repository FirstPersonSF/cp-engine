# Hosted cp — connect Claude to shared team memory

**What this is:** the cp-engine MCP server, hosted, reachable from claude.ai,
Claude mobile, and Claude Code — no local install, no git checkout. You get
live access to the spine, commitments, project sources, meetings, semantic
search, and the tenant tree (Exec Summaries, sprint files), plus full spine
authoring — elements, versions, relations, journey steps, stakeholders,
commitments, and notes — from anywhere. Everything runs
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

## Claude Code (as of option B, cp-engine #143)

Nothing to configure: the cp tenant repo's `.mcp.json` now includes the
`cp-hosted` connector. On your next session in the tenant, run `/mcp` and
complete the one-time OAuth sign-in (Google via mc2.1p.is). Your sessions
then use the hosted server for reads *and* writes — meaning your spine
writes carry YOUR `author_id` and land in the audit log, instead of running
anonymously under the service key.

The stdio `cp-sources` server stays connected for seven verbs that haven't
migrated: the three framework verbs (`framework_compose`,
`framework_decompose`, `framework_readiness`), plus `push_to_dropbox`,
`pull_document_comments`, `pull_element_from_project`, and
`fetch_project_source`. Everything else `cp-sources` does, hosted now does
too — under your identity rather than the service key.

## Requirements (already true for all partners)

- You must be on the **team roster** (`profiles` row in MC-2). Not just "able
  to log in" — an account without a roster row authenticates fine but reads
  **zero rows everywhere**. That's the security model working, not a bug.
- For `create_note` specifically you also need an **entities row** whose
  email matches your login email (the Notes feature's people registry; the
  match is case-insensitive). All five partners have one. Every other tool
  works without it — this requirement is `create_note`-only.

## What you can do

Ask naturally — "where are we on ibx-5153?", "search the Infoblox material
for the pillar rewrite", "what commitments are open on sap-5174?", "add a
spine note that the client confirmed the Q4 date". Under the hood:

| | Tools |
|---|---|
| Project state | `get_project_state` (Exec Summary + current sprint file), `read_project_file` |
| Search | `semantic_search` (vector search over the ingested corpus) |
| Sources & meetings | `list_project_sources`, `pull_project_source`, `list_project_meetings` |
| Spine — read | `list_spine_elements`, `pull_spine_element` |
| Spine — author | `create_spine_element`, `set_spine_element`, `add_spine_version`, `add_spine_document`, `promote_spine_transcript` |
| Spine — retire | `retire_spine_element`, `retire_spine_elements` |
| Spine — sources & provenance | `add_element_source`, `remove_element_source`, `add_element_provenance`, `remove_element_provenance`, `set_element_account_scope` |
| Spine — relations | `create_spine_relation`, `retire_spine_relation` |
| Spine — lifecycle (spec v04) | `promote_to_canon` (curate the ≤7-element "current truth" set on the Inputs & Briefing anchor), `seal_to_deliverable` (absorb research into a shipped deliverable; hidden from list defaults, back via `include_absorbed=true`) |
| Spine — journey steps | `propose_spine_step`, `add_spine_step`, `set_spine_step`, `reorder_spine_step`, `remove_spine_step` |
| Stakeholders | `promote_stakeholder`, `demote_stakeholder` |
| Commitments | `list_commitments`, `create_commitment`, `resolve_commitment`, `resolve_commitments` (batch, #159), `resolve_commitments_by_meeting` (delivery-event sweep, #159) |
| Notes | `create_note` (self-note, or `recipient_email` to ping a partner) |
| Identity | `whoami` (who the server thinks you are — start here when reads come back empty) |

That's the full surface — 38 tools. For per-verb signatures and usage
discipline (when to propose a step vs. let one auto-journal, version status
vocabulary, etc.), run `/cp-tools` in Claude Code; this page deliberately
doesn't duplicate it.

Not available by design: long-form document authoring and edits to the
tenant tree itself. Those stay in Claude Code, where diffs and review work —
the hosted server can read the tree (`read_project_file`) but never writes
to it.

## Troubleshooting

- **Every tool returns 0 rows** → run `whoami` first. If it answers with your
  email, you're authenticated but not on the roster — ask Drew to add you to
  `profiles`. If it fails outright, it's the connection, not permissions.
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
