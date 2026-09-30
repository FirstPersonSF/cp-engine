"""Human curation on a source row, and how to tell it from the engine's (#341).

A re-ingest writes a NEW `rag_assets` row chained to the old one by
`prev_asset_id`. Two columns on the old row can hold a PERSON's words, and a
new row starts with neither:

  - `status_note` — the trust caveat (embargoed, draft, form-gated). A human
    writes it with `set_source_status`; ingest also writes one when the body
    carries a marking (#324), and that one always starts with
    `source_markers._NOTE_PREFIX`. So the prefix is the whole distinction.
  - `description` — what the document IS. The sync summariser writes it, and
    a human may correct it with `set_source_status`. The column itself cannot
    say which: both are one line of prose, and the summariser only ever
    declines to overwrite a non-empty value — it never records authorship.

So authorship is recorded in `meta` (jsonb), where the engine already keeps
ingest bookkeeping:

  - `meta.curation_tracked` — stamped on every row ingest writes from #341 on.
    It is what makes the absence of the next key MEAN something.
  - `meta.description_generated_sha` — the fingerprint of the description the
    summariser persisted. A description that no longer matches it (or that was
    written with no fingerprint at all) was written by someone else.

A row WITHOUT `curation_tracked` predates this and its description is of
unknown authorship; it is treated as generated (not carried). That is the
conservative side: a generated description comes back on the next sync, while
carrying a stale generated one would freeze it — sync never overwrites a
non-empty description.

PURE: rows in, verdicts out. No client, no I/O.
"""

from __future__ import annotations

import hashlib

from cp_engine.source_markers import _NOTE_PREFIX

TRACKED_KEY = "curation_tracked"
GENERATED_SHA_KEY = "description_generated_sha"


def description_fingerprint(text: str | None) -> str | None:
    """A short stable fingerprint of a description (whitespace-trimmed, as
    `rag_asset_set_status` trims what it stores)."""
    if not text or not text.strip():
        return None
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def is_human_note(note: str | None) -> bool:
    """A non-empty `status_note` that ingest's marker detector did not write."""
    return bool(note and note.strip()) and not note.startswith(_NOTE_PREFIX)


def is_hand_written_description(description: str | None, meta: dict | None) -> bool:
    """True iff the row's description was written by a person, as far as the
    row can PROVE it. Unknown authorship (a pre-#341 row) is not proof."""
    fp = description_fingerprint(description)
    if fp is None:
        return False
    meta = meta if isinstance(meta, dict) else {}
    if not meta.get(TRACKED_KEY):
        return False
    return meta.get(GENERATED_SHA_KEY) != fp


def carry_forward_payload(new_row: dict, prior_row: dict | None) -> dict:
    """The UPDATE a freshly ingested row needs so its predecessor's human
    curation survives the re-ingest.

    Always stamps `meta.curation_tracked` (merged into the new row's meta, never
    clobbering it). Adds, only when the new row lacks one:

      - `status_note` — the prior's HUMAN note. An auto-detected note already on
        the new row does not count as having one: a human note outranks a
        detected one. The prior's own auto-detected note is never carried — the
        detector re-reads the new body and decides afresh.
      - `description` — the prior's description, only if hand-written.
    """
    meta = new_row.get("meta") if isinstance(new_row.get("meta"), dict) else {}
    payload: dict = {"meta": {**meta, TRACKED_KEY: True}}
    if not prior_row:
        return payload
    prior_note = prior_row.get("status_note")
    new_note = new_row.get("status_note")
    if is_human_note(prior_note) and not is_human_note(new_note):
        payload["status_note"] = prior_note
    prior_desc = prior_row.get("description")
    if not (new_row.get("description") or "").strip() and is_hand_written_description(
        prior_desc, prior_row.get("meta")
    ):
        payload["description"] = prior_desc
    return payload
