"""Author a partner Note into MC-2 (in-app unread + Slack DM) — the write side
of the `create_note` MCP verb.

MC-2's Notes feature (mig 116, 2026-07-18) is normally authored through the
dashboard's `POST /{code}/notes`, which resolves the author from the request's
Supabase JWT and fires the Slack DM server-side. The MCP has no JWT and the
endpoint has no service-token path, so this module reproduces the endpoint's two
effects using the SAME service key the other MCP write verbs use:

  1. insert the `notes` row (status='unread'), and
  2. deliver the Slack DM (reusing cp-engine's `slack.post_dm`, with the Block Kit
     layout mirrored from mc-2's `notes_slack.py`).

Recipient/author resolve by NAME or EMAIL against `entities` (agents know
"Marcello", not a UUID). Delivery never blocks the insert: the note exists in-app
regardless, and `slack_delivery` records what happened — parity with the endpoint.

The DM block layout is the shared `note-dm-format` component
(1p-component-library), also used by mc-2's `src/notes_slack.py` — one
definition for both senders (#161).
"""

from __future__ import annotations

from typing import Any, Optional

from note_dm_format import note_dm_blocks

from cp_engine.mc2_db import Tables
# The tenant's default acting partner when no author is given (the MCP runs as
# Drew). Overridable per call; resolved against entities like any recipient.
_DEFAULT_AUTHOR_EMAIL = "drew@firstperson.is"


def _resolve_entity(client, who: str) -> Optional[dict]:
    """Resolve `who` (an email or a display-name substring) to ONE entity row,
    or None on no-match / ambiguity. Email is matched exact (case-insensitive);
    a name is matched as a case-insensitive substring — a single distinct hit
    wins, 2+ is ambiguous (None)."""
    cols = "id, name, email, slack_user_id"
    who = (who or "").strip()
    if not who:
        return None
    if "@" in who:
        rows = (
            client.table(Tables.ENTITIES).select(cols)
            .ilike("email", who).execute().data
        ) or []
        return rows[0] if len(rows) == 1 else None
    # Name path: pull candidates and match distinct case-insensitively.
    rows = (
        client.table(Tables.ENTITIES).select(cols)
        .ilike("name", f"%{who}%").execute().data
    ) or []
    distinct = {r["id"]: r for r in rows}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def _deliver_dm(client, config, *, recipient: dict, author_name: str,
                project_label: str, project_id: str,
                body: str,
                errors: list | None = None) -> tuple[str, Optional[str]]:
    """Send the note as a Slack DM. Returns (status, ts) where status is
    'sent' | 'failed' | 'skipped'. Never raises — the note already exists
    in-app; delivery is best-effort, mirroring the endpoint.

    'skipped' means there is nothing to deliver TO (no Slack configured, no
    email, no Slack user for the email). A Slack API error is 'failed', never
    'skipped' (step 3; cf. #227 — "bot not in channel" read as a quiet
    week), and its reason is appended to ``errors`` when given."""
    from cp_engine import slack as slack_mod

    # Resolve the recipient's Slack user id (cache on the entity if we look it
    # up by email), exactly like mc-2 notes_slack.py.
    sid = recipient.get("slack_user_id")
    try:
        token = slack_mod.load_slack_token(config)
    except Exception:  # noqa: BLE001 — no Slack configured → in-app only
        return "skipped", None
    from slack_sdk import WebClient

    web = WebClient(token=token)
    if not sid:
        email = recipient.get("email")
        if not email:
            return "skipped", None
        try:
            resp = web.users_lookupByEmail(email=email)
            sid = (resp.get("user") or {}).get("id") if resp.get("ok") else None
        except Exception as exc:  # noqa: BLE001
            # `users_not_found` is a real "nobody to DM" answer; anything else
            # (auth, missing scope, network) is a delivery FAILURE.
            code = None
            try:
                code = exc.response.get("error")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — diagnostics only
                code = None
            if code == "users_not_found":
                return "skipped", None
            if errors is not None:
                errors.append(
                    f"Slack user lookup failed: {code or type(exc).__name__}: {exc}"
                )
            return "failed", None
        if not sid:
            return "skipped", None
        try:  # cache for next time; a failure here isn't fatal
            client.table(Tables.ENTITIES).update({"slack_user_id": sid}).eq(
                "id", recipient["id"]).execute()
        except Exception:  # noqa: BLE001
            pass

    fallback, blocks = note_dm_blocks(author_name, project_label, body, project_id)
    try:
        ts = slack_mod.post_dm(web, user_id=sid, text=fallback, blocks=blocks)
        return "sent", ts
    except Exception as exc:  # noqa: BLE001 — DM failure is recorded, not raised
        if errors is not None:
            errors.append(f"Slack DM failed: {exc}")
        return "failed", None


def write_note(client, config, *, project_code: str, project_id: str,
               recipient: str, body: str,
               author: str | None = None) -> dict:
    """Author a partner note against `project_code` and deliver the Slack DM.

    Resolves `recipient` and `author` (name or email) to entities, inserts the
    `notes` row (status='unread'), then best-effort delivers the DM. Returns a
    structured dict: {note_id, recipient, author, slack_delivery[, slack_ts]},
    or {note: ...} on a resolution miss. Raises only on an actual insert failure.
    """
    import uuid
    from datetime import datetime, timezone

    text = (body or "").strip()
    if not text:
        return {"note": "body is required"}

    author_who = (author or "").strip() or _DEFAULT_AUTHOR_EMAIL
    author_row = _resolve_entity(client, author_who)
    if author_row is None:
        return {"note": f"no single entity matching author '{author_who}'"}

    recipient_row = _resolve_entity(client, recipient)
    if recipient_row is None:
        return {"note": f"no single entity matching recipient '{recipient}'"}
    if recipient_row["id"] == author_row["id"]:
        return {"note": "author and recipient resolve to the same person"}

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": str(uuid.uuid4()),
        "project_code": project_code,
        "author_id": author_row["id"],
        "recipient_id": recipient_row["id"],
        "body": text,
        "status": "unread",
        "slack_delivery": "pending",
        "created_at": now,
    }
    result = client.table(Tables.NOTES).insert(row).execute()
    if not result.data:
        raise RuntimeError("failed to insert note row")

    delivery_errors: list[str] = []
    delivery, slack_ts = _deliver_dm(
        client, config, recipient=recipient_row,
        author_name=author_row.get("name") or "A partner",
        project_label=project_code, project_id=project_id, body=text,
        errors=delivery_errors,
    )
    # Record what the DM did, mirroring the endpoint (visible, not silent).
    patch: dict[str, Any] = {"slack_delivery": delivery}
    if slack_ts:
        patch["slack_ts"] = slack_ts
    warnings: list[str] = []
    try:
        client.table(Tables.NOTES).update(patch).eq("id", row["id"]).execute()
    except Exception as exc:  # noqa: BLE001 — status write is best-effort
        # The row still says 'pending' while this result says otherwise; the
        # caller must know the two disagree.
        warnings.append(
            f"delivery status not recorded on the note row (it still reads "
            f"'pending'): {type(exc).__name__}: {exc}"
        )

    return {
        "note_id": row["id"],
        "recipient": recipient_row.get("name") or recipient_row.get("email"),
        "author": author_row.get("name") or author_row.get("email"),
        "slack_delivery": delivery,
        **({"slack_ts": slack_ts} if slack_ts else {}),
        **({"slack_error": delivery_errors[0]} if delivery_errors else {}),
        **({"warnings": warnings} if warnings else {}),
    }
