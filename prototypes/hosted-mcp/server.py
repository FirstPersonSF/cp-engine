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

SERVER_VERSION = "hosted-cp-spike/0.0.3"

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
    "scope, project_id, version_label, version_date, synced_at"
)
SPINE_PULL_COLUMNS = SPINE_LIST_COLUMNS + ", body, sources, note, project_code, rel_path"
COMMITMENT_COLUMNS = (
    "id, description, owner_email, owner_name, direction, due_date, "
    "date_status, status, source_kind, created_at, updated_at"
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
def list_spine_elements(project_code: str) -> dict[str, Any]:
    """List live spine elements for a project, under the caller's identity.

    Args:
        project_code: engagement, initiative, or standalone-repo code
                      (e.g. "ibx-5153", "mission-control").
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
    elements = [
        {
            "slug": r.get("est_item_id"),
            "framing": r.get("framing"),
            "status": r.get("status"),
            "layer": r.get("layer"),
            "binding": r.get("binding"),
            "important": bool(r.get("important")),
            "version_label": r.get("version_label"),
            "version_date": r.get("version_date"),
            # `synced_at` stands in for the requested `updated_at`, which this
            # table does not have.
            "synced_at": r.get("synced_at"),
        }
        for r in rows
        if not r.get("archived")
    ]
    audit(client, "list_spine_elements", {"project_code": project_code}, len(elements))
    return {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(elements),
        "elements": elements,
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

# `commitments.direction` — mirrors the mc-2 CHECK constraint.
_DIRECTIONS = {"us_to_them", "them_to_us", "internal"}


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


@mcp_server.tool()
def create_note(project_code: str, body: str, title: str | None = None) -> dict[str, Any]:
    """Create a partner Note against a project, under the caller's identity.

    INSERT-only into `public.notes`, stamping `author_id` with the caller's
    `sub` claim — which is what the INSERT policy
    `is_team_member() AND author_id = auth.uid()` requires.

    KNOWN BLOCKER, surfaced by this tool rather than worked around: on the live
    database those two requirements are mutually unsatisfiable. `notes.author_id`
    carries a FOREIGN KEY to `public.entities(id)`, but the policy demands it
    equal `auth.uid()` — and auth-user ids and `entities` ids are DISJOINT
    namespaces (verified live: Drew's entity id has no `profiles` row, and no
    `profiles` id appears in `entities`). So a policy-satisfying value fails the
    FK, and an FK-satisfying value fails the policy. The tool therefore returns a
    structured, diagnosable error naming the exact collision instead of a raw
    Postgres 23503. Closing it is a schema decision (repoint the FK at
    `auth.users`, or add an `author_entity_id` alongside), not something a
    client can paper over.

    `notes.recipient_id` is additionally NOT NULL with the same FK, and this
    tool's signature takes no recipient — a second reason the hosted note path
    needs the schema question answered before it can work.

    There is NO `title` column on `notes` (live columns: id, project_code,
    author_id, recipient_id, body, status, slack_ts, slack_delivery, created_at,
    read_at, done_at). `title`, when given, is prepended to the body as a
    markdown H3 rather than dropped — the body is markdown and renders in-app.

    Args:
        project_code: engagement, initiative, or standalone-repo code.
        body: markdown note text.
        title: optional heading, folded into the body (no `title` column exists).
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

    row = {
        "id": str(uuid.uuid4()),
        "project_code": project_code,
        # The policy's requirement. It is also the FK collision described above.
        "author_id": subject,
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
        if "notes_author_id_fkey" in message or "23503" in message:
            return {
                "error": "notes.author_id is FK->entities(id) but the INSERT "
                "policy requires author_id = auth.uid(); auth-user ids and "
                "entities ids are disjoint namespaces, so no value satisfies "
                "both. This is a schema gap, not a client error.",
                "detail": message[:400],
                "project_code": project_code,
                "caller": subject,
            }
        if "recipient_id" in message:
            return {
                "error": "notes.recipient_id is NOT NULL (FK->entities) and this "
                "tool takes no recipient — the hosted note path needs the "
                "author/recipient identity model resolved first.",
                "detail": message[:400],
            }
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

    row: dict[str, Any] = {
        "description": text,
        "owner_email": (owner_email or "").strip() or None,
        "owner_name": None,
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
        "sources": [],
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

    NOTE ON SCOPE: no per-user RLS. Any team member with a valid JWT can read
    any file in the tenant tree through this tool.

    Args:
        path: repo-relative path, e.g. "1p/infoblox/ibx-5153-ai-campaign/cp.md".
    """
    usable, reason = tree_available()
    if not usable:
        return {"path": path, "available": False, "error": reason}

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
    log.info("writes: insert-only (create_note, create_commitment, create_spine_element)")
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
