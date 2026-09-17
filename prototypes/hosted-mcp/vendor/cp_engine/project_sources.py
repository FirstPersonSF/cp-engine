"""VENDORED from `cp_engine.project_sources` — the #113 live-row collapse only.

`spine_lint` imports `_one_live_per_element` at call time. The real module
imports `asset_ingest` (openai, cloud_storage) and `spine_done` at MODULE level,
so importing it in the container is not an option; these four functions are pure
row manipulation and carry none of that.

`version_number` is inlined from `cp_engine.substance` for the same reason —
`_version_rank` imports it at call time and `substance` pulls frontmatter+yaml.

THE RULE THIS ENCODES (#113): `spine_substance` stores one row per VERSION and
the data can carry TWO `status='live'` rows for one element. A read path that
emits both, or resolves the stale one, turns one dirty row into wrong answers
everywhere. A drift test asserts each function matches its source.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def version_number(label) -> int:
    """The leading version integer of a version_label, or -1 when unparseable.

    Shared version-ordering primitive (#113): both the spine read path (dedupe
    duplicate live rows to the latest version) and the substance mirror (shield
    a newer authored live version from a stale disk 'live') need to compare
    version labels numerically — "v10" must beat "v9", which string comparison
    gets wrong. Parses "v7", "V2", bare "2", "v2.1" → 2, "v2 (note)" → 2.
    Tolerant of None/odd labels so a dirty row can't raise — but note the -1
    fallback means a truly unparseable label LOSES to any parseable one
    regardless of recency (callers tie-break on version_date next)."""
    m = _VERSION_NUM_RE.match(str(label or ""))
    return int(m.group(1)) if m else -1


def _row_scope(row: dict) -> str:
    """A row's scope, defaulting pre-migration rows to 'project'."""
    return row.get("scope") or "project"


def _element_key(r: dict) -> tuple:
    """Identity key for ONE spine element within a scoped fetch.

    `est_item_id` is only unique PER PROJECT (authored ids are
    `_authored/<label-slug>`), so an account-scoped row must also carry its
    ORIGIN `project_id` in the key — two sibling projects can each promote
    `_authored/janet-dossier` and both are legitimately-distinct elements.
    Project-arm rows all share one project_id (the fetch filters on it), so
    the eid+scope pair already identifies them; keying them without project_id
    keeps behavior stable for callers whose column set omits it."""
    scope = _row_scope(r)
    return (r.get("est_item_id"), scope,
            r.get("project_id") if scope == "account" else None)


def _version_rank(r: dict) -> tuple:
    """Version ordering for rows of ONE element: numeric label, then date."""
    from cp_engine.substance import version_number

    return (version_number(r.get("version_label")),
            str(r.get("version_date") or ""))


def _one_live_per_element(rows: list[dict]) -> list[dict]:
    """Collapse duplicate live rows to ONE per element (`_element_key`) — the
    latest by version ordering (#113 defense).

    `spine_substance` stores one row per VERSION; a live-only fetch should
    yield exactly one row per element, but the data can carry two `status='live'`
    version rows for the same element (e.g. sap-5174's e94d0a03: an authored v7
    plus a distilled v6 the substance mirror re-flipped live). Every read path
    must tolerate that — a listing that emits the same element twice, or a pull
    that resolves to the stale older body, turns one dirty row into wrong
    answers everywhere. Keeps the row with the highest numeric version_label
    (fallback: version_date, then last-fetched) and WARNS when it drops one (a
    collapsed row is dirty data someone should clean, not silently mask).
    Deduped elements keep first-seen order; rows without an est_item_id are
    never merged (unidentifiable) and are appended at the END, after the keyed
    elements — callers relying on interleaving must not pass id-less rows."""
    best: dict[tuple, dict] = {}
    order: list = []
    passthrough: list[dict] = []
    for r in rows:
        if not r.get("est_item_id"):
            passthrough.append(r)
            continue
        k = _element_key(r)
        if k not in best:
            best[k] = r
            order.append(k)
        else:
            kept, dropped = ((r, best[k]) if _version_rank(r) >= _version_rank(best[k])
                             else (best[k], r))
            best[k] = kept
            logger.warning(
                "spine dedup (#113): element %s has multiple live version rows; "
                "keeping %s, hiding %s — the hidden row is dirty data and "
                "should be superseded in MC-2",
                k[0], kept.get("version_label"), dropped.get("version_label"),
            )
    return [best[k] for k in order] + passthrough
