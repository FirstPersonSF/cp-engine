"""Bridge Fathom meetings into the project/RAG world.

Meetings are tagged in MC-2's ``fathom_meetings.project_tags`` (a ``text[]``)
with human DISPLAY STRINGS, e.g. ``["IBX 5167 DDI Platform Video"]`` — not a
resolved project id. This module resolves those tags to a ``projects.id``.
"""

from __future__ import annotations

_SKIP_TAGS = {"untagged", ""}


def _default_resolver(client, code):
    from cp_engine.mcp_server import _resolve_project_id

    return _resolve_project_id(client, code)


def resolve_meeting_project(
    client,
    project_tags,
    *,
    resolver=_default_resolver,
) -> tuple[str | None, str | None]:
    """Resolve a meeting's ``project_tags`` to a ``(project_id, matched_tag)``.

    Iterates ``project_tags`` (may be ``None`` or empty), skipping any tag that
    is empty/whitespace or an "untagged" marker. The first tag that ``resolver``
    maps to a truthy project id wins. Returns ``(None, None)`` if none resolve.
    """
    for tag in project_tags or []:
        if not tag or tag.strip().lower() in _SKIP_TAGS:
            continue
        project_id = resolver(client, tag)
        if project_id:
            return project_id, tag
    return None, None
