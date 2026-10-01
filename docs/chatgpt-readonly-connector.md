---
Project: cp-engine
Provenance: Version 01 | 2026-09-30
Filename: chatgpt-readonly-connector.md
Author: Claude (cp-engine #141)
---

# Connect ChatGPT Business to cp, read-only

**Who this is for:** the ChatGPT Business workspace admin at First Person
(or an agent helping them). You need to be able to open the ChatGPT
workspace admin settings, and to be on the cp team roster yourself. You do
not need to know how cp works inside. Part 0 is for whoever deploys the
server, and it has to be done first.

**What this gives you:** ChatGPT can **read** the team's shared cp memory:
Exec Summaries, sprint files, the spine, commitments, meeting lists,
ingested documents, and semantic search. It can read every workstream you
can already see in Claude, and nothing more. It **cannot write anything.**
The endpoint it connects to has no write tools on it at all.

**Who may use it:** the four partners (Drew, Marcello, Brandon, Tony).
Decided by Drew on 2026-09-30. Client contracts do not restrict it.

**How a person uses it:** in ChatGPT, turn on the "cp" app in a chat and ask
normally, for example "where are we on ibx-5153?". Each partner signs in
once, as themselves, with their `@firstperson.is` Google account. After that
there is nothing to remember.

---

## Part 0 — Server prerequisites (deployer, once)

The read-only endpoint is `https://cp.mc-2.1p.is/mcp/read`. It is on branch
`feat/141-readonly-connector` and is **not deployed yet**.

1. Merge the branch, then deploy the way `prototypes/hosted-mcp/railway.toml`
   says (`prototypes/hosted-mcp/deploy.sh`; pushing to `main` deploys nothing).
2. Leave `READ_RESOURCE_URL` unset. It defaults to `RESOURCE_URL + "/read"`,
   which is `https://cp.mc-2.1p.is/mcp/read`.
3. Check it:

   ```bash
   curl -s https://cp.mc-2.1p.is/health | python3 -m json.tool
   # expect: "read_endpoint": {"path": "/mcp/read", "tool_count": 24}

   curl -s https://cp.mc-2.1p.is/.well-known/oauth-protected-resource/mcp/read
   # expect: {"resource":"https://cp.mc-2.1p.is/mcp/read",
   #          "authorization_servers":["https://mgheymslksfyhuvhmvmj.supabase.co/auth/v1"], ...}

   curl -s -i -X POST https://cp.mc-2.1p.is/mcp/read | grep -i www-authenticate
   # expect: 401 with resource_metadata=".../.well-known/oauth-protected-resource/mcp/read"
   ```

4. Supabase OAuth must stay set up as it is now. Checked live 2026-09-30:
   - **Dynamic client registration is ON** ("Allow Dynamic OAuth Apps",
     Authentication → OAuth server). The AS metadata at
     `https://mgheymslksfyhuvhmvmj.supabase.co/.well-known/oauth-authorization-server/auth/v1`
     lists a `registration_endpoint`. ChatGPT uses this to register itself,
     so it must stay on.
   - **PKCE S256** is advertised (`code_challenge_methods_supported`
     includes `S256`). ChatGPT requires this.
   - The consent page is `https://mc2.1p.is/oauth/consent`. It is the same
   one Claude uses.

**You do not need to add a redirect URI.** Supabase does not advertise
`authorization_response_iss_parameter_supported`, so ChatGPT uses a
per-connection callback (`https://chatgpt.com/connector/oauth/{callback_id}`),
not the stable `https://chatgpt.com/connector_platform_oauth_redirect`.
With DCR, ChatGPT registers that callback itself. You only need to
pre-register a client by hand if DCR is ever turned off. In that case, copy
the exact callback URI from the app's settings page in ChatGPT.

## Part 1 — ChatGPT Business workspace settings (admin, once)

The plan is **ChatGPT Business** (formerly Team). Public OpenAI docs cover
some of this. Anything marked **verify in the admin console** could not be
confirmed from public docs, so check it on screen.

1. **Training exclusion — confirm.** OpenAI says Business workspace data
   (conversations, and what connectors return into them) is **not used for
   training by default**. Open Workspace settings → data controls and confirm
   that nothing has opted the workspace in. *(Verify in the admin console:
   the setting's exact name and location.)*
2. **Turn on custom MCP apps.** Workspace settings → **Permissions & roles**
   → Connected data → enable **Developer mode / Create custom MCP connectors**.
   *(Verify in the admin console: the exact label.)*
3. **Turn on developer mode for yourself.** Settings → Security and login
   (or Apps / Connectors) → **Developer mode**. On Business, each admin or
   owner turns this on for themselves. It does not apply to other admins,
   and admins cannot turn it on for members. That is fine, because members
   do not need it to use an app the admin publishes. *(Verify in the admin
   console.)*

## Part 2 — Create the app (admin)

1. In ChatGPT (web), open Settings → **Apps** (or Connectors) → **Create**.
2. Fill in:
   - **Name:** `cp (read-only)`
   - **MCP server URL:** `https://cp.mc-2.1p.is/mcp/read`
     **Always `/mcp/read`, never `/mcp`.** `/mcp` is the full Claude surface
     and includes writes. The endpoint you enter is the control.
   - **Authentication:** OAuth. Leave client ID and secret blank so ChatGPT
     uses dynamic registration.
3. Click Create / Connect. ChatGPT sends you to Mission Control
   (`mc2.1p.is`). Sign in with your **`@firstperson.is`** Google account and
   click **Approve** on the consent screen.
   - Do not use another Google account. For example, `drew@canonic-os.com`
     exists in Supabase but is **not** on the team roster, so every read
     would come back empty.
4. ChatGPT lists the tools. You should see **24**, all read-only, including
   `whoami`, `get_project_state`, `read_project_file`, `semantic_search`,
   `list_spine_elements` and `pull_spine_element`. No tool name should start
   with `create_`, `set_`, `add_`, `promote_`, `retire_`, `capture_` or
   `resolve_`.
5. If the app has an **Action control** setting (all, read-only, or custom),
   set it to **read-only**. This is extra protection; the endpoint is
   already read-only. *(Verify in the admin console.)*

## Part 3 — Make it available to the partners

**Preferred:** the admin **publishes** the app to the workspace. Members
then use it without turning on developer mode. **Each partner still has to
connect it with their own sign-in.** cp decides access per person under
Postgres row-level security, so a connection shared under the admin's
identity would let every partner read as the admin.

- *(Verify in the admin console:)* after publishing, each partner is asked
  to sign in when they first use the app. If ChatGPT does **not** ask, stop.
  The published app is probably reusing the admin's connection. Each partner
  should then create their own connection (Part 2), or you should use a
  per-user setting if the console has one.
- Business has no per-app access controls (role-based access is Enterprise
  and Edu only). A published app is available to everyone in the workspace.
  Only people on the cp roster can read anything through it: anyone else
  signs in and gets zero rows.

**Fallback:** each partner turns on developer mode and creates the app
themselves (Part 2). On Business only admins and owners can do this, so this
works only if every partner is a workspace admin. *(Verify in the admin
console.)*

## Part 4 — Check it works (each partner, 1 minute)

In a ChatGPT chat with the app on, ask: **"call whoami"**. Expect:

- `authenticated: true`, and `email` is **your** `@firstperson.is` address.
- `connection.endpoint` is `"/mcp/read"`.
- `connection.oauth_client_id` is a UUID. This is ChatGPT's registered
  client. Send it to Drew if you want the Part 6 hardening.

Then ask something real: "what's the Exec Summary for ibx-5153?". The
answer should come from `get_project_state`.

To see what an auditor sees (Supabase SQL editor, read-only):

```sql
select at, tool, row_count, client
from public.mcp_audit_log
where client like '%endpoint=/mcp/read%'
order by at desc limit 20;
```

`client` reads like
`hosted-cp/0.126.3;endpoint=/mcp/read;oauth_client=<uuid>;app=<name>/<ver>;ua=<user-agent>`.
To name the app behind a client id:

```sql
select id, client_name, registration_type, created_at
from auth.oauth_clients where id = '<uuid>';
```

## Part 5 — Turn it off / offboard

- **One person:** remove their `public.profiles` row. Their reads return zero
  rows at once, on every client, with no token revocation needed. To also
  end their live sessions, sign them out in Supabase Auth (Users → the
  user → sign out / revoke sessions).
- **ChatGPT as a whole:** delete the app in ChatGPT. Then soft-delete its
  row(s) in `auth.oauth_clients` (the ids come from the audit query above).
  Their refresh tokens stop working.
- **The endpoint itself:** only in a redeploy. `READ_ONLY_TOOLS` in
  `prototypes/hosted-mcp/server.py` defines it. An empty allowlist fails the
  server's import check, so pull the route out of `build_app` instead.

## Part 6 — Optional hardening: bind ChatGPT's tokens to `/mcp/read`

Supabase gives every token `aud="authenticated"` no matter which `resource`
ChatGPT asked for. So a token ChatGPT holds is **not refused at `/mcp`**
just because of how it was issued. ChatGPT only calls the URL it was given,
so this is defense in depth. To close it anyway:

1. Collect ChatGPT's OAuth client id(s) from `whoami` or the audit query.
   DCR can register one per connection, so there may be several.
2. On the Railway `hosted-mcp` service, set
   `READ_ONLY_OAUTH_CLIENT_IDS=<uuid>,<uuid>` and redeploy.
3. From then on, those clients get an error on `/mcp` ("registered
   read-only; connect it to /mcp/read"). Each refusal writes an audit row
   (`tool = refused_read_only_client`). `/mcp/read` still works.

## What is not supported

- **Deep research / company-knowledge search.** Those ChatGPT features have
  asked for tools named exactly `search` and `fetch`. Developer-mode apps
  do not, and this endpoint has neither. Expect it to work in normal chat,
  not as a deep-research source. *(Verify in the admin console.)*
- **Tool list refresh.** ChatGPT snapshots the tool list when the app is
  created. After a deploy that changes `/mcp/read`, refresh the app (Action
  control → Refresh, or re-create it). *(Verify in the admin console.)*
- **Prompt injection.** Tools return ingested third-party documents (client
  decks, transcripts) into a second vendor's model. The endpoint cannot act
  on that text, because it has no writes. The model's answers can still be
  steered by it, so treat a surprising answer as a reason to open the source.

## Open items

1. **Per-user sign-in on a published Business app** (Part 3). This is the
   one thing that has to be verified before partners rely on it.
2. **Roster is five, decision says four.** `is_team_member()` means "has a
   `public.profiles` row", and the roster has five: the four partners plus
   `kelly@firstperson.is` (role `partner`). Kelly could connect ChatGPT the
   same way she can connect Claude today. Enforcement stays "RLS/team
   membership as today", as decided, so decide whether that is intended.
   Nothing in code restricts this endpoint to four people.
3. **Codex** is untried. It is a spec-compliant MCP client, so the same URL
   and flow should work. Do not design for it (per #141).

---
*Code: `prototypes/hosted-mcp/server.py` (`READ_ONLY_TOOLS`,
`MAIN_ONLY_TOOLS`, `build_app`, `_audit_guaranteed`, `audit_client_label`).
Tests: `prototypes/hosted-mcp/test_readonly_endpoint.py`. Issue: cp-engine #141.*
