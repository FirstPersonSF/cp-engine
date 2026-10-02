"""Verbs ported from the retired stdio server (architecture plan step 5b).

`cxp mcp` (the local `cp-sources` server) is gone; the hosted server is the
only cp MCP server. These seven verbs were the part of its surface the hosted
server did not already carry. Each is a thin hosted wrapper around an ENGINE
function — the implementation lives in `cp_engine`, not here:

  fetch_project_source     project_sources.fetch_source
  compare_project_sources  project_sources.fetch_source + source_compare.compare_files
  pull_document_comments   project_sources.pull_document_comments
  push_to_dropbox          project_sources.push_to_dropbox / dropbox_destination
  preflight                preflight.read_tree_context + read_mc2_titles + run_preflight
  list_vendors             rfp_pipeline.read_vendors
  list_rfp_respondents     rfp_pipeline.read_respondents

WHAT CHANGED IN THE MOVE. Two things the stdio verbs could assume are not true
of a server the caller reaches over HTTP:

1. **The caller's disk is not this machine's.** Stdio `fetch_project_source`
   returned a `local_path` on the caller's own machine and `push_to_dropbox`
   read one. Here the bytes travel in the result (`content_base64`, capped)
   or, for Dropbox, outside the conversation entirely: fetch returns a
   temporary `download_url`, and push without content returns a temporary
   `upload_url` to POST the file to. A multi-megabyte deck never has to pass
   through the model's context.
2. **Some reads need a credential RLS does not cover.** The asset row is read
   under the caller's JWT (RLS decides whether they may see it); the ORIGINAL
   file is then fetched from Drive/Dropbox with the service's own connector
   credentials. Because those credentials reach past RLS, every verb here that
   uses one also requires team membership (`is_team_member()`, the tree's
   gate), and `push_to_dropbox` — the one writer — is registered on `/mcp`
   only. A missing credential is a clean, named error, never a crash.

REGISTRATION. `register(server_module)` is called from `server.py` before the
strict-arguments pass and before `/mcp/read` is populated, so these verbs get
the audit guarantee, strictness and the read/main classification exactly like
the verbs defined in `server.py`. Helpers are reached through the server
module (`_srv.user_client()` …) AT CALL TIME, so a test that monkeypatches
`server.user_client` patches these verbs too.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

# The server module, bound by `register`. Every helper is looked up on it at
# call time (never imported by value), so monkeypatching the server patches
# these verbs as well.
_srv: Any = None

TOOLS: list = []


def hosted_tool(fn):
    """Mark a function for registration on the hosted server.

    `test_readonly_endpoint._derive_writers` recognises this decorator, so a
    verb defined here is classified by the same AST derivation as one defined
    in `server.py`.
    """
    TOOLS.append(fn)
    return fn


def register(server_module) -> None:
    """Register every `@hosted_tool` on `server_module.mcp_server`."""
    global _srv
    _srv = server_module
    for fn in TOOLS:
        server_module.mcp_server.tool()(fn)


# Where generated work belongs in a project's Dropbox (Drew, 2026-08-03).
SPINE_OUTPUT_DIR = "03 Assets/06 Spine"

# Bytes are carried inline only up to a point: base64 lands in the caller's
# context, where a large file is expensive and a huge one is unusable.
DEFAULT_INLINE_BYTES = 256 * 1024
MAX_INLINE_BYTES = 10 * 1024 * 1024
MAX_PUSH_BYTES = 10 * 1024 * 1024

_DROPBOX_REFRESH_KEYS = ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
_DROPBOX_ACCESS_KEY = "DROPBOX_ACCESS_TOKEN"


# ── credentials ───────────────────────────────────────────────────────


def dropbox_credential_gap() -> str | None:
    """None when the DropboxConnector can authenticate; else what to set."""
    if os.environ.get(_DROPBOX_ACCESS_KEY):
        return None
    missing = [k for k in _DROPBOX_REFRESH_KEYS if not os.environ.get(k)]
    if not missing:
        return None
    return (
        "no Dropbox credentials on the hosted server: set "
        + " + ".join(_DROPBOX_REFRESH_KEYS)
        + f" (or the legacy {_DROPBOX_ACCESS_KEY}) on the hosted-mcp Railway service"
    )


def drive_credential_gap() -> str | None:
    """None when a Google Drive credential is present; else what to set."""
    from cp_engine.mc2_db import DRIVE_CRED_KEYS

    if any(os.environ.get(k) for k in DRIVE_CRED_KEYS):
        return None
    return (
        "no Google Drive credentials on the hosted server: set "
        + " or ".join(DRIVE_CRED_KEYS)
        + " on the hosted-mcp Railway service"
    )


def _with_credential_hint(result: dict[str, Any]) -> dict[str, Any]:
    """Append the missing-credential names to a failed download's error."""
    err = result.get("error")
    if not err or not any(m in err for m in ("download failed", "Drive comments")):
        return result
    gaps = [g for g in (dropbox_credential_gap(), drive_credential_gap()) if g]
    if gaps:
        result = {**result, "error": f"{err} — " + "; ".join(gaps)}
    return result


# ── shared preamble ───────────────────────────────────────────────────


def _team_gate(tool: str, project_code: str | None) -> dict[str, Any] | None:
    """The refusal for a non-team caller, or None. Audited when refused."""
    allowed, denial = _srv.caller_is_team_member()
    if allowed:
        return None
    _srv.audit(_srv.user_client(), tool,
               {"project_code": project_code, "result": "error"}, 0)
    return {"project_code": project_code, "error": denial.replace("tree access", "access")}


def _resolve(client, project_code: str) -> tuple[str, str | None] | None:
    """`(project_id, company_id)` under the caller's identity, or None."""
    pid = _srv.resolve_project_id(client, project_code)
    if pid is None:
        return None
    return pid, _srv.resolve_company_id(client, pid)


def _not_found(client, tool: str, project_code: str) -> dict[str, Any]:
    _srv.audit(client, tool, {"project_code": project_code}, 0)
    return {
        "project_code": project_code,
        "error": f"no project or initiative resolves for code {project_code!r}",
    }


def _looks_like_local_path(doc: str) -> bool:
    return doc.startswith(("/", "~", "./", "../")) or "\\" in doc


# ── reads that need the original file ─────────────────────────────────


@hosted_tool
def fetch_project_source(
    project_code: str, doc_title: str, inline_max_bytes: int = DEFAULT_INLINE_BYTES,
) -> dict[str, Any]:
    """Fetch an ingested source's ORIGINAL file (the binary, not the chunked text).

    Use when the extracted text is not enough — a .pptx's hidden slides, an
    image, the original layout — or when `list_project_sources` flags the
    source `empty` (zero chunks: the original is the only readable copy).
    `doc_title` resolves with the engine's one resolver in lookup mode: a
    source id, else the exact title, else a case-insensitive exact or UNIQUE
    substring match over active sources and the company's account docs;
    several matches return `candidates` (pass one's id).

    The file comes back to YOU, not to a path on this server:

    - `content_base64` — the bytes, when the file is at most
      `inline_max_bytes` (default 256 KB, hard cap 10 MB). Decode and write
      it locally. Larger files return `inline: false` and the size.
    - `download_url` — for a Dropbox-hosted source, a temporary (~4h) direct
      link, whatever the size. From a shell: `curl -L -o <name> '<url>'`.
      Prefer this for anything large.

    Also returns `filename`, `size`, `sha256`, `provider`, `url`, and
    `source_path` — the Dropbox folder the original came FROM, to pass to
    `push_to_dropbox(dest_path=...)` when writing a correction back. Needs
    team membership: the original is read with the service's Drive/Dropbox
    credentials, which reach past RLS.
    """
    tool = "fetch_project_source"
    refused = _team_gate(tool, project_code)
    if refused:
        return refused
    client = _srv.user_client()
    resolved = _resolve(client, project_code)
    if resolved is None:
        return _not_found(client, tool, project_code)
    pid, cid = resolved

    cap = max(0, min(int(inline_max_bytes or 0), MAX_INLINE_BYTES))
    with tempfile.TemporaryDirectory(prefix="cp-fetch-") as tmp:
        result = _srv._engine_project_sources.fetch_source(
            client, pid, doc_title, tmp, company_id=cid,
        )
        if result.get("error"):
            _srv.audit(client, tool, {"project_code": project_code,
                                      "source_title": doc_title, "result": "error"}, 0)
            return {"project_code": project_code, **_with_credential_hint(result)}
        local = Path(result.pop("local_path"))
        data = local.read_bytes()

    out: dict[str, Any] = {
        "project_code": project_code,
        **result,
        "filename": local.name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) <= cap:
        out["inline"] = True
        out["content_base64"] = base64.b64encode(data).decode("ascii")
    else:
        out["inline"] = False
        out["inline_note"] = (
            f"{len(data)} bytes exceeds inline_max_bytes={cap}; "
            + ("use download_url" if result.get("provider") == "dropbox"
               else f"raise inline_max_bytes (max {MAX_INLINE_BYTES})")
        )
    if result.get("provider") == "dropbox" and result.get("source_file_id"):
        link = _dropbox_temporary_link(result["source_file_id"])
        out.update(link)

    _srv.audit(client, tool, {"project_code": project_code,
                              "source_title": doc_title}, 1)
    return out


def _dropbox_temporary_link(file_id: str) -> dict[str, Any]:
    """`{download_url}` or `{download_url_error}` — never raises; best-effort
    beside bytes that were already fetched."""
    try:
        from cloud_storage.dropbox_connector import DropboxConnector

        res = DropboxConnector().dbx.files_get_temporary_link(file_id)
        return {"download_url": res.link, "download_url_expires": "about 4 hours"}
    except Exception as exc:  # noqa: BLE001 — an enrichment, not the result
        return {"download_url_error": f"{type(exc).__name__}: {exc}"}


@hosted_tool
def compare_project_sources(project_code: str, doc_a: str, doc_b: str) -> dict[str, Any]:
    """Structural text diff between two versions of a document (#160).

    The read for feedback that arrives as a REVISED COPY of the artifact
    ("comments added" = comments resolved in): per-slide (pptx) or
    per-section (docx/md) text, aligned by best-match similarity — NOT by
    index, decks get reordered — reporting matched pairs (similarity +
    unchanged/edited/moved), cut units, new units, and thin placeholder
    units. `overall_similarity` also answers "are these duplicates?"
    without pulling either doc into context.

    `doc_a` / `doc_b` are INGESTED source titles (resolved like
    `fetch_project_source`). A local file path is not readable from this
    server: ingest the file first, then compare by title. Needs team
    membership (the originals are read with the service's credentials).
    """
    tool = "compare_project_sources"
    for side, doc in (("doc_a", doc_a), ("doc_b", doc_b)):
        if _looks_like_local_path(doc):
            _srv.audit(_srv.user_client(), tool,
                       {"project_code": project_code, "result": "error"}, 0)
            return {"project_code": project_code, "error": (
                f"{side} looks like a local path ({doc!r}); the hosted server "
                "cannot read your disk — ingest it, then compare by source title"
            )}
    refused = _team_gate(tool, project_code)
    if refused:
        return refused
    client = _srv.user_client()
    resolved = _resolve(client, project_code)
    if resolved is None:
        return _not_found(client, tool, project_code)
    pid, cid = resolved

    from cp_engine.source_compare import compare_files

    with tempfile.TemporaryDirectory(prefix="cp-compare-") as tmp:
        paths = []
        for side, doc in (("doc_a", doc_a), ("doc_b", doc_b)):
            dest = Path(tmp) / side
            dest.mkdir()
            fetched = _srv._engine_project_sources.fetch_source(
                client, pid, doc, str(dest), company_id=cid,
            )
            if fetched.get("error"):
                _srv.audit(client, tool, {"project_code": project_code,
                                          "result": "error"}, 0)
                hinted = _with_credential_hint(fetched)
                return {"project_code": project_code, "error": f"{side}: {hinted['error']}"}
            paths.append(fetched["local_path"])
        try:
            result = compare_files(paths[0], paths[1])
        except ValueError as exc:  # unsupported extension — actionable as-is
            _srv.audit(client, tool, {"project_code": project_code, "result": "error"}, 0)
            return {"project_code": project_code, "error": str(exc)}

    _srv.audit(client, tool, {"project_code": project_code}, 2)
    return {"project_code": project_code, **result}


@hosted_tool
def pull_document_comments(project_code: str, doc_title: str) -> dict[str, Any]:
    """Read the reviewer COMMENTS on an ingested document LIVE (#108).

    Inline comments are the highest-signal stakeholder capture on a project.
    Office-file comments are also ingested into the document's tail (a
    `## Comments` block `pull_project_source` returns); this verb reads the
    live file instead: a Google-Drive-hosted doc via the Drive API (the ONLY
    way to reach Google Docs comments, which exist in no export), otherwise by
    fetching the original .docx/.pptx/.xlsx and parsing its comment XML.
    `doc_title` resolves like `fetch_project_source` (id, exact, else a
    unique match; several return `candidates`).

    Returns {title, provider, comment_count, comments} where each comment is
    {author, date, anchored_text, comment, replies[]} — grouped by author it
    doubles as stakeholder intelligence. `comment_count: 0` means the file was
    read and has none; an unreadable thread is an `error` saying the count is
    UNKNOWN, never a zero. Needs team membership (service credentials).
    """
    tool = "pull_document_comments"
    refused = _team_gate(tool, project_code)
    if refused:
        return refused
    client = _srv.user_client()
    resolved = _resolve(client, project_code)
    if resolved is None:
        return _not_found(client, tool, project_code)
    pid, cid = resolved
    with tempfile.TemporaryDirectory(prefix="cp-comments-") as tmp:
        result = _srv._engine_project_sources.pull_document_comments(
            client, pid, doc_title, tmp, company_id=cid,
        )
    if result.get("error"):
        _srv.audit(client, tool, {"project_code": project_code,
                                  "source_title": doc_title, "result": "error"}, 0)
        return {"project_code": project_code, **_with_credential_hint(result)}
    _srv.audit(client, tool, {"project_code": project_code, "source_title": doc_title},
               int(result.get("comment_count") or 0))
    return {"project_code": project_code, **result}


# ── the one writer ────────────────────────────────────────────────────


@hosted_tool
def push_to_dropbox(
    project_code: str,
    filename: str,
    content_base64: str = "",
    dest_name: str | None = None,
    overwrite: bool = False,
    dest_path: str | None = None,
) -> dict[str, Any]:
    """Put a file INTO a project's Dropbox folder (the write-back path).

    The inverse of `fetch_project_source`: a deck, doc or PDF you generated
    goes back where the humans look for it. Two ways to send the bytes:

    - **`content_base64`** — the file itself, base64-encoded (max 10 MB).
      Uploaded now; returns {dropbox_path, name, size, overwrote}.
    - **omit it** — returns a temporary (~4h) `upload_url` for the same
      destination, plus the `curl` line to POST the file to it. Use this
      from a shell for anything but a small file: the bytes never pass
      through the conversation.

    **`dest_name` DEFAULTS TO `03 Assets/06 Spine/<filename>`** — the
    tenant's home for generated work. Pass a bare name for the project root.
    **To correct a document where it came FROM**, pass `dest_path`: the
    absolute folder from `fetch_project_source`'s `source_path`; `dest_name`
    is then just the file name. Refuses to overwrite unless `overwrite=True`
    (the upload link is no-clobber the same way). The connector has no delete
    or move, so a misplaced copy is removed by hand: when `source_path` is
    None, ASK where it goes, never guess.

    A WRITE with the service's Dropbox credentials, so it needs team
    membership and is not served on the read-only endpoint.
    """
    tool = "push_to_dropbox"
    refused = _team_gate(tool, project_code)
    if refused:
        return refused
    client = _srv.user_client()

    def _fail(error: str) -> dict[str, Any]:
        _srv.audit(client, tool, {"project_code": project_code, "result": "error"}, 0)
        return {"project_code": project_code, "error": error}

    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return _fail("filename must be a bare file name (no folders) — "
                     "put folders in dest_name or dest_path")
    gap = dropbox_credential_gap()
    if gap:
        return _fail(gap)

    resolved = _resolve(client, project_code)
    if resolved is None:
        return _not_found(client, tool, project_code)
    pid, _cid = resolved

    from cp_engine.asset_ingest import resolve_project_folders_by_id

    folders = resolve_project_folders_by_id(client, pid)
    if dest_path is None and (folders is None or not folders.mc_dropbox_folder_id):
        return _fail(f"{project_code!r} has no Dropbox folder configured in MC-2 — "
                     "nowhere to push to (pass dest_path to name one)")
    folder_id = folders.mc_dropbox_folder_id if folders is not None else ""
    if dest_name is None:
        dest_name = name if dest_path else f"{SPINE_OUTPUT_DIR}/{name}"

    try:
        from cloud_storage.dropbox_connector import DropboxConnector

        connector = DropboxConnector()
    except Exception as exc:  # noqa: BLE001 — auth/config: a named error
        return _fail(f"Dropbox connector failed: {type(exc).__name__}: {exc}")

    engine = _srv._engine_project_sources
    if not content_base64:
        destination = engine.dropbox_destination(connector, folder_id, dest_name, dest_path)
        if isinstance(destination, dict):
            return _fail(destination["error"])
        try:
            from dropbox.files import CommitInfo, WriteMode

            link = connector.dbx.files_get_temporary_upload_link(
                commit_info=CommitInfo(
                    path=destination,
                    mode=WriteMode.overwrite if overwrite else WriteMode.add,
                    autorename=False,
                )
            ).link
        except Exception as exc:  # noqa: BLE001
            return _fail(f"could not create an upload link: {type(exc).__name__}: {exc}")
        _srv.audit(client, tool, {"project_code": project_code, "path": destination,
                                  "kind": "upload_link"}, 0)
        return {
            "project_code": project_code,
            "dropbox_path": destination,
            "upload_url": link,
            "upload_url_expires": "about 4 hours",
            "overwrite": bool(overwrite),
            "how": ("curl -X POST '<upload_url>' --header "
                    "'Content-Type: application/octet-stream' "
                    "--data-binary @<local file>"),
        }

    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return _fail(f"content_base64 is not valid base64: {exc}")
    if len(data) > MAX_PUSH_BYTES:
        return _fail(f"{len(data)} bytes exceeds the {MAX_PUSH_BYTES}-byte inline cap — "
                     "omit content_base64 to get an upload_url instead")

    with tempfile.TemporaryDirectory(prefix="cp-push-") as tmp:
        local = Path(tmp) / name
        local.write_bytes(data)
        result = engine.push_to_dropbox(
            connector, folder_id, str(local),
            dest_name=dest_name, overwrite=overwrite, dest_path=dest_path,
        )
    if result.get("error"):
        return _fail(result["error"])
    _srv.audit(client, tool, {"project_code": project_code,
                              "path": result.get("dropbox_path")}, 1)
    return {"project_code": project_code, **result}


# ── pure reads ────────────────────────────────────────────────────────


@hosted_tool
def preflight(project_code: str, artifact_kind: str = "rfp") -> dict[str, Any]:
    """Is this project ready for an artifact to be drafted against it?

    **Run this BEFORE drafting an RFP, SOW, brief or estimate.** It answers
    three questions, in order of severity:

    1. `shape_warning` — is the project even the right KIND of thing? A
       production RFP does not fit a strategy engagement. This fires only
       on positive evidence of the wrong shape, never on thin data.
    2. `found` — what CP actually knows, read broadly: the exec summary
       AND recent sprint files (from the tenant tree), plus spine and
       source titles (from MC-2, under your identity).
    3. `missing` / `conflicts` — what to elicit, and what two sources
       disagree about. Conflicts are surfaced, never silently resolved.

    `ready=false` means do not draft. The commonest cause is an unauthored
    scaffold: a project whose every exec-summary field is still placeholder
    text. `warnings` names any source that could not be read — a verdict
    built without the tree or without MC-2 says so rather than calling the
    project empty.

    **`partner_budget` is always reported missing for an RFP** and must be
    supplied by a human. It is NOT the engagement fee, and this verb will not
    infer one from the other.

    `artifact_kind`: rfp | sow | brief | estimate.
    """
    tool = "preflight"
    from cp_engine.loud import Warnings
    from cp_engine.preflight import (
        ARTIFACT_KINDS,
        read_mc2_titles,
        read_tree_context,
        run_preflight,
    )

    client = _srv.user_client()
    kind = (artifact_kind or "rfp").strip().lower()
    if kind not in ARTIFACT_KINDS:
        _srv.audit(client, tool, {"project_code": project_code, "result": "error"}, 0)
        return {"error": f"unknown artifact_kind '{artifact_kind}'",
                "expected": list(ARTIFACT_KINDS)}

    warnings = Warnings()
    cp_md_text, sprint_texts = None, []
    usable, reason = _srv.tree_available()
    if usable:
        usable, reason = _srv.caller_is_team_member()
    if usable:
        try:
            cp_md_text, sprint_texts = read_tree_context(
                _srv.tree_root(), project_code, warnings,
            )
        except Exception as exc:  # noqa: BLE001 — degraded, and said so
            warnings.add("tenant tree read", exc)
    else:
        warnings.add("tenant tree unavailable — cp.md and sprint files NOT read",
                     RuntimeError(reason))

    spine_titles: list[str] = []
    source_titles: list[str] = []
    pid = None
    try:
        resolved = _resolve(client, project_code)
        if resolved is not None:
            pid, cid = resolved
            spine_titles, source_titles = read_mc2_titles(client, pid, cid)
    except Exception as exc:  # noqa: BLE001 — optional context, loudly absent
        warnings.add("MC-2 spine/source context unavailable", exc)

    report = run_preflight(
        project_code, kind,
        cp_md_text=cp_md_text, sprint_texts=sprint_texts,
        spine_titles=spine_titles, source_titles=source_titles,
    )
    out = report.to_dict()
    if warnings:
        out["warnings"] = list(warnings)
    _srv.audit(client, tool, {"project_code": project_code, "kind": kind},
               1 if report.ready else 0)
    return _srv._with_project_status(out, client, pid, project_code)


@hosted_tool
def list_vendors(capability: str = "", include_archived: bool = False) -> dict[str, Any]:
    """The cross-client partner registry — shops reusable on any project.

    Not spine: a vendor belongs to no project. Filter by `capability` to
    match one capability tag (case-insensitive substring).

    **`email_confidence` is load-bearing.** `confirmed` means read off the
    company's own site; `likely` means two independent sources agree.
    Anything else means there is no usable address — and the answer is never
    to synthesise one, because a bounced RFP reads as carelessness to exactly
    the shops you most want. `watch_outs` is first-class on purpose: "feature
    in production may constrain Q4 capacity" changes the send order.

    Team members only: the table's read policy admits any authenticated
    account, so this verb applies the team gate itself.
    """
    tool = "list_vendors"
    refused = _team_gate(tool, None)
    if refused:
        refused.pop("project_code", None)
        return refused
    from cp_engine.rfp_pipeline import read_vendors

    client = _srv.user_client()
    try:
        result = read_vendors(client, capability, include_archived)
    except Exception as exc:  # noqa: BLE001 — MCP boundary
        _srv.audit(client, tool, {"result": "error"}, 0)
        return {"error": f"failed to list vendors: {type(exc).__name__}: {exc}"}
    _srv.audit(client, tool, {}, result["count"])
    return result


@hosted_tool
def list_rfp_respondents(project_code: str) -> dict[str, Any]:
    """Who was invited to this project's RFP, and where each one got to.

    Ladder: not_sent → sent → acknowledged → responded → shortlisted →
    selected | declined | passed. **`declined` is them saying no; `passed`
    is us not choosing them** — two different facts about a relationship you
    will want again.

    Surfaces two things the table alone would bury: **watch-outs**, which
    change the send order, and respondents with **no confirmed address**,
    which must never be resolved by synthesising one from a pattern. Team
    members only (see `list_vendors`).
    """
    tool = "list_rfp_respondents"
    refused = _team_gate(tool, project_code)
    if refused:
        return refused
    from cp_engine.rfp_pipeline import read_respondents

    client = _srv.user_client()
    resolved = _resolve(client, project_code)
    if resolved is None:
        return _not_found(client, tool, project_code)
    pid, _cid = resolved
    try:
        result = read_respondents(client, pid, project_code)
    except Exception as exc:  # noqa: BLE001 — MCP boundary
        _srv.audit(client, tool, {"project_code": project_code, "result": "error"}, 0)
        return {"project_code": project_code,
                "error": f"failed to list respondents: {type(exc).__name__}: {exc}"}
    _srv.audit(client, tool, {"project_code": project_code},
               len(result["respondents"]))
    return _srv._with_project_status(result, client, pid, project_code)
