#!/usr/bin/env python
"""Hosted-cp OAuth spike (cp-engine #137) — PROTOTYPE, not production code.

What this proves
----------------
A streamable-HTTP MCP server can:

  1. accept a Supabase-issued user access token as an OAuth 2.1 bearer token,
     validating it against the project's LIVE JWKS (ES256, asymmetric — no
     shared secret on the server);
  2. advertise its authorization server via RFC 9728 protected-resource
     metadata, so an MCP client can discover where to get a token;
  3. serve read tools whose database access runs UNDER THE CALLER'S IDENTITY —
     a per-request PostgREST client built from the ANON key plus the caller's
     JWT, so Postgres RLS is the authorization boundary.

There is NO service-role key anywhere in this file, and no code path that could
introduce one. That is the whole point of the spike: today's `cp mcp` runs
stdio-local with a service key in the environment; hosted-cp cannot.

0.0.3 adds two work packages on top of that read surface:

  A. **Narrow, insert-only writes** (#139). `create_note`, `create_commitment`,
     and `create_spine_element` INSERT under the caller's identity, stamping
     `author_id = auth.uid()` where the INSERT policy demands it. There are
     deliberately NO authenticated UPDATE policies on `spine_substance`, so
     nothing here updates an existing spine row — `add_spine_version` is
     DEFERRED BY DESIGN (it requires superseding the prior live row, an UPDATE
     on an engine-owned status column that the policy set does not grant).

  B. **Read-only tenant-tree tools** (#138). `get_project_state` and
     `read_project_file` serve the cp working tree out of a shallow clone of
     TENANT_REPO, pulled on read with a debounce. The tree has NO per-user RLS:
     a valid team JWT reads the whole tenant tree. The auth gate is the same
     middleware that guards every other tool — these are inside the
     authenticated tool surface, not a separate route.

Still out of scope: deployment, session persistence, token caching, the
DCR/authorize dance (Supabase is the AS), and any UPDATE/DELETE path.

Run:
    SUPABASE_URL=... SUPABASE_ANON_KEY=... .venv/bin/python prototypes/hosted-mcp/server.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hosted-mcp")


# ──────────────────────────────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────────────────────────────

SERVER_VERSION = "hosted-cp-spike/0.0.6"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
PORT = int(os.environ.get("PORT", "8788"))
HOST = os.environ.get("HOST", "127.0.0.1")

if not SUPABASE_URL:
    raise SystemExit("SUPABASE_URL is required")
if not SUPABASE_ANON_KEY:
    raise SystemExit("SUPABASE_ANON_KEY is required")

# GoTrue is the authorization server. Its issuer is the /auth/v1 sub-path, and
# its AS metadata lives at the RFC 8414 path-suffixed location:
#   https://<ref>.supabase.co/.well-known/oauth-authorization-server/auth/v1
ISSUER = f"{SUPABASE_URL}/auth/v1"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"

# This resource server's own public identity (RFC 8707 / RFC 9728 `resource`).
# In a real deployment this is the public https URL of the /mcp endpoint.
RESOURCE_URL = os.environ.get("RESOURCE_URL", f"http://{HOST}:{PORT}/mcp")

# Supabase user tokens carry aud="authenticated".
EXPECTED_AUDIENCE = os.environ.get("EXPECTED_AUDIENCE", "authenticated")

# ── Tenant tree (#138) ──
# TENANT_REPO is a git remote (`git@github.com:FirstPersonSF/cp.git`) in the
# deployment, and may be a LOCAL PATH for development. GIT_SSH_KEY carries a
# read-only deploy key; it is required for an ssh remote and IRRELEVANT for a
# local-path remote, so its absence only degrades the ssh case.
TENANT_REPO = os.environ.get("TENANT_REPO", "").strip()
GIT_SSH_KEY = os.environ.get("GIT_SSH_KEY", "")
# Skip `git pull` if the last one was this recent. A read-heavy tool surface
# must not fire a network round-trip per call.
TREE_PULL_DEBOUNCE_SECONDS = int(os.environ.get("TREE_PULL_DEBOUNCE_SECONDS", "60"))
# read_project_file cap. Beyond this the file is truncated with a notice rather
# than silently clipped or streamed whole.
TREE_MAX_FILE_BYTES = int(os.environ.get("TREE_MAX_FILE_BYTES", str(200 * 1024)))

# ── mc-2 backend (#143 batch 5) ──
# `promote_spine_transcript` DELEGATES rather than ports. The engine's version
# runs the full local ingest pipeline (tenant file + service key + Voyage) —
# none of which a hosted, service-key-free server can or should do. mc-2's
# backend already exposes the same promotion service-side at
# `POST {MC2_API_BASE}/api/meetings/{recording_id}/promote-transcript`, and it
# authenticates with the SAME Supabase JWTs this server verifies (its
# `src/auth.py` validates ES256 against the same JWKS with aud=authenticated —
# verified live 2026-08-02). So the hosted verb forwards the CALLER'S OWN token
# and the promotion runs as that user, end to end. No new trust is minted here.
#
# Absent config is a CLEAN DEGRADE, never a crash: local runs without the env
# var get a structured "promotion unavailable" note rather than a stack trace.
MC2_API_BASE = os.environ.get(
    "MC2_API_BASE", "https://api-production-a247.up.railway.app"
).rstrip("/")
# The promote hop is webhook-proxied inside mc-2 (backend -> cp-engine-webhook
# -> ingest), so it is slower than a plain DB write. mc-2's own timeout maps to
# a 504; ours must be no tighter than that or we would report a timeout for a
# promotion that is still succeeding upstream.
MC2_TIMEOUT_SECONDS = float(os.environ.get("MC2_TIMEOUT_SECONDS", "120"))

# ──────────────────────────────────────────────────────────────────────
#  Token verification — the SDK's TokenVerifier protocol
# ──────────────────────────────────────────────────────────────────────
#
# `mcp.server.auth.provider.TokenVerifier` is a bare Protocol: one async
# `verify_token(token) -> AccessToken | None`. Handing an instance to
# `MCPServer(token_verifier=..., auth=AuthSettings(...))` makes the SDK
# install, in order:
#
#   AuthenticationMiddleware(backend=BearerAuthBackend(verifier))
#       -> parses the Authorization header, calls verify_token, and on success
#          puts an AuthenticatedUser in the ASGI scope
#   AuthContextMiddleware
#       -> copies that user into a contextvar, readable from inside a tool via
#          mcp.server.auth.middleware.auth_context.get_access_token()
#   RequireAuthMiddleware(app, required_scopes, resource_metadata_url)
#       -> wraps the /mcp mount; 401s anything unauthenticated with a
#          WWW-Authenticate header carrying resource_metadata="<RFC 9728 url>"
#
# and registers the RFC 9728 route via create_protected_resource_routes().


class SupabaseJWTVerifier(TokenVerifier):
    """Verify a Supabase user access token against the project's live JWKS.

    ES256 (asymmetric): the server holds no signing secret, only the public
    JWKS it fetches from GoTrue. PyJWKClient caches the key set in-process
    (`lifespan` seconds) and refetches on an unknown `kid`, so key rotation
    heals itself without a restart.
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: str,
        audience: str | None = None,
    ):
        self._issuer = issuer
        self._audience = audience
        # cache_jwk_set=True + lifespan: one network fetch per 5 min, not per
        # request. A `kid` miss forces a refetch (PyJWKClient handles this).
        self._jwks = PyJWKClient(jwks_uri, cache_jwk_set=True, lifespan=300, timeout=10)

    def _claims_options(self, verify_aud: bool) -> dict[str, Any]:
        # exp/iat/nbf are verified by default; spelled out so the spike's
        # security posture is legible rather than implied.
        return {
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "verify_aud": verify_aud,
            "verify_iss": True,
            "require": ["exp", "sub", "iss"],
        }

    def _decode_es256(self, token: str) -> dict[str, Any]:
        """PRIMARY path: asymmetric verification against the live JWKS."""
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            issuer=self._issuer,
            audience=self._audience,
            options=self._claims_options(self._audience is not None),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # Dispatch on the token's own `alg` header, but only ever to a branch
        # that PINS its algorithm list. An unexpected alg falls through to
        # rejection rather than being tried against every key we hold.
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except Exception as exc:  # noqa: BLE001 — malformed token
            log.info("token rejected: unparseable header: %s", exc)
            return None

        try:
            if alg == "ES256":
                claims: dict[str, Any] = self._decode_es256(token)
            else:
                # `none`/HS256/RS256/anything else. The server holds only the
                # public JWKS — it can verify tokens, never mint them.
                log.info("token rejected: unsupported alg %r", alg)
                return None
        except Exception as exc:  # noqa: BLE001 — any failure is "not a valid token"
            # Returning None (not raising) is the protocol: BearerAuthBackend
            # turns it into an unauthenticated scope, which RequireAuthMiddleware
            # renders as a 401 + WWW-Authenticate.
            log.info("token rejected: %s: %s", type(exc).__name__, exc)
            return None

        subject = claims.get("sub")
        if not subject:
            log.info("token rejected: no sub claim")
            return None

        expires_at = claims.get("exp")
        if expires_at is not None and int(expires_at) < int(time.time()):
            # Redundant with verify_exp, but the SDK's BearerAuthBackend also
            # re-checks AccessToken.expires_at — keep the field truthful.
            log.info("token rejected: expired")
            return None

        # AccessToken.token holds the RAW compact JWT. That is what the tools
        # forward to PostgREST, and it is the ONLY credential they use.
        return AccessToken(
            token=token,
            # Supabase user tokens have no OAuth client_id; the subject is the
            # stable principal. Using it here keeps principal_components()
            # meaningful for session binding.
            client_id=str(subject),
            scopes=[],
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=RESOURCE_URL,
            subject=str(subject),
            claims=claims,
        )


# ──────────────────────────────────────────────────────────────────────
#  Per-request Supabase client — the RLS boundary
# ──────────────────────────────────────────────────────────────────────


def caller_jwt() -> str:
    """The raw JWT of the current caller, or raise.

    `get_access_token()` reads the contextvar AuthContextMiddleware set for
    THIS request. There is no ambient/global identity: a tool that cannot see
    a caller must fail, never fall back.
    """
    access = get_access_token()
    if access is None or not access.token:
        raise RuntimeError("no authenticated caller in context")
    return access.token


def caller_subject() -> str | None:
    access = get_access_token()
    return access.subject if access else None


def caller_email() -> str | None:
    """The caller's verified email claim, or None.

    Read off the SAME verified claims the token verifier produced — never off a
    request header or a caller-supplied argument. `spine_relations`' INSERT
    policy is `is_team_member() AND created_by = auth.jwt()->>'email'`, so this
    is not decoration: a row whose `created_by` disagrees with the JWT is
    rejected by Postgres. Confirmed live: Supabase user tokens carry `email` at
    the top level of the claim set (alongside `sub`, `role`, `aud`).
    """
    access = get_access_token()
    if access is None:
        return None
    claims = access.claims or {}
    email = claims.get("email")
    return str(email) if email else None


def user_client():
    """Build a FRESH PostgREST client bound to the caller's identity.

    Three rules, all load-bearing:

      * ANON key as the apikey — never the service key. The anon key alone
        grants the `anon` role, which the RLS policies here deny.
      * The caller's JWT as the Authorization bearer — PostgREST decodes it,
        assumes the `authenticated` role, and `auth.uid()` resolves to that
        user. RLS is then the whole authorization story.
      * Constructed PER REQUEST and never cached. A cached client with mutable
        shared headers is exactly how one user's token leaks into another
        user's query under concurrency; `cp_engine.mc2_db.get_client()` caches
        by (url, key) and is deliberately NOT imported here.
    """
    jwt_token = caller_jwt()
    options = SyncClientOptions(
        headers={"Authorization": f"Bearer {jwt_token}"},
        auto_refresh_token=False,
        persist_session=False,
    )
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY, options)
    # supabase-py also stamps the apikey-derived Authorization on its
    # sub-clients; overwrite postgrest's explicitly so the user JWT wins
    # regardless of construction order in the installed version.
    client.postgrest.auth(jwt_token)
    return client


def resolve_project_id(client, project_code: str) -> str | None:
    """`<code>` -> a uuid usable as `spine_substance.project_id`.

    A trimmed stand-in for `cp_engine.mc2_db._resolve_project_id`, in the order
    that actually resolves against live data:

      1. `initiatives.code` — initiative ids land in `spine_substance.project_id`
         exactly like a project's (`mission-control`, `storyos`).
      2. `spine_substance.project_code` — the DIR-SLUG the cp tree uses
         (`ibx-5153-ai-campaign`). This is the branch that matters: the engine's
         resolver reaches the same id via slugified `full_job_name`, but the
         spine table already stores the slug next to the id, so one indexed
         lookup replaces a table scan.
      3. `projects.code` — the raw MC-2 code, which is a DIFFERENT string
         (`IBX-ai-campaign`, uppercase, no number). Accepted for callers who
         have it, but it is NOT the cp-tree code.

    The `ibx-5153` short form resolves via prefix-matching branch 2, covering
    the legacy `<prefix>-<number>` shape without the companies/number join.

    Note the drift this encodes: THREE distinct strings name one project
    (`ibx-5153`, `ibx-5153-ai-campaign`, `IBX-ai-campaign`). A hosted server
    needs one resolver all clients share, or every tool re-invents this.
    Explicit columns only.
    """
    rows = (
        client.table("initiatives").select("id").eq("code", project_code).limit(1).execute().data
        or []
    )
    if rows:
        return rows[0]["id"]

    # Exact dir-slug, then the `<prefix>-<number>` short form as a prefix match.
    for query in (
        lambda: client.table("spine_substance")
        .select("project_id")
        .eq("project_code", project_code)
        .limit(1),
        lambda: client.table("spine_substance")
        .select("project_id")
        .like("project_code", f"{project_code}-%")
        .limit(1),
    ):
        rows = query().execute().data or []
        if rows and rows[0].get("project_id"):
            return rows[0]["project_id"]

    rows = (
        client.table("projects").select("id").eq("code", project_code).limit(1).execute().data
        or []
    )
    return rows[0]["id"] if rows else None


# ──────────────────────────────────────────────────────────────────────
#  Server + tools
# ──────────────────────────────────────────────────────────────────────

mcp_server = MCPServer(
    "hosted-cp-spike",
    title="hosted-cp OAuth spike",
    instructions=(
        "Read-only prototype of a hosted cp-sources MCP server. Every tool runs "
        "under the calling user's Supabase identity with RLS enforced."
    ),
    version=SERVER_VERSION,
    token_verifier=SupabaseJWTVerifier(JWKS_URI, ISSUER, EXPECTED_AUDIENCE),
    auth=AuthSettings(
        # The AS that issues tokens for this resource. The SDK publishes this
        # in the RFC 9728 protected-resource document as authorization_servers[0];
        # a client appends /.well-known/oauth-authorization-server<path> to reach
        # Supabase's AS metadata.
        issuer_url=AnyHttpUrl(ISSUER),
        # Presence of resource_server_url is what turns on the RFC 9728 route
        # AND the resource_metadata="..." hint in 401 WWW-Authenticate headers.
        resource_server_url=AnyHttpUrl(RESOURCE_URL),
        required_scopes=None,  # Supabase user tokens carry no scopes
    ),
)

# `spine_substance` has NO `updated_at` column (verified against the live
# schema). The freshness signals it does carry are `synced_at` (last mirror
# write), `version_date`, and `confirmed_at`; `synced_at` is the closest
# analogue and is what these tools return.
SPINE_LIST_COLUMNS = (
    "est_item_id, framing, layer, binding, status, important, archived, "
    "scope, project_id, version_label, version_date, synced_at, actor"
)

# `spine_substance.actor` (mig 126) — who is speaking, for the v04
# authority-precedence ordering (#146). Tag deliberately; default 'inferred'.
_ACTORS = frozenset({"partner", "client", "vendor", "inferred"})
SPINE_PULL_COLUMNS = SPINE_LIST_COLUMNS + ", body, sources, note, project_code, rel_path"
COMMITMENT_COLUMNS = (
    "id, description, owner_email, owner_name, direction, due_date, "
    "date_status, status, source_kind, source_meeting_id, created_at, updated_at"
)

# `rag_assets` — manifest list shape, mirroring `mc2_db.RAG_ASSET_LIST_COLUMNS`.
# `meta` is JSONB and can be megabytes per row; it is NEVER selected. The table
# has NO extracted-text column at all (verified against the live schema) —
# document text lives only in `asset_chunks.text`.
RAG_ASSET_LIST_COLUMNS = (
    "id, title, source_type, status, created_at, file_hash, prev_asset_id"
)
RAG_ASSET_PULL_COLUMNS = RAG_ASSET_LIST_COLUMNS + ", url, source_path, scope"

# `fathom_meetings` — mirrors `mc2_db.FATHOM_LIST_COLUMNS`. `transcript` and
# `summary` are the big text columns and are deliberately excluded from the
# list shape.
FATHOM_LIST_COLUMNS = (
    "id, title, meeting_date, project_tags, duration_minutes, meeting_type"
)

# `asset_chunks` — `text` is the extracted content. There is NO `chunk_index`
# column (verified live); the keys are `id`, `asset_id`, `start_seconds`,
# `end_seconds`, `text`, `meta`, `content_hash`. `embedding` lives in a separate
# table and is never selected here.
ASSET_CHUNK_COLUMNS = "id, asset_id, start_seconds, end_seconds, text"

TEAM_EMPTY_HINT = (
    "0 rows. Read policies on this table are TEAM-KEYED via `public.is_team_member()` "
    "(the caller must have a `public.profiles` row). If EVERY table read comes back "
    "empty while `whoami` succeeds, the caller is an authenticated Supabase user who "
    "is not a team member — `auth.users` membership is not team membership."
)


# ──────────────────────────────────────────────────────────────────────
#  Audit logging — fire-and-forget, under the caller's own JWT
# ──────────────────────────────────────────────────────────────────────
#
# `public.mcp_audit_log` has INSERT policy `with check (user_id = auth.uid()
# and is_team_member())`, so the row can only be written by the caller, about
# the caller. That is the point: the audit trail is not a privileged side
# channel, it is the user's own attributable action, and a non-team caller
# simply cannot write one.
#
# Two hard rules:
#   * NEVER log body content. Args are sanitized down to identifiers and codes;
#     free-text (a semantic-search query, a document body) is recorded only as a
#     length, never verbatim.
#   * A logging failure must NEVER fail the tool call. Every path is wrapped and
#     downgraded to a warning.

# Arg keys safe to record verbatim: identifiers, codes, and small scalars.
_AUDIT_SAFE_ARGS = {
    "project_code",
    "element_id",
    "asset_id",
    "limit",
    "max_chars",
    # ── writes (#139): identifiers and controlled vocabulary only ──
    "slug",          # the element slug WE derive — an identifier, not prose
    "layer",         # canonical layer vocabulary
    "direction",     # us_to_them | them_to_us | internal
    "due_date",      # an ISO date, already validated
    "owner_email",   # an addressee, like recipient — an identifier
    "recipient",
    # ── tree (#138): paths and codes only ──
    "path",
    "rel_path",
    "week",
    # ── relations + steps (#143 batch 1): identifiers and closed vocabularies ──
    # NOTE the asymmetry with `title`, which stays REDACTED below: these are all
    # identifier-like (an element key, a relation kind, a step status/date), and
    # a step's `title` is user prose like any other body field.
    "kind",          # closed relation vocabulary
    "from_key",      # an element key the caller named — an identifier
    "to_key",
    "key",           # the element key steps resolve against
    "step_date",     # free-form but tiny ('7/16') — a date, not prose
    "status",        # done | active | upcoming
    # ── #143 batch 2 (UPDATE verbs): identifiers and closed vocabularies ──
    "step_id",       # a step uuid — an identifier
    "outcome",       # done | dropped
    # `order` is a LIST OF UUIDS, never logged as itself: a reorder is recorded
    # as how many steps moved, not which. The verbs pass `order_len`, and the
    # bare `order` key is absent from both lists so it is DROPPED if ever passed.
    "order_len",
    # ── #143 batch 3 (sources/provenance): resolution keys ──
    # Both name a REFERENT, not content, which is the line this allow-list has
    # drawn since batch 1 (`key`/`from_key`/`to_key` are logged; `title`/`note`/
    # `framing` are redacted to a length because they are the user's own prose).
    #
    # `source_title` is the one that deserves the argument spelled out, since it
    # IS a title and `title` right below is redacted. It is not the caller's
    # prose: it is a lookup key naming an already-ingested rag_asset — a
    # document filename, authored elsewhere, that the tool resolves to an
    # `asset_id` logged beside it. Redacting it to a length would make the audit
    # row strictly less useful (you would know a source was attached but not
    # which) while protecting nothing the `asset_id` doesn't already reveal.
    # `title`, by contrast, is text the caller is WRITING, and stays redacted.
    "source_title",
    "source_key",   # an element key — the same class as `key`
    # ── #143 batch 4 (retire + account scope) ──
    # `key`/`kind`/`from_key`/`to_key` are already allow-listed above and carry
    # these verbs too. The one addition is the BATCH verb's projection: `keys` is
    # a LIST of element keys and is never logged as itself, on the same rule
    # `order`/`order_len` set in batch 2 — a batch retire is auditable as HOW
    # MANY elements were named, not which. The bare `keys` is on neither list,
    # so it is DROPPED if ever passed; the verb passes `keys_count`.
    "keys_count",
    # `account` is a BOOLEAN direction flag (promote vs demote) — a closed
    # two-value vocabulary, the same class as `outcome`, and the single most
    # useful thing to know about a scope write after the element it named.
    "account",
    # ── #143 batch 5 (transcript promotion) ──
    # `recording_id` is the Fathom bigint the promotion is keyed on — an
    # identifier in the purest sense, and the ONE fact that makes a promote
    # audit row useful (which meeting's transcript entered the RAG store).
    # NOTE the id shape trap this records: `fathom_meetings` carries BOTH a
    # uuid `id` and a bigint `recording_id`, and only the latter addresses the
    # mc-2 endpoint. Logging it verbatim is what lets an auditor tell which of
    # the two a caller actually reached.
    "recording_id",
    # How the recording_id was ARRIVED AT (element | meeting_id | recording_id)
    # — a closed three-value vocabulary, not prose. Worth recording because the
    # resolution path is the part of this verb most likely to be wrong.
    "resolved_via",
}
# Arg keys that are free text — recorded as a length only, never their content.
# `body`/`description`/`framing`/`title` are USER PROSE: the whole point of the
# audit table is to record that a write happened and by whom, never what it said.
_AUDIT_REDACTED_ARGS = {"query", "body", "description", "framing", "title"}


def sanitize_audit_args(args: dict[str, Any]) -> dict[str, Any]:
    """Reduce tool args to identifiers/codes; never body content.

    Anything not explicitly allow-listed is dropped rather than logged, so a
    future tool that takes a new free-text param cannot silently start writing
    user content into the audit table.
    """
    out: dict[str, Any] = {}
    for key, value in args.items():
        if value is None:
            continue
        if key in _AUDIT_REDACTED_ARGS:
            out[f"{key}_len"] = len(str(value))
        elif key in _AUDIT_SAFE_ARGS:
            if key in ("path", "rel_path") and isinstance(value, str):
                # Path args are recorded, but neutralized first: a traversal
                # ATTEMPT (../..., absolute /etc/...) recorded verbatim reads
                # as an attack payload to Supabase's Cloudflare WAF, which
                # then 403s the whole audit INSERT (seen live 2026-08-02).
                # The tool already rejected the read; the audit row only needs
                # to say a bad path was tried, not replay it.
                cleaned = value.replace("..", "~UP~").lstrip("/")[:200]
                out[key] = cleaned
                if cleaned != value[:200]:
                    out[f"{key}_neutralized"] = True
            else:
                out[key] = value
    return out


def audit(client, tool: str, args: dict[str, Any], row_count: int) -> None:
    """Fire-and-forget INSERT into `mcp_audit_log`. Never raises."""
    try:
        subject = caller_subject()
        if not subject:
            return
        client.table("mcp_audit_log").insert(
            {
                "user_id": subject,
                "tool": tool,
                "args": sanitize_audit_args(args),
                "row_count": int(row_count),
                "client": SERVER_VERSION,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — auditing must never break a read
        log.warning("audit log write failed for tool %s: %s: %s", tool, type(exc).__name__, exc)


# ──────────────────────────────────────────────────────────────────────
#  Query embedding — must match what INGEST used, not what's convenient
# ──────────────────────────────────────────────────────────────────────
#
# The brief said "embed with OpenAI". The live corpus says otherwise, and the
# corpus wins: `cp_engine.asset_ingest` embeds with **Voyage `voyage-3-large`**
# (`asset_ingest.py:1070`, `asset_ingest_settings.INGEST_EMBEDDING_MODEL`), and
# `match_chunks_simple` accepts a **1024-dim** vector — confirmed live by a
# successful 1024-float probe call.
#
# An OpenAI embedding would be both the wrong DIMENSION (1536/3072 vs 1024 —
# a hard Postgres error) and, more fundamentally, from a different vector space:
# cosine distance between a Voyage-embedded corpus and an OpenAI-embedded query
# is noise even where the arithmetic happens to line up. Query embeddings MUST
# come from the same model as the stored ones.
#
# So: VOYAGE_API_KEY is the key that makes search work. OPENAI_API_KEY is still
# read and reported, because the brief named it and because a mismatch should be
# stated out loud rather than silently producing garbage rankings.

EMBED_MODEL = os.environ.get("INGEST_EMBEDDING_MODEL", "voyage-3-large")
EMBED_DIM = 1024

_embedder_cache: list[Any] = []


def embedding_available() -> tuple[bool, str]:
    """(usable, reason) for the query-embedding path."""
    if not os.environ.get("VOYAGE_API_KEY"):
        if os.environ.get("OPENAI_API_KEY"):
            return False, (
                "search unavailable: no embedding key configured for the corpus model. "
                f"OPENAI_API_KEY is set, but the corpus was embedded with {EMBED_MODEL} "
                f"({EMBED_DIM}-dim, Voyage) — an OpenAI query vector is the wrong "
                "dimension AND the wrong vector space. Set VOYAGE_API_KEY."
            )
        return False, (
            "search unavailable: no embedding key configured "
            f"(VOYAGE_API_KEY, for the corpus model {EMBED_MODEL})"
        )
    return True, ""


def embed_query(text: str) -> list[float]:
    """Embed a query with the SAME model the corpus was ingested with.

    Uses the `voyageai` client directly rather than importing cp_engine's
    ingest wiring — this prototype stays off the `cp` import path by design.
    """
    if not _embedder_cache:
        import voyageai

        _embedder_cache.append(voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"]))
    client = _embedder_cache[0]
    result = client.embed([text], model=EMBED_MODEL, input_type="query")
    return result.embeddings[0]


@mcp_server.tool()
def list_spine_elements(
    project_code: str, include_absorbed: bool = False
) -> dict[str, Any]:
    """List live spine elements for a project, under the caller's identity.

    Lifecycle-aware (spec v04): an element with an active `absorbed_by` edge
    was sealed into a shipped deliverable and is HISTORICAL — excluded by
    default, with the count reported as `absorbed_hidden`. Pass
    `include_absorbed=true` (retrospective mode) to include them, each
    annotated with the deliverable that absorbed it. Canon members (active
    `canon_of` edge to the standing brief) carry `canon: true`.

    Args:
        project_code: engagement, initiative, or standalone-repo code
                      (e.g. "ibx-5153", "mission-control").
        include_absorbed: retrospective mode — include sealed elements.
    """
    client = user_client()
    project_id = resolve_project_id(client, project_code)
    if project_id is None:
        return {
            "project_code": project_code,
            "caller": caller_subject(),
            "error": f"no project or initiative resolves for code {project_code!r}",
            "elements": [],
        }

    rows = (
        client.table("spine_substance")
        .select(SPINE_LIST_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "live")
        .execute()
        .data
        or []
    )

    # One edge read serves both annotations: canon membership and absorption.
    absorbed_into: dict[str, str] = {}
    canon_ids: set[str] = set()
    try:
        for e in (
            client.table("spine_relations")
            .select("kind, from_item_id, to_item_id")
            .eq("project_id", project_id)
            .eq("status", "active")
            .in_("kind", ["canon_of", "absorbed_by"])
            .execute()
            .data
            or []
        ):
            if e["kind"] == "canon_of":
                canon_ids.add(e["from_item_id"])
            else:
                absorbed_into[e["from_item_id"]] = e["to_item_id"]
    except Exception:  # noqa: BLE001 — annotations degrade, the list survives
        pass

    elements = []
    absorbed_hidden = 0
    for r in rows:
        if r.get("archived"):
            continue
        eid = r.get("est_item_id")
        if eid in absorbed_into and not include_absorbed:
            absorbed_hidden += 1
            continue
        elements.append(
            {
                "slug": eid,
                "framing": r.get("framing"),
                "status": r.get("status"),
                "layer": r.get("layer"),
                "binding": r.get("binding"),
                "important": bool(r.get("important")),
                "version_label": r.get("version_label"),
                "version_date": r.get("version_date"),
                # `synced_at` stands in for the requested `updated_at`, which
                # this table does not have.
                "synced_at": r.get("synced_at"),
                "actor": r.get("actor"),
                **({"canon": True} if eid in canon_ids else {}),
                **(
                    {"absorbed_by": absorbed_into[eid]}
                    if eid in absorbed_into
                    else {}
                ),
            }
        )
    audit(
        client,
        "list_spine_elements",
        {"project_code": project_code, "include_absorbed": include_absorbed},
        len(elements),
    )
    return {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(elements),
        "elements": elements,
        **({"canon_size": len(canon_ids)} if canon_ids else {}),
        **(
            {
                "absorbed_hidden": absorbed_hidden,
                "note_on_absorbed": "sealed into a deliverable; pass "
                "include_absorbed=true for retrospective mode",
            }
            if absorbed_hidden
            else {}
        ),
        **({"note": TEAM_EMPTY_HINT} if not elements else {}),
    }


@mcp_server.tool()
def pull_spine_element(element_id: str, project_code: str | None = None) -> dict[str, Any]:
    """Pull one spine element's body + metadata, under the caller's identity.

    Args:
        element_id: `spine_substance.est_item_id` (e.g. "_authored/janet-dossier")
                    or the row's own `id`.
        project_code: optional scope, disambiguating an est_item_id that several
                      projects share (authored slugs are unique only per project).
    """
    client = user_client()
    q = client.table("spine_substance").select(SPINE_PULL_COLUMNS).eq("est_item_id", element_id)
    if project_code:
        project_id = resolve_project_id(client, project_code)
        if project_id is None:
            return {"element_id": element_id, "error": f"unknown code {project_code!r}"}
        q = q.eq("project_id", project_id)
    rows = q.execute().data or []

    if not rows:
        # Fall back to the surrogate primary key.
        rows = (
            client.table("spine_substance")
            .select(SPINE_PULL_COLUMNS)
            .eq("id", element_id)
            .execute()
            .data
            or []
        )
    rows = [r for r in rows if not r.get("archived")]
    if not rows:
        return {
            "element_id": element_id,
            "caller": caller_subject(),
            "error": "no element found (it may not exist, or RLS may hide it)",
        }

    live = [r for r in rows if r.get("status") == "live"] or rows
    # Newest version wins when several rows survive.
    row = sorted(live, key=lambda r: str(r.get("version_date") or ""))[-1]
    audit(
        client,
        "pull_spine_element",
        {"element_id": element_id, "project_code": project_code},
        1,
    )
    return {
        "element_id": element_id,
        "caller": caller_subject(),
        "slug": row.get("est_item_id"),
        "project_code": row.get("project_code"),
        "framing": row.get("framing"),
        "status": row.get("status"),
        "layer": row.get("layer"),
        "binding": row.get("binding"),
        "version_label": row.get("version_label"),
        "version_date": row.get("version_date"),
        "synced_at": row.get("synced_at"),
        "note": row.get("note"),
        "actor": row.get("actor"),
        "sources": row.get("sources"),
        "body": row.get("body"),
        "versions_visible": len(rows),
    }


@mcp_server.tool()
def list_commitments(project_code: str) -> dict[str, Any]:
    """List commitments for a project, under the caller's identity.

    As of the 2026-08-01 policy pass, `commitments` is no longer deny-all: SELECT
    is gated on `public.is_team_member()`, so a TEAM caller gets real rows. An
    empty result is now a plain "0 rows", not a sentinel — with one hint kept,
    because the remaining ambiguity is membership, not policy absence.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
    """
    client = user_client()
    project_id = resolve_project_id(client, project_code)
    if project_id is None:
        return {
            "project_code": project_code,
            "caller": caller_subject(),
            "error": f"no project or initiative resolves for code {project_code!r}",
            "commitments": [],
        }

    # A code resolves to ONE uuid, but that uuid lands in `project_id` for an
    # engagement and `initiative_id` for an initiative. Query both and dedupe.
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for column in ("project_id", "initiative_id"):
        try:
            for row in (
                client.table("commitments")
                .select(COMMITMENT_COLUMNS)
                .eq(column, project_id)
                .execute()
                .data
                or []
            ):
                if row.get("id") not in seen:
                    seen.add(row.get("id"))
                    rows.append(row)
        except Exception as exc:  # noqa: BLE001 — a policy denial can surface as an error
            errors.append(f"{column}: {type(exc).__name__}: {exc}")

    audit(client, "list_commitments", {"project_code": project_code}, len(rows))
    result: dict[str, Any] = {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(rows),
        "commitments": rows,
    }
    if not rows:
        result["note"] = TEAM_EMPTY_HINT
    if errors:
        result["errors"] = errors
    return result


@mcp_server.tool()
def list_project_sources(project_code: str) -> dict[str, Any]:
    """List a project's ingested RAG source documents, under the caller's identity.

    Mirrors `cp_engine.mc2_db.RAG_ASSET_LIST_COLUMNS`. `meta` (JSONB, up to
    megabytes per row) is never selected — this is the table the "never SELECT *"
    rule was written about.

    Superseded assets are dropped: an asset is superseded when a NEWER asset's
    `prev_asset_id` points at it (the same rule as
    `cp_engine.project_sources.drop_superseded_assets`).

    Args:
        project_code: engagement, initiative, or standalone-repo code.
    """
    client = user_client()
    project_id = resolve_project_id(client, project_code)
    if project_id is None:
        return {
            "project_code": project_code,
            "caller": caller_subject(),
            "error": f"no project or initiative resolves for code {project_code!r}",
            "sources": [],
        }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for column in ("project_id", "initiative_id"):
        try:
            for row in (
                client.table("rag_assets")
                .select(RAG_ASSET_LIST_COLUMNS)
                .eq(column, project_id)
                .execute()
                .data
                or []
            ):
                if row.get("id") not in seen:
                    seen.add(row.get("id"))
                    rows.append(row)
        except Exception:  # noqa: BLE001 — `initiative_id` may not apply
            continue

    superseded = {r["prev_asset_id"] for r in rows if r.get("prev_asset_id")}
    sources = [
        {
            "asset_id": r.get("id"),
            "title": r.get("title"),
            "source_type": r.get("source_type"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
        }
        for r in rows
        if r.get("id") not in superseded and r.get("status") != "archived"
    ]
    sources.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

    audit(client, "list_project_sources", {"project_code": project_code}, len(sources))
    return {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(sources),
        "superseded_hidden": len(superseded),
        "sources": sources,
        **({"note": TEAM_EMPTY_HINT} if not sources else {}),
    }


@mcp_server.tool()
def pull_project_source(asset_id: str, max_chars: int = 40000) -> dict[str, Any]:
    """Pull one source document's extracted text, under the caller's identity.

    `rag_assets` carries NO extracted-text column — verified against the live
    schema, whose columns are id/scope/company_id/project_id/archived_at/
    promoted_at/source_type/title/url/file_path/file_hash/meta/prev_asset_id/
    status/created_at/updated_at/source_provider/source_file_id/source_path/
    initiative_id/author_id. The text lives ONLY in `asset_chunks.text`, so this
    tool concatenates the asset's chunks.

    Chunk ORDER is a real caveat: `asset_chunks` has no `chunk_index` column.
    Its ordering signals are `start_seconds` (populated for time-based media,
    NULL for documents) and the row `id`. Chunks are ordered by `start_seconds`
    where present, and otherwise returned in the table's natural insertion
    order, which is the order the ingest pipeline wrote them. That is right in
    practice for documents but is NOT a guarantee the schema makes.

    Args:
        asset_id: `rag_assets.id` (from `list_project_sources`).
        max_chars: truncate the assembled text at this many characters.
    """
    client = user_client()
    asset_rows = (
        client.table("rag_assets")
        .select(RAG_ASSET_PULL_COLUMNS)
        .eq("id", asset_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not asset_rows:
        audit(client, "pull_project_source", {"asset_id": asset_id}, 0)
        return {
            "asset_id": asset_id,
            "caller": caller_subject(),
            "error": "no asset found (it may not exist, or RLS may hide it)",
            "note": TEAM_EMPTY_HINT,
        }
    asset = asset_rows[0]

    chunks = (
        client.table("asset_chunks")
        .select(ASSET_CHUNK_COLUMNS)
        .eq("asset_id", asset_id)
        .execute()
        .data
        or []
    )
    if any(c.get("start_seconds") is not None for c in chunks):
        chunks.sort(key=lambda c: (c.get("start_seconds") is None, c.get("start_seconds") or 0))

    text = "\n\n".join((c.get("text") or "") for c in chunks)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    audit(client, "pull_project_source", {"asset_id": asset_id, "max_chars": max_chars}, len(chunks))
    return {
        "asset_id": asset_id,
        "caller": caller_subject(),
        "title": asset.get("title"),
        "source_type": asset.get("source_type"),
        "status": asset.get("status"),
        "url": asset.get("url"),
        "source_path": asset.get("source_path"),
        "created_at": asset.get("created_at"),
        "chunk_count": len(chunks),
        "truncated": truncated,
        "text": text,
    }


@mcp_server.tool()
def list_project_meetings(project_code: str) -> dict[str, Any]:
    """List a project's Fathom meetings, under the caller's identity.

    Mirrors `cp_engine.mc2_db.FATHOM_LIST_COLUMNS`. `transcript` and `summary`
    are the large text columns on this table and are deliberately excluded —
    a meeting list must not drag every transcript across the wire.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
    """
    client = user_client()
    project_id = resolve_project_id(client, project_code)
    if project_id is None:
        return {
            "project_code": project_code,
            "caller": caller_subject(),
            "error": f"no project or initiative resolves for code {project_code!r}",
            "meetings": [],
        }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for column in ("project_id", "initiative_id"):
        try:
            for row in (
                client.table("fathom_meetings")
                .select(FATHOM_LIST_COLUMNS)
                .eq(column, project_id)
                .order("meeting_date", desc=True)
                .execute()
                .data
                or []
            ):
                if row.get("id") not in seen:
                    seen.add(row.get("id"))
                    rows.append(row)
        except Exception:  # noqa: BLE001 — `initiative_id` may not apply
            continue

    meetings = [
        {
            "meeting_id": r.get("id"),
            "title": r.get("title"),
            "meeting_date": r.get("meeting_date"),
            "meeting_type": r.get("meeting_type"),
            "duration_minutes": r.get("duration_minutes"),
            "project_tags": r.get("project_tags"),
        }
        for r in rows
    ]
    meetings.sort(key=lambda m: str(m.get("meeting_date") or ""), reverse=True)

    audit(client, "list_project_meetings", {"project_code": project_code}, len(meetings))
    return {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(meetings),
        "meetings": meetings,
        **({"note": TEAM_EMPTY_HINT} if not meetings else {}),
    }


@mcp_server.tool()
def semantic_search(
    query: str, project_code: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """Vector-search ingested chunk text, under the caller's identity.

    The query is embedded with the SAME model the corpus was ingested with
    (`voyage-3-large`, 1024-dim) and passed to the `match_chunks_simple` RPC.
    That RPC is SECURITY INVOKER, so PostgREST executes it as the caller and RLS
    applies inside it — the same authorization boundary as a plain table read,
    which is what makes vector search safe to expose here at all.

    Two constraints worth stating:

      * `match_chunks_by_documents` — the RPC that would let this filter server-
        side by asset — is BROKEN on the live database: it references a relation
        `assets` that does not exist (Postgres 42P01). So `project_code` is
        applied as a POST-FILTER here: search runs corpus-wide, then results are
        intersected with that project's asset ids. The `limit` is therefore
        applied to the pre-filter candidate set, and a narrow project may return
        fewer than `limit` rows. Fixing the RPC would make this exact and cheap.
      * With no embedding key configured the tool still EXISTS and returns a
        clear unavailable message rather than crashing the server.

    Args:
        query: natural-language search text.
        project_code: optional project scope (post-filter, see above).
        limit: maximum chunks to return.
    """
    usable, reason = embedding_available()
    if not usable:
        return {
            "query_len": len(query),
            "caller": caller_subject(),
            "available": False,
            "error": reason,
            "results": [],
        }

    client = user_client()

    asset_ids: set[str] | None = None
    project_id: str | None = None
    if project_code:
        project_id = resolve_project_id(client, project_code)
        if project_id is None:
            return {
                "query_len": len(query),
                "caller": caller_subject(),
                "error": f"no project or initiative resolves for code {project_code!r}",
                "results": [],
            }
        asset_ids = set()
        for column in ("project_id", "initiative_id"):
            try:
                for row in (
                    client.table("rag_assets")
                    .select("id")
                    .eq(column, project_id)
                    .execute()
                    .data
                    or []
                ):
                    asset_ids.add(row["id"])
            except Exception:  # noqa: BLE001
                continue

    try:
        vector = embed_query(query)
    except Exception as exc:  # noqa: BLE001 — an embedding-provider failure
        return {
            "query_len": len(query),
            "caller": caller_subject(),
            "available": False,
            "error": f"embedding failed: {type(exc).__name__}: {exc}",
            "results": [],
        }

    # Over-fetch when post-filtering, so a project scope has candidates to keep.
    match_count = limit * 20 if asset_ids is not None else limit
    try:
        rows = (
            client.rpc(
                "match_chunks_simple",
                {
                    # PostgREST sends the vector as a JSON string; the RPC's
                    # `query_embedding text` param takes the pgvector literal.
                    "query_embedding": "[" + ",".join(str(f) for f in vector) + "]",
                    "match_threshold": 0.0,
                    "match_count": match_count,
                },
            )
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "query_len": len(query),
            "caller": caller_subject(),
            "error": f"match_chunks_simple failed: {type(exc).__name__}: {exc}",
            "results": [],
        }

    if asset_ids is not None:
        rows = [r for r in rows if r.get("asset_id") in asset_ids]
    rows = rows[:limit]

    # Title the chunks from their assets, in one batched read.
    titles: dict[str, str] = {}
    hit_assets = [r.get("asset_id") for r in rows if r.get("asset_id")]
    if hit_assets:
        try:
            for row in (
                client.table("rag_assets")
                .select("id, title")
                .in_("id", list(set(hit_assets)))
                .execute()
                .data
                or []
            ):
                titles[row["id"]] = row.get("title")
        except Exception:  # noqa: BLE001 — titles are a nicety, not the result
            pass

    results = [
        {
            "chunk_id": r.get("chunk_id"),
            "asset_id": r.get("asset_id"),
            "title": titles.get(r.get("asset_id")),
            "similarity": r.get("similarity"),
            "text": (r.get("text") or "")[:2000],
        }
        for r in rows
    ]

    audit(
        client,
        "semantic_search",
        {"query": query, "project_code": project_code, "limit": limit},
        len(results),
    )
    return {
        "query_len": len(query),
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "available": True,
        "embed_model": EMBED_MODEL,
        "scope": "project (post-filtered)" if asset_ids is not None else "corpus-wide",
        "count": len(results),
        "results": results,
    }


# ──────────────────────────────────────────────────────────────────────
#  Package A — narrow, INSERT-ONLY writes (cp-engine #139)
# ──────────────────────────────────────────────────────────────────────
#
# The policy set (applied 2026-08-01) grants exactly this much to an
# `authenticated` team caller:
#
#   spine_substance  INSERT  with check (is_team_member() AND author_id = auth.uid())
#   notes            INSERT  with check (is_team_member() AND author_id = auth.uid())
#   commitments      INSERT  with check (is_team_member())
#
# and NOTHING else — in particular no authenticated UPDATE policy or grant on
# `spine_substance`. That absence is the design, not an oversight: the
# column-guard trigger on that table restricts WHICH columns a writer may
# change, which is not the same thing as authorization, and must never be
# mistaken for it. So every tool here is insert-only, and:
#
#   `add_spine_version` is DEFERRED BY DESIGN.
#
# Adding a version is not an insert — it is an insert PLUS superseding the
# prior live row (`status: live -> superseded`), an UPDATE on an engine-owned
# status column. With no authenticated UPDATE policy, a hosted caller can write
# the new row but cannot demote the old one, leaving the element with TWO live
# versions: worse than not writing at all, because every reader that picks "the
# live version" then picks arbitrarily. It stays on the `cp mcp` service-key
# path until a reviewed UPDATE policy exists.

# Canonical `layer` vocabulary, copied (not imported) from
# `spine_authoring.authored_element.LAYER_ALIASES` — this prototype stays off
# the cp_engine/spine_authoring import path by design. Keep in sync with that
# package; it is the single source of truth for stored `layer` strings, and a
# divergence here means the spine UI's by-layer filters miss what we wrote.
_LAYER_ALIASES = {
    "email": "Email",
    "note": "Note",
    "decision": "Decisions",
    "decisions": "Decisions",
    "source": "Source material",
    "sourcematerial": "Source material",
    "brief": "Brief",
    "stakeholder": "Stakeholders",
    "stakeholders": "Stakeholders",
    "agreement": "Agreement",
    "synthesis": "Synthesis",
    "output": "Output",
    "activity": "Activity",
    "retrospective": "Retrospective",
    "research": "Research",
    "deliverable": "Deliverables",
    "deliverables": "Deliverables",
    "clientfeedback": "Client feedback",
    "timeline": "Timeline",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Canonical uuid shape. Used by `resolve_recording_id` (#143 batch 5) to tell a
# `fathom_meetings.id` (uuid) apart from a `recording_id` (bigint) — two ids on
# ONE table, only one of which addresses mc-2's promote endpoint.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# `commitments.direction` — mirrors the mc-2 CHECK constraint.
_DIRECTIONS = {"us_to_them", "them_to_us", "internal"}

# `spine_relations.kind` — the closed vocabulary. The first five are copied
# verbatim from `mcp_server._RELATION_KINDS`; mig 125 added the two lifecycle
# kinds (spec v04: canon #147, seal-on-delivery #148). Validated in-process so
# an unknown kind is a clear tool error rather than an opaque 500 from the DB
# CHECK.
_RELATION_KINDS = frozenset(
    {
        "responds_to",
        "supersedes",
        "derives_from",
        "informs",
        "contradicts",
        "canon_of",
        "absorbed_by",
    }
)

# The per-project canon anchor: the standing Inputs & Briefing element
# (spec v04 §2). Canon membership = an active `canon_of` edge member -> brief.
BRIEF_ITEM_ID = "_authored/inputs-briefing"

# Canon size target (spec v04): promotion past this succeeds but warns —
# scarcity is the feature; the warn mirrors spine-lint's posture, not a block.
CANON_TARGET_MAX = 7


def canon_layer(type_: str) -> str:
    """Map an element `type` onto its canonical `layer` string.

    Case/space-insensitive; an unmapped value passes through unchanged so
    already-canonical TitleCase forms are idempotent and a future kind is never
    invented or dropped. Verbatim behaviour of `spine_authoring.canon_layer`.
    """
    if not type_:
        return type_
    return _LAYER_ALIASES.get(type_.lower().replace(" ", ""), type_)


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "untitled"


def valid_due_date(raw: str | None) -> str | None:
    """ISO `YYYY-MM-DD` or None. Mirrors `cp_engine.commitments._valid_due_date`.

    Only a real ISO date belongs in `due_date`; a caller's free-text date is
    rejected loudly rather than guessed at, because an invented deadline is
    worse than an undated row (which downstream flags as "needs a date").
    """
    if not raw or not str(raw).strip():
        return None
    try:
        return date.fromisoformat(str(raw).strip()).isoformat()
    except ValueError:
        return None


def resolve_write_scope(client, project_code: str) -> dict[str, Any] | None:
    """`<code>` -> {id, kind, project_code} for a write.

    Writes need MORE than `resolve_project_id` returns, for two reasons:

      * `commitments` has a `num_nonnulls(project_id, initiative_id) = 1`
        CHECK — the row must name exactly one owner column, so the caller has
        to know WHICH KIND of thing the code named, not just its uuid.
      * `spine_substance` stores BOTH `project_id` and `project_code`, and the
        `project_code` it stores is the cp-tree DIR-SLUG (`ibx-5153-ai-campaign`),
        not the short code the caller typed (`ibx-5153`). Writing the short form
        would create a SECOND project_code for the same project — exactly the
        slug drift already recorded against this corpus. So the canonical
        `project_code` is read back off the project's existing spine rows, and
        only falls back to the caller's string when the project has no spine
        rows yet (a genuinely new project, where the caller's code IS the
        first one written and there is nothing to drift from).
    """
    rows = (
        client.table("initiatives")
        .select("id, code")
        .eq("code", project_code)
        .limit(1)
        .execute()
        .data
        or []
    )
    kind = "initiative" if rows else None
    if not rows:
        pid = resolve_project_id(client, project_code)
        if pid is None:
            return None
        kind = "project"
        scope_id = pid
    else:
        scope_id = rows[0]["id"]

    # Canonical dir-slug from existing spine rows for this uuid, if any.
    canonical = project_code
    try:
        existing = (
            client.table("spine_substance")
            .select("project_code")
            .eq("project_id", scope_id)
            # Deterministic pick: newest row's spelling. Unordered limit(1)
            # was a coin-flip on a drifted project (pre-mig-129); the store
            # is uniform now, but never leave the pick to physical order.
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing and existing[0].get("project_code"):
            canonical = existing[0]["project_code"]
    except Exception:  # noqa: BLE001 — a resolver nicety, never a hard failure
        pass

    return {"id": scope_id, "kind": kind, "project_code": canonical}


_ELEMENT_RESOLVE_COLUMNS = (
    "id, est_item_id, project_code, project_id, phase, binding, layer, "
    "placement, serves, version_label, version_date, status, framing, "
    "sources, origin, important, note, scope"
)


def resolve_element_versions(
    client, project_id: str, key: str
) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None]:
    """`key` -> (est_item_id, all its version rows, error).

    The hosted stand-in for `cp_engine.project_sources.resolve_element_versions`,
    extracted verbatim from what `add_spine_version` already did inline so the
    relation and step verbs resolve elements EXACTLY the way the version verb
    does. Three key forms, in the engine's order:

      1. an exact `est_item_id` (`_authored/<slug>`),
      2. a bare slug, slugified into `_authored/<slug>`,
      3. a case-insensitive `framing` substring — which must match exactly ONE
         element. An ambiguous substring is an ERROR, never a silent pick: the
         whole discipline of these verbs is "bind to one element or skip".

    Scoped by project UUID, not by code string, because the caller's code form
    and the row's stored `project_code` routinely differ (slug drift).
    """
    key = (key or "").strip()
    if not key:
        return None, [], {"error": "an element key is required"}

    candidates = [key] if key.startswith("_authored/") else [f"_authored/{slugify(key)}", key]
    for cand in candidates:
        found = (
            client.table("spine_substance")
            .select(_ELEMENT_RESOLVE_COLUMNS)
            .eq("project_id", project_id)
            .eq("est_item_id", cand)
            .execute()
            .data
            or []
        )
        if found:
            return found[0]["est_item_id"], found, None

    found = (
        client.table("spine_substance")
        .select(_ELEMENT_RESOLVE_COLUMNS)
        .eq("project_id", project_id)
        .ilike("framing", f"%{key}%")
        .execute()
        .data
        or []
    )
    est_ids = {v["est_item_id"] for v in found}
    if len(est_ids) > 1:
        return None, [], {
            "error": f"{key!r} matches {len(est_ids)} elements — be more specific",
            "matches": sorted(est_ids)[:10],
        }
    if not found:
        return None, [], None
    return found[0]["est_item_id"], found, None


def resolve_live_element_id(client, project_id: str, key: str) -> tuple[str | None, dict | None]:
    """`key` -> the est_item_id of ONE LIVE element, mirroring the engine's
    `resolve_live_element`. Returns (est_item_id, error_payload)."""
    est_item_id, versions, err = resolve_element_versions(client, project_id, key)
    if err is not None:
        return None, err
    if est_item_id is None:
        return None, None
    if not any(v.get("status") == "live" for v in versions):
        return None, {"error": f"element {est_item_id!r} has no live version"}
    return est_item_id, None


# ──────────────────────────────────────────────────────────────────────
#  Sources + provenance — shared helpers (#143 batch 3)
# ──────────────────────────────────────────────────────────────────────
#
# The WRITE path here is NOT a PostgREST PATCH. `spine_substance.sources` has
# no authenticated column grant, and the batch-2 UPDATE policy is live-rows-only
# anyway — while the engine semantics these verbs mirror explicitly write EVERY
# version row, because a source link is an ELEMENT-level fact like `serves`, and
# a live-only write would scatter one element's provenance across its history.
#
# So the whole mutation is one guarded SECURITY DEFINER call:
#
#   spine_element_modify_source(p_project_id, p_est_item_id, p_entry, p_add)
#       -> integer (rows updated)
#
# It validates team membership, the entry shape (type ∈ rag_asset|spine_element,
# `id` present, `title` required on add), that the referent actually exists
# (rag_asset by uuid; spine_element by est_item_id within the project, at ANY
# status — a retired provenance source is the POINT, see below), dedupes by
# (type, id), and applies to every version row of the element. The function is
# the authorization boundary; these tools do resolution and reporting only.


def _source_entry_attached(entries: Any, type_: str, ident: str) -> bool:
    """Is a typed link of (type_, ident) already in this `sources` array?

    Dedup is by the PAIR, matching the engine and the DB function: an element
    link and a rag_asset link that happen to share an id string are distinct
    entries, never collapsed into one.
    """
    return any(
        isinstance(entry, dict) and entry.get("type") == type_ and entry.get("id") == ident
        for entry in (entries or [])
    )


def _live_row(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((v for v in versions if v.get("status") == "live"), None)


def _read_live_sources(client, project_id: str, est_item_id: str) -> Any:
    """Re-read the element's LIVE row `sources` after a guarded write.

    The RPC returns a row COUNT, not the rows, so the resulting array is read
    back rather than reconstructed client-side — what the DB actually wrote is
    the only honest thing to return.
    """
    rows = (
        client.table("spine_substance")
        .select("sources, status")
        .eq("project_id", project_id)
        .eq("est_item_id", est_item_id)
        .eq("status", "live")
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0].get("sources") if rows else None


def _resolve_active_asset(
    client, scope_id: str, source_title: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`source_title` -> ONE active rag_asset, or (None, structured note).

    Mirrors `modify_element_sources`' ladder: exact (case-insensitive) title
    first, else `_title_matches` — a case-insensitive CONTAINS where the query
    must be a substring of the stored title (query ⊆ stored, the engine's
    direction). Ambiguity returns the candidate titles and NEVER guesses; that
    discipline is the whole reason these verbs are safe to hand a loose title.

    Superseded assets are dropped the same way `list_project_sources` does (an
    asset with a successor pointing at it), so a stale predecessor cannot be
    attached in place of the document that replaced it.
    """
    want = (source_title or "").strip()
    if not want:
        return None, {"note": "source_title is required"}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for column in ("project_id", "initiative_id"):
        try:
            for row in (
                client.table("rag_assets")
                .select("id, title, status, prev_asset_id")
                .eq(column, scope_id)
                .eq("status", "active")
                .execute()
                .data
                or []
            ):
                if row.get("id") not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        except Exception:  # noqa: BLE001 — `initiative_id` may not apply
            continue

    superseded = {r["prev_asset_id"] for r in rows if r.get("prev_asset_id")}
    rows = [r for r in rows if r.get("id") not in superseded]

    exact = [r for r in rows if (r.get("title") or "").strip().lower() == want.lower()]
    matched = exact or [r for r in rows if want.lower() in (r.get("title") or "").lower()]
    if not matched:
        return None, {"note": f"no active source titled {want!r}"}
    if len(matched) > 1:
        titles = sorted((m.get("title") or "") for m in matched)
        return None, {
            "note": f"ambiguous: {want!r} matched {len(matched)} sources",
            "candidates": titles[:10],
        }
    return matched[0], None


def resolve_source_element(
    client, project_id: str, key: str
) -> dict[str, Any] | None:
    """`key` -> ONE element usable as PROVENANCE — **including retired ones**.

    The hosted mirror of `cp_engine.project_sources._resolve_source_element`,
    and the one resolver on this server that deliberately does NOT filter to
    live/unarchived rows. The provenance case (#104) is precisely "fold a
    now-retired raw card into the synthesis card that absorbed it", so an
    archived source element is the normal input, not an edge case.

    Ladder, matching the engine: exact `est_item_id` first, else a distinct
    case-insensitive `framing` substring, across ALL of the project's elements
    regardless of status/archived. Returns the first matching row (carrying
    est_item_id, framing, archived) or None on no-match OR ambiguity — the same
    "one element or nothing" rule every other resolver here follows.
    """
    key = (key or "").strip()
    if not key:
        return None
    rows = (
        client.table("spine_substance")
        .select("est_item_id, framing, archived, status, version_date")
        .eq("project_id", project_id)
        .execute()
        .data
        or []
    )
    candidates = [key] if key.startswith("_authored/") else [key, f"_authored/{slugify(key)}"]
    for cand in candidates:
        exact = [r for r in rows if r.get("est_item_id") == cand]
        if exact:
            return exact[0]

    matched = [r for r in rows if key.lower() in (r.get("framing") or "").lower()]
    distinct = {r.get("est_item_id"): r for r in matched}
    if len(distinct) != 1:
        return None  # no-match or ambiguous — never guess
    return next(iter(distinct.values()))


def _modify_element_sources(
    client,
    project_code: str,
    key: str,
    entry: dict[str, Any],
    *,
    add: bool,
    tool: str,
    audit_args: dict[str, Any],
    scope: dict[str, Any],
    est_item_id: str,
    versions: list[dict[str, Any]],
) -> dict[str, Any]:
    """The shared attach/detach tail: already-checks, the RPC, the read-back.

    Both quartet halves converge here once their own resolution is done, so the
    already/not-attached semantics, the guarded call, and the returned shape are
    written once rather than four times.
    """
    type_ = entry["type"]
    ident = entry["id"]
    live = _live_row(versions)
    current_live = list((live or {}).get("sources") or [])
    attached_now = _source_entry_attached(current_live, type_, ident)

    # Engine parity: the LIVE row is the authority for already/not-attached.
    if add and attached_now:
        audit(client, tool, audit_args, 0)
        return {
            "est_item_id": est_item_id,
            "source": entry,
            "already": True,
            "sources": current_live,
        }
    if not add and not attached_now:
        audit(client, tool, audit_args, 0)
        return {
            "note": f"{entry.get('title') or ident!r} is not attached to {est_item_id!r}",
            "est_item_id": est_item_id,
            "source": entry,
            "sources": current_live,
        }

    try:
        updated = (
            client.rpc(
                "spine_element_modify_source",
                {
                    "p_project_id": scope["id"],
                    "p_est_item_id": est_item_id,
                    "p_entry": entry,
                    "p_add": add,
                },
            )
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, tool, audit_args, 0)
        return {
            "error": f"guarded source write failed: {type(exc).__name__}: {str(exc)[:400]}",
            "est_item_id": est_item_id,
            "source": entry,
        }

    rows_updated = int(updated or 0)
    if rows_updated == 0:
        # The function validated but matched nothing — say so rather than
        # reporting a success that wrote no row.
        audit(client, tool, audit_args, 0)
        return {
            "error": f"0 version rows updated for {est_item_id!r} — the element "
            "may have been retired or re-keyed between resolution and write.",
            "est_item_id": est_item_id,
            "source": entry,
        }

    audit(client, tool, audit_args, rows_updated)
    return {
        "est_item_id": est_item_id,
        "source": entry,
        ("attached" if add else "removed"): True,
        "versions_updated": rows_updated,
        "sources": _read_live_sources(client, scope["id"], est_item_id),
        "project_code": scope["project_code"],
        "caller": caller_subject(),
    }


# ──────────────────────────────────────────────────────────────────────
#  Spine steps — the shared write helpers (#143 batch 1)
# ──────────────────────────────────────────────────────────────────────
#
# Copied, not imported, from `cp_engine.spine_steps` — the vocabularies and
# row fields below are that module's, verbatim:
#
#   STEP_STATUSES = ("done", "active", "upcoming")   NOTE_MAX = 8000
#   add_step      -> source/review LEFT UNSET (the table's defaults stand for a
#                    live human step: engine writes neither column)
#   propose_step  -> source='auto', review='proposed'
#   upsert_auto_step -> source='auto', review='proposed', status='done'
#
# The one hosted-specific constraint is the UPDATE policy: an authenticated
# caller may update ONLY rows that are BOTH source='auto' AND review='proposed'.
# That is exactly the guardrail `upsert_auto_step` already enforces in code, so
# the engine semantics and the RLS policy agree rather than fight.

STEP_STATUSES = ("done", "active", "upcoming")
STEP_NOTE_MAX = 8000
_STEP_SELECT = "id, est_item_id, position, title, status, step_date, note, source, review"


def read_steps(client, project_id: str, est_item_id: str) -> list[dict[str, Any]]:
    """This element's steps, ordered by position (the outline read order)."""
    return (
        client.table("spine_steps")
        .select(_STEP_SELECT)
        .eq("project_id", project_id)
        .eq("est_item_id", est_item_id)
        .order("position")
        .execute()
        .data
        or []
    )


def next_step_position(existing: list[dict[str, Any]]) -> int:
    return max((s.get("position") or 0 for s in existing), default=0) + 1


def upsert_auto_step(
    client, project_id: str, est_item_id: str, title: str, step_date: str
) -> dict[str, Any]:
    """Auto-journal a content-write as a step, ONE per (element, day).

    Mirrors `cp_engine.spine_steps.upsert_auto_step` exactly, including the part
    that matters most — the collapse key IGNORES title. A second version bump of
    the same element on the same day RETITLES the day's existing proposed
    auto-step rather than stacking a near-identical row.

    Guardrails (engine semantics AND the hosted UPDATE policy, which agree): it
    only ever touches a row that is BOTH source='auto' AND review='proposed'. A
    human step, or an auto-step a human already CONFIRMED, is frozen. A
    DISMISSED auto-step does not block a fresh one — the human rejected that
    title, not the day's work.

    Always status='done' (the move happened) + review='proposed' (the gate).
    Callers treat any {error} as NON-FATAL: a journal miss must never fail the
    content-write that triggered it.
    """
    if not (title and title.strip()):
        return {"error": "title is required to journal a step"}

    title_clean = title.strip()
    existing = read_steps(client, project_id, est_item_id)
    open_auto = next(
        (
            s
            for s in existing
            if s.get("source") == "auto"
            and s.get("review") == "proposed"
            and (s.get("step_date") or None) == (step_date or None)
        ),
        None,
    )
    if open_auto is not None:
        if (open_auto.get("title") or "").strip() != title_clean:
            client.table("spine_steps").update({"title": title_clean}).eq(
                "id", open_auto["id"]
            ).execute()
        return {
            "est_item_id": est_item_id,
            "updated": True,
            "step_id": open_auto["id"],
        }

    inserted = (
        client.table("spine_steps")
        .insert(
            {
                "project_id": project_id,
                "est_item_id": est_item_id,
                "position": next_step_position(existing),
                "title": title_clean,
                "status": "done",
                "step_date": step_date,
                "note": None,
                "source": "auto",
                "review": "proposed",
            }
        )
        .execute()
    )
    step_id = inserted.data[0]["id"] if inserted.data else None
    return {"est_item_id": est_item_id, "created": True, "step_id": step_id}


@mcp_server.tool()
def create_spine_relation(
    project_code: str,
    kind: str,
    from_key: str,
    to_key: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Create a typed directed edge between two live spine elements (#97).

    The hosted port of the stdio verb, with identical semantics. `kind` is one of
    the closed vocabulary: responds_to | supersedes | derives_from | informs |
    contradicts — anything else is rejected HERE rather than left to reach the
    DB CHECK (which would surface as an opaque 500). `from_key`/`to_key` each
    resolve to ONE live element the same way `pull_spine_element` does: an exact
    est_item_id or a distinct `framing` (title) substring.

    The edge is written live (`status='active'`, `source='manual'`) and keys on
    est_item_id — stable across version bumps, so the live version resolves at
    READ time rather than being frozen into the edge.

    Idempotent on the mig-117 unique constraint (project_id, kind, from, to): a
    duplicate is reported as `{created: false, already: true}`, both by checking
    first and by catching the 23505 a concurrent writer can still produce.

    HOSTED DIFFERENCE from the stdio verb: `created_by` is stamped with the
    CALLER'S VERIFIED EMAIL, not the literal "cp-sources" the service-key path
    writes. The INSERT policy is `is_team_member() AND created_by =
    auth.jwt()->>'email'`, so attribution is Postgres-enforced — a hosted edge
    always names the human who drew it.

    Authoring vocab (which edge for which change): responds_to = their voice
    reacting to ours; derives_from = built from named inputs; supersedes = a
    genuine fork (rare); informs = shaped but didn't generate; contradicts = a
    conflicting claim.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        kind: responds_to | supersedes | derives_from | informs | contradicts.
        from_key: the source element (est_item_id or unique framing substring).
        to_key: the target element.
        note: optional annotation on the edge.
    """
    kind_n = (kind or "").strip().lower()
    if kind_n not in _RELATION_KINDS:
        return {"error": f"unknown relation kind {kind!r}; use one of {sorted(_RELATION_KINDS)}"}

    client = user_client()
    email = caller_email()
    if not email:
        return {
            "error": "no email claim on the caller's token — `spine_relations` "
            "attributes every edge to a verified email (INSERT policy "
            "`created_by = auth.jwt()->>'email'`), so an edge cannot be written "
            "without one."
        }

    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    from_eid, err = resolve_live_element_id(client, scope["id"], from_key)
    if err is not None:
        return err
    if from_eid is None:
        return {"note": f"no single live element matching from_key {from_key!r}"}
    to_eid, err = resolve_live_element_id(client, scope["id"], to_key)
    if err is not None:
        return err
    if to_eid is None:
        return {"note": f"no single live element matching to_key {to_key!r}"}
    if from_eid == to_eid:
        return {"error": "an element cannot relate to itself"}

    audit_args = {
        "project_code": project_code,
        "kind": kind_n,
        "from_key": from_key,
        "to_key": to_key,
    }

    existing = (
        client.table("spine_relations")
        .select("id")
        .eq("project_id", scope["id"])
        .eq("kind", kind_n)
        .eq("from_item_id", from_eid)
        .eq("to_item_id", to_eid)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        audit(client, "create_spine_relation", audit_args, 0)
        return {
            "kind": kind_n,
            "from_item_id": from_eid,
            "to_item_id": to_eid,
            "created": False,
            "already": True,
            "relation_id": existing[0].get("id"),
        }

    try:
        result = (
            client.table("spine_relations")
            .insert(
                {
                    "project_id": scope["id"],
                    "project_code": scope["project_code"],
                    "kind": kind_n,
                    "from_item_id": from_eid,
                    "to_item_id": to_eid,
                    "status": "active",
                    "source": "manual",
                    "note": note,
                    "created_by": email,
                }
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        # 23505 = the mig-117 unique constraint. A concurrent writer can win the
        # race between the check above and this insert; that is the edge already
        # existing, which is the SAME outcome, not a failure.
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            audit(client, "create_spine_relation", audit_args, 0)
            return {
                "kind": kind_n,
                "from_item_id": from_eid,
                "to_item_id": to_eid,
                "created": False,
                "already": True,
            }
        audit(client, "create_spine_relation", audit_args, 0)
        return {"error": f"relation insert failed: {type(exc).__name__}: {str(exc)[:400]}"}

    created = (result.data or [{}])[0]
    audit(client, "create_spine_relation", audit_args, 1)
    return {
        "relation_id": created.get("id"),
        "kind": kind_n,
        "from_item_id": from_eid,
        "to_item_id": to_eid,
        "created": True,
        "project_code": scope["project_code"],
        "created_by": email,
        "caller": caller_subject(),
    }


def _live_framing(client, project_id: str, est_item_id: str) -> str | None:
    rows = (
        client.table("spine_substance")
        .select("framing")
        .eq("project_id", project_id)
        .eq("est_item_id", est_item_id)
        .eq("status", "live")
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0].get("framing") if rows else None


def _insert_lifecycle_edge(
    client,
    scope: dict[str, Any],
    kind: str,
    from_eid: str,
    to_eid: str,
    email: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Idempotent active-edge insert, shared by the two lifecycle verbs.

    Same discipline as `create_spine_relation`: check-first, then treat a
    concurrent 23505 as the edge already existing rather than a failure.
    Returns {created: bool, already?: bool} or {error}.
    """
    existing = (
        client.table("spine_relations")
        .select("id")
        .eq("project_id", scope["id"])
        .eq("kind", kind)
        .eq("from_item_id", from_eid)
        .eq("to_item_id", to_eid)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return {"created": False, "already": True, "relation_id": existing[0].get("id")}
    try:
        result = (
            client.table("spine_relations")
            .insert(
                {
                    "project_id": scope["id"],
                    "project_code": scope["project_code"],
                    "kind": kind,
                    "from_item_id": from_eid,
                    "to_item_id": to_eid,
                    "status": "active",
                    "source": "manual",
                    "note": note,
                    "created_by": email,
                }
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            return {"created": False, "already": True}
        return {"error": f"{kind} insert failed: {type(exc).__name__}: {str(exc)[:400]}"}
    return {"created": True, "relation_id": (result.data or [{}])[0].get("id")}


def _canon_member_ids(client, project_id: str) -> list[str]:
    """est_item_ids with an active canon_of edge in this project."""
    rows = (
        client.table("spine_relations")
        .select("from_item_id")
        .eq("project_id", project_id)
        .eq("kind", "canon_of")
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    return [r["from_item_id"] for r in rows]


@mcp_server.tool()
def promote_to_canon(
    project_code: str,
    key: str,
    replaces_key: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Promote an element into the project's canon (#147, spec v04 §2).

    The canon is the small curated "current truth" set, anchored on the
    standing Inputs & Briefing element (`_authored/inputs-briefing`): membership
    is an active `canon_of` edge member -> brief. Promotion is DELIBERATE AND
    DISPLACING — when `replaces_key` is given, this verb also writes a
    `supersedes` edge (new -> old) so the lineage survives, and removes the old
    member's `canon_of` edge. Scarcity is the feature: past ~7 members the verb
    still succeeds but returns a warning (spine-lint posture, not a block).

    The move is auto-journaled as ONE review-gated step on the BRIEF element
    (the canon's trail lives on its anchor). Journaling is non-fatal.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the element to promote (est_item_id or unique framing substring).
        replaces_key: optional canon member this one displaces.
        note: optional one-line "why" stored on the canon_of edge.
    """
    client = user_client()
    email = caller_email()
    if not email:
        return {"error": "no email claim on the caller's token — canon edges are attributed."}

    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    brief_eid, err = resolve_live_element_id(client, scope["id"], BRIEF_ITEM_ID)
    if err is not None:
        return err
    if brief_eid is None:
        return {
            "error": f"no live standing Inputs & Briefing element "
            f"({BRIEF_ITEM_ID!r}) in {project_code!r} — the canon anchors on the "
            "brief; scaffold/author it first."
        }

    member_eid, err = resolve_live_element_id(client, scope["id"], key)
    if err is not None:
        return err
    if member_eid is None:
        return {"note": f"no single live element matching key {key!r}"}
    if member_eid == brief_eid:
        return {"error": "the brief anchors the canon; it cannot be a member of itself"}

    audit_args = {"project_code": project_code, "key": key, "replaces_key": replaces_key}
    edge = _insert_lifecycle_edge(
        client, scope, "canon_of", member_eid, brief_eid, email, note
    )
    if edge.get("error"):
        audit(client, "promote_to_canon", audit_args, 0)
        return edge

    out: dict[str, Any] = {
        "project_code": scope["project_code"],
        "promoted": member_eid,
        "canon_anchor": brief_eid,
        "created": edge.get("created", False),
        **({"already": True} if edge.get("already") else {}),
        "caller": caller_subject(),
    }

    if replaces_key:
        # Live first, raw est_item_id fallback — the displaced member may
        # already be retired (same tolerance as retire_spine_relation).
        old_eid, _ = resolve_live_element_id(client, scope["id"], replaces_key)
        old_eid = old_eid or replaces_key
        if old_eid == member_eid:
            out["replaces"] = {"note": "replaces_key resolves to the promoted element; skipped"}
        else:
            supersede = _insert_lifecycle_edge(
                client, scope, "supersedes", member_eid, old_eid, email,
                note or "displaced from canon",
            )
            removed = (
                client.table("spine_relations")
                .delete()
                .eq("project_id", scope["id"])
                .eq("kind", "canon_of")
                .eq("from_item_id", old_eid)
                .eq("to_item_id", brief_eid)
                .execute()
                .data
            ) or []
            out["replaces"] = {
                "displaced": old_eid,
                "supersedes_edge": supersede,
                "canon_edge_removed": len(removed),
            }

    members = _canon_member_ids(client, scope["id"])
    out["canon_size"] = len(members)
    if len(members) > CANON_TARGET_MAX:
        out["warning"] = (
            f"canon has {len(members)} members (target ≤{CANON_TARGET_MAX}). "
            "Scarcity is the feature — consider displacing (replaces_key) "
            "rather than accreting."
        )

    if edge.get("created"):
        try:
            framing = _live_framing(client, scope["id"], member_eid) or member_eid
            out["step"] = upsert_auto_step(
                client,
                scope["id"],
                brief_eid,
                f"Canon: promoted {framing}"[:120],
                step_date=date.today().isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — journaling is non-fatal
            out["step"] = {"error": f"auto-step failed: {type(exc).__name__}: {str(exc)[:300]}"}

    audit(client, "promote_to_canon", audit_args, 1 if edge.get("created") else 0)
    return out


@mcp_server.tool()
def seal_to_deliverable(
    project_code: str,
    deliverable_key: str,
    absorbed_keys: list[str],
    note: str | None = None,
) -> dict[str, Any]:
    """Seal elements into a shipped deliverable (#148, spec v04 §3).

    A shipped deliverable is a COMPRESSION EVENT: it absorbs the elements it
    was synthesized from. This verb batch-writes `absorbed_by` edges
    (source element -> deliverable element). An element with an active
    `absorbed_by` edge is HISTORICAL on the read side — excluded from
    `list_spine_elements` by default, included (annotated) with
    `include_absorbed=true`. Absorbed is NOT archived: the element stays one
    hop behind its deliverable for retrospectives.

    The whole seal journals as ONE review-gated step on the DELIVERABLE
    element ("Sealed N elements on delivery"). Journaling is non-fatal.
    Idempotent per pair: re-sealing reports `already` per element.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        deliverable_key: the absorbing deliverable (est_item_id or unique
            framing substring).
        absorbed_keys: the elements it absorbed.
        note: optional one-line annotation stored on each edge (e.g. the
            delivery date or deliverable version).
    """
    client = user_client()
    email = caller_email()
    if not email:
        return {"error": "no email claim on the caller's token — seal edges are attributed."}

    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    deliv_eid, err = resolve_live_element_id(client, scope["id"], deliverable_key)
    if err is not None:
        return err
    if deliv_eid is None:
        return {"note": f"no single live element matching deliverable_key {deliverable_key!r}"}

    sealed: list[str] = []
    already: list[str] = []
    skipped: list[dict[str, str]] = []
    for k in absorbed_keys or []:
        eid, err = resolve_live_element_id(client, scope["id"], k)
        if err is not None or eid is None:
            skipped.append({"key": k, "reason": "no single live element match"})
            continue
        if eid == deliv_eid:
            skipped.append({"key": k, "reason": "is the deliverable itself"})
            continue
        edge = _insert_lifecycle_edge(
            client, scope, "absorbed_by", eid, deliv_eid, email, note
        )
        if edge.get("error"):
            skipped.append({"key": k, "reason": edge["error"]})
        elif edge.get("already"):
            already.append(eid)
        else:
            sealed.append(eid)

    audit_args = {
        "project_code": project_code,
        "deliverable_key": deliverable_key,
        "absorbed_count": len(absorbed_keys or []),
    }
    out: dict[str, Any] = {
        "project_code": scope["project_code"],
        "deliverable": deliv_eid,
        "sealed": sealed,
        "already": already,
        "skipped": skipped,
        "caller": caller_subject(),
    }

    if sealed:
        try:
            out["step"] = upsert_auto_step(
                client,
                scope["id"],
                deliv_eid,
                f"Sealed {len(sealed)} element(s) on delivery",
                step_date=date.today().isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — journaling is non-fatal
            out["step"] = {"error": f"auto-step failed: {type(exc).__name__}: {str(exc)[:300]}"}

    audit(client, "seal_to_deliverable", audit_args, len(sealed))
    return out


@mcp_server.tool()
def add_spine_step(
    project_code: str,
    key: str,
    title: str,
    status: str = "upcoming",
    step_date: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Append an ordered STEP to a spine element's progress trail (#119).

    A step is a lightweight marker of one move toward finishing the element
    (drafted -> ratified -> rewriting -> booked) — NOT a version, source, or
    body. `key` resolves to ONE live element (est_item_id exact, or a unique
    framing substring — same discipline as `pull_spine_element`). The step is
    appended at the end (position = max+1 within this (project, element)).

    This writes a LIVE HUMAN step: `source` and `review` are left UNSET so the
    table's own defaults stand, exactly as `cp_engine.spine_steps.add_step`
    writes it. Use `propose_spine_step` instead when YOU are recording progress
    you just made — that one lands review-gated.

    `status` ∈ done|active|upcoming (default upcoming); `step_date` is free-form
    ('7/16', optional); `note` is a sentence or two (optional, ≤8000 chars). A
    step NEVER completes the work-item on the schedule — that stays
    human-confirmed.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the parent element (est_item_id or unique framing substring).
        title: terse past/present-tense label for the move.
        status: done | active | upcoming.
        step_date: optional free-form date.
        note: optional annotation (≤8000 chars).
    """
    if not (title and title.strip()):
        return {"error": "title is required to add a step"}
    if status not in STEP_STATUSES:
        return {"error": f"status must be one of {list(STEP_STATUSES)}"}
    if note is not None and len(note) > STEP_NOTE_MAX:
        return {"error": f"note exceeds {STEP_NOTE_MAX} characters"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, err = resolve_live_element_id(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    audit_args = {
        "project_code": project_code,
        "key": key,
        "status": status,
        "step_date": step_date,
        "title": title,
    }
    existing = read_steps(client, scope["id"], est_item_id)
    position = next_step_position(existing)
    try:
        result = (
            client.table("spine_steps")
            .insert(
                {
                    "project_id": scope["id"],
                    "est_item_id": est_item_id,
                    "position": position,
                    "title": title.strip(),
                    "status": status,
                    "step_date": step_date,
                    "note": note,
                }
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, "add_spine_step", audit_args, 0)
        return {"error": f"step insert failed: {type(exc).__name__}: {str(exc)[:400]}"}

    created = (result.data or [{}])[0]
    audit(client, "add_spine_step", audit_args, 1)
    return {
        "est_item_id": est_item_id,
        "step_id": created.get("id"),
        "position": position,
        "caller": caller_subject(),
        "steps": read_steps(client, scope["id"], est_item_id),
    }


@mcp_server.tool()
def propose_spine_step(
    project_code: str,
    key: str,
    title: str,
    status: str = "done",
    step_date: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """PROPOSE a machine-authored step on an element's trail (auto-journey-steps).

    Author a step as work moves DURING a session — but it lands PROPOSED, not
    live (`source='auto'`, `review='proposed'`): a human confirms or dismisses it
    on the spine trail. Use this (not `add_spine_step`, which writes a live human
    step) when YOU are recording progress you just made, e.g. at the end of a
    content/synthesis session on an engagement.

    Contract (design 2026-07-21 §2): one MOVE = one step (not one edit); bind to
    exactly ONE element (`key` resolves like `pull_spine_element` — skip rather
    than guess if you can't attribute the work to a single element); prefer
    `status='done'` (the move already happened); a terse past-tense `title`
    (≤~60 chars, "Ratified the pillars", not "worked on pillars"). **Cap yourself
    at ≤2 proposed steps per session across all elements.**

    Idempotent: re-proposing the same (element, title, step_date) is a no-op in
    ANY review state — a confirmed or already-dismissed twin is not re-proposed,
    so a re-run never double-proposes and never resurrects a rejected step.
    Returns {est_item_id, proposed: bool, already?: bool, steps}.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the parent element (est_item_id or unique framing substring).
        title: terse past-tense label for the move.
        status: done | active | upcoming (default done).
        step_date: optional free-form date.
        note: optional annotation (≤8000 chars).
    """
    if not (title and title.strip()):
        return {"error": "title is required to propose a step"}
    if status not in STEP_STATUSES:
        return {"error": f"status must be one of {list(STEP_STATUSES)}"}
    if note is not None and len(note) > STEP_NOTE_MAX:
        return {"error": f"note exceeds {STEP_NOTE_MAX} characters"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, err = resolve_live_element_id(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    audit_args = {
        "project_code": project_code,
        "key": key,
        "status": status,
        "step_date": step_date,
        "title": title,
    }
    title_clean = title.strip()
    existing = read_steps(client, scope["id"], est_item_id)
    # Idempotency guard on the natural key, matching ANY review state — a
    # confirmed or rejected twin blocks a re-propose.
    dup = next(
        (
            s
            for s in existing
            if (s.get("title") or "").strip().lower() == title_clean.lower()
            and (s.get("step_date") or None) == (step_date or None)
        ),
        None,
    )
    if dup is not None:
        audit(client, "propose_spine_step", audit_args, 0)
        return {
            "est_item_id": est_item_id,
            "proposed": False,
            "already": True,
            "step_id": dup.get("id"),
            "steps": existing,
        }

    position = next_step_position(existing)
    try:
        result = (
            client.table("spine_steps")
            .insert(
                {
                    "project_id": scope["id"],
                    "est_item_id": est_item_id,
                    "position": position,
                    "title": title_clean,
                    "status": status,
                    "step_date": step_date,
                    "note": note,
                    "source": "auto",
                    "review": "proposed",
                }
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, "propose_spine_step", audit_args, 0)
        return {"error": f"step insert failed: {type(exc).__name__}: {str(exc)[:400]}"}

    created = (result.data or [{}])[0]
    audit(client, "propose_spine_step", audit_args, 1)
    return {
        "est_item_id": est_item_id,
        "proposed": True,
        "step_id": created.get("id"),
        "position": position,
        "caller": caller_subject(),
        "steps": read_steps(client, scope["id"], est_item_id),
    }


# ──────────────────────────────────────────────────────────────────────
#  #143 batch 2 — the UPDATE-shaped verbs
# ──────────────────────────────────────────────────────────────────────
#
# Batch 1 was insert-only because no authenticated UPDATE policy existed. The
# `ratchet_batch2_update_verb_policies` migration adds exactly three, and the
# tools below are shaped to fit them rather than to work around them:
#
#   spine_substance  UPDATE  using/with check (is_team_member() AND status='live')
#   commitments      UPDATE  using (is_team_member() AND status='open')
#                            with check (is_team_member() AND status IN ('done','dropped'))
#   spine_steps      UPDATE/DELETE  using/with check (is_team_member())
#
# Two consequences the verbs encode rather than fight:
#
#   * `set_spine_element` can only touch LIVE rows. The engine verb writes
#     layer/framing/serves to EVERY version of an element (they are
#     element-level facts); the hosted policy makes superseded rows unwritable,
#     so the hosted verb is live-row-only and SAYS SO in its return. See the
#     docstring — this is a real semantic difference, not an oversight.
#   * `resolve_commitment`'s USING clause means a non-open commitment matches
#     ZERO rows. PostgREST reports that as a successful 0-row update, not an
#     error, so the verb checks the row count and explains the denial rather
#     than reporting a silent success.
#
# ──────────────────────────────────────────────────────────────────────
#  Transcript promotion (#143 batch 5) — DELEGATED, not ported
# ──────────────────────────────────────────────────────────────────────
#
# THE ARCHITECTURE DECISION, stated once so no future reader re-litigates it.
#
# The engine's `promote_spine_transcript` cannot be ported to a hosted server.
# It resolves an element's `rel_path` to a file in a LOCAL tenant checkout,
# copies it to a stable temp path, and runs the full ingest pipeline (Voyage
# embeddings, a SERVICE-KEY Supabase write to `rag_assets`). A hosted server has
# no tenant checkout it can trust as authoritative and — the whole point of this
# prototype — no service key at all.
#
# mc-2's backend already does this promotion service-side, and its auth is the
# SAME Supabase JWT this server verifies. So the hosted verb DELEGATES: it
# resolves the caller's key to a `recording_id` and POSTs to
# `{MC2_API_BASE}/api/meetings/{recording_id}/promote-transcript` carrying the
# CALLER'S OWN bearer token. The promotion therefore runs as the caller, with
# mc-2's own authorization applying — this server never becomes a confused
# deputy, because it forwards an identity rather than substituting its own.
#
# WHAT DELEGATION CHANGES (both real, both surfaced in the return):
#
#   1. **A different promotion universe.** The engine promotes a tenant FILE
#      (`rel_path` -> a `spine/<activity>/<deliverable>.md`), landing a
#      `source_provider='spine-promote'` asset. mc-2 promotes a MEETING
#      (`recording_id` -> the Fathom transcript), landing
#      `source_provider='fathom'`. Verified live: 2 spine-promote assets vs 171
#      fathom assets, and NO live spine element's `rel_path` points at a
#      transcript — every one points at a spine markdown file. These are not
#      the same operation wearing two names, and the return says which ran.
#   2. **The id shape.** `fathom_meetings` has BOTH a uuid `id` and a bigint
#      `recording_id`. `list_project_meetings` returns the UUID as `meeting_id`;
#      the mc-2 endpoint takes the BIGINT. Handing it the uuid is the known
#      call-id-vs-recording-id gotcha, and `resolve_recording_id` below exists
#      precisely so a caller can pass either and land on the right one.


def _meeting_scope_filter(query, scope: dict[str, Any]):
    """Constrain a `fathom_meetings` query to one project or initiative.

    `fathom_meetings` names its owner in one of two columns and the right one
    depends on what the code resolved to — the same split `resolve_write_scope`
    already encodes for `commitments`' num_nonnulls CHECK.
    """
    column = "initiative_id" if scope.get("kind") == "initiative" else "project_id"
    return query.eq(column, scope["id"])


def resolve_recording_id(
    client, scope: dict[str, Any], key: str
) -> tuple[int | None, dict[str, Any] | None, dict[str, Any] | None]:
    """`key` -> (recording_id, meeting_row, error/note).

    Mirrors the ENGINE VERB'S KEY SEMANTICS as closely as a delegating server
    can, accepting three forms and reporting which one matched:

      1. **A bare recording_id** (all digits) — the mc-2 endpoint's native key.
         Accepted directly, but still verified to EXIST and to belong to this
         project, so a typo'd id cannot promote another project's meeting.
      2. **A meeting uuid** (`fathom_meetings.id`) — what `list_project_meetings`
         hands back as `meeting_id`. This is the gotcha branch: it looks like a
         valid id and is NOT the one the endpoint wants, so it is TRANSLATED
         here rather than forwarded and 404'd upstream.
      3. **A spine element key** (est_item_id / bare slug / framing substring) —
         the engine verb's own key form. The element is resolved with the SAME
         `resolve_element_versions` discipline every other verb uses, then
         bridged to a meeting (see `_recording_id_for_element`).

    Returns `(None, None, {...})` with a structured note/error on any miss —
    never a guess, and never a raw exception.
    """
    key = (key or "").strip()
    if not key:
        return None, None, {"error": "a key is required (element, meeting id, or recording id)"}

    # ── Form 1: a bare recording_id. Verified against THIS project's meetings. ──
    if key.isdigit():
        rid = int(key)
        rows = (
            _meeting_scope_filter(
                client.table("fathom_meetings").select(
                    "id, recording_id, title, meeting_date, transcript_promoted_at"
                ),
                scope,
            )
            .eq("recording_id", rid)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return None, None, {
                "note": f"no meeting with recording_id {rid} belongs to "
                f"{scope.get('project_code')!r}. A recording_id from another "
                "project is refused rather than promoted."
            }
        return rid, rows[0], None

    # ── Form 2: a meeting uuid — translate, don't forward. ──
    if _UUID_RE.match(key):
        rows = (
            _meeting_scope_filter(
                client.table("fathom_meetings").select(
                    "id, recording_id, title, meeting_date, transcript_promoted_at"
                ),
                scope,
            )
            .eq("id", key)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            rid = rows[0].get("recording_id")
            if not rid:
                return None, rows[0], {
                    "note": f"meeting {key} has no recording_id — it cannot be "
                    "promoted (the mc-2 endpoint is keyed on the Fathom recording)."
                }
            return int(rid), rows[0], None
        # Fall through: a uuid can also be a spine element's id, so a miss here
        # is not yet a failure.

    # ── Form 3: a spine element key. ──
    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return None, None, err
    if est_item_id is None:
        return None, None, {
            "note": f"no meeting or live element matching {key!r} in "
            f"{scope.get('project_code')!r}"
        }
    live = next((v for v in versions if v.get("status") == "live"), None) or versions[0]
    return _recording_id_for_element(client, scope, est_item_id, live)


def _recording_id_for_element(
    client, scope: dict[str, Any], est_item_id: str, element: dict[str, Any]
) -> tuple[int | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Bridge a spine element to the Fathom meeting behind it, or explain why not.

    THIS IS THE SEAM WHERE THE TWO PROMOTION UNIVERSES MEET, and it is worth
    being explicit about how thin the bridge really is.

    The engine promotes `element.rel_path` — a file. There is NO column linking
    a spine element to a `fathom_meetings` row, so a delegating server cannot
    reproduce that by construction. What it CAN do is follow the element's
    attached sources: a meeting that has already been ingested lands a
    `rag_assets` row with `source_provider='fathom'` and `source_file_id` = the
    recording_id AS TEXT (verified live). If the element cites such a source,
    that citation IS the element->meeting link, and it is exact rather than
    guessed.

    Returns a structured note (never an error) when no bridge exists — for most
    elements this is the shape of the world, not a failure: their substance came
    from a document, not a recording.
    """
    sources = element.get("sources") or []
    asset_ids = [
        s.get("id")
        for s in sources
        if isinstance(s, dict) and s.get("type") == "rag_asset" and s.get("id")
    ]
    if not asset_ids:
        return None, None, {
            "note": f"element {est_item_id!r} cites no ingested source, so no "
            "Fathom recording can be resolved from it. Promotion is keyed on a "
            "meeting: pass a recording_id or a meeting id directly.",
            "est_item_id": est_item_id,
        }

    assets = (
        client.table("rag_assets")
        .select("id, title, source_provider, source_file_id")
        .in_("id", asset_ids)
        .eq("source_provider", "fathom")
        .execute()
        .data
        or []
    )
    recording_ids = sorted(
        {
            int(a["source_file_id"])
            for a in assets
            if str(a.get("source_file_id") or "").isdigit()
        }
    )
    if not recording_ids:
        return None, None, {
            "note": f"element {est_item_id!r} cites {len(asset_ids)} source(s), "
            "none of which came from a Fathom recording (its substance is "
            "document-derived). Nothing to promote.",
            "est_item_id": est_item_id,
        }
    if len(recording_ids) > 1:
        # Same discipline as every other resolver here: ambiguity is an ERROR
        # that hands back the candidates, never a silent pick.
        return None, None, {
            "error": f"element {est_item_id!r} cites {len(recording_ids)} Fathom "
            "recordings; promotion targets exactly one. Re-key by recording_id.",
            "est_item_id": est_item_id,
            "candidates": recording_ids,
        }

    rid = recording_ids[0]
    rows = (
        _meeting_scope_filter(
            client.table("fathom_meetings").select(
                "id, recording_id, title, meeting_date, transcript_promoted_at"
            ),
            scope,
        )
        .eq("recording_id", rid)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rid, (rows[0] if rows else None), None


def call_mc2_promote(recording_id: int) -> dict[str, Any]:
    """POST the promote to mc-2 under the CALLER'S OWN JWT. Never raises.

    Response translation mirrors what mc-2 itself does one hop upstream, so a
    caller reads one vocabulary rather than three: 2xx is `ok:true` with the
    backend's JSON attached; a 401/403 is an AUTHORIZATION answer about the
    caller (not a plumbing failure) and says so; a 502 carrying `no meeting
    with recording_id` upstream is reported as not-found rather than as a
    generic gateway error, because that is what it actually means.
    """
    if not MC2_API_BASE:
        return {
            "ok": False,
            "reason": "promotion unavailable: MC2_API_BASE not configured",
            "degraded": True,
        }

    url = f"{MC2_API_BASE}/api/meetings/{recording_id}/promote-transcript"
    try:
        token = caller_jwt()
    except RuntimeError as exc:
        return {"ok": False, "reason": f"no authenticated caller: {exc}"}

    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=MC2_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        return {
            "ok": False,
            "reason": f"mc-2 promote timed out after {MC2_TIMEOUT_SECONDS:.0f}s. "
            "The promotion may still be completing upstream — re-read the "
            "meeting's transcript_promoted flag before retrying.",
            "timeout": True,
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"could not reach mc-2: {type(exc).__name__}: {exc}"}

    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:400]

    if 200 <= resp.status_code < 300:
        return {"ok": True, "status": resp.status_code, "backend": body}

    detail = body.get("detail") if isinstance(body, dict) else str(body)
    detail = str(detail)[:400]
    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "status": resp.status_code,
            "reason": f"mc-2 refused the caller's token: {detail}",
            "unauthorized": True,
        }
    if "no meeting with recording_id" in detail:
        return {
            "ok": False,
            "status": resp.status_code,
            "reason": f"mc-2 has no meeting with recording_id {recording_id}: {detail}",
            "not_found": True,
        }
    return {"ok": False, "status": resp.status_code, "reason": detail}


def _promotion_on_important_flip(
    client, scope: dict[str, Any], est_item_id: str, element: dict[str, Any], *, fired: bool
) -> dict[str, Any]:
    """The `important` false->true side effect, as a value. NEVER raises.

    This closes the gap batch 2 left open: the stdio `set_spine_element` fired a
    RAG promotion on the flip, and the hosted port returned a "not mirrored"
    sentinel instead. It now fires the DELEGATED promotion.

    Three outcomes, always shaped the same so a caller can branch on `fired`:

      * `{"fired": False, "skipped": <reason>}` — no flip happened, the project
        is an initiative (the engine's engagement-only guard, mirrored verbatim
        because mc-2 meetings are never initiative-linked — verified live: zero
        `fathom_meetings` rows carry an `initiative_id`), or no Fathom recording
        resolves from the element. For most elements this LAST case is normal,
        not broken: their substance is document-derived.
      * `{"fired": True, "ok": True, ...}` — mc-2 accepted the promotion.
      * `{"fired": True, "ok": False, "reason": ...}` — it did not, and the
        metadata write still stands. That is the whole contract: importance is
        set either way, and this key only ever REPORTS.
    """
    if not fired:
        return {"fired": False, "skipped": "no false->true important transition"}

    try:
        # ENGAGEMENT-ONLY GUARD — mirrors `spine_promote.promote_transcript`'s
        # Contract A ("initiative promotion not yet supported"), checked BEFORE
        # any resolution work, exactly as the engine does.
        if scope.get("kind") == "initiative":
            return {
                "fired": False,
                "skipped": "initiative promotion not yet supported "
                "(engagement-only, mirroring the engine guard)",
            }

        recording_id, meeting, problem = _recording_id_for_element(
            client, scope, est_item_id, element
        )
        if problem is not None:
            return {
                "fired": False,
                "skipped": problem.get("note") or problem.get("error")
                or "no recording resolved",
            }

        result = call_mc2_promote(recording_id)
        return {"fired": True, "recording_id": recording_id, **result}
    except Exception as exc:  # noqa: BLE001 — promotion is NON-FATAL, always
        return {"fired": False, "skipped": f"promotion error: {type(exc).__name__}: {exc}"}


@mcp_server.tool()
def promote_spine_transcript(project_code: str, key: str) -> dict[str, Any]:
    """Promote a meeting's transcript into the RAG store, so it is retrievable.

    The hosted counterpart of the stdio verb (#143 batch 5). It DELEGATES to
    mc-2's `POST /api/meetings/{recording_id}/promote-transcript` carrying YOUR
    token, so the promotion runs under your identity — this server holds no
    service key and runs no ingest pipeline of its own.

    `key` accepts three forms and tells you which one matched (`resolved_via`):
    a **recording_id** (the Fathom bigint — the endpoint's native key), a
    **meeting id** (the uuid `list_project_meetings` returns, translated here so
    the uuid-vs-bigint mix-up cannot reach the endpoint), or a **spine element
    key** (est_item_id / slug / unique framing substring), which is bridged to a
    meeting through the element's own cited Fathom source.

    TWO DIFFERENCES from the stdio verb worth knowing before relying on this:

    1. **It promotes a MEETING, not a tenant file.** The engine verb embeds the
       file at the element's `rel_path` (landing a `spine-promote` asset); this
       one promotes the Fathom transcript behind the meeting (landing a
       `fathom` asset). For most spine elements `rel_path` is a spine markdown
       file with no recording behind it at all — those return a clean note
       saying so rather than promoting the wrong thing.
    2. **Idempotency is mc-2's, not ours.** Re-promoting is safe (the upstream
       path is keyed on the recording and updates in place), and the return
       reports `already_promoted` when the meeting was already stamped, so a
       no-op is never mistaken for fresh work.

    Returns `{recording_id, resolved_via, meeting, promotion: {ok, ...}}`, or a
    structured `{note}`/`{error}` when nothing resolves. Never raises.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: a recording_id, a meeting id, or a spine element key.
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    recording_id, meeting, problem = resolve_recording_id(client, scope, key)
    if problem is not None:
        audit(client, "promote_spine_transcript", {"project_code": project_code, "key": key}, 0)
        return problem

    resolved_via = (
        "recording_id" if key.strip().isdigit()
        else "meeting_id" if (meeting and str(meeting.get("id")) == key.strip())
        else "element"
    )
    already = bool((meeting or {}).get("transcript_promoted_at"))

    audit_args = {
        "project_code": project_code,
        "key": key,
        "recording_id": recording_id,
        "resolved_via": resolved_via,
    }

    promotion = call_mc2_promote(recording_id)
    audit(client, "promote_spine_transcript", audit_args, 1 if promotion.get("ok") else 0)

    out: dict[str, Any] = {
        "project_code": scope.get("project_code"),
        "recording_id": recording_id,
        "resolved_via": resolved_via,
        "caller": caller_subject(),
        "promotion": promotion,
    }
    if meeting:
        out["meeting"] = {
            "meeting_id": meeting.get("id"),
            "title": meeting.get("title"),
            "meeting_date": meeting.get("meeting_date"),
        }
    if already:
        out["already_promoted"] = True
        out["note"] = (
            "this meeting was already stamped transcript_promoted_at before this "
            "call; re-promotion updates the existing asset in place."
        )
    return out


# WHERE THE COLUMN BOUNDARY ACTUALLY LIVES (verified live 2026-08-02, and NOT
# what the batch-2 brief assumed). The migration's per-column grant
# `UPDATE(important, note, layer, framing, serves)` is real but INERT: the
# table-level ACL already carries `authenticated=arwdDxtm`, and Postgres unions
# table- and column-level grants rather than intersecting them, so the blanket
# table grant subsumes the narrow one. Nothing is denied at the grant layer.
#
# What actually stops a `body`/`status`/`origin` write is the mc-2 #130
# COLUMN-GUARD TRIGGER (`spine_substance_column_guard`), which raises SQLSTATE
# P0130. Probed directly under the smoke user's JWT: UPDATE body -> P0130,
# UPDATE status -> P0130, UPDATE origin -> P0130, UPDATE important -> 200.
#
# That trigger is an ATTRIBUTION guard, not an authorization boundary: it only
# demands the writer name itself, and setting an `X-Spine-Writer` header
# satisfies it. Confirmed live — the same body UPDATE that fails with P0130
# succeeds with `X-Spine-Writer: probe` set. So engine-owned columns are
# protected from ACCIDENT here, not from INTENT. Closing that would mean
# revoking the table-wide UPDATE grant so the column grant becomes load-bearing.
# Recorded as a finding; no DB change was made by this batch.


@mcp_server.tool()
def set_spine_element(
    project_code: str,
    key: str,
    important: bool | None = None,
    note: str | None = None,
    layer: str | None = None,
    framing: str | None = None,
    serves: list[str] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Set `important`, `note`, `layer`, `framing` (title), `serves`, and/or
    `actor` on a spine element — the hosted port of the stdio verb (#143
    batch 2; `actor` added by #146/mig 126).

    `key` resolves to ONE live element (exact est_item_id, bare slug, or a
    distinct `framing` substring — the same discipline as `pull_spine_element`).
    Args left None are NOT touched: this is a partial update, and it can never
    null a field. `layer` is normalized through the canonical vocabulary, so
    'decision' and 'Decisions' land identically and the spine UI's by-layer
    filters keep working. `serves` rebinds the element to work-item ids; pass
    `[]` to unbind, and `binding` follows automatically ('live' when serves is
    non-empty, 'unbound' when empty) — the same rule the authored-element
    builders use.

    TWO DELIBERATE DIFFERENCES from the stdio verb, both worth knowing before
    you rely on this:

    1. **LIVE ROWS ONLY.** The engine verb applies layer/framing/serves to EVERY
       version of the element, because those are element-level facts and a
       partial write scatters one element's history (#47). The hosted UPDATE
       policy is `status='live'`, so superseded rows are unwritable here and
       only the live row moves. For an element with history, its superseded rows
       keep the OLD layer/framing/serves. The return says so explicitly
       (`versions_updated` / `superseded_untouched`) rather than implying a
       whole-element move. Use the stdio verb when the whole history must move.

    2. **TRANSCRIPT PROMOTION IS DELEGATED, AND USUALLY SKIPS.** Like the engine
       verb, a genuine `important` false->true transition fires a transcript
       promotion, engagement-only and strictly NON-FATAL — its outcome lands
       under `promotion` and can never turn the metadata write into an error.
       What differs is WHAT gets promoted: the engine embeds the tenant file at
       the element's `rel_path`, while this fires mc-2's promotion for the
       Fathom recording behind the element (see `promote_spine_transcript`).
       An element that cites no Fathom source — which is MOST of them — gets
       `promotion: {fired: false, skipped: ...}` with the reason, not a failure.
       Use the stdio verb when the tenant FILE is what must be embedded.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the element (est_item_id, bare slug, or unique framing substring).
        important: element-level importance flag (no promotion side effect).
        note: element-level annotation.
        layer: element kind, normalized to the canonical string.
        framing: retitle the element (est_item_id never changes).
        serves: work-item ids to bind to; `[]` unbinds.
        actor: who is speaking — partner | client | vendor | inferred
            (spec v04 authority ordering; tag deliberately).
    """
    if all(v is None for v in (important, note, layer, framing, serves, actor)):
        return {
            "note": "nothing to update (pass important/note/layer/framing/serves/actor)"
        }
    if actor is not None and actor.strip().lower() not in _ACTORS:
        return {"error": f"unknown actor {actor!r}; use one of {sorted(_ACTORS)}"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"note": f"no single live element matching {key!r} in {project_code!r}"}
    live = next((v for v in versions if v.get("status") == "live"), None)
    if live is None:
        return {"error": f"element {est_item_id!r} has no live version to update"}

    # PRE-STATE, captured BEFORE the patch: promotion fires only on a genuine
    # false->true transition, never when the element was already important. Read
    # off the live row the resolver already fetched — re-reading after the
    # UPDATE would make every flip look like a no-op.
    prior_important = bool(live.get("important"))

    patch: dict[str, Any] = {}
    if important is not None:
        patch["important"] = bool(important)
    if note is not None:
        patch["note"] = note
    canonical_layer = None
    if layer is not None:
        canonical_layer = canon_layer(layer)
        patch["layer"] = canonical_layer
    if framing is not None:
        patch["framing"] = framing
    if serves is not None:
        patch["serves"] = list(serves)
        patch["binding"] = "live" if serves else "unbound"
    if actor is not None:
        patch["actor"] = actor.strip().lower()

    audit_args = {
        "project_code": project_code,
        "key": key,
        "layer": canonical_layer,
        "framing": framing,
        "note": note,
    }
    try:
        result = (
            client.table("spine_substance")
            .update(patch)
            .eq("id", live["id"])
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, "set_spine_element", audit_args, 0)
        message = str(exc)
        if "P0130" in message:
            # Only reachable if a future edit adds an engine-owned column to the
            # patch — name the guard rather than leaking a bare SQLSTATE.
            return {
                "error": "the mc-2 #130 column guard rejected this update: "
                "body/status/origin are engine-owned and this verb must never "
                f"patch them. {message[:200]}"
            }
        return {"error": f"update failed: {type(exc).__name__}: {message[:400]}"}

    updated = result.data or []
    if not updated:
        # RLS matched no row: the live row is gone, or the caller is not a team
        # member. A 0-row UPDATE is a SUCCESS to PostgREST — never report it as one.
        audit(client, "set_spine_element", audit_args, 0)
        return {
            "error": f"0 rows updated for {est_item_id!r}. The UPDATE policy on "
            "spine_substance is `is_team_member() AND status='live'` — either the "
            "row is no longer live, or the caller is not a team member.",
            "est_item_id": est_item_id,
        }

    row = updated[0]
    superseded_count = sum(1 for v in versions if v.get("status") != "live")
    audit(client, "set_spine_element", audit_args, len(updated))
    out: dict[str, Any] = {
        "est_item_id": est_item_id,
        "important": row.get("important"),
        "note": row.get("note"),
        "caller": caller_subject(),
        "versions_updated": len(updated),
        # Say the quiet part out loud: the stdio verb would have moved these too.
        "superseded_untouched": superseded_count,
        # Fired ONLY on a genuine false->true flip, and never fatal — the
        # metadata write above has already committed by the time this runs.
        "promotion": _promotion_on_important_flip(
            client, scope, est_item_id, live,
            fired=(important is True and not prior_important),
        ),
    }
    if canonical_layer is not None:
        out["layer"] = row.get("layer")
    if framing is not None:
        out["framing"] = row.get("framing")
    if serves is not None:
        out["serves"] = row.get("serves")
        out["binding"] = row.get("binding")
    if actor is not None:
        out["actor"] = row.get("actor")
    if superseded_count:
        out["note_on_scope"] = (
            f"{superseded_count} superseded version(s) kept their prior "
            "layer/framing/serves — the hosted UPDATE policy is live-rows-only."
        )
    return out


def _match_open_commitment(
    open_rows: list[dict[str, Any]], key: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve `key` to exactly ONE row of `open_rows` — engine order.

    Exact id first, then a case-insensitive description substring. Returns
    `(row, None)` on a unique hit and `(None, error_payload)` otherwise; the
    ambiguity payload carries up to five candidates so the caller can re-key
    by id. Pure — no client, no I/O — so the batch verbs and the single verb
    share one matching truth and the contract is unit-testable.
    """
    matches = [r for r in open_rows if r.get("id") == key]
    if not matches:
        needle = (key or "").strip().lower()
        if needle:
            matches = [
                r for r in open_rows if needle in (r.get("description") or "").lower()
            ]
    if not matches:
        return None, {
            "error": f"no open commitment matches {key!r}",
            "open_count": len(open_rows),
        }
    if len(matches) > 1:
        return None, {
            "error": f"{len(matches)} open commitments match {key!r} — pass an id instead",
            "candidates": [
                {"id": r.get("id"), "description": (r.get("description") or "")[:80]}
                for r in matches[:5]
            ],
        }
    return matches[0], None


def _fetch_open_commitments(client, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """The project's OPEN commitment rows, engagement or initiative scoped."""
    column = "initiative_id" if scope["kind"] == "initiative" else "project_id"
    return (
        client.table("commitments")
        .select(COMMITMENT_COLUMNS)
        .eq(column, scope["id"])
        .eq("status", "open")
        .execute()
        .data
        or []
    )


def _close_commitment_row(client, row: dict[str, Any], outcome: str) -> dict[str, Any]:
    """UPDATE one commitment row to `outcome`, detecting the 0-row denial.

    The UPDATE policy is `using (status='open')`, so a row resolved
    concurrently matches ZERO rows — PostgREST reports that as success, and
    this helper converts it into an explicit error instead of a phantom win.
    """
    try:
        result = (
            client.table("commitments")
            .update(
                {
                    "status": outcome,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", row["id"])
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"update failed: {type(exc).__name__}: {str(exc)[:400]}"}
    updated = result.data or []
    if not updated:
        return {
            "error": f"0 rows updated for commitment {row['id']}. The UPDATE policy "
            "only matches OPEN commitments (`using status='open'`), so this one is "
            "already resolved or was closed concurrently — re-read it with "
            "list_commitments before retrying.",
            "commitment_id": row["id"],
        }
    return {
        "resolved": row["id"],
        "description": row.get("description"),
        "outcome": outcome,
        "status": updated[0].get("status"),
        "updated_at": updated[0].get("updated_at"),
    }


@mcp_server.tool()
def resolve_commitment(
    project_code: str, key: str, outcome: str = "done"
) -> dict[str, Any]:
    """Close an OPEN commitment: `outcome` 'done' (delivered) or 'dropped'.

    The hosted port of the stdio verb (#143 batch 2), with the engine's
    resolution semantics intact. `key` is a commitment id (exact) or a
    case-insensitive substring of the description, matched against the project's
    OPEN commitments only. Ambiguity is an ERROR that returns the candidates —
    never a guess — so a vague key makes you re-key by id rather than closing
    the wrong obligation.

    Commitments are never deleted: a dropped row stays as the archive, and its
    `cp_hash` keeps a re-ingest of the same meeting from resurrecting it. Sets
    `status` and `updated_at` (the table has no auto-update trigger, so
    `updated_at` is written explicitly, mirroring the mc-2 router).

    The UPDATE policy is `using (status='open')` with
    `with check (status IN ('done','dropped'))`, so a commitment that is already
    done/dropped matches ZERO rows. PostgREST reports that as a successful
    0-row update; this verb detects it and explains the denial instead of
    reporting a success that did not happen.

    Args:
        project_code: engagement or initiative code.
        key: a commitment id, or a distinct substring of its description.
        outcome: done | dropped.
    """
    if outcome not in ("done", "dropped"):
        return {"error": "outcome must be 'done' or 'dropped'"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    open_rows = _fetch_open_commitments(client, scope)
    row, match_err = _match_open_commitment(open_rows, key)
    if match_err is not None:
        if "no open commitment" in match_err.get("error", ""):
            match_err["error"] = (
                f"no open commitment in {project_code!r} matches {key!r}"
            )
        return match_err

    audit_args = {"project_code": project_code, "key": key, "outcome": outcome}
    closed = _close_commitment_row(client, row, outcome)
    if "error" in closed:
        audit(client, "resolve_commitment", audit_args, 0)
        return closed

    audit(client, "resolve_commitment", audit_args, 1)
    return {**closed, "caller": caller_subject()}


# The ingest's off-project detection annotation (webhook/commitments_propose.py
# #114): display-only, appended to the description, never part of cp_hash.
_OFF_PROJECT_ANNOTATION_RE = re.compile(r"\s*\[off-project\?\s*→\s*[^\]]+\]")


def _partition_off_project(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into (clean, off_project_flagged) by the #114 annotation."""
    flagged = [
        r for r in rows
        if _OFF_PROJECT_ANNOTATION_RE.search(r.get("description") or "")
    ]
    flagged_ids = {r.get("id") for r in flagged}
    return [r for r in rows if r.get("id") not in flagged_ids], flagged


def _routed_copy_row(
    row: dict[str, Any], source_code: str, target_scope: dict[str, Any]
) -> dict[str, Any]:
    """Build the target-project INSERT row for a routed commitment.

    The off-project annotation is STRIPPED (it described the mis-scope this
    route fixes) and a `[routed from <source-code>]` provenance marker is
    appended. Owner, direction, due date, ratification state, and the source
    meeting linkage all survive — the row keeps its history; only its home
    changes. `cp_hash` follows the hosted create_commitment semantics
    (unique-per-call, not the engine's content hash).
    """
    clean = _OFF_PROJECT_ANNOTATION_RE.sub("", row.get("description") or "").strip()
    copy: dict[str, Any] = {
        "description": f"{clean} [routed from {source_code}]",
        "owner_email": row.get("owner_email"),
        "owner_name": row.get("owner_name"),
        "direction": row.get("direction") or "internal",
        "due_date": row.get("due_date"),
        "date_status": row.get("date_status") or "proposed",
        "status": "open",
        "source_kind": row.get("source_kind") or "session",
        "source_meeting_id": row.get("source_meeting_id"),
        "cp_hash": uuid.uuid4().hex[:8],
    }
    if target_scope["kind"] == "initiative":
        copy["initiative_id"] = target_scope["id"]
    else:
        copy["project_id"] = target_scope["id"]
    return copy


def _resolve_commitment_batch(
    client, open_rows: list[dict[str, Any]], keys: list[str], outcome: str
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """The batch loop behind `resolve_commitments`, extracted for testability.

    Matches each key against a SHRINKING snapshot: a closed row leaves
    `open_rows`, so a substring can never re-match a row an earlier key took,
    and a key error never aborts the batch. Returns
    `(resolved_count, per_key_results, remaining_rows)`.
    """
    remaining = list(open_rows)
    results: list[dict[str, Any]] = []
    resolved = 0
    for key in keys:
        try:
            row, match_err = _match_open_commitment(remaining, key)
            if match_err is not None:
                results.append({"key": key, **match_err})
                continue
            closed = _close_commitment_row(client, row, outcome)
        except Exception as exc:  # noqa: BLE001 — one bad key must not abort the batch
            results.append({"key": key, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if "error" in closed:
            results.append({"key": key, **closed})
            continue
        resolved += 1
        remaining = [r for r in remaining if r.get("id") != row["id"]]
        results.append({"key": key, **closed})
    return resolved, results, remaining


@mcp_server.tool()
def resolve_commitments(
    project_code: str, keys: list[str], outcome: str = "done"
) -> dict[str, Any]:
    """Close several OPEN commitments in one call (#159) — batch cleanup.

    Each entry of `keys` resolves and closes exactly as `resolve_commitment`
    (exact id first, then a distinct description substring, matched against
    OPEN rows only), so a wrap-up sweep that closes forty delivered rows is
    ONE operation instead of forty.

    Per-key results are returned rather than a single verdict, and a miss does
    NOT abort the batch — the `retire_spine_elements` (#105) contract:
    resolving thirty-nine of forty rows should not be undone because the
    fortieth key was a typo. `results` carries {key, resolved, description,
    outcome} for each hit and {key, error, candidates?} for each miss.

    The open-row snapshot is fetched ONCE and each closed row leaves it, so a
    substring key can never re-match a row an earlier key already closed, and
    two keys naming the same row report the second as already-taken instead
    of double-writing.

    Returns {resolved: int, results: [...], remaining_open: int}.

    Args:
        project_code: engagement or initiative code.
        keys: commitment ids or distinct description substrings.
        outcome: done | dropped — applied to every key in the batch.
    """
    if outcome not in ("done", "dropped"):
        return {"error": "outcome must be 'done' or 'dropped'"}
    if not keys:
        return {"error": "at least one key is required"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    remaining = _fetch_open_commitments(client, scope)
    resolved, results, remaining = _resolve_commitment_batch(
        client, remaining, keys, outcome
    )

    # `keys` is a LIST of identifiers — logged as a COUNT, per the batch-2
    # audit rule; per-key detail belongs in the returned payload.
    audit(
        client,
        "resolve_commitments",
        {"project_code": project_code, "keys_count": len(keys), "outcome": outcome},
        resolved,
    )
    return {
        "resolved": resolved,
        "results": results,
        "remaining_open": len(remaining),
        "project_code": scope["project_code"],
        "caller": caller_subject(),
    }


@mcp_server.tool()
def resolve_commitments_by_meeting(
    project_code: str,
    meeting_ids: list[str],
    outcome: str = "done",
    except_keys: list[str] | None = None,
    dry_run: bool = False,
    include_off_project: bool = False,
) -> dict[str, Any]:
    """Close every OPEN commitment proposed by the named meetings (#159) —
    the delivery-event sweep.

    A build sprint's commitments are meeting-scoped tasks, and the delivery
    is the natural resolution event for all of them at once: "everything
    proposed from these three working sessions shipped Thursday night." This
    verb turns that sentence into one call instead of one call per row.

    `meeting_ids` are `source_meeting_id` values (list_commitments returns
    them). Rows whose source meeting is not in the list are untouched — rows
    with NO source meeting (manual/session rows) are never swept by this verb.

    `except_keys` protects still-live rows inside a swept meeting (id or
    distinct description substring). An except_key that matches nothing or
    ambiguously is a HARD error and nothing is written — an exclusion that
    silently failed would resolve exactly the row the caller meant to keep.

    Rows carrying the ingest's `[off-project? → <code>]` annotation are
    SKIPPED by default and reported under `off_project_skipped` — a delivery
    sweep must not close the very rows that belong to another project; route
    them first (`route_commitment`) or pass `include_off_project=true` to
    sweep them anyway.

    ALWAYS preview first: `dry_run=true` returns the would-resolve rows
    grouped by meeting, writes nothing, and is the confirm surface — show the
    groups, get a yes, then run with `dry_run=false`.

    Returns {groups: {meeting_id: [...]}, would_resolve|resolved: int,
    excepted: [...], results?: [...]}.

    Args:
        project_code: engagement or initiative code.
        meeting_ids: source_meeting_id values whose open rows should close.
        outcome: done | dropped — applied to every swept row.
        except_keys: rows inside the swept meetings to leave open.
        dry_run: True → report the sweep without writing (default False).
    """
    if outcome not in ("done", "dropped"):
        return {"error": "outcome must be 'done' or 'dropped'"}
    if not meeting_ids:
        return {"error": "at least one meeting_id is required"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    open_rows = _fetch_open_commitments(client, scope)
    wanted = set(meeting_ids)
    candidates = [r for r in open_rows if r.get("source_meeting_id") in wanted]

    # Exclusions resolve against the CANDIDATES (not all open rows): an
    # except_key exists to protect a row the sweep would otherwise take.
    excepted: list[dict[str, Any]] = []
    for ek in except_keys or []:
        row, match_err = _match_open_commitment(candidates, ek)
        if match_err is not None:
            return {
                "error": f"except_key {ek!r} did not resolve to one swept row — "
                "nothing was written. Fix the exclusion and re-run.",
                "detail": match_err,
            }
        excepted.append({"id": row["id"], "description": row.get("description")})
        candidates = [r for r in candidates if r.get("id") != row["id"]]

    off_project_skipped: list[dict[str, Any]] = []
    if not include_off_project:
        candidates, flagged = _partition_off_project(candidates)
        off_project_skipped = [
            {"id": r.get("id"), "description": r.get("description")} for r in flagged
        ]

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in candidates:
        groups.setdefault(r["source_meeting_id"], []).append(
            {
                "id": r.get("id"),
                "description": r.get("description"),
                "owner_name": r.get("owner_name"),
            }
        )

    if dry_run:
        return {
            "dry_run": True,
            "would_resolve": len(candidates),
            "groups": groups,
            "excepted": excepted,
            "off_project_skipped": off_project_skipped,
            "meetings_with_no_open_rows": sorted(wanted - set(groups)),
        }

    results: list[dict[str, Any]] = []
    resolved = 0
    for row in candidates:
        try:
            closed = _close_commitment_row(client, row, outcome)
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort the sweep
            results.append({"id": row.get("id"), "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if "error" in closed:
            results.append({"id": row.get("id"), **closed})
            continue
        resolved += 1
        results.append(closed)

    audit(
        client,
        "resolve_commitments_by_meeting",
        {
            "project_code": project_code,
            "meetings_count": len(meeting_ids),
            "outcome": outcome,
            "excepted_count": len(excepted),
        },
        resolved,
    )
    return {
        "resolved": resolved,
        "groups": groups,
        "excepted": excepted,
        "off_project_skipped": off_project_skipped,
        "results": results,
        "meetings_with_no_open_rows": sorted(wanted - set(groups)),
        "project_code": scope["project_code"],
        "caller": caller_subject(),
    }


@mcp_server.tool()
def route_commitment(
    project_code: str, key: str, target_code: str
) -> dict[str, Any]:
    """Move a mis-scoped OPEN commitment to the project it belongs to (#159
    part 3) — the action behind the ingest's `[off-project? → <code>]` flag.

    The ingest DETECTS mis-scoped rows but deliberately never auto-routes
    (detection is heuristic; #114). Until now the only disposition was a
    lossy drop on the wrong project. This verb completes the loop:

    1. a COPY of the row is inserted OPEN on the target project — the
       off-project annotation stripped, `[routed from <source-code>]`
       appended, and owner/direction/due-date/ratification/source-meeting
       linkage all preserved (the row keeps its history; only its home
       changes). The copy is a proposal on the target exactly like any
       ingested row — review-gated there, nothing auto-confirmed.
    2. only after the target insert SUCCEEDS is the source row closed as
       `routed` — a first-class terminal state (mc-2 mig 132 extended the
       status CHECK and the resolve policy's WITH CHECK), distinct from
       `dropped`: not abandoned, moved. The route is recorded on the
       surviving row, in the audit log, and in this payload.

    Insert-fails-leave-everything-untouched: a failed target write returns
    an error with the source row still open, so a bad target code can never
    strand the obligation.

    Args:
        project_code: the project the row currently (wrongly) lives on.
        key: commitment id or distinct description substring, open rows only.
        target_code: the engagement or initiative that should own it.
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}
    target = resolve_write_scope(client, target_code)
    if target is None:
        return {"error": f"no project or initiative resolves for target {target_code!r}"}
    if target["id"] == scope["id"]:
        return {"error": "target_code resolves to the SAME project — nothing to route"}

    open_rows = _fetch_open_commitments(client, scope)
    row, match_err = _match_open_commitment(open_rows, key)
    if match_err is not None:
        return match_err

    copy = _routed_copy_row(row, scope["project_code"], target)
    audit_args = {
        "project_code": project_code,
        "key": key,
        "target_code": target_code,
        "commitment_id": row["id"],
    }
    try:
        inserted = client.table("commitments").insert(copy).execute()
    except Exception as exc:  # noqa: BLE001
        audit(client, "route_commitment", audit_args, 0)
        return {
            "error": f"target insert failed — source row untouched: "
            f"{type(exc).__name__}: {str(exc)[:400]}"
        }
    new_row = (inserted.data or [{}])[0]

    closed = _close_commitment_row(client, row, "routed")
    if "error" in closed:
        # The copy exists; the source close was denied (likely resolved
        # concurrently). Surface both facts — do NOT report failure of the
        # route itself, the obligation is safely on the target.
        audit(client, "route_commitment", audit_args, 1)
        return {
            "routed": row["id"],
            "target_commitment_id": new_row.get("id"),
            "target_code": target["project_code"],
            "warning": "target copy created but the source row would not close: "
            + closed["error"],
            "caller": caller_subject(),
        }

    audit(client, "route_commitment", audit_args, 1)
    return {
        "routed": row["id"],
        "description": copy["description"],
        "source_code": scope["project_code"],
        "source_status": "routed",
        "target_code": target["project_code"],
        "target_commitment_id": new_row.get("id"),
        "caller": caller_subject(),
    }


@mcp_server.tool()
def set_spine_step(
    project_code: str,
    key: str,
    step_id: str,
    title: str | None = None,
    status: str | None = None,
    step_date: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Update one step on a spine element's trail (#119, hosted port).

    Advance a step (`status` ∈ done|active|upcoming) or edit its title/
    step_date/note. `key` resolves the parent element; `step_id` picks the step.
    Only the fields you pass change (None = untouched — this verb never nulls a
    field), matching the partial-update discipline of `set_spine_element`. The
    common move is advancing a step to `done` as the work lands.

    The UPDATE is scoped by (id, project_id, est_item_id) exactly as
    `cp_engine.spine_steps.set_step` does, so a stray `step_id` can never reach
    another element's trail even if the id is valid elsewhere.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the parent element (est_item_id or unique framing substring).
        step_id: the step to update.
        title: new title (non-blank).
        status: done | active | upcoming.
        step_date: free-form date ('7/16').
        note: annotation (≤8000 chars).
    """
    if status is not None and status not in STEP_STATUSES:
        return {"error": f"status must be one of {list(STEP_STATUSES)}"}
    if note is not None and len(note) > STEP_NOTE_MAX:
        return {"error": f"note exceeds {STEP_NOTE_MAX} characters"}

    patch: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            return {"error": "title cannot be blank"}
        patch["title"] = title.strip()
    if status is not None:
        patch["status"] = status
    if step_date is not None:
        patch["step_date"] = step_date
    if note is not None:
        patch["note"] = note
    if not patch:
        return {"note": "nothing to update (pass title/status/step_date/note)"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, err = resolve_live_element_id(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    audit_args = {
        "project_code": project_code,
        "key": key,
        "step_id": step_id,
        "status": status,
        "step_date": step_date,
        "title": title,
        "note": note,
    }
    try:
        result = (
            client.table("spine_steps")
            .update(patch)
            .eq("id", step_id)
            .eq("project_id", scope["id"])
            .eq("est_item_id", est_item_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, "set_spine_step", audit_args, 0)
        return {"error": f"step update failed: {type(exc).__name__}: {str(exc)[:400]}"}

    updated = result.data or []
    if not updated:
        audit(client, "set_spine_step", audit_args, 0)
        return {
            "error": f"0 rows updated — step {step_id!r} is not on element "
            f"{est_item_id!r} in this project (or the caller is not a team member).",
            "est_item_id": est_item_id,
        }

    audit(client, "set_spine_step", audit_args, len(updated))
    return {
        "est_item_id": est_item_id,
        "step_id": step_id,
        "caller": caller_subject(),
        "steps": read_steps(client, scope["id"], est_item_id),
    }


@mcp_server.tool()
def reorder_spine_step(
    project_code: str, key: str, order: list[str]
) -> dict[str, Any]:
    """Reorder a spine element's steps (#119, hosted port).

    `order` is the FULL list of the element's step_ids in the desired order;
    positions are renumbered 1..N to match. `key` resolves the parent element.

    The set is validated BEFORE anything is written: `order` must match the
    element's current step ids EXACTLY — no extras, no omissions, no duplicates.
    The engine helper renumbers whatever it is handed, which on a partial list
    silently leaves the omitted steps at stale positions (two steps sharing a
    position, or a gap). Hosted, a partial or foreign list is rejected with the
    difference spelled out, because a half-renumbered trail is worse than an
    unchanged one and there is no transaction here to roll back.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the parent element (est_item_id or unique framing substring).
        order: the complete list of this element's step_ids, in the new order.
    """
    if not order:
        return {"error": "order (the full list of step_ids) is required"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, err = resolve_live_element_id(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    existing = read_steps(client, scope["id"], est_item_id)
    current_ids = [s["id"] for s in existing]
    if len(set(order)) != len(order):
        return {"error": "order contains duplicate step_ids"}
    if set(order) != set(current_ids):
        return {
            "error": "order must list this element's steps EXACTLY once each — "
            "a partial reorder would leave the omitted steps at stale positions",
            "missing": sorted(set(current_ids) - set(order)),
            "unknown": sorted(set(order) - set(current_ids)),
            "expected_count": len(current_ids),
        }

    audit_args = {
        "project_code": project_code,
        "key": key,
        "order_len": len(order),
    }
    renumbered = 0
    try:
        for pos, sid in enumerate(order, start=1):
            client.table("spine_steps").update({"position": pos}).eq("id", sid).eq(
                "project_id", scope["id"]
            ).eq("est_item_id", est_item_id).execute()
            renumbered += 1
    except Exception as exc:  # noqa: BLE001
        audit(client, "reorder_spine_step", audit_args, renumbered)
        return {
            "error": f"reorder failed after {renumbered}/{len(order)} steps: "
            f"{type(exc).__name__}: {str(exc)[:300]}",
            "steps": read_steps(client, scope["id"], est_item_id),
        }

    audit(client, "reorder_spine_step", audit_args, renumbered)
    return {
        "est_item_id": est_item_id,
        "reordered": renumbered,
        "caller": caller_subject(),
        "steps": read_steps(client, scope["id"], est_item_id),
    }


@mcp_server.tool()
def remove_spine_step(
    project_code: str, key: str, step_id: str
) -> dict[str, Any]:
    """Delete one step from a spine element's trail (#119, hosted port).

    `key` resolves the parent element; `step_id` picks the step. Remaining steps
    densify to stay 1..N contiguous, exactly as
    `cp_engine.spine_steps.remove_step` does. The DELETE is scoped by
    (id, project_id, est_item_id) so a stray id cannot reach another element.

    `spine_steps` is the ONLY table on this server with an authenticated DELETE
    policy (`is_team_member()`), and it is deliberately narrow: a step is a
    lightweight progress marker, not a versioned record, so removing a
    mis-authored one is a correction rather than a loss of history. Nothing else
    here deletes — spine versions and commitments are superseded or resolved.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the parent element (est_item_id or unique framing substring).
        step_id: the step to delete.
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, err = resolve_live_element_id(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"error": f"no live element matching {key!r}"}

    audit_args = {"project_code": project_code, "key": key, "step_id": step_id}
    try:
        deleted = (
            client.table("spine_steps")
            .delete()
            .eq("id", step_id)
            .eq("project_id", scope["id"])
            .eq("est_item_id", est_item_id)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, "remove_spine_step", audit_args, 0)
        return {"error": f"step delete failed: {type(exc).__name__}: {str(exc)[:400]}"}

    if not deleted:
        audit(client, "remove_spine_step", audit_args, 0)
        return {
            "error": f"0 rows deleted — step {step_id!r} is not on element "
            f"{est_item_id!r} in this project (or the caller is not a team member).",
            "est_item_id": est_item_id,
        }

    # Densify: renumber the survivors to a contiguous 1..N.
    for pos, step in enumerate(read_steps(client, scope["id"], est_item_id), start=1):
        if step.get("position") != pos:
            client.table("spine_steps").update({"position": pos}).eq(
                "id", step["id"]
            ).execute()

    audit(client, "remove_spine_step", audit_args, len(deleted))
    return {
        "est_item_id": est_item_id,
        "removed": step_id,
        "caller": caller_subject(),
        "steps": read_steps(client, scope["id"], est_item_id),
    }


# ──────────────────────────────────────────────────────────────────────
#  #143 batch 3 — the sources / provenance quartet
# ──────────────────────────────────────────────────────────────────────
#
# Four verbs, one DB write path. Unlike batch 2 — where each verb fit a
# table-level UPDATE policy — `spine_substance.sources` is written ONLY through
# `spine_element_modify_source`, the guarded SECURITY DEFINER function shipped
# by `ratchet_batch3_element_sources_fn`. There are no grants on the column, so
# a direct PATCH is not merely discouraged here, it is impossible.
#
# That constraint buys back a semantic the hosted server otherwise loses. Batch
# 2's `set_spine_element` is live-rows-only (the UPDATE policy says
# `status='live'`), so its element-level fields DIVERGE across an element's
# history. These verbs do NOT have that gap: the function writes every version
# row, so hosted and stdio agree exactly — a source link attached here rides the
# whole history, which is what makes `versions_updated > 1` on an element with
# versions the assertion worth making.


@mcp_server.tool()
def add_element_source(
    project_code: str, key: str, source_title: str
) -> dict[str, Any]:
    """Attach an ingested source document to a spine element (#143 batch 3).

    `key` resolves to ONE LIVE element (exact est_item_id, bare slug, or a
    distinct `framing` substring — the same discipline as `pull_spine_element`);
    `source_title` resolves to ONE of the project's ACTIVE ingested sources
    (exact title first, else a unique case-insensitive substring, the query
    being a substring of the stored title). Ambiguity returns the candidate
    titles and never guesses — attaching the wrong provenance is worse than
    attaching none.

    Writes the typed link `{"type": "rag_asset", "id", "title"}` into `sources`
    on EVERY version row, exactly as the stdio verb and MC-2's dashboard do:
    a source is an element-level fact, like `serves`, so a partial write would
    scatter one element's provenance across its own history. Deduped by
    (type, id); re-attaching is a no-op reported as `already: true`.

    Use it to close attach-as-source loops — an Agreement whose body says
    "attach the signed SOW" with no attached source is exactly what
    `cp spine-lint` flags.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the element (est_item_id, bare slug, or unique framing substring).
        source_title: the ingested source's title (see `list_project_sources`).
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"note": f"no single live element matching {key!r}"}
    if _live_row(versions) is None:
        return {"error": f"element {est_item_id!r} has no live version"}

    asset, note = _resolve_active_asset(client, scope["id"], source_title)
    if note is not None:
        return note

    entry = {"type": "rag_asset", "id": asset["id"], "title": asset.get("title")}
    return _modify_element_sources(
        client,
        project_code,
        key,
        entry,
        add=True,
        tool="add_element_source",
        audit_args={
            "project_code": project_code,
            "key": key,
            "source_title": source_title,
            "asset_id": asset["id"],
        },
        scope=scope,
        est_item_id=est_item_id,
        versions=versions,
    )


@mcp_server.tool()
def remove_element_source(
    project_code: str, key: str, source_title: str
) -> dict[str, Any]:
    """Detach an ingested source document from a spine element (#143 batch 3).

    The inverse of `add_element_source`: resolves the element and the source the
    same way, then removes the matching `{"type": "rag_asset", ...}` link BY
    ASSET ID from every version's `sources`. Detaching a source that is not
    attached is NOT an error — it returns a structured note, because "already
    not there" is the outcome the caller wanted.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the element (est_item_id, bare slug, or unique framing substring).
        source_title: the ingested source's title.
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"note": f"no single live element matching {key!r}"}
    if _live_row(versions) is None:
        return {"error": f"element {est_item_id!r} has no live version"}

    asset, note = _resolve_active_asset(client, scope["id"], source_title)
    if note is not None:
        return note

    entry = {"type": "rag_asset", "id": asset["id"], "title": asset.get("title")}
    return _modify_element_sources(
        client,
        project_code,
        key,
        entry,
        add=False,
        tool="remove_element_source",
        audit_args={
            "project_code": project_code,
            "key": key,
            "source_title": source_title,
            "asset_id": asset["id"],
        },
        scope=scope,
        est_item_id=est_item_id,
        versions=versions,
    )


@mcp_server.tool()
def add_element_provenance(
    project_code: str, key: str, source_key: str
) -> dict[str, Any]:
    """Attach ANOTHER spine element as provenance to a spine element (#104).

    The tiering-rule counterpart to `add_element_source`: where that attaches an
    ingested `rag_asset`, this attaches a spine ELEMENT — the move for "this
    synthesis card absorbed these raw cards".

    The asymmetry between the two keys is the whole design and is deliberate:

      * `key` (the TARGET, the survivor) must resolve to ONE **live** element.
      * `source_key` (the folded-in raw material) resolves across ALL of the
        project's elements **including RETIRED ones** — that is the normal case,
        not an edge case, since the cleanup being recorded is usually "retire
        the raw card, keep its lineage".

    Writes `{"type": "spine_element", "id": <est_item_id>, "title": <framing>,
    "retired": <bool>}` into the target's `sources` on every version. Because
    the link is a property of the SURVIVING card, it outlives the source's
    retirement — closing the lineage hole where retire-and-lose-the-link was the
    only option. Deduped by (type, id), so an element link never collides with a
    rag_asset that happens to share the id. Re-attaching returns `already: true`.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the TARGET element — must be live.
        source_key: the element to fold in as provenance; MAY be retired.
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"note": f"no single live element matching {key!r}"}
    if _live_row(versions) is None:
        return {"error": f"element {est_item_id!r} has no live version"}

    src = resolve_source_element(client, scope["id"], source_key)
    if src is None:
        return {"note": f"no single element matching source {source_key!r}"}
    src_eid = src.get("est_item_id")
    if src_eid == est_item_id:
        return {"note": "an element cannot be its own provenance"}

    entry = {
        "type": "spine_element",
        "id": src_eid,
        "title": src.get("framing") or src_eid,
        "retired": bool(src.get("archived")),
    }
    return _modify_element_sources(
        client,
        project_code,
        key,
        entry,
        add=True,
        tool="add_element_provenance",
        audit_args={
            "project_code": project_code,
            "key": key,
            "source_key": source_key,
        },
        scope=scope,
        est_item_id=est_item_id,
        versions=versions,
    )


@mcp_server.tool()
def remove_element_provenance(
    project_code: str, key: str, source_key: str
) -> dict[str, Any]:
    """Detach a spine-element provenance link from a spine element (#104).

    The inverse of `add_element_provenance`: resolves the target (live) and the
    source (which may be retired) the same way, then removes the matching
    `{"type": "spine_element", ...}` link BY ELEMENT ID from every version's
    `sources`. Detaching one that is not attached returns a structured note, not
    an error.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the TARGET element — must be live.
        source_key: the provenance element to detach; MAY be retired.
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"note": f"no single live element matching {key!r}"}
    if _live_row(versions) is None:
        return {"error": f"element {est_item_id!r} has no live version"}

    src = resolve_source_element(client, scope["id"], source_key)
    if src is None:
        return {"note": f"no single element matching source {source_key!r}"}
    src_eid = src.get("est_item_id")

    entry = {
        "type": "spine_element",
        "id": src_eid,
        "title": src.get("framing") or src_eid,
        "retired": bool(src.get("archived")),
    }
    return _modify_element_sources(
        client,
        project_code,
        key,
        entry,
        add=False,
        tool="remove_element_provenance",
        audit_args={
            "project_code": project_code,
            "key": key,
            "source_key": source_key,
        },
        scope=scope,
        est_item_id=est_item_id,
        versions=versions,
    )


# ──────────────────────────────────────────────────────────────────────
#  Retire + account scope — the guarded-function verbs (#143 batch 4)
# ──────────────────────────────────────────────────────────────────────
#
# Batch 4 is the first set whose engine originals do their work with MULTI-STEP
# UPDATE/DELETE sequences rather than a single write. The stdio `retire_spine_
# element`, for instance, is four statements (archive every version, demote the
# live row, then a delete per edge direction) run with the service key. That
# shape cannot be ported verbatim: an authenticated caller has no blanket UPDATE
# grant on `spine_substance`, and even if it did, a sequence that fails halfway
# leaves an element archived-but-live or edges dangling from a dead endpoint —
# exactly the graph corruption #96 was filed about.
#
# So the two mutations move into SECURITY DEFINER functions
# (mig `ratchet_batch4_retire_and_scope_fns`), each ONE transaction:
#
#   spine_retire_element(p_project_id uuid, p_est_item_id text)
#       -> jsonb {versions, edges_removed}
#     Requires a LIVE unarchived element, archives every version, supersedes
#     the live rows, and DELETES the element's spine_relations edges (#96).
#
#   spine_set_element_scope(p_project_id uuid, p_est_item_id text,
#                           p_account boolean) -> integer (rows moved)
#     Engagements only (raises when the project has no company), with the
#     sibling-twin guard baked in: promoting a slug that already sits at
#     account scope from ANOTHER project raises rather than creating a twin.
#     Demote only touches account-scoped rows, so a non-account element
#     returns 0 — a note, not an error.
#
# Both raise P0001 with a `<fn_name>: <message>` prefix. `_guarded_fn_error`
# strips that prefix so the caller reads the sentence, not the plumbing.
#
# The THIRD mutation needs no function: batch 4's migration also added a team
# DELETE policy on `spine_relations`, so `retire_spine_relation` is a direct
# filtered delete — the row is fully identified by (project_id, kind,
# from_item_id, to_item_id) and RLS is the whole authorization story.


def _guarded_fn_error(exc: Exception) -> str:
    """A DB raise -> the sentence the function actually wrote.

    The guarded functions signal refusals with `RAISE EXCEPTION` (P0001), and
    postgrest-py surfaces that as an APIError whose string is a JSON blob with
    the message buried in it. These verbs' whole contract is that a refusal
    reads as a clean sentence ("engagements only — this project has no
    company"), so the message is dug out and the `<fn_name>: ` prefix the
    functions stamp on is stripped.
    """
    message = ""
    for attr in ("message", "details"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break
    if not message:
        raw = str(exc)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or parsed.get("details") or raw)
            else:
                message = raw
        except (json.JSONDecodeError, TypeError):
            message = raw
    # Strip the `spine_retire_element: ` / `spine_set_element_scope: ` prefix.
    for prefix in ("spine_retire_element: ", "spine_set_element_scope: "):
        if message.startswith(prefix):
            message = message[len(prefix):]
    return message[:400]


def _retire_one(client, project_id: str, key: str) -> dict[str, Any]:
    """Retire ONE live element via the guarded function.

    The shared body of `retire_spine_element` and `retire_spine_elements`,
    mirroring the engine's `_retire_one`: resolve, call, report. A resolution
    miss is a `{note}` and a DB refusal is an `{error}` — neither raises, so the
    batch verb can keep going past a bad key exactly like the engine's does.
    """
    est_item_id, versions, err = resolve_element_versions(client, project_id, key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"note": f"no single live element matching {key!r}"}
    if _live_row(versions) is None:
        return {"note": f"element {est_item_id!r} has no live version"}

    try:
        payload = (
            client.rpc(
                "spine_retire_element",
                {"p_project_id": project_id, "p_est_item_id": est_item_id},
            )
            .execute()
            .data
        ) or {}
    except Exception as exc:  # noqa: BLE001
        return {"est_item_id": est_item_id, "error": _guarded_fn_error(exc)}

    return {
        "est_item_id": est_item_id,
        "retired": True,
        "versions": int(payload.get("versions") or 0),
        "edges_removed": int(payload.get("edges_removed") or 0),
    }


@mcp_server.tool()
def retire_spine_element(project_code: str, key: str) -> dict[str, Any]:
    """Retire a spine element — remove it from the live spine, keeping history.

    The cleanup verb for duplicates and elements that no longer belong (the same
    source doc ingested twice, a raw card folded into a synthesis). `key`
    resolves to ONE **live** element — an exact est_item_id or a distinct
    `framing` substring, the same discipline as `pull_spine_element`.

    Every version is marked `archived=true` and the live version is superseded,
    so the element disappears from list/pull/resolve immediately and reaps from
    the repo mirror on next sync. Nothing is deleted: the element is recoverable
    via a dashboard un-archive.

    Its typed edges (`spine_relations`) ARE deleted, not archived (#96) — a
    retired element must not leave `active` edges dangling from a dead endpoint,
    which an agent walking the graph would still follow.

    LINEAGE, and why this verb is safe to use: retiring does NOT destroy the
    element's provenance links elsewhere. `add_element_provenance` writes the
    link into the SURVIVING card's `sources`, so folding a raw card into a
    synthesis and then retiring the raw card keeps the lineage legible — and a
    provenance link attached AFTER retirement rides `retired: true`. Retire the
    raw card, keep the trail.

    Returns {est_item_id, retired: true, versions, edges_removed}, or a
    structured {note} when the key resolves to no single live element.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        key: the element to retire (est_item_id or unique framing substring).
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    result = _retire_one(client, scope["id"], key)
    audit(
        client,
        "retire_spine_element",
        {"project_code": project_code, "key": key},
        int(result.get("versions") or 0),
    )
    if result.get("retired"):
        result["project_code"] = scope["project_code"]
        result["caller"] = caller_subject()
    return result


@mcp_server.tool()
def retire_spine_elements(project_code: str, keys: list[str]) -> dict[str, Any]:
    """Retire several spine elements in one call (#105) — batch cleanup.

    Each entry of `keys` resolves and retires exactly as `retire_spine_element`
    (archive every version, supersede the live row, cascade typed edges #96), so
    a slot cleanup that collapses many raw cards is ONE operation instead of N.

    Per-key results are returned rather than a single verdict, and a miss does
    NOT abort the batch — this is the engine's contract and the reason the verb
    exists: retiring nine of ten cards should not be undone because the tenth
    key was a typo. `results` carries {key, est_item_id, retired, versions,
    edges_removed} for each hit and {key, note} (or {key, error}) for each miss.

    Returns {retired: int, edges_removed: int, results: [...]}.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        keys: element keys (est_item_ids or unique framing substrings).
    """
    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}
    if not keys:
        return {"error": "at least one key is required"}

    results: list[dict[str, Any]] = []
    retired = 0
    edges_removed = 0
    versions_total = 0
    for key in keys:
        try:
            one = _retire_one(client, scope["id"], key)
        except Exception as exc:  # noqa: BLE001 — one bad key must not abort the batch
            results.append({"key": key, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if one.get("retired"):
            retired += 1
            edges_removed += int(one.get("edges_removed") or 0)
            versions_total += int(one.get("versions") or 0)
        results.append({"key": key, **one})

    # `keys` is a LIST of identifiers. It is logged as a COUNT, not as itself —
    # the same rule `order`/`order_len` set in batch 2: an audit row records the
    # shape of the write, and per-key detail belongs in the returned payload.
    audit(
        client,
        "retire_spine_elements",
        {"project_code": project_code, "keys_count": len(keys)},
        versions_total,
    )
    return {
        "retired": retired,
        "edges_removed": edges_removed,
        "results": results,
        "project_code": scope["project_code"],
        "caller": caller_subject(),
    }


@mcp_server.tool()
def retire_spine_relation(
    project_code: str, kind: str, from_key: str, to_key: str
) -> dict[str, Any]:
    """Delete a typed edge between two spine elements (#97).

    The inverse of `create_spine_relation`, and the fix for a mis-recorded edge
    (a `supersedes` that should have been `responds_to`). `kind` must be in the
    same closed vocabulary the create verb enforces — responds_to | supersedes |
    derives_from | informs | contradicts — rejected here rather than at the DB
    CHECK.

    Resolution deliberately TOLERATES A DEAD ENDPOINT, matching the engine: each
    key is first resolved to a live element, and if it does not resolve, it is
    used VERBATIM as an est_item_id. Without that fallback an edge orphaned by an
    older retire would be permanently uncleanable — the endpoint it names no
    longer resolves, so a live-only resolver could never name the row to delete.
    Pass the raw est_item_ids in that case.

    (Edges created by `retire_spine_element` from this point on cascade
    automatically (#96); this fallback is for edges left behind before that
    cascade existed, and for edges whose endpoint was retired by other means.)

    Authorization is the batch-4 team DELETE policy on `spine_relations` — no
    guarded function, because the row is fully identified by (project_id, kind,
    from_item_id, to_item_id) and RLS is the entire authorization story.

    Returns {kind, from_item_id, to_item_id, removed: int}, or a {note} when
    there is no such edge.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        kind: responds_to | supersedes | derives_from | informs | contradicts.
        from_key: the source element (est_item_id, framing substring, or a raw
            est_item_id when the endpoint is already retired).
        to_key: the target element, resolved the same way.
    """
    kind_n = (kind or "").strip().lower()
    if kind_n not in _RELATION_KINDS:
        return {"error": f"unknown relation kind {kind!r}; use one of {sorted(_RELATION_KINDS)}"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    # Live first, raw est_item_id as the fallback — a dead endpoint is expected.
    from_eid, _ = resolve_live_element_id(client, scope["id"], from_key)
    to_eid, _ = resolve_live_element_id(client, scope["id"], to_key)
    from_eid = from_eid or from_key
    to_eid = to_eid or to_key

    audit_args = {
        "project_code": project_code,
        "kind": kind_n,
        "from_key": from_key,
        "to_key": to_key,
    }

    try:
        removed_rows = (
            client.table("spine_relations")
            .delete()
            .eq("project_id", scope["id"])
            .eq("kind", kind_n)
            .eq("from_item_id", from_eid)
            .eq("to_item_id", to_eid)
            .execute()
            .data
        ) or []
    except Exception as exc:  # noqa: BLE001
        audit(client, "retire_spine_relation", audit_args, 0)
        return {"error": f"relation delete failed: {type(exc).__name__}: {str(exc)[:400]}"}

    removed = len(removed_rows)
    audit(client, "retire_spine_relation", audit_args, removed)
    if removed == 0:
        return {
            "note": f"no {kind_n} edge {from_eid} -> {to_eid} to remove",
            "kind": kind_n,
            "from_item_id": from_eid,
            "to_item_id": to_eid,
            "removed": 0,
        }
    return {
        "kind": kind_n,
        "from_item_id": from_eid,
        "to_item_id": to_eid,
        "removed": removed,
        "project_code": scope["project_code"],
        "caller": caller_subject(),
    }


def _company_id_for(client, scope: dict[str, Any]) -> str | None:
    """The company uuid behind a resolved write scope, or None.

    An initiative has none BY DEFINITION (that is what makes it an initiative),
    so `kind == "initiative"` short-circuits without a query. For a project the
    column is read explicitly — account scope is a COMPANY-level fact, and both
    stakeholder verbs need to know before they call the guarded function whether
    "engagements only" even applies.
    """
    if scope.get("kind") == "initiative":
        return None
    rows = (
        client.table("projects")
        .select("company_id")
        .eq("id", scope["id"])
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0].get("company_id") if rows else None


def _set_account_scope(
    client, project_code: str, key: str, *, account: bool, tool: str
) -> dict[str, Any]:
    """The shared body of promote/demote — resolve, guard, call, report.

    Both directions are the SAME guarded call with `p_account` flipped, so the
    resolution, the engagements-only translation, and the read-back live once.
    The direction-specific parts are the pre-checks (already-account vs
    not-account) and the returned shape.
    """
    scope = resolve_write_scope(client, project_code)
    audit_args = {"project_code": project_code, "key": key, "account": account}
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    # Every REFUSAL below is audited with row_count=0 before it returns, the
    # same discipline batch 3's already/not-attached paths follow. It matters
    # more here than it looks: `set_element_account_scope` DELEGATES to the
    # stakeholder verbs, so if only the successful write audited, a call that
    # was refused would leave no trace under the name the caller actually
    # invoked — the audit log would say a promote was attempted and never that
    # the type-agnostic verb was the thing that asked.
    company_id = _company_id_for(client, scope)
    if account and company_id is None:
        # Translated BEFORE the call rather than caught after: for an initiative
        # this is not a failure, it is the shape of the world.
        audit(client, tool, audit_args, 0)
        return {
            "note": "initiatives have no company — account promotion applies to "
            "engagements only"
        }

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        audit(client, tool, audit_args, 0)
        return err
    if est_item_id is None:
        audit(client, tool, audit_args, 0)
        return {"note": f"no single live element matching {key!r} in {project_code!r}"}
    live = _live_row(versions)
    if live is None:
        audit(client, tool, audit_args, 0)
        return {"note": f"element {est_item_id!r} has no live version"}

    current_scope = (live.get("scope") or "project").lower()
    if account and current_scope == "account":
        audit(client, tool, audit_args, 0)
        return {"note": f"{est_item_id!r} is already account-scoped", "est_item_id": est_item_id}
    if not account and current_scope != "account":
        # The function would return 0 rows here; say what that means rather
        # than reporting a write that moved nothing.
        audit(client, tool, audit_args, 0)
        return {
            "note": f"{est_item_id!r} is not account-scoped — nothing to demote",
            "est_item_id": est_item_id,
        }

    try:
        moved = (
            client.rpc(
                "spine_set_element_scope",
                {
                    "p_project_id": scope["id"],
                    "p_est_item_id": est_item_id,
                    "p_account": account,
                },
            )
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001
        audit(client, tool, audit_args, 0)
        return {"error": _guarded_fn_error(exc), "est_item_id": est_item_id}

    rows_moved = int(moved or 0)
    if rows_moved == 0:
        audit(client, tool, audit_args, 0)
        return {
            "note": f"no version rows moved for {est_item_id!r} — it may have been "
            "re-scoped between resolution and write",
            "est_item_id": est_item_id,
        }

    audit(client, tool, audit_args, rows_moved)
    result: dict[str, Any] = {
        "est_item_id": est_item_id,
        "scope": "account" if account else "project",
        "versions_moved": rows_moved,
        "layer": live.get("layer"),
        "project_code": scope["project_code"],
        "caller": caller_subject(),
    }
    if account:
        result["company_id"] = company_id
    else:
        result["returned_to_project_id"] = live.get("project_id") or scope["id"]
    return result


@mcp_server.tool()
def promote_stakeholder(project_code: str, key: str) -> dict[str, Any]:
    """Promote a project's stakeholder element to ACCOUNT scope.

    Stakeholders are account-level people wearing project clothes: promotion
    makes the element readable from EVERY project of the company (it appears in
    their list/pull with `scope='account'`), while `project_id` stays as
    provenance — there is always exactly one home to return to. Every version of
    the element moves together, the same element-level discipline as
    layer/framing/serves.

    Opt-in and human-triggered. Engagement-specific reads that should NOT travel
    to sibling projects belong in a separate project-scoped element.

    Engagements only — an initiative has no company, which is reported as a
    structured note rather than an error.

    The SIBLING-TWIN guard is enforced inside the guarded function: if the same
    slug already sits at account scope having been promoted from ANOTHER
    project, this refuses rather than creating a duplicate person — version the
    existing account element instead.

    LAYER IS A WARNING, NOT A GATE: promoting an element whose layer is not
    Stakeholders still applies, and rides a `warning` field. The verb is named
    for its usual subject, but nothing about account scope is stakeholder-only —
    `set_element_account_scope` is the same move without the sanity check.

    Returns {est_item_id, scope, company_id, layer, versions_moved[, warning]},
    or a structured {note}/{error}.

    Args:
        project_code: the engagement the element lives in.
        key: the element to promote (est_item_id or unique framing substring).
    """
    client = user_client()
    result = _set_account_scope(
        client, project_code, key, account=True, tool="promote_stakeholder"
    )
    layer = (result.get("layer") or "") if isinstance(result, dict) else ""
    if result.get("scope") == "account" and layer.lower() not in ("stakeholders", "stakeholder"):
        result["warning"] = (
            f"layer is {result.get('layer')!r}, not Stakeholders — promotion applied, "
            "but check this is really an account-level element"
        )
    return result


@mcp_server.tool()
def demote_stakeholder(project_code: str, key: str) -> dict[str, Any]:
    """Remove an element from ACCOUNT scope — the inverse of promote_stakeholder.

    The element returns to its PROVENANCE project (`scope='project'`,
    `company_id` cleared). `project_id` was never changed by promotion, so there
    is exactly one home for it to land in. It disappears from sibling projects'
    spines and from the account roster; NOTHING is deleted, and re-promoting
    restores account visibility. Every version moves together.

    `key` resolves the account element from ANY of the company's projects.
    Demoting something that is not account-scoped is a structured note, not an
    error — the guarded function touches account-scoped rows only, so that case
    moves zero rows by design.

    Returns {est_item_id, scope, returned_to_project_id, versions_moved}, or a
    structured {note}/{error}.

    Args:
        project_code: an engagement of the company the element is scoped to.
        key: the element to demote (est_item_id or unique framing substring).
    """
    client = user_client()
    return _set_account_scope(
        client, project_code, key, account=False, tool="demote_stakeholder"
    )


@mcp_server.tool()
def set_element_account_scope(
    project_code: str, key: str, account: bool = True
) -> dict[str, Any]:
    """Tag ANY spine element account-level (or return it to project scope).

    The type-agnostic generalization of `promote_stakeholder`/`demote_stakeholder`:
    use it to make a synthesis, a source, a decision — any element, not just a
    stakeholder — readable from EVERY project of the same company
    (`account=True`), or to pull it back to its home project (`account=False`).

    Delegates to the two stakeholder verbs, exactly as the engine's original
    does, so all three share one implementation and one set of guards. The only
    difference is the layer sanity-check, which belongs to the stakeholder-named
    verb: `promote_stakeholder` warns when the layer is not Stakeholders, and
    this one does not, because "any element" is the whole point.

    Engagements only; every version moves together; provenance project unchanged.

    Args:
        project_code: the engagement the element lives in.
        key: the element (est_item_id or unique framing substring).
        account: True to promote to account scope, False to return it to project.
    """
    client = user_client()
    tool = "set_element_account_scope"
    return _set_account_scope(client, project_code, key, account=account, tool=tool)


@mcp_server.tool()
def create_note(
    project_code: str,
    body: str,
    title: str | None = None,
    recipient_email: str | None = None,
) -> dict[str, Any]:
    """Create a partner Note against a project, under the caller's identity.

    INSERT-only into `public.notes`. The Notes feature's identity model is the
    `entities` registry (author_id and recipient_id are FK->entities), and the
    caller is bridged to their own entity row BY EMAIL — the same bridge the
    mc-2 backend's `_acting_entity` uses. The INSERT policy enforces
    `author_id = caller_entity_id()` (a definer helper doing that email
    lookup), so self-attribution is Postgres-enforced without repointing the
    feature's FKs. Decided with Drew 2026-08-02.

    `recipient_email` addresses the note to another entity (partner ping);
    omitted, the note is a self-note (recipient = the author's own entity).
    Slack delivery is NOT triggered from here (`slack_delivery='skipped'`) —
    the hosted path records; the mc-2 backend owns DM side effects.

    There is NO `title` column on `notes`; `title`, when given, is prepended
    to the body as a markdown H3 — the body is markdown and renders in-app.
    """
    text = (body or "").strip()
    if not text:
        return {"error": "body is required"}
    if title and title.strip():
        text = f"### {title.strip()}\n\n{text}"

    client = user_client()
    subject = caller_subject()
    if not subject:
        return {"error": "no authenticated caller in context"}

    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    try:
        entity_id = (client.rpc("caller_entity_id").execute().data) or None
    except Exception as exc:  # noqa: BLE001
        return {"error": f"entity lookup failed: {type(exc).__name__}: {str(exc)[:200]}"}
    if not entity_id:
        return {
            "error": "no entities row matches your login email — the Notes "
            "feature identifies people via the entities registry. Ask a "
            "partner to add you (mc-2 → entities) and retry."
        }

    recipient_id = entity_id
    if recipient_email and recipient_email.strip():
        found = (
            client.table("entities")
            .select("id, name")
            .ilike("email", recipient_email.strip())
            .limit(1)
            .execute()
        )
        if not found.data:
            return {"error": f"no entities row with email {recipient_email!r}"}
        recipient_id = found.data[0]["id"]

    row = {
        "id": str(uuid.uuid4()),
        "project_code": project_code,
        "author_id": entity_id,
        "recipient_id": recipient_id,
        "body": text,
        "status": "unread",
        "slack_delivery": "skipped",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        result = client.table("notes").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        audit(client, "create_note", {"project_code": project_code, "body": text}, 0)
        return {"error": f"insert failed: {type(exc).__name__}: {message[:400]}"}

    created = (result.data or [{}])[0]
    audit(client, "create_note", {"project_code": project_code, "body": text}, 1)
    return {
        "note_id": created.get("id", row["id"]),
        "project_code": project_code,
        "caller": subject,
        "status": created.get("status", "unread"),
        "body_chars": len(text),
        "created_at": created.get("created_at"),
    }


@mcp_server.tool()
def create_commitment(
    project_code: str,
    description: str,
    owner_email: str | None = None,
    due_date: str | None = None,
    direction: str = "internal",
) -> dict[str, Any]:
    """Register a dated commitment, under the caller's identity.

    INSERT-only into `public.commitments`, mirroring the row shape
    `cp_engine.commitments.write_commitment` builds: it lands as a PROPOSAL
    (`date_status='proposed'`, `status='open'`, `source_kind='session'`) — the
    same review gate the meeting auto-ingest path uses. Nothing is auto-confirmed.

    The owner column is chosen by KIND, not guessed: the table carries a
    `num_nonnulls(project_id, initiative_id) = 1` CHECK, so an initiative code
    writes `initiative_id` and an engagement code writes `project_id`. Getting
    this wrong is a constraint violation, not a silent mis-file.

    `due_date` must be ISO `YYYY-MM-DD` or omitted. An unparseable date is
    REJECTED rather than dropped or guessed — an invented deadline is worse than
    an undated row, which downstream flags as "needs a date".

    Unlike `cp mcp`'s verb this does NOT dedupe on a content hash: `cp_hash`
    dedupe reads existing rows to decide, and re-implementing that check here
    would diverge from the engine's hash derivation. A `cp_hash` is written
    (uuid-derived, unique) so the column is populated and the partial unique
    index is satisfied, but re-creating identical text WILL create a second row.

    Args:
        project_code: engagement or initiative code (standalone repos can't own
                      commitments — the table has no column for them).
        description: what is owed.
        owner_email: who owes it (an email; stored as `owner_email`).
        due_date: ISO `YYYY-MM-DD`, or omitted if no date was agreed.
        direction: us_to_them | them_to_us | internal.
    """
    text = (description or "").strip()
    if not text:
        return {"error": "description is required"}
    direction = (direction or "").strip() or "internal"
    if direction not in _DIRECTIONS:
        return {"error": f"direction must be one of {sorted(_DIRECTIONS)}"}

    due_iso = None
    if due_date and str(due_date).strip():
        due_iso = valid_due_date(due_date)
        if due_iso is None:
            return {
                "error": f"due_date {due_date!r} is not an ISO date (YYYY-MM-DD); "
                "omit it if no date was agreed"
            }

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    # #157: resolve the owner email against the entities person roster so
    # the stored row carries the canonical display name (the commitments
    # filter labels rows by owner_name and keys on the email). Best-effort:
    # an unknown email stays name-less rather than blocking the insert.
    email_norm = (owner_email or "").strip().lower() or None
    resolved_name: str | None = None
    if email_norm:
        try:
            ent = (
                client.table("entities")
                .select("name")
                .ilike("email", email_norm)
                .in_("kind", ["staff", "freelancer"])
                .limit(1)
                .execute()
            )
            if ent.data:
                resolved_name = (ent.data[0].get("name") or "").strip() or None
        except Exception:  # noqa: BLE001 — enrichment only
            pass

    row: dict[str, Any] = {
        "description": text,
        "owner_email": email_norm,
        "owner_name": resolved_name,
        "direction": direction,
        "due_date": due_iso,
        "date_status": "proposed",
        "status": "open",
        "source_kind": "session",
        # See the docstring: a unique-per-call hash, NOT the engine's content
        # hash — this path does not claim the engine's dedupe semantics.
        "cp_hash": uuid.uuid4().hex[:8],
    }
    if scope["kind"] == "initiative":
        row["initiative_id"] = scope["id"]
    else:
        row["project_id"] = scope["id"]

    try:
        result = client.table("commitments").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        audit(client, "create_commitment", {"project_code": project_code, "description": text}, 0)
        return {"error": f"insert failed: {type(exc).__name__}: {str(exc)[:400]}"}

    created = (result.data or [{}])[0]
    audit(
        client,
        "create_commitment",
        {
            "project_code": project_code,
            "description": text,
            "due_date": due_iso,
            "direction": direction,
            "owner_email": row["owner_email"],
        },
        1,
    )
    return {
        "commitment_id": created.get("id"),
        "project_code": project_code,
        "scope_kind": scope["kind"],
        "caller": caller_subject(),
        "description_chars": len(text),
        "owner_email": created.get("owner_email"),
        "due_date": created.get("due_date"),
        "date_status": created.get("date_status"),
        "status": created.get("status"),
        "direction": created.get("direction"),
    }


@mcp_server.tool()
def create_spine_element(
    project_code: str,
    framing: str,
    body: str,
    layer: str = "note",
    slug: str | None = None,
    important: bool = False,
    note: str | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a new AUTHORED spine element (live v1), under the caller's identity.

    INSERT-only into `spine_substance`, building the row shape
    `spine_authoring.authored_element.build_create_rows` produces — copied, not
    imported (this prototype stays off the cp_engine import path). The
    engine-owned values are taken verbatim from that builder and confirmed
    against live `_authored/%` rows rather than invented:

        id           = "<project_code>/<est_item_id>/v1"   (the composite key)
        est_item_id  = "_authored/<slug>"                  (the authored convention)
        origin       = "authored"
        placement    = "context"
        binding      = "unbound"   (nothing to bind — `serves` is not exposed here)
        status       = "live"
        version_label= "v1"
        layer        = canon_layer(layer)

    `author_id` is stamped with the caller's `sub`, as the INSERT policy
    `is_team_member() AND author_id = auth.uid()` requires. Unlike `notes`, this
    column has no conflicting FK, so the policy is satisfiable.

    BOTH `project_id` and `project_code` are written, and consistently: the
    `project_code` is the canonical dir-slug read off the project's existing
    spine rows, NOT the caller's short code — see `resolve_write_scope`. Writing
    the short form would fork a second project_code for one project, which is
    precisely the drift already on record for this corpus.

    Creation is guarded against clobbering: an existing element with the same
    `est_item_id` under the same `project_id` is reported rather than
    overwritten. The scope is the UUID, not the code string, because the
    caller's code may differ from the stored slug and a code-scoped check would
    MISS the collision.

    NOT DONE HERE (and deliberately): the engine's auto-journalled spine step.
    That writes `spine_steps` under a different policy surface and is out of
    scope for the insert-only subset.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        framing: the human-facing label/title line.
        body: the element's content (markdown).
        layer: element kind — email|note|decision|source|brief|stakeholder|
               agreement|synthesis|output|activity|retrospective|research|
               deliverable|timeline|clientfeedback (normalized to canonical form).
        slug: optional explicit slug; defaults to `slugify(framing)`.
        important: element-level importance flag.
        note: optional element-level annotation.
    """
    label = (framing or "").strip()
    if not label:
        return {"error": "framing is required"}
    if not (body or "").strip():
        return {"error": "body is required"}

    client = user_client()
    subject = caller_subject()
    if not subject:
        return {"error": "no authenticated caller in context"}

    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    element_slug = slugify(slug or label)
    est_item_id = f"_authored/{element_slug}"
    canonical_code = scope["project_code"]

    # Collision guard, scoped by UUID (see docstring).
    try:
        existing = (
            client.table("spine_substance")
            .select("id, version_label, status")
            .eq("project_id", scope["id"])
            .eq("est_item_id", est_item_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        existing = []
    if existing:
        return {
            "error": f"an element {est_item_id!r} already exists in this project; "
            "adding a version requires superseding the prior live row, which is "
            "an UPDATE — deferred by design (no authenticated UPDATE policy on "
            "spine_substance).",
            "existing_id": existing[0].get("id"),
        }

    now = datetime.now(timezone.utc)
    row = {
        "id": f"{canonical_code}/{est_item_id}/v1",
        "project_id": scope["id"],
        "project_code": canonical_code,
        "est_item_id": est_item_id,
        "est_item_kind": None,
        "phase": None,
        "binding": "unbound",
        "layer": canon_layer(layer),
        "placement": "context",
        "serves": [],
        "version_label": "v1",
        "version_date": now.date().isoformat(),
        "status": "live",
        "framing": label,
        "body": body,
        # Provenance must ride the INSERT: there is no authenticated UPDATE
        # path on spine_substance, so post-insert attachment is impossible.
        # Entries follow the engine's typed-link shape:
        # {"type": "rag_asset", "id": <asset uuid>, "title": <title>}.
        "sources": sources or [],
        "origin": "authored",
        "version_note": None,
        "rel_path": None,
        "important": bool(important),
        "note": note,
        # The policy's requirement — and, unlike notes, unconflicted.
        "author_id": subject,
    }

    try:
        result = client.table("spine_substance").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        audit(
            client,
            "create_spine_element",
            {"project_code": project_code, "slug": est_item_id, "body": body, "framing": label},
            0,
        )
        return {"error": f"insert failed: {type(exc).__name__}: {str(exc)[:400]}"}

    created = (result.data or [{}])[0]
    audit(
        client,
        "create_spine_element",
        {
            "project_code": project_code,
            "slug": est_item_id,
            "layer": row["layer"],
            "body": body,
            "framing": label,
        },
        1,
    )
    return {
        "row_id": created.get("id", row["id"]),
        "element_id": est_item_id,
        "project_code": canonical_code,
        "requested_code": project_code,
        "project_id": scope["id"],
        "caller": subject,
        "version_label": "v1",
        "status": "live",
        "layer": row["layer"],
        "framing": label,
        "body_chars": len(body),
    }


@mcp_server.tool()
def add_spine_version(
    project_code: str,
    element_id: str,
    body: str,
    version_note: str | None = None,
) -> dict[str, Any]:
    """Add a new version to an existing authored spine element (cp-engine #142).

    Two steps, mirroring the engine verb's semantics exactly:

    1. INSERT the new live version row — vN+1, carrying forward the live row's
       framing/layer/serves/sources/important/note (a routine bump must not
       drop provenance, #110), `author_id` stamped from the JWT and enforced
       by the INSERT policy.
    2. Demote the prior live row via `spine_supersede_prior_versions(new_id)` —
       a SECURITY DEFINER function that is the ONLY authenticated write path
       to engine-owned `status`, and can only perform live->superseded on
       sibling versions of an element whose new live row already exists.
       There is still no authenticated UPDATE grant on `spine_substance`.

    Team-wide by decision (2026-08-02): any team member may supersede any
    element's prior version — the same trust model as a Claude Code session;
    the new row's `author_id` records who did it.

    Ordering note: between steps 1 and 2 the element briefly has two live
    rows; the supersede function demotes every live sibling except the new id,
    so the end state is consistent even if a concurrent bump interleaves.

    Auto-journals the move (#143): on success it upserts ONE `source='auto'`,
    `review='proposed'` step for TODAY on this element's trail, so a
    content-write always leaves an activity record without a manual wrap-up
    proposal. A second bump of the same element the same day RETITLES that step
    rather than stacking a row. Title falls back to `version_note`, else
    "Updated <framing> (v<N>)". The auto-step is NON-FATAL: any failure
    surfaces under `step` in the return, never as a tool `{error}` — a journal
    miss must never fail the version write that triggered it.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        element_id: the element's est_item_id (`_authored/<slug>`), bare slug,
            or a distinct framing substring — same keys the read path takes.
        body: the new version's full body (markdown).
        version_note: optional "what changed" line, stored on the new version.
    """
    if not (body or "").strip():
        return {"error": "body is required"}

    client = user_client()
    subject = caller_subject()
    if not subject:
        return {"error": "no authenticated caller in context"}

    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    # Resolve the element within the project by UUID scope; accept the same
    # key forms the read path does (est_item_id, bare slug, framing substring).
    key = (element_id or "").strip()
    if not key:
        return {"error": "element_id is required"}

    est_item_id, versions, err = resolve_element_versions(client, scope["id"], key)
    if err is not None:
        return err
    if est_item_id is None:
        return {"error": f"no authored element {key!r} in {project_code!r}"}
    base = next((v for v in versions if v.get("status") == "live"), versions[0])
    nums = [
        int(str(v.get("version_label", ""))[1:])
        for v in versions
        if str(v.get("version_label", "")).startswith("v")
        and str(v.get("version_label", ""))[1:].isdigit()
    ]
    next_n = (max(nums) + 1) if nums else 1
    # Carry the row's OWN canonical code, not the caller's form (slug drift).
    row_code = base.get("project_code") or scope["project_code"]

    now = datetime.now(timezone.utc)
    new_id = f"{row_code}/{est_item_id}/v{next_n}"
    row = {
        "id": new_id,
        "project_id": base.get("project_id") or scope["id"],
        "project_code": row_code,
        "est_item_id": est_item_id,
        "est_item_kind": None,
        "phase": base.get("phase"),
        "binding": base.get("binding") or "unbound",
        "layer": base.get("layer"),
        "placement": base.get("placement") or "context",
        "serves": base.get("serves") or [],
        "version_label": f"v{next_n}",
        "version_date": now.date().isoformat(),
        "status": "live",
        "framing": base.get("framing"),
        "body": body,
        "sources": base.get("sources") or [],
        "origin": base.get("origin") or "authored",
        "version_note": version_note,
        "rel_path": None,
        "important": bool(base.get("important", False)),
        "note": base.get("note"),
        "scope": base.get("scope"),
        "author_id": subject,
    }
    try:
        client.table("spine_substance").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        audit(client, "add_spine_version", {"project_code": project_code, "element_id": key, "body": body}, 0)
        return {"error": f"version insert failed: {type(exc).__name__}: {str(exc)[:400]}"}

    try:
        demoted = client.rpc("spine_supersede_prior_versions", {"p_new_id": new_id}).execute().data
    except Exception as exc:  # noqa: BLE001
        # The new row exists but the prior live row was not demoted — surface
        # loudly; two live versions is exactly the state to not leave silent.
        audit(client, "add_spine_version", {"project_code": project_code, "element_id": key, "body": body}, 1)
        return {
            "error": f"new version {new_id} inserted but supersede failed: "
            f"{type(exc).__name__}: {str(exc)[:300]} — the element now has two "
            "live rows; retry or flag it.",
            "new_id": new_id,
        }

    audit(client, "add_spine_version", {"project_code": project_code, "element_id": key, "body": body}, 1)
    result: dict[str, Any] = {
        "element_id": est_item_id,
        "version_label": f"v{next_n}",
        "superseded": demoted,
        "project_code": row_code,
        "caller": subject,
        "version_note": version_note,
        "body_chars": len(body),
    }
    # Auto-journal the move as a review-gated step. Title priority mirrors the
    # engine verb (minus its `step_title` arg, which this tool does not take):
    # version_note > derived "Updated <framing> (v<N>)".
    try:
        step_title = version_note or (
            f"Updated {base.get('framing') or est_item_id} (v{next_n})"
        )
        result["step"] = upsert_auto_step(
            client,
            row["project_id"],
            est_item_id,
            step_title,
            step_date=now.date().isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 — journaling is non-fatal
        result["step"] = {"error": f"auto-step failed: {type(exc).__name__}: {str(exc)[:300]}"}
    return result


@mcp_server.tool()
def add_spine_document(
    project_code: str,
    label: str,
    content: str | None = None,
    source_title: str | None = None,
    type: str = "synthesis",
) -> dict[str, Any]:
    """Author a whole DOCUMENT into the spine as a new element (#140).

    The hosted counterpart of the engine's `add_spine_document`, with the
    `content=` form the design called for (a phone has no file_path). Provide
    exactly ONE of:

    - `content` — the document text itself (a draft this conversation just
      produced, a pasted email, meeting notes). This is Phase 3's
      "read spine context -> draft -> write back" loop closing.
    - `source_title` — an ALREADY-INGESTED source's title (resolved like
      `list_project_sources`: exact match first, else unique substring). Its
      full text (assembled from chunks) becomes the element body, and the
      source is attached as a typed provenance link on the new element —
      "turn this ingested brief into a spine card."

    `type` is the element kind (default `synthesis`). To UPDATE an existing
    element from a document, use `add_spine_version` instead.
    """
    if bool(content and content.strip()) == bool(source_title and source_title.strip()):
        return {"error": "provide exactly one of content or source_title"}

    client = user_client()
    scope = resolve_write_scope(client, project_code)
    if scope is None:
        return {"error": f"no project or initiative resolves for code {project_code!r}"}

    sources_link: list[dict[str, Any]] | None = None
    if source_title:
        want = source_title.strip()
        rows = (
            client.table("rag_assets")
            .select("id, title, status")
            .or_(f"project_id.eq.{scope['id']},initiative_id.eq.{scope['id']}")
            .is_("archived_at", "null")
            .execute()
            .data
            or []
        )
        exact = [r for r in rows if (r.get("title") or "").strip().lower() == want.lower()]
        matches = exact or [r for r in rows if want.lower() in (r.get("title") or "").lower()]
        if not matches:
            return {"error": f"no ingested source matches {want!r} in {project_code!r}"}
        if len(matches) > 1:
            return {
                "error": f"{want!r} matches {len(matches)} sources — be more specific",
                "matches": sorted((m.get("title") or "?") for m in matches)[:10],
            }
        asset = matches[0]
        pulled = pull_project_source(asset_id=asset["id"])
        body_text = pulled.get("text") or pulled.get("body") or ""
        if not str(body_text).strip():
            return {
                "error": f"source {asset.get('title')!r} resolved but has no "
                f"assembled text ({pulled.get('error') or 'no chunks'})"
            }
        content = str(body_text)
        sources_link = [{"type": "rag_asset", "id": asset["id"], "title": asset.get("title")}]

    created = create_spine_element(
        project_code=project_code,
        framing=label,
        body=content or "",
        layer=type,
        sources=sources_link,
    )
    if "error" in created:
        return created
    result = dict(created)
    if sources_link:
        result["source_attached"] = sources_link[0]["title"]
    return result


# ──────────────────────────────────────────────────────────────────────
#  Package B — read-only tenant tree (cp-engine #138, review finding 1)
# ──────────────────────────────────────────────────────────────────────
#
# SECURITY: the tree has NO per-user RLS and no equivalent. Any team member
# holding a valid JWT reads the WHOLE tenant tree — every client engagement's
# cp.md and sprint files — exactly as they already read the whole spine. The
# auth gate is the same `RequireAuthMiddleware` that guards every other tool:
# these are ordinary `@mcp_server.tool()` functions inside the authenticated
# tool surface, NOT a separate unauthenticated route. Authentication is the
# only boundary here; there is no authorization layer below it.

_TREE_LOCK = threading.Lock()
_TREE_STATE: dict[str, Any] = {"root": None, "last_pull": 0.0}

_EXEC_START = "<!-- cp-engine:start exec-summary -->"
_EXEC_END = "<!-- cp-engine:end exec-summary -->"
_SPRINT_DIR_RE = re.compile(r"^\d{4}-W\d{2}$")


def tree_ssh_env() -> dict[str, str]:
    """A subprocess env that authenticates git with GIT_SSH_KEY.

    Copies the tempfile + GIT_SSH_COMMAND pattern from
    `cp-engine/webhook/git_ops.py:_ssh_env` (copied, not imported — the webhook
    is a different deployable). With no key material the env is returned
    unchanged, which is correct for a LOCAL-PATH `TENANT_REPO`: a local clone
    needs no ssh at all, so the key is optional exactly when the remote is local.
    """
    env = dict(os.environ)
    if not GIT_SSH_KEY:
        return env
    key_path = Path(tempfile.mkdtemp(prefix="hosted-cp-key-")) / "id_ed25519"
    key_path.write_text(GIT_SSH_KEY if GIT_SSH_KEY.endswith("\n") else GIT_SSH_KEY + "\n")
    key_path.chmod(0o600)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    )
    return env


def tree_available() -> tuple[bool, str]:
    """(usable, reason) for the tree tools."""
    if not TENANT_REPO:
        return False, (
            "tree access unavailable: TENANT_REPO is not configured. Set it to the "
            "tenant repo remote (git@github.com:FirstPersonSF/cp.git) plus GIT_SSH_KEY, "
            "or to a local clone path for development."
        )
    is_local = Path(TENANT_REPO).exists()
    if not is_local and not GIT_SSH_KEY:
        return False, (
            "tree access unavailable: TENANT_REPO is a remote but GIT_SSH_KEY is not "
            "configured (a key is only optional when TENANT_REPO is a local path)."
        )
    if shutil.which("git") is None:
        return False, "tree access unavailable: git is not installed in this image"
    return True, ""


def caller_is_team_member() -> tuple[bool, str]:
    """(allowed, reason) — is the caller a First Person team member?

    THE TREE'S AUTHORIZATION BOUNDARY. Unlike every DB verb here, the tenant
    tree is a git clone: PostgREST is not in the path, so RLS cannot scope it.
    A valid JWT alone is NOT sufficient to read it.

    That distinction became load-bearing on 2026-08-02, when Supabase dynamic
    client registration was enabled so MCP connectors could self-register (see
    cp-engine #144). DCR is open registration by design — anyone who can reach
    GoTrue can mint a client and authenticate. RLS still zeroes their DB reads,
    but the tree tools would otherwise hand any Supabase account the entire
    tenant repo, client engagement content included.

    The predicate is the DATABASE's, not ours: `public.is_team_member()` is
    `exists (select 1 from public.profiles where id = auth.uid())`, STABLE
    SECURITY DEFINER. Calling it as an RPC under the caller's own JWT means
    `auth.uid()` resolves to that caller and Postgres renders the verdict — the
    same function the spine/notes/commitments RLS policies use. We never
    reimplement membership here; drift between two definitions is exactly how
    a gate rots.

    Fails CLOSED: any error (network, PostgREST, malformed response) denies.
    """
    try:
        client = user_client()
        result = client.rpc("is_team_member", {}).execute()
    except Exception as exc:  # noqa: BLE001 — any failure denies
        log.info("team check failed, denying: %s: %s", type(exc).__name__, exc)
        return False, (
            "tree access denied: could not verify team membership. This is a "
            "fail-closed default, not a statement about your account."
        )

    # PostgREST renders a scalar-returning function as a bare JSON value.
    if result.data is True:
        return True, ""
    return False, (
        "tree access denied: the tenant tree is restricted to First Person team "
        "members (a `public.profiles` row). Your token is valid and the "
        "database tools remain available under your own RLS scope — the tree "
        "specifically is not covered by RLS, so it is gated separately."
    )


def tree_root() -> Path:
    """The clone root, cloning on first use and pulling on read with a debounce.

    Shallow (`--depth 1`): these tools read the CURRENT state of the tree, never
    its history, so fetching history would be pure cost. The pull debounce
    (TREE_PULL_DEBOUNCE_SECONDS) keeps a read-heavy tool surface from firing a
    network round-trip per call; staleness is bounded by that window.

    A pull FAILURE is non-fatal on purpose — a momentarily unreachable remote
    should serve a slightly stale tree rather than fail the read. A CLONE
    failure does raise: there is nothing to serve.
    """
    with _TREE_LOCK:
        root = _TREE_STATE.get("root")
        env = tree_ssh_env()

        if root is None or not Path(root).exists():
            target = Path(tempfile.mkdtemp(prefix="hosted-cp-tree-")) / "cp"
            subprocess.run(
                ["git", "clone", "--depth", "1", TENANT_REPO, str(target)],
                check=True,
                env=env,
                capture_output=True,
            )
            _TREE_STATE["root"] = str(target)
            _TREE_STATE["last_pull"] = time.time()
            log.info("tenant tree cloned to %s", target)
            return target

        root_path = Path(root)
        if time.time() - float(_TREE_STATE.get("last_pull", 0)) >= TREE_PULL_DEBOUNCE_SECONDS:
            pull = subprocess.run(
                ["git", "pull", "--ff-only", "--depth", "1"],
                cwd=root_path,
                env=env,
                capture_output=True,
                text=True,
            )
            # Always advance the clock: a failing remote must not turn the
            # debounce off and retry on every single call.
            _TREE_STATE["last_pull"] = time.time()
            if pull.returncode != 0:
                log.warning("tenant tree pull failed (serving stale): %s", (pull.stderr or "")[:200])
        return root_path


def find_project_dir(root: Path, project_code: str) -> Path | None:
    """Locate a project's working dir in the tree.

    The real layout is DEEPER than a flat `<scope>/<code>/`: engagements nest
    under a company dir (`1p/infoblox/ibx-5153-ai-campaign/`), while initiatives
    and standalone repos sit directly under their scope
    (`firstpersonsf/mission-control/`, `canonic/storyos/`). So this walks the
    known scope roots to a bounded depth rather than assuming one shape.

    Match order is EXACT before PREFIX: `ibx-5153-ai-campaign` must not be
    reachable-by-accident when a caller asks for something that exactly exists,
    and a prefix match requires the `<code>-` boundary so `ggl-517` cannot claim
    `ggl-5177`. `inactive/` subtrees are skipped — an inactive project is not
    the project's current state.
    """
    code = project_code.strip().lower()
    scopes = [root / "1p", root / "firstpersonsf", root / "canonic"]
    exact: Path | None = None
    prefix: list[Path] = []
    for scope in scopes:
        if not scope.is_dir():
            continue
        # depth 1 (initiatives/repos) and depth 2 (company-nested engagements)
        candidates: list[Path] = []
        for child in scope.iterdir():
            if not child.is_dir() or child.name == "inactive":
                continue
            candidates.append(child)
            for grandchild in child.iterdir():
                if grandchild.is_dir() and grandchild.name != "inactive":
                    candidates.append(grandchild)
        for candidate in candidates:
            name = candidate.name.lower()
            if not (candidate / "cp.md").is_file():
                continue
            if name == code:
                exact = exact or candidate
            elif name.startswith(f"{code}-"):
                prefix.append(candidate)
    if exact:
        return exact
    return sorted(prefix)[0] if prefix else None


def extract_exec_summary(cp_md: Path) -> tuple[str | None, str | None]:
    """(exec_summary_text, note) from a cp.md's engine-managed markers."""
    try:
        text = cp_md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"could not read {cp_md.name}: {exc}"
    start = text.find(_EXEC_START)
    end = text.find(_EXEC_END, start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        return None, (
            "no exec-summary markers in cp.md — the region is scaffolded by "
            "`cp sync`, so an unsynced project legitimately has none"
        )
    return text[start + len(_EXEC_START) : end].strip(), None


def current_sprint_week(today: date | None = None) -> str:
    """Today's ISO sprint-dir name, `YYYY-W##`."""
    d = today or date.today()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def find_sprint_file(root: Path, dir_slug: str, code: str) -> tuple[Path | None, str, str | None]:
    """(path, week, note) for the CURRENT sprint file, else the most recent one.

    Sprint files are named for the working-dir slug (`ibx-5153-ai-campaign.md`),
    not the short code, so both are tried. When this week has no file the search
    walks BACKWARD through existing sprint dirs and says which week it settled
    on — silently serving a stale week as if it were current is the failure mode
    worth avoiding here.
    """
    sprints = root / "sprints"
    if not sprints.is_dir():
        return None, current_sprint_week(), "no sprints/ directory in the tree"

    weeks = sorted(
        (d.name for d in sprints.iterdir() if d.is_dir() and _SPRINT_DIR_RE.match(d.name)),
        reverse=True,
    )
    this_week = current_sprint_week()
    names = [f"{dir_slug}.md", f"{code}.md"]

    for week in weeks:
        if week > this_week:
            # The tree can carry a week ahead of today (the sprint week rolls
            # forward mid-week). Don't serve a future week as "current".
            continue
        for name in names:
            candidate = sprints / week / name
            if candidate.is_file():
                note = None if week == this_week else (
                    f"no sprint file for the current week ({this_week}); "
                    f"showing the most recent week that has one ({week})"
                )
                return candidate, week, note
    return None, this_week, f"no sprint file found for {code!r} in any week up to {this_week}"


@mcp_server.tool()
def get_project_state(project_code: str) -> dict[str, Any]:
    """Read a project's durable state + current sprint file from the tenant tree.

    Returns the `## Exec Summary` region of the project's `cp.md` (the
    engine-scaffolded, model-authored region between the `cp-engine:start
    exec-summary` / `:end` markers — the durable project-state surface) plus the
    CURRENT sprint file's full text. If this ISO week has no sprint file, the
    most recent week that does is returned instead and `sprint_note` says so.

    Served from a shallow clone of TENANT_REPO, pulled on read with a debounce.
    With no TENANT_REPO configured the tool still EXISTS and returns a clean
    "tree access unavailable" rather than erroring.

    NOTE ON SCOPE: the tenant tree has no per-user RLS. Any team member with a
    valid JWT can read any project's state through this tool.

    Args:
        project_code: engagement, initiative, or standalone-repo code
                      (e.g. "ibx-5153", "mission-control").
    """
    usable, reason = tree_available()
    if not usable:
        return {"project_code": project_code, "available": False, "error": reason}

    allowed, denial = caller_is_team_member()
    if not allowed:
        return {"project_code": project_code, "available": False, "error": denial}

    try:
        root = tree_root()
    except Exception as exc:  # noqa: BLE001 — a clone failure has nothing to serve
        return {
            "project_code": project_code,
            "available": False,
            "error": f"tree clone failed: {type(exc).__name__}: {str(exc)[:300]}",
        }

    client = user_client()
    project_dir = find_project_dir(root, project_code)
    if project_dir is None:
        audit(client, "get_project_state", {"project_code": project_code}, 0)
        return {
            "project_code": project_code,
            "available": True,
            "error": f"no working dir in the tree for code {project_code!r} "
            "(it may be inactive, or the code may not match a directory)",
        }

    dir_slug = project_dir.name
    exec_summary, exec_note = extract_exec_summary(project_dir / "cp.md")
    sprint_path, week, sprint_note = find_sprint_file(root, dir_slug, project_code)

    sprint_text = None
    if sprint_path is not None:
        try:
            sprint_text = sprint_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sprint_note = f"could not read sprint file: {exc}"

    audit(
        client,
        "get_project_state",
        {"project_code": project_code, "week": week},
        1 if exec_summary else 0,
    )
    return {
        "project_code": project_code,
        "available": True,
        "caller": caller_subject(),
        "working_dir": str(project_dir.relative_to(root)),
        "cp_md": str((project_dir / "cp.md").relative_to(root)),
        "exec_summary": exec_summary,
        **({"exec_summary_note": exec_note} if exec_note else {}),
        "sprint_week": week,
        "sprint_file": str(sprint_path.relative_to(root)) if sprint_path else None,
        "sprint_text": sprint_text,
        **({"sprint_note": sprint_note} if sprint_note else {}),
    }


@mcp_server.tool()
def read_project_file(path: str) -> dict[str, Any]:
    """Read one text file from the tenant tree by repo-relative path.

    Three guards, all load-bearing:

      * **Path traversal is rejected by CONTAINMENT, not by string inspection.**
        The path is joined to the clone root and `resolve()`d — following any
        symlinks — and the result must still be inside the resolved root. A
        blocklist of `..` segments would miss symlinks and absolute paths;
        containment misses neither. Absolute paths are rejected outright.
      * **Binary is rejected**, not mangled: a NUL byte in the first 8 KiB, or a
        UTF-8 decode failure, returns a structured refusal. Binary content
        belongs in Dropbox per the tenant's own `.gitignore`.
      * **Size is capped** (200 KiB by default) with an explicit
        `truncated: true` rather than a silent clip.

    NOTE ON SCOPE: gated on TEAM MEMBERSHIP, not RLS. The tree is a git clone,
    so PostgREST/RLS is not in the path and cannot scope it per user. A valid
    JWT alone is not enough: the caller must satisfy `public.is_team_member()`
    (a `public.profiles` row). Within the team the tree is unscoped — any
    member reads any file, the same posture as the spine today.

    Args:
        path: repo-relative path, e.g. "1p/infoblox/ibx-5153-ai-campaign/cp.md".
    """
    usable, reason = tree_available()
    if not usable:
        return {"path": path, "available": False, "error": reason}

    allowed, denial = caller_is_team_member()
    if not allowed:
        return {"path": path, "available": False, "error": denial}

    try:
        root = tree_root().resolve()
    except Exception as exc:  # noqa: BLE001
        return {
            "path": path,
            "available": False,
            "error": f"tree clone failed: {type(exc).__name__}: {str(exc)[:300]}",
        }

    client = user_client()
    raw = (path or "").strip()
    if not raw:
        return {"path": path, "error": "path is required"}
    if Path(raw).is_absolute():
        audit(client, "read_project_file", {"path": raw}, 0)
        return {"path": raw, "error": "path must be repo-relative, not absolute"}

    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        audit(client, "read_project_file", {"path": raw}, 0)
        return {
            "path": raw,
            "error": "path escapes the tenant tree root — rejected",
        }

    if not target.is_file():
        audit(client, "read_project_file", {"path": raw}, 0)
        return {"path": raw, "error": "no such file in the tenant tree"}

    size = target.stat().st_size
    try:
        head = target.open("rb").read(8192)
    except OSError as exc:
        return {"path": raw, "error": f"could not read: {exc}"}
    if b"\x00" in head:
        audit(client, "read_project_file", {"path": raw}, 0)
        return {
            "path": raw,
            "error": "file appears to be binary — this tool serves text only "
            "(binary content lives in Dropbox per the tenant .gitignore)",
            "bytes": size,
        }

    try:
        data = target.open("rb").read(TREE_MAX_FILE_BYTES + 1)
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        audit(client, "read_project_file", {"path": raw}, 0)
        return {"path": raw, "error": "file is not valid UTF-8 text", "bytes": size}
    except OSError as exc:
        return {"path": raw, "error": f"could not read: {exc}"}

    truncated = len(data) > TREE_MAX_FILE_BYTES
    if truncated:
        # Re-decode the capped slice, tolerating a split multi-byte char at the
        # boundary rather than failing a large-but-valid file.
        text = data[:TREE_MAX_FILE_BYTES].decode("utf-8", errors="ignore")
        text += f"\n\n[truncated at {TREE_MAX_FILE_BYTES} bytes of {size}]"

    audit(client, "read_project_file", {"path": raw}, 1)
    return {
        "path": raw,
        "available": True,
        "caller": caller_subject(),
        "bytes": size,
        "truncated": truncated,
        "text": text,
    }


@mcp_server.tool()
def whoami() -> dict[str, Any]:
    """Echo the verified identity of the caller (spike diagnostic)."""
    access = get_access_token()
    if access is None:
        return {"authenticated": False}
    claims = access.claims or {}
    return {
        "authenticated": True,
        "sub": access.subject,
        "email": claims.get("email"),
        "role": claims.get("role"),
        "issuer": claims.get("iss"),
        "expires_at": access.expires_at,
    }


def main() -> None:
    log.info("hosted-cp spike listening on http://%s:%d/mcp", HOST, PORT)
    log.info("issuer:  %s", ISSUER)
    log.info("jwks:    %s", JWKS_URI)
    log.info("resource:%s", RESOURCE_URL)
    log.info("verification: ES256/JWKS only")
    usable, reason = embedding_available()
    log.info(
        "semantic_search: %s",
        f"enabled ({EMBED_MODEL}, {EMBED_DIM}-dim)" if usable else reason,
    )
    tree_ok, tree_reason = tree_available()
    log.info(
        "tenant tree: %s",
        f"enabled (repo={TENANT_REPO}, pull debounce {TREE_PULL_DEBOUNCE_SECONDS}s)"
        if tree_ok
        else tree_reason,
    )
    log.info(
        "writes (insert): create_note, create_commitment, create_spine_element, "
        "add_spine_version (+auto-step), add_spine_document, "
        "create_spine_relation, add_spine_step, propose_spine_step"
    )
    log.info(
        "writes (update/delete, #143 batch 2): set_spine_element, "
        "resolve_commitment, set_spine_step, reorder_spine_step, remove_spine_step"
    )
    log.info(
        "writes (sources/provenance, #143 batch 3, via the guarded "
        "spine_element_modify_source fn): add_element_source, "
        "remove_element_source, add_element_provenance, remove_element_provenance"
    )
    log.info("audit log: mcp_audit_log as client=%s", SERVER_VERSION)
    mcp_server.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        # Stateless: every request is self-contained and authenticated on its
        # own. This is what lets a hosted deployment scale horizontally and is
        # the shape the 2026-07-28 protocol assumes for a plain tools/call.
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
