"""Slack interactive-component route + background action plumbing.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, date

import git_ops
import observability
import pipeline
import signatures
from fastapi import APIRouter, HTTPException, Request

from cp_engine.ingest import execute_plan

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


@router.post("/slack-action")
async def slack_action(request: Request) -> dict:
    """Handle a Slack interactive-component click.

    Slack POSTs `application/x-www-form-urlencoded` with a single `payload`
    field containing the JSON. We verify the signature, parse the payload,
    route by `value` prefix, and IMMEDIATELY return 200 (Slack's 3-second
    ack window). Actual work happens in a background asyncio task.

    `value` format: `<verb>|<code>|<hash>` for fixed-action buttons.
    For `snooze-{ask,risk}-pick`, the click opens a modal via views.open
    inline (the trigger_id expires after 3s, so this can't be backgrounded);
    the actual snooze happens on the subsequent `view_submission`.

    Reference: https://api.slack.com/interactivity/handling
    """
    raw_body = await request.body()
    signatures._verify_slack_signature(
        raw_body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
    )

    import urllib.parse as _up
    form = _up.parse_qs(raw_body.decode("utf-8"))
    payload_raw = form.get("payload", [""])[0]
    if not payload_raw:
        raise HTTPException(400, "missing payload field")
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"invalid JSON in payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload must be a JSON object")

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        return await _handle_block_action(payload)
    if payload_type == "view_submission":
        return await _handle_view_submission(payload)
    return {"ok": True, "ignored": payload_type}


async def _handle_block_action(payload: dict) -> dict:
    """Acknowledge IMMEDIATELY; do the work in a background task."""
    actions = payload.get("actions") or []
    if not actions:
        raise HTTPException(400, "no actions in payload")
    action = actions[0]
    value = action.get("value") or ""
    # Slack guarantees action_id is unique per block-element. v0.14's
    # button emitters namespace it as `<verb>_<code>_<hash>` so even
    # duplicate-hash items still produce unique ids. _post_response_url_update
    # uses this to find and surgically replace ONLY the clicked item's
    # actions block (Bug 1 in v0.14.0/1: walking ALL actions blocks made
    # one click visually close every item in the digest).
    action_id = action.get("action_id") or ""
    user_id = (payload.get("user") or {}).get("id", "")
    response_url = payload.get("response_url") or ""
    trigger_id = payload.get("trigger_id", "")
    original_message = payload.get("message", {})

    parts = value.split("|")
    if len(parts) not in (3, 4):
        raise HTTPException(400, f"malformed value: {value!r}")
    verb, code, cp_hash = parts[0], parts[1], parts[2]
    # 4th part (added fix/slack-close-week-context): the sprint week the
    # digest was rendered for. Old 3-part buttons still in Slack → None →
    # execute_plan defaults to the current week (unchanged behavior).
    week_iso = parts[3] if len(parts) > 3 else None

    # Snooze-pick: modal must open NOW (trigger_id expires in 3s). views.open
    # is one API call (~200-400ms); within budget. Run via asyncio.to_thread
    # so the sync slack_sdk call doesn't block the loop, but AWAIT before
    # returning (the modal must be open before we ack).
    if verb.endswith("-pick"):
        underlying_verb = verb.replace("-pick", "")
        await asyncio.to_thread(
            _open_snooze_modal,
            trigger_id=trigger_id,
            verb=underlying_verb,
            code=code,
            cp_hash=cp_hash,
            response_url=response_url,
            week_iso=week_iso,
        )
        return {"ok": True}

    # Cross-project routing proposals (#88): Accept routes the item to the
    # target project's sprint file, Dismiss archives the proposal. `code`
    # in the pipe payload is the TARGET project; cp_hash keys the MC-2
    # cross_project_proposals row. Dedicated background path — the generic
    # one below builds hash-only plans for existing sprint bullets, which
    # is the wrong shape here.
    if verb in ("xproj-accept", "xproj-dismiss"):
        log.info(
            "slack_action_spawn code=%s verb=%s hash=%s action_id=%s user=%s",
            code, verb, cp_hash, action_id, user_id,
        )
        pipeline._spawn_background(_run_xproject_in_background(
            verb=verb, target_code=code, cp_hash=cp_hash,
            response_url=response_url, original_message=original_message,
            clicked_action_id=action_id,
        ))
        return {"ok": True, "queued": True}

    extras: dict = {"closed_by": "slack", "user": user_id}

    if verb in ("snooze-ask-7d", "snooze-risk-7d"):
        from datetime import timedelta
        underlying_verb = verb.replace("-7d", "")
        extras["until"] = (date.today() + timedelta(days=7)).isoformat()
        verb = underlying_verb

    # Dispatch the slow path (clone + plan + push + Slack update) to a
    # background task via _spawn_background (strong-ref retention).
    #
    # Structured-log every spawn so a Railway restart that interrupts the
    # background task is recoverable from logs (postmortem: grep for
    # `slack_action_spawn` and replay any missing `slack_action_complete`
    # in the same time window).
    # TODO(v0.16): persist to slack_action_intents for automatic recovery
    # sweep on restart — until we see real drops in prod, structured
    # logs are good enough.
    log.info(
        "slack_action_spawn code=%s verb=%s hash=%s action_id=%s user=%s",
        code, verb, cp_hash, action_id, user_id,
    )
    pipeline._spawn_background(_run_action_in_background(
        verb=verb, code=code, cp_hash=cp_hash, extras=extras,
        response_url=response_url, original_message=original_message,
        clicked_action_id=action_id, week_iso=week_iso,
    ))
    return {"ok": True, "queued": True}


async def _handle_view_submission(payload: dict) -> dict:
    """Date-picker modal submission. Validates synchronously (so we can
    return inline field errors), then backgrounds the plan run + response
    update so we ack Slack inside the 3-second window."""
    view = payload.get("view") or {}
    if view.get("callback_id") != "snooze_until_modal":
        # Unknown modal — DO NOT return `response_action: clear` (that would
        # close a modal we don't own). Return `{"ok": True, "ignored": ...}`.
        return {"ok": True, "ignored": view.get("callback_id")}
    # Defensive: Slack echoes private_metadata verbatim, but version skew or
    # test traffic could feed garbage. Bare json.loads → 500 → Slack retries 3x.
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except json.JSONDecodeError:
        log.warning("view_submission has malformed private_metadata; ignoring")
        return {"ok": True, "ignored": "malformed metadata"}
    verb = meta.get("verb")
    code = meta.get("code")
    cp_hash = meta.get("hash")
    response_url = meta.get("response_url", "")
    week_iso = meta.get("week_iso")  # None for old modals; execute_plan defaults
    # Schema-drift / replay guard: missing fields → inline modal error rather
    # than silently dispatching a nonsense plan (verb=None, code=None, …).
    if not (verb and code and cp_hash):
        log.warning("view_submission missing required fields: %s", meta)
        return {
            "response_action": "errors",
            "errors": {"date_block": "Snooze request expired; please click the button again."},
        }

    state = view.get("state", {}).get("values", {})
    until = (
        state.get("date_block", {})
             .get("until_date", {})
             .get("selected_date", "")
    )
    if not until:
        return {
            "response_action": "errors",
            "errors": {"date_block": "Pick a date"},
        }

    # Same structured-log shape as _handle_block_action so a single
    # logfilter catches both spawn paths.
    log.info(
        "slack_action_spawn code=%s verb=%s hash=%s action_id=%s "
        "source=view_submission until=%s",
        code, verb, cp_hash, "", until,
    )
    pipeline._spawn_background(_run_action_in_background(
        verb=verb, code=code, cp_hash=cp_hash,
        extras={"until": until},  # No closed_by — only meaningful for close/resolve verbs
        response_url=response_url,
        original_message={},  # modal submission has no original to splice into
        week_iso=week_iso,
    ))
    return {"response_action": "clear"}


async def _run_action_in_background(
    *,
    verb: str, code: str, cp_hash: str, extras: dict,
    response_url: str, original_message: dict,
    clicked_action_id: str = "",
    week_iso: str | None = None,
) -> None:
    """Background coroutine: run the cp plan via to_thread, update Slack.

    Wraps sync work (git subprocess, slack_sdk sync calls, requests.post)
    via asyncio.to_thread so the event loop stays free.

    `clicked_action_id` is threaded through to _post_response_url_update so
    that only the actions block carrying that action_id is replaced with
    a confirmation (Bug 1 fix in v0.14.2: previously the loop replaced
    EVERY actions block in the message, so one click visually closed all
    digest items at once).

    Logs but never raises — exceptions here would be lost. Surface failures
    via the in-place Slack message instead.
    """
    try:
        result = await asyncio.to_thread(
            _run_plan_for_one_item,
            verb=verb, code=code, cp_hash=cp_hash, week_iso=week_iso, **extras,
        )
    except Exception as exc:  # noqa: BLE001 — background must not crash
        log.exception(
            "slack-action background failed: %s/%s/%s", verb, code, cp_hash
        )
        observability.capture(exc, area="slack_action_background")
        result = {"committed": False, "commit_sha": None, "errors": [str(exc)]}

    # Pairs with `slack_action_spawn` so postmortem can correlate spawns
    # with completions and identify clicks that never wrapped up (Railway
    # restart mid-task). action_id isn't in scope here; correlation key
    # is (code, verb, hash) which is unique per displayed digest item.
    log.info(
        "slack_action_complete code=%s verb=%s hash=%s committed=%s "
        "commit_sha=%s errors=%d",
        code, verb, cp_hash,
        result.get("committed"),
        (result.get("commit_sha") or "")[:8],
        len(result.get("errors") or []),
    )

    confirmation = _confirmation_text(verb=verb, extras=extras, result=result)
    try:
        await asyncio.to_thread(
            _post_response_url_update,
            response_url=response_url,
            original_message=original_message,
            confirmation=confirmation,
            clicked_action_id=clicked_action_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "slack-action response_url update failed: %s/%s", code, cp_hash
        )


def _run_plan_for_one_item(
    *, verb: str, code: str, cp_hash: str, week_iso: str | None = None, **extras
) -> dict:
    """Clone tenant, run a 1-item plan for the given verb, commit, push.

    Mirrors `_perform_auto_ingest`'s shape but for the tiny resolve/snooze/
    close plans triggered by Slack button clicks.
    """
    item: dict = {"hash": cp_hash}
    if "until" in extras:
        item["until"] = extras["until"]
    if "closed_by" in extras:
        item["closed_by"] = extras["closed_by"]

    plan = {"projects": {code: {verb: [item]}}}

    with git_ops._cloned_tenant() as tenant_root:
        # Slack-button plans are close-ask / snooze-ask / resolve-risk — no
        # ClickUp-proposal verbs in scope here. Pass the client for parity
        # (cheap) and meeting_id=None (this is a Slack click, not a meeting).
        result = execute_plan(
            plan,
            tenant_root=tenant_root,
            today=date.today(),
            # The digest embedded the week it was rendered for; route the
            # close/resolve/snooze to THAT week's sprint file. None (old
            # 3-part buttons) → execute_plan defaults to the current week.
            week_iso=week_iso,
            supabase=pipeline._create_supabase_client(),
            meeting_id=None,
        )

        ingested_entry = {
            "code": code,
            "files_written": [str(p) for p in result.files_written],
            "errors": result.errors,
            "plan_summary": {verb: 1},
        }
        if result.files_written:
            commit_sha = git_ops._commit_and_push(
                tenant_root=tenant_root,
                meeting_id=f"slack-{verb}-{cp_hash}",
                ingested=[ingested_entry],
            )
            return {
                "committed": True,
                "commit_sha": commit_sha,
                "errors": result.errors,
            }
        return {
            "committed": False,
            "commit_sha": None,
            "errors": result.errors,
        }


def _post_response_url_update(
    *,
    response_url: str,
    original_message: dict,
    confirmation: str,
    clicked_action_id: str = "",
) -> None:
    """POST to Slack's `response_url` to replace ONLY the clicked item's
    actions block with the confirmation context.

    The original digest message has N items, each with its own actions
    block carrying 3 buttons. When a user clicks one button, we want
    the corresponding actions block (and ONLY that one) replaced with
    "✅ Resolved · 10:42 AM UTC · `abc12345`" — every OTHER item must
    keep its action buttons intact.

    `clicked_action_id` is the `action_id` of the button the user
    clicked. We walk the message blocks and only replace the actions
    block that contains an element with that action_id.

    Fallback: if `clicked_action_id` is empty (older view_submission
    code path that doesn't have a single source block), fall through
    to the old replace-all behavior — modal submissions pass
    `original_message={}` anyway, so no blocks are touched.
    """
    import requests as _req

    def _block_contains_action_id(block: dict, target_id: str) -> bool:
        if not target_id or block.get("type") != "actions":
            return False
        for el in block.get("elements", []) or []:
            if el.get("action_id") == target_id:
                return True
        return False

    new_blocks: list[dict] = []
    replaced = False
    for block in original_message.get("blocks", []):
        if _block_contains_action_id(block, clicked_action_id):
            new_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": confirmation}],
            })
            replaced = True
        elif block.get("type") == "actions" and not clicked_action_id:
            # Backward-compat fallback: no clicked_action_id provided,
            # collapse all actions blocks (legacy view_submission path).
            new_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": confirmation}],
            })
            replaced = True
        else:
            new_blocks.append(block)

    if not replaced and clicked_action_id:
        # The action_id we were told to replace wasn't found in the
        # message — e.g. message was edited mid-click, or Slack
        # delivered a stale message blob. Log it; don't silently
        # disappear the confirmation.
        log.warning(
            "response_url update: action_id %r not found in message blocks; "
            "no in-place update applied",
            clicked_action_id,
        )

    resp = _req.post(response_url, json={
        "replace_original": True,
        "blocks": new_blocks,
        "text": confirmation,
    }, timeout=5)
    if not resp.ok:
        log.warning(
            "response_url update returned %s: %s",
            resp.status_code, resp.text[:200],
        )


async def _run_xproject_in_background(
    *,
    verb: str, target_code: str, cp_hash: str,
    response_url: str, original_message: dict,
    clicked_action_id: str = "",
) -> None:
    """Background coroutine for cross-project proposal decisions (#88).

    Mirrors `_run_action_in_background`'s shape: sync work via to_thread,
    logs-never-raises, surgical response_url update."""
    try:
        result = await asyncio.to_thread(
            _run_xproject_action,
            verb=verb, target_code=target_code, cp_hash=cp_hash,
        )
    except Exception as exc:  # noqa: BLE001 — background must not crash
        log.exception(
            "xproject background failed: %s/%s/%s", verb, target_code, cp_hash
        )
        observability.capture(exc, area="slack_action_background")
        result = {"committed": False, "commit_sha": None, "errors": [str(exc)]}

    log.info(
        "slack_action_complete code=%s verb=%s hash=%s committed=%s "
        "commit_sha=%s errors=%d",
        target_code, verb, cp_hash,
        result.get("committed"),
        (result.get("commit_sha") or "")[:8],
        len(result.get("errors") or []),
    )

    confirmation = _xproject_confirmation_text(
        verb=verb, target_code=target_code, result=result
    )
    try:
        await asyncio.to_thread(
            _post_response_url_update,
            response_url=response_url,
            original_message=original_message,
            confirmation=confirmation,
            clicked_action_id=clicked_action_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "xproject response_url update failed: %s/%s", target_code, cp_hash
        )


def _run_xproject_action(*, verb: str, target_code: str, cp_hash: str) -> dict:
    """Decide one cross-project proposal (sync worker).

    Dismiss: stamp the MC-2 row dismissed (no tenant clone).
    Accept: build the one-item routed plan, clone the tenant, write it to
    the target project's CURRENT sprint file (provenance marker + the
    usual hash-dedup), commit + push, stamp the row accepted.
    """
    from cp_engine.cross_project import build_routed_plan, decide, get_by_hash

    client = pipeline._create_supabase_client()
    if client is None:
        return {"committed": False, "commit_sha": None,
                "errors": ["MC-2 (Supabase) unavailable"]}

    proposal = get_by_hash(client, cp_hash)
    if proposal is None:
        return {"committed": False, "commit_sha": None,
                "errors": [f"proposal {cp_hash} not found"]}
    if proposal.get("status") != "pending":
        return {"committed": False, "commit_sha": None, "errors": [],
                "already": proposal.get("status")}

    if verb == "xproj-dismiss":
        decide(client, proposal["id"], "dismissed")
        return {"committed": False, "commit_sha": None, "errors": [],
                "dismissed": True}

    plan = build_routed_plan(proposal)
    with git_ops._cloned_tenant() as tenant_root:
        result = execute_plan(
            plan,
            tenant_root=tenant_root,
            today=date.today(),
            supabase=client,
            meeting_id=proposal.get("meeting_id"),
        )
        commit_sha = None
        if result.files_written:
            ingested_entry = {
                "code": proposal["target_code"],
                "files_written": [str(p) for p in result.files_written],
                "errors": result.errors,
                "plan_summary": {proposal["verb"]: 1},
            }
            commit_sha = git_ops._commit_and_push(
                tenant_root=tenant_root,
                meeting_id=f"xproj-{cp_hash}",
                ingested=[ingested_entry],
            )
    if result.errors and not result.files_written:
        # Nothing landed — keep the proposal pending so the human can retry.
        return {"committed": False, "commit_sha": None, "errors": result.errors}
    # Routed (or an identical bullet was already there — hash dedupe):
    # either way the routing decision is settled.
    decide(client, proposal["id"], "accepted", routed_commit_sha=commit_sha)
    return {"committed": bool(commit_sha), "commit_sha": commit_sha,
            "errors": result.errors, "accepted": True}


def _xproject_confirmation_text(*, verb: str, target_code: str, result: dict) -> str:
    """Post-click confirmation for cross-project decisions (#88)."""
    from datetime import datetime as _datetime
    now_str = _datetime.now(UTC).strftime("%I:%M %p UTC").lstrip("0")
    errors = result.get("errors") or []
    if errors and not result.get("accepted") and not result.get("dismissed"):
        return f"⚠️ Action failed: {errors[0][:120]}"
    if result.get("already"):
        return f"ℹ️ Already {result['already']} · {now_str}"
    if result.get("dismissed"):
        return f"✖️ Dismissed · {now_str}"
    sha = result.get("commit_sha")
    sha_str = f" · `{sha[:8]}`" if sha else " · already in sprint file"
    return f"✅ Routed to `{target_code}` · {now_str}{sha_str}"


def _confirmation_text(*, verb: str, extras: dict, result: dict) -> str:
    """Human-readable confirmation rendered in the post-click message.

    Uses `%I` (zero-padded) rather than `%-I` (unpadded extension) for
    cross-platform consistency.
    """
    from datetime import datetime as _datetime
    now_str = _datetime.now(UTC).strftime("%I:%M %p UTC").lstrip("0")
    label = {
        "resolve-risk": "✅ Resolved",
        "close-ask": "✅ Closed",
        "snooze-ask": f"💤 Snoozed until {extras.get('until', '?')}",
        "snooze-risk": f"💤 Snoozed until {extras.get('until', '?')}",
    }.get(verb, "✓ Done")
    sha = result.get("commit_sha")
    sha_str = f" · `{sha[:8]}`" if sha else ""
    errors = result.get("errors") or []
    if errors and not sha:
        return f"⚠️ Action failed: {errors[0][:120]}"
    # Silent dedupe: hash not in current sprint file (item already resolved
    # on a previous click, or rolled forward to a different sprint). The
    # resolve-risk / snooze-* writers treat this as a no-op (per Task 1.1
    # / 1.2 patterns). Surface to the user so the message isn't misleading.
    if not result.get("committed"):
        return f"ℹ️ No matching item (already resolved or moved sprint) · {now_str}"
    return f"{label} · {now_str}{sha_str}"


def _open_snooze_modal(
    *, trigger_id: str, verb: str, code: str, cp_hash: str, response_url: str,
    week_iso: str | None = None,
) -> None:
    """Open a Slack modal with a date picker; the actual snooze happens
    on the subsequent view_submission callback.

    `verb` is the underlying verb without the -pick suffix: 'snooze-ask' or
    'snooze-risk'. Packed into private_metadata so _handle_view_submission
    can route the submission to the right plan.
    """
    from datetime import date, timedelta

    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise HTTPException(500, "SLACK_BOT_TOKEN not configured")
    client = WebClient(token=token)
    private_metadata = json.dumps({
        "verb": verb, "code": code, "hash": cp_hash, "response_url": response_url,
        # Carry the digest's sprint week through the modal round-trip so the
        # snoozed item resolves against the right sprint file (see
        # fix/slack-close-week-context). None for old buttons.
        "week_iso": week_iso,
    })
    client.views_open(trigger_id=trigger_id, view={
        "type": "modal",
        "callback_id": "snooze_until_modal",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Snooze until"},
        "submit": {"type": "plain_text", "text": "Snooze"},
        "blocks": [{
            "type": "input",
            "block_id": "date_block",
            "label": {"type": "plain_text", "text": f"Snooze {code} until:"},
            "element": {
                "type": "datepicker",
                "action_id": "until_date",
                "initial_date": (date.today() + timedelta(days=7)).isoformat(),
            },
        }],
    })
