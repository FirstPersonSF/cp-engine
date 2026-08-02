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

Deliberately out of scope: writes, deployment, semantic search, session
persistence, token caching, the DCR/authorize dance (Supabase is the AS).

Run:
    SUPABASE_URL=... SUPABASE_ANON_KEY=... .venv/bin/python prototypes/hosted-mcp/server.py
"""

from __future__ import annotations

import logging
import os
import time
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
    version="0.0.1-spike",
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

RLS_DENIED_NOTE = (
    "0 rows (RLS denies access under user JWT — expected until policies land). "
    "`commitments` has RLS enabled with ZERO policies, so every row is invisible "
    "to the `authenticated` role. This is a real gap the hosted-cp rollout must "
    "close, not an empty result set."
)


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
    return {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(elements),
        "elements": elements,
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

    KNOWN FAILURE MODE, surfaced rather than hidden: `commitments` has RLS
    enabled and no policies, so a user JWT sees nothing. An empty list here is
    ambiguous between "no commitments" and "no access", so the tool says which.
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

    rows: list[dict[str, Any]] = []
    rls_error: str | None = None
    for column in ("project_id", "initiative_id"):
        try:
            rows.extend(
                client.table("commitments")
                .select(COMMITMENT_COLUMNS)
                .eq(column, project_id)
                .execute()
                .data
                or []
            )
        except Exception as exc:  # noqa: BLE001 — a policy denial can surface as an error
            rls_error = f"{type(exc).__name__}: {exc}"

    if not rows:
        return {
            "project_code": project_code,
            "project_id": project_id,
            "caller": caller_subject(),
            "count": 0,
            "commitments": [],
            "rls_note": RLS_DENIED_NOTE,
            "error": rls_error,
        }
    return {
        "project_code": project_code,
        "project_id": project_id,
        "caller": caller_subject(),
        "count": len(rows),
        "commitments": rows,
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
