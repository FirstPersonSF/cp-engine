"""Integration ids/urls from MC-2's ``project_integrations`` bindings.

Phase B of the flat-column retirement (mc-2 PR #146 was Phase A): MC-2's
``projects.slack_channel_id`` / ``clickup_list_id`` / folder-id columns are
being replaced by one row per (owner × service × label) in
``project_integrations``, with the connection coordinates in a normalized
``external_ref`` ``{id, url, extra}``. cp-engine used to SELECT the flat
columns directly; this module reads bindings instead and overlays the same
legacy keys onto the fetched rows (hydration), so every downstream consumer
— the Slack channel map, ClickUp routing, asset ingest — keeps reading the
dict keys it always has. Once every reader everywhere is on bindings, the
flat columns get dropped (Phase C).

Label semantics: ``label == ''`` is the singleton binding that mirrors the
old scalar column; named labels are extra bindings (e.g. related Slack
channels) with no flat-column equivalent.
"""

from __future__ import annotations

from typing import Any

BINDINGS_TABLE = "project_integrations"

_SELECT = "project_id, initiative_id, service, external_ref, label"


def fetch_binding_rows(
    client: Any,
    *,
    project_ids: tuple[str, ...] | list[str] = (),
    initiative_ids: tuple[str, ...] | list[str] = (),
) -> dict[str, list[dict]]:
    """Batch-fetch bindings for many owners → ``{owner_id: [rows]}``.

    One query per owner kind. Non-list responses (e.g. loose test mocks)
    collapse to "no rows" rather than crashing.
    """
    out: dict[str, list[dict]] = {}
    for owner_col, ids in (
        ("project_id", list(project_ids)),
        ("initiative_id", list(initiative_ids)),
    ):
        if not ids:
            continue
        data = (
            client.table(BINDINGS_TABLE)
            .select(_SELECT)
            .in_(owner_col, ids)
            .execute()
            .data
        )
        if not isinstance(data, list):
            continue
        for row in data:
            owner = row.get(owner_col)
            if owner:
                out.setdefault(owner, []).append(row)
    return out


def _singleton_ref(binding_rows: list[dict] | None, service: str) -> dict:
    for row in binding_rows or []:
        if row.get("service") == service and not row.get("label"):
            return row.get("external_ref") or {}
    return {}


def _slack_channel_ids(binding_rows: list[dict] | None) -> tuple[str | None, list[str]]:
    """(primary, all_ids) — the ``''`` singleton first, labeled extras after
    (sorted by label for a stable order)."""
    primary = _singleton_ref(binding_rows, "slack").get("id") or None
    labeled = sorted(
        (row.get("label"), (row.get("external_ref") or {}).get("id"))
        for row in binding_rows or []
        if row.get("service") == "slack" and row.get("label")
    )
    ids: list[str] = [primary] if primary else []
    for _label, cid in labeled:
        if cid and cid not in ids:
            ids.append(cid)
    return primary, ids


def _clickup_list_id(binding_rows: list[dict] | None) -> str | None:
    """The list id from the ``''`` clickup binding.

    Project refs carry ``extra.list_id`` (folder-only projects have
    ``extra.folder_id`` and NO list); initiative refs carry the list id as
    the plain ref ``id``.
    """
    ref = _singleton_ref(binding_rows, "clickup")
    extra = ref.get("extra") or {}
    if extra.get("list_id"):
        return extra["list_id"]
    if extra.get("folder_id"):
        return None  # folder-only project — no list to route tasks to
    return ref.get("id") or None


def hydrate_project_row(row: dict, binding_rows: list[dict] | None) -> dict:
    """Overlay bindings-derived values onto an MC-2 ``projects`` row using
    the legacy flat keys cp-engine consumes. Always sets every key (None /
    empty when unbound) so callers never see a stale column value."""
    primary, all_ids = _slack_channel_ids(binding_rows)
    row["slack_channel_id"] = primary
    row["slack_channel_ids"] = all_ids
    row["slack_channel_name"] = None  # dead column: never populated in MC-2
    row["clickup_list_id"] = _clickup_list_id(binding_rows)
    row["google_drive_folder_id"] = (
        _singleton_ref(binding_rows, "google_drive").get("id") or None
    )
    # The dropbox ref's `url` holds whatever the flat column held — the
    # folder PATH for ingestable projects (or a legacy share link).
    row["mc_dropbox_folder_id"] = _singleton_ref(binding_rows, "dropbox").get("url") or None
    return row


def hydrate_initiative_row(row: dict, binding_rows: list[dict] | None) -> dict:
    """Ditto for ``initiatives`` rows (slack multi-channel + clickup list +
    Drive/Dropbox folder coordinates for asset ingest — mc-2 #192)."""
    _primary, all_ids = _slack_channel_ids(binding_rows)
    row["slack_channel_ids"] = all_ids
    row["clickup_list_id"] = _clickup_list_id(binding_rows)
    # Same ref shapes the project hydration uses: Drive stores the folder id
    # in `id`; the dropbox ref's `url` holds the folder PATH (or a legacy
    # share link).
    row["google_drive_folder_id"] = (
        _singleton_ref(binding_rows, "google_drive").get("id") or None
    )
    row["mc_dropbox_folder_id"] = (
        _singleton_ref(binding_rows, "dropbox").get("url") or None
    )
    return row
