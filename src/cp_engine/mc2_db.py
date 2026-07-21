"""MC-2 data-access primitives — the ONE home for Supabase access.

arch-phase-3 (#27). Everything cp-engine (and the webhook) needs to talk to
the shared MC-2 Supabase lives here:

* :func:`load_supabase_creds` — the single credential resolver
  (env → 1Password ``op://`` refs → ``<mc-2 clone>/backend/.env``).
* :func:`get_client` — the single client constructor, cached per ``(url, key)``.
* :class:`Tables` — the registry of every MC-2 table cp-engine touches.
  ``tests/test_mc2_db.py`` greps the tree and fails if any ``.table("...")``
  string literal bypasses it, so "one grep finds every table reference"
  stays true permanently.
* The consolidated column-set constants (explicit columns, never ``*`` —
  several MC-2 tables carry megabyte JSONB cache columns).
* Lightweight typed row mappers for the highest-traffic row shapes.

History: the resolver moved here verbatim from ``sync_mc2`` (which keeps
thin re-exports); the 8 inline env-only constructors in ``webhook/`` and the
divergent ``cli.py`` copy were deleted in favor of :func:`get_client`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from supabase import Client

    from cp_engine.config import TenantConfig


# ──────────────────────────────────────────────────────────────────────
#  Table registry
# ──────────────────────────────────────────────────────────────────────


class Tables:
    """Every MC-2 table cp-engine or the webhook touches. Grep-enforced.

    ``public`` schema unless noted. The estimator-schema names carry an
    ``EST_``/explicit prefix and must be used with
    ``client.schema("estimator")``.
    """

    # public — core entities
    PROJECTS = "projects"
    REPOS = "repos"
    INITIATIVES = "initiatives"
    COMPANIES = "companies"
    ENTITIES = "entities"  # people (partners/recipients); note author + recipient
    GITHUB_ORGS = "github_orgs"  # embedded-join only today (see *_COLUMNS)
    SPRINT_ALLOCATIONS = "sprint_allocations"
    NOTES = "notes"  # partner pings (in-app unread + Slack DM, mig 116)

    # public — meetings + ingest
    FATHOM_MEETINGS = "fathom_meetings"
    AUTO_INGEST_RUNS = "auto_ingest_runs"
    ASSET_INGEST_RUNS = "asset_ingest_runs"
    RAG_ASSETS = "rag_assets"
    ASSET_CHUNKS = "asset_chunks"  # chunk text; embeddings FK-cascade on delete
    CLICKUP_TASK_PROPOSALS = "clickup_task_proposals"
    COMMITMENTS = "commitments"
    APP_CONFIG = "app_config"  # tenant-level key/value settings (jsonb)

    # public — spine
    SPINE_SUBSTANCE = "spine_substance"
    SPINE_CONTEXT = "spine_context"
    SPINE_ELEMENTS = "spine_elements"
    SPINE_SNAPSHOTS = "spine_snapshots"
    SPINE_INBOX = "spine_inbox"
    SPINE_PROMOTE_RUNS = "spine_promote_runs"
    SPINE_RELATIONS = "spine_relations"  # typed element->element edges (mig 117)

    # estimator schema
    EST_PROJECTS = "projects"
    EST_PHASES = "phases"
    EST_PHASE_ACTIVITIES = "phase_activities"
    EST_PHASE_DELIVERABLES = "phase_deliverables"
    EST_SCHEDULE_ITEMS = "schedule_items"


# ──────────────────────────────────────────────────────────────────────
#  Column-set constants (explicit lists, never `*`)
# ──────────────────────────────────────────────────────────────────────
#
# Where different consumers legitimately read different shapes of the same
# table, each shape gets a purposeful name here instead of an anonymous
# inline string in its module. `cached_messages`/`cached_analysis` on
# `projects` and `meta` on `rag_assets` can be megabytes per row — that is
# WHY these are explicit lists.

# projects — the sync read (engagement rows → ProjectState).
PROJECTS_SYNC_COLUMNS = (
    "id, number, full_job_name, name, mc_status, account_manager, "
    "is_internal, deal_stage, budget, dropbox_folder_url, updated_at, "
    "companies(code, name, kind), "
    "repos!project_id(repo_name, status, description, github_orgs!inner(name))"
)

# projects — the Slack channel-map read (mapping columns aren't part of sync).
# Channel ids come from project_integrations bindings (read-flip; the flat
# slack columns are being retired) — `id` is here so the map can join them.
PROJECTS_SLACK_COLUMNS = (
    "id, number, name, mc_status, is_internal, enable_slack, "
    "full_job_name, companies!inner(code)"
)

# projects — the public-side estimate anchor (start_date only).
PROJECTS_ESTIMATE_COLUMNS = "id, start_date"

# repos — standalone repo rows (project_id IS NULL).
REPOS_SYNC_COLUMNS = (
    "id, repo_name, status, description, owner, updated_at, "
    "github_orgs!inner(name), "
    "companies!inner(code, name, kind)"
)

# initiatives — the sync read. (slack_channel_ids dropped in the read-flip;
# sync never consumed it.)
INITIATIVES_SYNC_COLUMNS = (
    "id, code, name, description, status, owner, updated_at, "
    "enable_slack, "
    "companies!inner(code, name, kind), "
    "repos!initiative_id(repo_name, status, description, github_orgs!inner(name))"
)

# initiatives — the Slack channel-map read (channel ids come from bindings).
INITIATIVES_SLACK_COLUMNS = (
    "id, code, name, status, enable_slack, companies!inner(code)"
)

# fathom_meetings — list vs full-fetch vs webhook artifact/transcript shapes.
FATHOM_LIST_COLUMNS = (
    "id, title, meeting_date, project_tags, duration_minutes, meeting_type"
)
FATHOM_FULL_COLUMNS = FATHOM_LIST_COLUMNS + ", transcript, summary"
FATHOM_TRANSCRIPT_COLUMNS = "id, title, meeting_date, transcript, participants"
FATHOM_ARTIFACT_COLUMNS = (
    "id, title, meeting_date, summary, action_items, "
    "participants, duration_minutes, fathom_url, "
    "recording_id, project_tags, project_id, summary_embedded_at"
)

# spine_substance — list / pull / resolve / status-fold shapes. Each carries
# `archived` so the read paths can hide retired elements (a live-but-archived
# row is a valid retire state; filtering on status alone leaks it — #47).
SPINE_LIST_COLUMNS = (
    "est_item_id, framing, layer, binding, status, serves, body, important, "
    "note, archived, scope, company_id, version_label, version_date"
)
SPINE_PULL_COLUMNS = (
    "est_item_id, framing, layer, binding, status, serves, sources, "
    "version_label, body, important, note, archived, scope, company_id"
)
SPINE_RESOLVE_COLUMNS = (
    "id, est_item_id, framing, status, important, note, rel_path, archived, "
    "scope, company_id, project_id, layer"
)
SPINE_STATUS_COLUMNS = (
    "est_item_id, status, version_date, version_label, binding, project_id"
)
# cp spine-lint — live-row hygiene shape (#69): flags + body + sources, no
# version bookkeeping.
SPINE_LINT_COLUMNS = (
    "est_item_id, framing, layer, binding, serves, important, body, "
    "sources, status, archived"
)
# add/remove_element_source — every version row + its sources array (the
# element-level fact moves across all versions together, like serves).
SPINE_SOURCES_EDIT_COLUMNS = (
    "id, est_item_id, framing, status, archived, scope, company_id, "
    "project_id, sources"
)

# rag_assets — manifest list shape. `meta` is JSONB and must NEVER be
# selected wholesale (`SELECT *` on this table is the classic 25MB mistake).
# `prev_asset_id` (a short uuid scalar) rides along so read paths can drop
# assets that have a SUCCESSOR — a newer asset whose prev_asset_id points at
# them (see project_sources.drop_superseded_assets).
RAG_ASSET_LIST_COLUMNS = (
    "id, title, source_type, status, created_at, file_hash, prev_asset_id"
)
RAG_ASSET_REFETCH_COLUMNS = "title, source_provider, source_file_id, source_path, url"

# estimator schema shapes.
EST_PROJECT_COLUMNS = "id, mc_project_id, name, is_default"
EST_PHASE_COLUMNS = "id, name, overview, position"
EST_ITEM_COLUMNS = "id, phase_id, name, short_description, library_item_id, position"
EST_SCHEDULE_COLUMNS = (
    "id, project_id, phase_id, label, start_week, duration, position, "
    "item_type, emphasis, work_item_id, work_item_kind, done"
)


# ──────────────────────────────────────────────────────────────────────
#  Typed row mappers
# ──────────────────────────────────────────────────────────────────────
#
# Lightweight, tolerant mappers for the highest-traffic row shapes: every
# field defaults so a narrower SELECT still maps cleanly, and `from_row`
# ignores unknown keys. They exist to stop N modules re-inventing the same
# `.get()` chains with subtly different key sets. Only shapes with live
# consumers are defined — add ProjectRow/InitiativeRow/FathomMeetingRow
# WHEN a read site adopts them, not before (an unused tolerant mapper
# silently drifts from the schema).


@dataclass(frozen=True)
class SpineSubstanceRow:
    """A `spine_substance` row across the list/pull/resolve/status shapes."""

    id: str | None = None
    est_item_id: str | None = None
    framing: str | None = None
    layer: str | None = None
    binding: str | None = None
    status: str | None = None
    serves: Any = None
    sources: Any = None
    version_label: str | None = None
    version_date: str | None = None
    body: str | None = None
    important: bool = False
    note: str | None = None
    rel_path: str | None = None
    project_id: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "SpineSubstanceRow":
        return cls(
            id=row.get("id"),
            est_item_id=(
                str(row["est_item_id"]) if row.get("est_item_id") is not None else None
            ),
            framing=row.get("framing"),
            layer=row.get("layer"),
            binding=row.get("binding"),
            status=row.get("status"),
            serves=row.get("serves"),
            sources=row.get("sources"),
            version_label=row.get("version_label"),
            version_date=(
                str(row["version_date"]) if row.get("version_date") else None
            ),
            body=row.get("body"),
            important=bool(row.get("important")),
            note=row.get("note"),
            rel_path=row.get("rel_path"),
            project_id=row.get("project_id"),
        )


@dataclass(frozen=True)
class RagAssetRow:
    """A `rag_assets` row (manifest list shape)."""

    id: str | None = None
    title: str | None = None
    source_type: str | None = None
    status: str | None = None
    created_at: str | None = None
    file_hash: str | None = None
    prev_asset_id: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "RagAssetRow":
        return cls(
            id=row.get("id"),
            title=row.get("title"),
            source_type=row.get("source_type"),
            status=row.get("status"),
            created_at=row.get("created_at"),
            file_hash=row.get("file_hash"),
            prev_asset_id=row.get("prev_asset_id"),
        )


# ──────────────────────────────────────────────────────────────────────
#  Credential resolution (moved verbatim from sync_mc2, arch-phase-3)
# ──────────────────────────────────────────────────────────────────────


_SUPABASE_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
_INGEST_KEYS = ("OPENAI_API_KEY", "VOYAGE_API_KEY")


def _backend_unavailable(message: str) -> Exception:
    """Build the canonical creds-missing error without a module-level import.

    `cp_engine.sync` imports `render` (Jinja and friends); deferring the
    import keeps `mc2_db` cheap to import from the webhook's hot path while
    still raising the exact exception type existing callers catch.
    """
    from cp_engine.sync import BackendUnavailable

    return BackendUnavailable(message)


def load_ingest_creds(config: "TenantConfig") -> None:
    """Export OPENAI_API_KEY / VOYAGE_API_KEY into `os.environ` for the pipeline.

    The asset-ingest pipeline (and the spine transcript-promotion path that
    reuses it) reads these keys via `os.getenv`, but `load_supabase_creds`
    loads only the SUPABASE_* keys. Without this, a process that has the MC-2
    `.env` on disk but no OPENAI/VOYAGE in its environment (the Railway promote
    webhook) builds a pipeline whose OpenAI client gets `None` and raises
    `OpenAIError: Missing credentials` — masked downstream as "stamp matched no
    row".

    Source order mirrors `load_supabase_creds`: an already-set env var WINS
    (preserves CI / explicit shell exports); otherwise fill from
    `<mc-2 clone>/backend/.env`. No clone configured → no-op (never raises): the
    keys may legitimately come from the real process environment in production.
    """
    env_file = _mc2_env_file(config)
    if env_file is None:
        return
    file_creds = _read_dotenv(env_file, _INGEST_KEYS)
    for key in _INGEST_KEYS:
        if not os.environ.get(key) and file_creds.get(key):
            os.environ[key] = file_creds[key]


def load_supabase_creds(config: "TenantConfig | None" = None) -> tuple[str, str]:
    """Resolve `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` for the MC-2 client.

    Order of preference:
      1. `os.environ` — preserves CI/GitHub Actions, Railway, and explicit
         shell exports. `SUPABASE_KEY` is accepted as a last-resort alias for
         the service key (legacy name a few deploy environments still set).
      2. A `[supabase]` block of `op://` references in `.cp-engine.local.toml`,
         resolved live via the 1Password CLI (`op read`). This is the
         second-user path: the secret lives only in a shared 1Password vault,
         never on disk. Configured refs are authoritative — if `op` can't
         resolve them we raise, never silently fall through to (3).
      3. `<mc-2 clone>/backend/.env` — the canonical local-dev location.
         The clone path comes from `TenantConfig.local_repos["mc-2"]`
         (per-machine, gitignored).

    `config=None` (the webhook / any context with no tenant on disk) checks
    the environment only — tiers 2 and 3 need a tenant root/clone map to
    exist and are skipped gracefully.

    On fallback (2 or 3), prints a one-line note to stderr so the implicit
    dependency stays visible. Raises `BackendUnavailable` if no source has the
    keys, naming what it tried.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not key and os.environ.get("SUPABASE_KEY"):
        key = os.environ.get("SUPABASE_KEY")
        print(
            "Using legacy SUPABASE_KEY env var as the service key "
            "(set SUPABASE_SERVICE_KEY to silence this)",
            file=sys.stderr,
        )
    if url and key:
        return url, key

    if config is None:
        raise _backend_unavailable(
            "MC-2 backend requires SUPABASE_URL and SUPABASE_SERVICE_KEY. "
            "Tried: environment only (no tenant config available, so the "
            "1Password and mc-2/backend/.env fallbacks don't apply)."
        )

    op_creds = _resolve_op_creds(config)
    if op_creds is not None:
        print("Resolved SUPABASE_* from 1Password (op://)", file=sys.stderr)
        return op_creds

    env_file = _mc2_env_file(config)
    file_creds = _read_dotenv(env_file, _SUPABASE_KEYS) if env_file else {}
    url = url or file_creds.get("SUPABASE_URL")
    key = key or file_creds.get("SUPABASE_SERVICE_KEY")
    if url and key:
        print(f"Loaded SUPABASE_* from {env_file}", file=sys.stderr)
        return url, key

    tried = "environment"
    if env_file:
        tried += f" and {env_file}"
    else:
        tried += " (no MC-2 clone configured in [local-repos] of .cp-engine.local.toml)"
    raise _backend_unavailable(
        "MC-2 backend requires SUPABASE_URL and SUPABASE_SERVICE_KEY. "
        f"Tried: {tried}. For local dev, copy mc-2/backend/.env.example "
        "and ensure [local-repos].\"mc-2\" points to your clone. To resolve "
        "from 1Password, add a [supabase] block with url_ref/service_key_ref "
        "(op:// references) to .cp-engine.local.toml. For the GitHub Action, "
        "configure repo secrets."
    )


def _resolve_op_creds(config: "TenantConfig") -> tuple[str, str] | None:
    """Resolve creds from a `[supabase]` op:// block in `.cp-engine.local.toml`.

    Returns `(url, key)` when the block is present and resolves, or `None` when
    no `[supabase]` block is configured (so the caller falls through to the
    dotenv path). A block that is present but malformed or whose refs fail to
    resolve raises `BackendUnavailable` — a configured 1Password ref is an
    explicit intent, so a failure to honor it is a loud error, never a silent
    fall-through to a different source.
    """
    local_path = config.root / ".cp-engine.local.toml"
    if not local_path.exists():
        return None
    try:
        with local_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        # A broken local toml is config.load's problem to report elsewhere; here
        # we just decline the op path rather than masking it as a cred error.
        return None

    block = data.get("supabase")
    if not isinstance(block, dict):
        return None

    url_ref = block.get("url_ref")
    key_ref = block.get("service_key_ref")
    missing = [
        name
        for name, val in (("url_ref", url_ref), ("service_key_ref", key_ref))
        if not isinstance(val, str) or not val
    ]
    if missing:
        raise _backend_unavailable(
            f"[supabase] in {local_path} is incomplete: missing {', '.join(missing)}. "
            "Both url_ref and service_key_ref (op:// references) are required."
        )

    try:
        url = _op_read(url_ref)
        key = _op_read(key_ref)
    except Exception as exc:  # noqa: BLE001 — surface ANY op failure loudly
        raise _backend_unavailable(
            f"Failed to resolve SUPABASE_* from 1Password (op://) refs in "
            f"{local_path}: {exc}. Is the `op` CLI installed and signed in "
            "(`op signin`), and do you have access to the referenced vault?"
        ) from exc

    if not url or not key:
        raise _backend_unavailable(
            f"1Password (op://) refs in {local_path} resolved to empty values. "
            "Check the item field names in url_ref/service_key_ref."
        )
    return url, key


def _op_read(reference: str) -> str:
    """Return the secret at an `op://vault/item/field` reference via `op read`.

    Thin wrapper around the 1Password CLI so the resolver stays testable
    (tests monkeypatch this). Raises on a non-zero exit (e.g. `op` missing,
    not signed in, or the reference doesn't resolve).
    """
    result = subprocess.run(
        ["op", "read", reference],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _mc2_env_file(config: "TenantConfig") -> Path | None:
    """Return `<mc-2 clone>/backend/.env` if the clone is configured, else None."""
    clone = config.local_repos.get("mc-2")
    if clone is None:
        return None
    return Path(clone) / "backend" / ".env"


def _read_dotenv(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    """Parse a dotenv file, returning only the requested keys.

    Trivial parser: `KEY=value` per line, skips comments and blanks, strips
    surrounding single or double quotes. Doesn't handle multi-line values or
    `export` prefixes — MC-2's .env doesn't use them.
    """
    if not path.is_file():
        return {}
    wanted = set(keys)
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k not in wanted:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


# ──────────────────────────────────────────────────────────────────────
#  The one client constructor
# ──────────────────────────────────────────────────────────────────────


_client_cache: dict[tuple[str, str], "Client"] = {}

# Resolved-creds memo, keyed by tenant root (None = env-only context). Without
# this, every get_client call would re-run full resolution even on a client
# cache hit — on the op:// tier that is two `op read` subprocesses (and a
# stderr note) per call. One resolution per (process, tenant) instead.
_creds_cache: dict[object, tuple[str, str]] = {}


def get_client(
    config: "TenantConfig | None" = None,
    *,
    required: bool = True,
    url: str | None = None,
    key: str | None = None,
) -> "Client | None":
    """Build (or return the cached) MC-2 Supabase client. THE constructor.

    Credential resolution goes through :func:`load_supabase_creds` unless the
    caller injects explicit ``url``/``key`` (the asset-ingest glue does).
    Clients are cached per ``(url, key)`` so every module in one process
    shares a connection for the same creds.

    ``required=False`` returns ``None`` instead of raising when the
    ``supabase`` package is missing or no creds resolve — the fail-soft
    contract every best-effort path (webhook side-writes, prep-planning
    ClickUp lookup, digest link decoration) relies on. ``required=True``
    raises ``BackendUnavailable`` (creds) or ``RuntimeError`` (package).

    ``config=None`` restricts resolution to environment variables — the
    webhook and other tenant-less contexts.
    """
    try:
        from supabase import create_client
    except ImportError:
        if required:
            raise RuntimeError(
                "the `supabase` package is not installed; MC-2 access is "
                "unavailable in this environment"
            ) from None
        return None

    if url is None or key is None:
        creds_key = getattr(config, "root", None)
        cached_creds = _creds_cache.get(creds_key)
        if cached_creds is not None:
            url, key = cached_creds
        else:
            try:
                url, key = load_supabase_creds(config)
            except Exception as exc:
                if required:
                    raise
                print(
                    f"MC-2 client unavailable (credential resolution): {exc}",
                    file=sys.stderr,
                )
                return None
            _creds_cache[creds_key] = (url, key)

    cached = _client_cache.get((url, key))
    if cached is not None:
        return cached

    try:
        client = create_client(url, key)
    except Exception as exc:  # noqa: BLE001 — construction itself can fail (bad URL)
        if required:
            raise
        print(
            f"MC-2 client unavailable (create_client failed): {exc!r}",
            file=sys.stderr,
        )
        return None
    # Writer identity for the spine_substance column guard (mc-2 #130): the
    # DB trigger rejects UPDATEs to engine-owned columns (body/status/origin)
    # unless this header names an authorized writer. cp-engine owns those
    # columns, so every client we build carries it.
    try:
        client.postgrest.session.headers["X-Spine-Writer"] = "cp-engine"
    except AttributeError:
        pass  # test stubs without a postgrest session; real clients have one
    _client_cache[(url, key)] = client
    return client


def reset_client_cache() -> None:
    """Drop cached clients + resolved creds (tests; after cred swaps)."""
    _client_cache.clear()
    _creds_cache.clear()


# ──────────────────────────────────────────────────────────────────────
#  Typed query helpers (arch-phase-4, #34)
#
#  Home for the ad-hoc queries that used to live inline in CLI command
#  bodies. Each helper takes an already-built client (callers own the
#  get_client / degrade decision), runs ONE query with explicit columns,
#  and documents its caller.
# ──────────────────────────────────────────────────────────────────────


def fetch_clickup_task_id_map(client: "Client", hashes: list[str]) -> dict[str, str]:
    """Map each ``cp_ask_hash`` to its linked ``clickup_task_id``.

    Caller: ``cli_cmds.planning._fetch_clickup_task_ids_for_hashes`` (the
    attention-digest Open-in-ClickUp link decoration). Rows without a task id
    are excluded by the query (``clickup_task_id IS NOT NULL``) and again
    defensively when shaping the map.
    """
    resp = (
        client.table(Tables.CLICKUP_TASK_PROPOSALS)
        .select("cp_ask_hash, clickup_task_id")
        .in_("cp_ask_hash", hashes)
        .not_.is_("clickup_task_id", "null")
        .execute()
    )
    return {
        row["cp_ask_hash"]: row["clickup_task_id"]
        for row in (resp.data or [])
        if row.get("clickup_task_id")
    }


def fetch_element_review_flags(client: "Client", element_id: str) -> list | None:
    """Return one spine element's current ``review_flags`` list, or ``None``
    when no row matches the ``element_id`` (unknown / hallucinated id).

    Caller: ``cli_cmds.spine._write_drift_flags`` (the sweep's advisory
    drift-flag recorder). An existing row with no flags returns ``[]`` —
    distinct from ``None`` so the caller can skip unknown elements without
    overcounting.
    """
    rows = (
        client.table(Tables.SPINE_ELEMENTS)
        .select("review_flags")
        .eq("element_id", element_id)
        .limit(1)
        .execute()
        .data
    ) or []
    if not rows:
        return None
    return list(rows[0].get("review_flags") or [])


def update_element_review_flags(
    client: "Client", element_id: str, review_flags: list
) -> None:
    """Overwrite one spine element's ``review_flags`` column.

    Caller: ``cli_cmds.spine._write_drift_flags`` — always paired with a
    prior :func:`fetch_element_review_flags` read + ``_merge_flag`` merge, so
    the write carries the merged list, never a blind append.
    """
    client.table(Tables.SPINE_ELEMENTS).update(
        {"review_flags": review_flags}
    ).eq("element_id", element_id).execute()


def upsert_spine_snapshot(client: "Client", row: dict) -> None:
    """Upsert one ``spine_snapshots`` index row (conflict key: ``id``).

    Caller: ``cli_cmds.spine.snapshot_cmd`` (`cp snapshot`). Best-effort at
    the callsite — the on-disk frozen file is canonical; this row is only
    the MC-2 index entry.
    """
    client.table(Tables.SPINE_SNAPSHOTS).upsert(row, on_conflict="id").execute()
