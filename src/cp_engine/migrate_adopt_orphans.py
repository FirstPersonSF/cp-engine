"""Adopt orphaned `distilled` substance rows before their file is reaped (#200).

The condition this repairs
--------------------------
An element that was re-keyed leaves TWO disk files claiming one
``est_item_id``: the new ``_authored/<uuid>.md`` (MC-2-owned) and the old
phase-dir file that still carries the pre-re-key version ladder. Sync parses
both, so the old file's top version is mirrored ``live`` and the authored-live
shield (#113) flips it to ``superseded`` and warns on EVERY sync.

Deleting the stale file does not work on its own. Its rows are
``origin='distilled'``, and the reap in ``spine_substance_sync`` only exempts
``origin='authored'``:

    if existing.get("origin") == "authored":
        continue  # MC-2 owns authored rows; disk is downstream — never reap.

A distilled row absent from ``present_ids`` is hard-DELETEd unless it carries a
confirmed field (``_has_confirmed_field``) — and re-keyed ladders are typically
all-``proposed``, so they are deleted outright. That is the 7-rows-to-1 collapse
recorded on #200.

What this does
--------------
Flips the stale file's rows to ``origin='authored'`` FIRST, which moves them
under MC-2 ownership and the reap's exemption. The file can then be removed and
every version survives as history.

``origin`` is an engine-owned column guarded by a DB trigger (mc-2 #130) that
rejects writes not naming an authorized writer. This runs through
``mc2_db.get_client()``, which sets ``X-Spine-Writer: cp-engine`` — cp-engine
owns the column, so this satisfies the guard rather than circumventing it. A
hand-rolled PATCH is refused with P0130, by design.

This does NOT delete the stale file. Adopt, verify, then remove the file and
sync as separate reviewable steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cp_engine.mc2_db import Tables


class AdoptOrphansError(RuntimeError):
    """Raised when the tenant state makes adoption unsafe."""


@dataclass
class AdoptResult:
    est_item_id: str
    project_code: str
    adopted: list[str] = field(default_factory=list)
    already_authored: list[str] = field(default_factory=list)
    live_rows: list[str] = field(default_factory=list)
    dry_run: bool = False


def adopt_orphaned_versions(
    project_code: str,
    est_item_id: str,
    *,
    config=None,
    dry_run: bool = False,
) -> AdoptResult:
    """Flip every ``distilled`` row of ``est_item_id`` to ``authored``.

    Refuses when the element does not present the re-keyed shape — exactly one
    live row, at least one distilled row to adopt. Idempotent: rows already
    ``authored`` are reported, not rewritten.
    """
    # Imported inside the call (not at module import) so the writer-header
    # client is built fresh per invocation and tests can monkeypatch
    # `cp_engine.mc2_db.get_client`.
    from cp_engine import mc2_db

    client = mc2_db.get_client(config, required=True)

    rows = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select("id, version_label, status, origin, archived")
        .eq("est_item_id", est_item_id)
        .execute()
        .data
    ) or []

    if not rows:
        raise AdoptOrphansError(
            f"No spine_substance rows for {est_item_id} — nothing to adopt. "
            f"(Wrong est_item_id, or the rows were already reaped.)"
        )

    live = [r for r in rows if r.get("status") == "live"]
    if len(live) != 1:
        raise AdoptOrphansError(
            f"{est_item_id}: expected exactly 1 live row, found {len(live)} "
            f"({', '.join(sorted(r.get('version_label', '?') for r in live)) or 'none'}). "
            f"Adoption assumes a healed ladder — resolve the live rows first."
        )

    result = AdoptResult(
        est_item_id=est_item_id,
        project_code=project_code,
        live_rows=[r["version_label"] for r in live],
        dry_run=dry_run,
    )

    targets = []
    for r in rows:
        if r.get("origin") == "authored":
            result.already_authored.append(r["version_label"])
        elif r.get("origin") == "distilled":
            targets.append(r)

    if not targets:
        return result  # fully adopted already — no-op

    for r in sorted(targets, key=lambda x: x.get("version_label", "")):
        if not dry_run:
            (
                client.table(Tables.SPINE_SUBSTANCE)
                .update({"origin": "authored"})
                .eq("id", r["id"])
                .execute()
            )
        result.adopted.append(r["version_label"])

    return result
