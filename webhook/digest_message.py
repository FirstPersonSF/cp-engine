"""What an attention-digest message should look like NOW (2026-10-05 fix).

The digest is one Slack message with N items, each carrying a row of
buttons. A click is acked inside Slack's 3-second window and the work runs
in the background for 2–11 s; the message is then rewritten through that
click's ``response_url`` with ``replace_original``. The rewrite used to be
built from ``payload.message`` — the message AS IT WAS WHEN THAT BUTTON WAS
CLICKED. Click A, then click B a few seconds later: B's copy still shows A
with its buttons, B's update lands last, and A looks open again although
MC-2 closed it on the first click.

The fix is to stop trusting the click-time copy for anything but layout.
Every item block is re-decided at render time, highest authority first:

1. the clicked item — this render's own confirmation;
2. a FINAL confirmation this process already posted for the item;
3. MC-2 — an ask whose commitment is no longer ``open`` renders closed
   (survives restarts and other processes; never demotes: a snoozed ask is
   still ``open`` in MC-2, so MC-2 is used only to promote to closed);
4. a PENDING ("⏳ Closing…") block this process posted — still in flight;
5. the click-time copy — except a pending block nobody here is working on
   (a restart lost it), which gets its buttons back so it can be retried.

Item blocks this module writes carry a ``block_id`` naming the item
(``cpi|<kind>|<code>|<hash>|<week>|<state>``), so a later render can tell
which item a confirmation belongs to and rebuild its buttons. Renders of
one message are serialised by a per-message lock, held across render+post,
so two finals cannot interleave a read of the overlay with a post.

State is process-local (one webhook replica). MC-2 rule 3 is what keeps a
restart honest for close-ask, the case that broke.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

BLOCK_ID_PREFIX = "cpi"

PENDING = "pending"
CLOSED = "closed"      # close-ask: the commitment is closed in MC-2
DONE = "done"          # any other terminal confirmation (resolved, snoozed, routed…)
FAILED = "failed"
NO_MATCH = "nomatch"

_TERMINAL = {CLOSED, DONE, FAILED, NO_MATCH}

PENDING_LABEL = {
    "close-ask": "⏳ Closing…",
    "resolve-risk": "⏳ Resolving…",
    "snooze-ask": "⏳ Snoozing…",
    "snooze-risk": "⏳ Snoozing…",
    "snooze-ask-7d": "⏳ Snoozing…",
    "snooze-risk-7d": "⏳ Snoozing…",
    "xproj-accept": "⏳ Routing…",
    "xproj-dismiss": "⏳ Dismissing…",
}


def item_kind(verb: str) -> str | None:
    if verb.startswith(("close-ask", "snooze-ask")):
        return "ask"
    if verb.startswith(("resolve-risk", "snooze-risk")):
        return "risk"
    if verb.startswith("xproj-"):
        return "xproj"
    return None


@dataclass(frozen=True)
class Item:
    kind: str
    code: str
    hash: str
    week: str = ""
    state: str = ""  # "" = the original button row

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.code, self.hash)


def item_of(block: dict) -> Item | None:
    """The digest item a block stands for: a button row, or a state block
    this module wrote. Anything else (headers, text, ClickUp links) → None."""
    bid = block.get("block_id") or ""
    if block.get("type") == "context" and bid.startswith(BLOCK_ID_PREFIX + "|"):
        parts = bid.split("|")
        if len(parts) >= 6:
            state = parts[5].split("#", 1)[0]
            return Item(parts[1], parts[2], parts[3], parts[4], state)
        return None
    if block.get("type") != "actions":
        return None
    for el in block.get("elements") or []:
        value = el.get("value") or ""
        parts = value.split("|")
        if len(parts) not in (3, 4):
            continue
        kind = item_kind(parts[0])
        if kind:
            return Item(kind, parts[1], parts[2], parts[3] if len(parts) > 3 else "")
    return None


def block_has_action_id(block: dict, action_id: str) -> bool:
    if not action_id or block.get("type") != "actions":
        return False
    return any(el.get("action_id") == action_id for el in block.get("elements") or [])


def state_block(item: Item, state: str, text: str) -> dict:
    return {
        "type": "context",
        "block_id": "|".join(
            [BLOCK_ID_PREFIX, item.kind, item.code, item.hash, item.week, state]
        ),
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def button_block(item: Item) -> dict:
    """The original button row for an item, rebuilt (same builders as the
    digest, so the payloads match what the digest posted)."""
    from cp_engine.attention_digest import (
        _ask_action_buttons,
        _risk_action_buttons,
        _xproject_action_buttons,
    )

    week = item.week or None
    if item.kind == "ask":
        els = _ask_action_buttons(code=item.code, cp_hash=item.hash, week_iso=week)
    elif item.kind == "risk":
        els = _risk_action_buttons(code=item.code, cp_hash=item.hash, week_iso=week)
    else:
        els = _xproject_action_buttons(
            target_code=item.code, cp_hash=item.hash, week_iso=week
        )
    return {"type": "actions", "elements": els}


def ask_hashes(blocks: list[dict]) -> list[str]:
    """Every close-ask item hash in the message (button rows AND state
    blocks), in order, de-duplicated — the input to the MC-2 read."""
    out: list[str] = []
    for b in blocks:
        it = item_of(b)
        if it and it.kind == "ask" and it.hash not in out:
            out.append(it.hash)
    return out


def closed_ask_hashes(client, hashes: list[str]) -> set[str]:
    """ONE batched MC-2 read: which of these ask hashes belong to a
    commitment that is no longer open. Explicit columns, never ``*``.
    A row keyed only by the text recipe (no ``cp_hash``) is not found here;
    it simply is not promoted (rule 3 never demotes)."""
    if client is None or not hashes:
        return set()
    from cp_engine.asks import OPEN
    from cp_engine.mc2_db import Tables

    rows = (
        client.table(Tables.COMMITMENTS)
        .select("cp_hash, status")
        .in_("cp_hash", hashes)
        .execute()
        .data
        or []
    )
    status_by_hash: dict[str, set[str]] = {}
    for r in rows:
        status_by_hash.setdefault(r.get("cp_hash") or "", set()).add(r.get("status"))
    # A hash with any OPEN row is open (a deliberate repeat of a closed ask).
    return {h for h, st in status_by_hash.items() if h and OPEN not in st}


# ──────────────────────────────────────────────────────────────────────
#  Process-local message state
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _MessageState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    overlay: dict = field(default_factory=dict)  # Item.key -> state block
    last_blocks: list | None = None              # what we last posted


_MAX_MESSAGES = 256
_registry: OrderedDict[str, _MessageState] = OrderedDict()
_registry_lock = threading.Lock()


def message_state(message_key: str) -> _MessageState | None:
    if not message_key:
        return None
    with _registry_lock:
        st = _registry.get(message_key)
        if st is None:
            st = _registry[message_key] = _MessageState()
            while len(_registry) > _MAX_MESSAGES:
                _registry.popitem(last=False)
        else:
            _registry.move_to_end(message_key)
        return st


def reset() -> None:
    """Tests only."""
    with _registry_lock:
        _registry.clear()


def message_key_of(payload: dict) -> str:
    """``<channel>:<ts>`` of the message a block_actions payload came from."""
    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id") or ""
    ts = container.get("message_ts") or (payload.get("message") or {}).get("ts") or ""
    return f"{channel}:{ts}" if ts else ""


def render(
    snapshot_blocks: list[dict],
    *,
    clicked: Item | None,
    clicked_block: dict | None,
    overlay: dict,
    closed_hashes: set[str],
    closed_text: str,
    clicked_action_id: str = "",
) -> tuple[list[dict], bool]:
    """Re-decide every item block (see module docstring for the order).

    Returns ``(blocks, clicked_found)``."""
    out: list[dict] = []
    seen_ids: dict[str, int] = {}
    found = False
    for block in snapshot_blocks:
        if clicked_block is not None and block_has_action_id(block, clicked_action_id):
            out.append(clicked_block)
            found = True
            continue
        it = item_of(block)
        if it is None:
            out.append(block)
            continue
        new = block
        held = overlay.get(it.key)
        held_state = item_of(held).state if held else ""
        if clicked is not None and it.key == clicked.key and clicked_block is not None:
            new = clicked_block
            found = True
        elif held is not None and held_state in _TERMINAL and not (
            it.kind == "ask" and it.hash in closed_hashes and held_state != CLOSED
        ):
            new = held
        elif it.kind == "ask" and it.hash in closed_hashes:
            new = block if it.state == CLOSED else state_block(it, CLOSED, closed_text)
        elif held is not None:
            new = held  # pending: still being worked on here
        elif it.state == PENDING:
            new = button_block(it)  # nobody here is working on it any more
        out.append(new)
    # Slack requires unique block_ids within a message.
    for i, b in enumerate(out):
        bid = b.get("block_id")
        if bid:
            n = seen_ids.get(bid, 0)
            seen_ids[bid] = n + 1
            if n:
                out[i] = {**b, "block_id": f"{bid}#{n}"}
    return out, found
