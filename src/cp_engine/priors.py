"""The master prompt — judgment priors sent as ``system=`` on every LLM call.

WHY THIS EXISTS
---------------
Until mig 139/140, every Anthropic call cp-engine made ran with **no system
prompt at all**. ``plan_from_transcript._call_claude`` — the universal
transport for ~9 of the 11 prompt sites — passed only a user message, and a
grep for ``system=`` across ``src/`` returned nothing.

``templates/CLAUDE.md.j2`` carries the authority-precedence rules and reading
modes, but it shapes only the *session agent*: it is rendered into the tenant's
``CLAUDE.md`` and read by Claude Code. It is never sent on an API call. So the
judgments that land in sprint files — what counts as a decision, whose voice
outranks whose, when to say "I don't know" — were made by prompts nobody could
see or edit, with no standing instruction about how this firm thinks.

Drew, 2026-08-15: *"cp offers judgements that are incorrect. If we had a short
set of instructions that might help, but it needs to be human editable in a
polished interface."*

WHAT THIS IS AND IS NOT
-----------------------
This is **judgment priors** — *how to think*. It is NOT project canon — *what is
true here* — which stays exactly where spec v04 put it: ``canon_of`` edges
anchored on ``_authored/inputs-briefing``. The 2026-08-03 deferral of
firm-level canon is untouched and remains deferred.

RESOLUTION
----------
``cp_prompt_resolve(project_id)`` in MC-2 returns the active global body, with
the project's override appended if one exists. Overrides ADD priors; they can
never replace or fork the global (mig 139). Resolution returns ``''`` — never
NULL — so a system prompt is either real text or nothing.

FAIL-SOFT, ALWAYS
-----------------
Every path here degrades to "no priors" rather than raising. A prompt that
cannot be fetched must never break an ingest: the pre-139 behaviour (no system
prompt) is the correct fallback, and it is what an empty tenant gets anyway.
This mirrors ``get_client(required=False)`` and the webhook side-write
contract throughout the engine.

CACHING
-------
Per-process, keyed by project id, with the resolved text held for the life of
the process. Ingest runs are short-lived (a webhook request, a CLI invocation),
so a stale prompt cannot outlive the work it shaped. A long-running process
that needs to pick up an edit should call :func:`clear_cache`.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Resolved prompt text, keyed by project id (``None`` = tenant-global).
# Values may be ``""`` — a *cached negative*, meaning "resolved, nothing set".
_CACHE: dict[str | None, str] = {}


def resolve_priors(
    project_id: str | None = None,
    *,
    config=None,
) -> str:
    """The resolved master prompt for this project, or ``""`` if none is set.

    ``project_id`` is the MC-2 uuid, not the project code — the RPC keys on id
    because codes drift between the dir slug and the MC-2 row (the recurring
    resolver gap; see ``resolve_write_scope`` on the hosted server). Pass
    ``None`` for tenant-global priors.

    Never raises. Any failure — no creds, no supabase package, RPC missing,
    network down — resolves to ``""``, which restores exactly the pre-mig-139
    behaviour of sending no system prompt.
    """
    if project_id in _CACHE:
        return _CACHE[project_id]

    text = ""
    try:
        from cp_engine.mc2_db import get_client

        client = get_client(config, required=False)
        if client is not None:
            resp = client.rpc(
                "cp_prompt_resolve", {"p_project_id": project_id}
            ).execute()
            # PostgREST returns the scalar directly for a scalar-returning fn.
            text = (resp.data or "") if resp is not None else ""
            if not isinstance(text, str):
                text = str(text or "")
    except Exception as exc:  # noqa: BLE001 — fail-soft is the contract
        # DEBUG, not WARNING: an empty prompt store is the expected resting
        # state for a tenant that has not authored priors yet, and this path
        # also covers the offline/test case. A real misconfiguration surfaces
        # as "the prompt I wrote is not being applied", which is diagnosable
        # from `cp priors` (below), not from log noise on every ingest.
        log.debug("priors unavailable (%s: %s)", type(exc).__name__, exc)
        text = ""

    _CACHE[project_id] = text
    return text


def project_id_for_code(code: str, *, config=None) -> str | None:
    """MC-2 uuid for a project or initiative code, or ``None``.

    THREE CODE SHAPES EXIST for the same project and none is canonical:

        cp code        ``ibx-5153``                       (what a human types)
        dir slug       ``ibx-5153-ai-campaign``           (the working dir)
        MC-2 row code  ``IBX-ai-campaign``                (projects.code)

    A naive ``.eq("code", code)`` matches none of them for that example — the
    recurring resolver gap (`MCP resolver slug gap`; mig 129 healed 45 spine
    edges for the same reason). So the lookup goes SPINE FIRST: every spine row
    carries both the dir slug in ``project_code`` and the uuid in
    ``project_id``, and a prefix match on the cp code resolves the common case
    exactly. The direct code match is the fallback, not the primary.

    Fail-soft like :func:`resolve_priors`: an unreachable MC-2 or an
    unresolvable code yields ``None``, which resolves to tenant-global priors
    rather than raising.
    """
    if not code:
        return None
    try:
        from cp_engine.mc2_db import Tables, get_client

        client = get_client(config, required=False)
        if client is None:
            return None

        # 1. Exact code on the row itself — initiatives and standalone repos
        #    match here (their code IS the slug), and so do any projects whose
        #    row code happens to agree with the cp code.
        for table in (Tables.PROJECTS, Tables.INITIATIVES):
            rows = (
                client.table(table).select("id").eq("code", code).limit(1).execute()
            ).data or []
            if rows:
                return rows[0].get("id")

        # 2. Spine bridge: project_code there is the dir slug, which starts
        #    with the cp code. `limit(1)` is safe — the prefix is unique per
        #    project by construction (`<company>-<number>-...`).
        rows = (
            client.table(Tables.SPINE_SUBSTANCE)
            .select("project_id")
            .like("project_code", f"{code}%")
            .limit(1)
            .execute()
        ).data or []
        if rows and rows[0].get("project_id"):
            return rows[0]["project_id"]
    except Exception as exc:  # noqa: BLE001 — fail-soft is the contract
        log.debug("project id lookup failed (%s: %s)", type(exc).__name__, exc)
    return None


def clear_cache() -> None:
    """Drop cached priors. For long-running processes and tests."""
    _CACHE.clear()
