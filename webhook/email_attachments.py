"""File an inbound email's attachments into the project's Dropbox (#376).

The cp-email-worker carries each kept attachment as base64 in the signed
payload (``attachments``) and lists the ones it left out
(``attachments_skipped``: tracking pixels, signature logos, over the caps).
For a ``cp+<code>@`` email that resolved to a project, this module:

1. uploads each attachment to
   ``<project Dropbox>/03 Assets/01 Client Assets/Email/<YYYY-MM-DD> <subject-slug>/<filename>``
   with the engine's own upload helper (``project_sources.push_to_dropbox``,
   the one hosted ``push_to_dropbox`` / ``ingest_url`` use);
   The folder is the bound one, else the one inherited from an ancestor
   (``folder_rules.resolve_dropbox_folder``, #396 — the rules ingest uses);
2. checks each uploaded path with ``asset_ingest.ingest_reach_gap`` (the
   same check the hosted verbs run before queueing), and
3. queues ONE file-scoped asset-ingest run for every reachable file, the way
   ``/api/assets/ingest`` does (an ``asset_ingest_runs`` row + the background
   ``_run_asset_ingest``). The webhook IS the ingest service, so it queues
   in-process rather than hopping through mc-2.

A file the ingest would not reach is still uploaded and reported
``uploaded, not queued: <reason>`` (the #350/#370 wording family).

**Never fails the email.** Every error is caught per attachment (or for the
whole step) and reported; the parked email and the distill go ahead.

**Re-delivery does not double-upload.** The folder is built from the email's
own Date header and subject, so a re-delivered message lands on the same
paths. Before uploading, the existing file's Dropbox ``content_hash`` is
compared with the attachment's: the same bytes are reported
``already filed`` and not uploaded again; different bytes under the same name
(two emails, same day, same subject, same ``image001.png``) get the next free
``name (2).ext``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("cp-engine-webhook")

# Where an email's attachments land, under the project's Dropbox folder.
EMAIL_ASSETS_DIR = "03 Assets/01 Client Assets/Email"

# Webhook-side caps. The Worker already enforces these; they are re-checked
# here because the endpoint must not trust its caller's arithmetic.
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024

# Candidate names tried when a different file already holds the name.
_MAX_NAME_ATTEMPTS = 20

_SUBJECT_PREFIX = re.compile(r"^\s*(re|fwd?|aw|wg|tr)\s*:\s*", re.IGNORECASE)
_DROPBOX_DISALLOWED = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class Attachment:
    filename: str
    mime_type: str
    data: bytes


# ── naming ────────────────────────────────────────────────────────────


def subject_slug(subject: str | None) -> str:
    """``Fwd: RE: Deck r02 — feedback!`` → ``deck-r02-feedback``."""
    s = subject or ""
    while True:
        stripped = _SUBJECT_PREFIX.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    slug = slug[:60].rstrip("-")
    return slug or "no-subject"


def email_date(payload: dict) -> str:
    """``YYYY-MM-DD`` from the email's Date header, else when it arrived.

    The Date header is the sender's, so it is the same on every re-delivery
    of the message; ``received_at`` (the Worker's clock) is not.
    """
    raw = (payload.get("date") or "").strip()
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).strftime("%Y-%m-%d")
        except (TypeError, ValueError, IndexError):
            pass
    received = (payload.get("received_at") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", received):
        return received[:10]
    return datetime.now(UTC).strftime("%Y-%m-%d")


def email_folder(payload: dict) -> str:
    """The project-relative folder: ``03 Assets/01 Client Assets/Email/<date> <slug>``."""
    return f"{EMAIL_ASSETS_DIR}/{email_date(payload)} {subject_slug(payload.get('subject'))}"


def safe_filename(name: str | None) -> str:
    """A bare Dropbox-safe file name: no folders, no reserved characters."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _DROPBOX_DISALLOWED.sub("-", base).strip().lstrip(".").strip()
    if len(base) > 200:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 200 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else base[:200]
    return base or "attachment"


def _numbered(name: str, n: int) -> str:
    """``deck.pdf`` → ``deck (2).pdf``."""
    if n <= 1:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem:
        return f"{stem} ({n}).{ext}"
    return f"{name} ({n})"


def dropbox_content_hash(data: bytes) -> str:
    """Dropbox's ``content_hash``: SHA-256 over the SHA-256s of 4 MiB blocks."""
    block = 4 * 1024 * 1024
    digests = b"".join(
        hashlib.sha256(data[i : i + block]).digest() for i in range(0, len(data), block)
    )
    return hashlib.sha256(digests).hexdigest()


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ── payload ───────────────────────────────────────────────────────────


def has_attachments(payload: dict) -> bool:
    return bool(payload.get("attachments")) or bool(payload.get("attachments_skipped"))


def attachment_names(payload: dict) -> list[str]:
    """Names of the attachments the payload carries (for a not-filed note)."""
    out = []
    for item in payload.get("attachments") or []:
        if isinstance(item, dict):
            out.append(safe_filename(item.get("filename")))
    return out


def decode_attachments(payload: dict) -> tuple[list[Attachment], list[dict]]:
    """The payload's attachments as bytes, plus a ``skipped`` row for each one
    left out (by the Worker, or here: bad base64, over a cap)."""
    kept: list[Attachment] = []
    skipped: list[dict] = []

    for item in payload.get("attachments_skipped") or []:
        if isinstance(item, dict):
            skipped.append({
                "filename": safe_filename(item.get("filename")),
                "size": int(item.get("size") or 0),
                "status": f"skipped: {item.get('reason') or 'left out by the mail worker'}",
            })

    total = 0
    for item in payload.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        name = safe_filename(item.get("filename"))
        try:
            data = base64.b64decode(item.get("content_base64") or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            skipped.append({"filename": name, "size": 0,
                            "status": f"skipped: content is not valid base64 ({exc})"})
            continue
        if len(data) > MAX_FILE_BYTES:
            skipped.append({"filename": name, "size": len(data),
                            "status": f"skipped: over the {MAX_FILE_BYTES}-byte per-file cap"})
            continue
        if total + len(data) > MAX_TOTAL_BYTES:
            cap = f"skipped: would exceed the {MAX_TOTAL_BYTES}-byte total cap"
            skipped.append({"filename": name, "size": len(data), "status": cap})
            continue
        total += len(data)
        kept.append(Attachment(filename=name,
                               mime_type=str(item.get("mime_type") or ""), data=data))
    return kept, skipped


# ── seams (monkeypatched in tests) ────────────────────────────────────


def _supabase_client():
    import pipeline

    return pipeline._create_supabase_client()


def _dropbox_connector():
    from cp_engine import asset_ingest

    return asset_ingest._dropbox_connector()


def _resolve_project_id(project_dir: Path, code: str, client) -> str | None:
    """The MC-2 ``projects.id``: the cp.md ``MC-id`` stamp, else by code."""
    from cp_engine.sync import _read_mc_id

    pid = _read_mc_id(project_dir / "cp.md")
    if pid:
        return pid
    from cp_engine.asset_ingest import resolve_project_folders

    folders = resolve_project_folders(client, code)
    return folders.project_id if folders else None


def _existing_content_hash(connector, path: str) -> str | None:
    """The content_hash of the file at ``path``, or None when nothing is there.

    Raises on any error other than not-found (the caller fails that one
    attachment, not the email).
    """
    try:
        meta = connector.dbx.files_get_metadata(path)
    except Exception as exc:  # noqa: BLE001 — classify below
        err = getattr(exc, "error", None)
        try:
            if err is not None and err.is_path() and err.get_path().is_not_found():
                return None
        except Exception:  # noqa: BLE001 — not a Dropbox ApiError shape
            pass
        raise
    # A folder at that path has no content_hash: treat as "taken by other".
    return getattr(meta, "content_hash", None) or "<not-a-file>"


def _queue_ingest(client, code: str, project_id: str, paths: list[str]) -> dict:
    """Queue one file-scoped asset ingest for ``paths`` — what
    ``/api/assets/ingest`` does for the mc-2 picker, done in-process."""
    import pipeline
    from routers import assets as assets_router

    from cp_engine.asset_ingest import _utc_now_iso

    run_id = str(uuid.uuid4())
    assets_router._asset_runs_table(client).insert({
        "id": run_id,
        "project_id": project_id,
        "project_code": code,
        "status": "running",
        "started_at": _utc_now_iso(),
    }).execute()
    pipeline._spawn_background(
        assets_router._run_asset_ingest(run_id, code, project_id, None, list(paths))
    )
    return {"queued": True, "run_id": run_id}


# ── the step ──────────────────────────────────────────────────────────


def _upload_one(engine, connector, folder_path: str, att: Attachment) -> dict:
    """Upload one attachment with dedupe. Returns
    ``{dropbox_path, outcome: 'uploaded'|'already filed'}`` or ``{error}``."""
    want = dropbox_content_hash(att.data)
    for n in range(1, _MAX_NAME_ATTEMPTS + 1):
        name = _numbered(att.filename, n)
        path = f"{folder_path}/{name}"
        existing = _existing_content_hash(connector, path)
        if existing == want:
            return {"dropbox_path": path, "name": name, "outcome": "already filed"}
        if existing is not None:
            continue  # a different file holds this name: try the next one
        with tempfile.TemporaryDirectory(prefix="cp-email-att-") as tmp:
            local = Path(tmp) / "attachment"
            local.write_bytes(att.data)
            result = engine.push_to_dropbox(
                connector, "", str(local), dest_name=name, dest_path=folder_path,
            )
        if result.get("error"):
            return {"error": result["error"]}
        return {"dropbox_path": result.get("dropbox_path") or path, "name": name,
                "outcome": "uploaded"}
    return {"error": f"{_MAX_NAME_ATTEMPTS} different files already use the name "
                     f"{att.filename!r} in {folder_path}"}


def file_attachments(*, code: str, project_dir: Path, payload: dict) -> dict | None:
    """Upload + queue the payload's attachments. None when it carries none.

    Returns ``{folder, folder_inherited_from?, attachments: [{filename, size,
    dropbox_path?, queued, status}], run_id?, error?}``. ``status`` is one line a person can read:
    ``queued`` · ``uploaded, not queued: <reason>`` · ``already filed, queued``
    · ``skipped: <reason>`` · ``not uploaded: <reason>``. Never raises.
    """
    if not has_attachments(payload):
        return None
    kept, skipped = decode_attachments(payload)
    folder_rel = email_folder(payload)
    out: dict = {"folder": folder_rel, "attachments": [], "run_id": None}

    def _all_not_uploaded(reason: str) -> dict:
        out["attachments"] = [
            {"filename": a.filename, "size": len(a.data), "queued": False,
             "status": f"not uploaded: {reason}"}
            for a in kept
        ] + [dict(s, queued=False) for s in skipped]
        out["error"] = reason
        return out

    if not kept:
        out["attachments"] = [dict(s, queued=False) for s in skipped]
        return out

    from cp_engine import folder_rules
    from cp_engine import project_sources as engine
    from cp_engine.asset_ingest import ingest_reach_gap

    client = _supabase_client()
    if client is None:
        return _all_not_uploaded("MC-2 is not configured on the webhook "
                                 "(SUPABASE_URL / SUPABASE_SERVICE_KEY)")
    try:
        project_id = _resolve_project_id(project_dir, code, client)
    except Exception as exc:  # noqa: BLE001
        return _all_not_uploaded(f"could not resolve {code} in MC-2: {type(exc).__name__}: {exc}")
    if not project_id:
        return _all_not_uploaded(f"{code} has no MC-2 project row")
    # Bound, else inherited from an ancestor — the same rules ingest applies
    # (#396), so the queued run reads the folder the files land in.
    try:
        resolved = folder_rules.resolve_dropbox_folder(
            client, project_id, connector_factory=lambda: _dropbox_connector())
    except Exception as exc:  # noqa: BLE001
        return _all_not_uploaded(f"could not read {code}'s folders from MC-2: "
                                 f"{type(exc).__name__}: {exc}")
    folders = resolved.folders
    if not resolved.folder:
        reason = (f"{code} has no Dropbox folder in MC-2 — add it under {code}'s "
                  "Integrations (a project_integrations row, service=dropbox)")
        if resolved.reason:
            reason += f"; nothing to inherit from a parent: {resolved.reason}"
        return _all_not_uploaded(reason)
    try:
        connector = resolved.connector or _dropbox_connector()
    except Exception as exc:  # noqa: BLE001
        return _all_not_uploaded(f"Dropbox connector failed: {type(exc).__name__}: {exc}")

    folder_path = engine.dropbox_destination(connector, resolved.folder, folder_rel)
    if isinstance(folder_path, dict):
        return _all_not_uploaded(folder_path.get("error") or "could not resolve the Dropbox folder")
    out["folder"] = folder_path
    if resolved.inherited_from:
        out["folder_inherited_from"] = resolved.inherited_from

    rows: list[dict] = []
    to_queue: list[str] = []
    for att in kept:
        row = {"filename": att.filename, "size": len(att.data), "queued": False}
        try:
            res = _upload_one(engine, connector, folder_path, att)
        except Exception as exc:  # noqa: BLE001 — one attachment, never the email
            log.warning("inbound-email: attachment %s failed", att.filename, exc_info=True)
            res = {"error": f"{type(exc).__name__}: {exc}"}
        if res.get("error"):
            row["status"] = f"not uploaded: {res['error']}"
            rows.append(row)
            continue
        row["dropbox_path"] = res["dropbox_path"]
        if res["name"] != att.filename:
            row["filed_as"] = res["name"]
        row["outcome"] = res["outcome"]
        gap = ingest_reach_gap(folders, res["dropbox_path"])
        if gap:
            row["status"] = f"{res['outcome']}, not queued: {gap}"
        else:
            to_queue.append(res["dropbox_path"])
        rows.append(row)

    if to_queue:
        try:
            hop = _queue_ingest(client, code, project_id, to_queue)
            out["run_id"] = hop.get("run_id")
            queued_reason = None
        except Exception as exc:  # noqa: BLE001
            log.warning("inbound-email: could not queue attachment ingest", exc_info=True)
            queued_reason = f"the ingest run could not be queued ({type(exc).__name__}: {exc})"
        for row in rows:
            if row.get("dropbox_path") in to_queue:
                if queued_reason is None:
                    row["queued"] = True
                    row["status"] = ("queued" if row["outcome"] == "uploaded"
                                     else "already filed, queued")
                else:
                    row["status"] = f"{row['outcome']}, not queued: {queued_reason}"

    for row in rows:
        row.pop("outcome", None)
    out["attachments"] = rows + [dict(s, queued=False) for s in skipped]
    return out


def render_section(result: dict | None) -> list[str]:
    """The park note's ``## Attachments`` lines (empty when there are none)."""
    if not result:
        return []
    folder = f"Folder: {result.get('folder')}"
    if result.get("folder_inherited_from"):
        folder += f" (inherited from {result['folder_inherited_from']})"
    lines = ["## Attachments", "", folder, ""]
    if result.get("run_id"):
        lines += [f"Ingest run: {result['run_id']}", ""]
    for a in result.get("attachments") or []:
        name = a.get("filename")
        if a.get("filed_as"):
            name = f"{name} (filed as {a['filed_as']})"
        lines.append(f"- {name} ({_human_size(int(a.get('size') or 0))}) — {a.get('status')}")
    lines.append("")
    return lines
