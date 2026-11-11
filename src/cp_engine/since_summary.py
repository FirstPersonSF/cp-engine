"""What arrived after a workstream's Exec Summary was written (#251).

WHY THIS EXISTS. Opening a workstream (`switch to`, `check status`) reads the
Exec Summary as the project's state. When the summary is days behind, the
work that happened since is spread over MC-2 (meetings, ingested sources,
commitments) and the sprint files (dated decisions), and rebuilding it took
git log and hand-reading (2026-10-30, ibx-5153: two meetings and seven
sources behind a 4-day-old summary).

This READS ONLY. It lists what arrived after the summary's `· updated`
stamp, so the reader sees the gap at once. It authors nothing: the engine
owns read and render, the model owns prose
(`docs/plans/2026-06-30-exec-summary.md`).

One function serves both surfaces, hosted `get_project_state` and
`cxp brief`, so they cannot disagree about what counts as "since".
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from cp_engine import mc2_db
from cp_engine.mc2_db import Tables

logger = logging.getLogger(__name__)

#: Items listed per kind; the total is always reported.
LIMIT = 10

_COMMENT = re.compile(r"\s*<!--.*?-->", re.DOTALL)

_STAMP = re.compile(r"##\s*Exec Summary\s*·\s*updated\s+(\d{4}-\d{2}-\d{2})")


def summary_stamp(exec_summary_text: str | None) -> date | None:
    """The `· updated YYYY-MM-DD` date on an Exec Summary region, or None."""
    m = _STAMP.search(exec_summary_text or "")
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _day(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _capped(items: list[dict]) -> dict:
    return {"count": len(items), "items": items[:LIMIT]}


def since_summary(
    client,
    project_id: str | None,
    *,
    since: date,
    tenant_root: Path | None = None,
    dir_slug: str | None = None,
) -> dict:
    """Meetings, sources, commitments and decisions dated after `since`.

    Each kind is `{count, items}` (newest first, `items` capped at LIMIT), or
    `{error}` when that read failed — a failed read is said, never shown as
    "nothing arrived". `total` sums the counts that were read.
    """
    out: dict = {"since": since.isoformat()}

    if client is not None and project_id:
        try:
            rows = mc2_db.project_meetings(
                client, project_id, "id, title, meeting_date")
            items = [
                {"date": str(r.get("meeting_date") or "")[:10],
                 "title": (r.get("title") or "").strip()}
                for r in rows
                if (_day(r.get("meeting_date")) or date.min) > since
            ]
            out["meetings"] = _capped(sorted(items, key=lambda i: i["date"], reverse=True))
        except Exception as exc:  # noqa: BLE001 — advisory read; say it failed
            out["meetings"] = {"error": str(exc)[:200]}

        try:
            rows = (
                client.table(Tables.RAG_ASSETS)
                .select("title, file_path, source_provider, created_at")
                .eq("project_id", project_id)
                .eq("status", "active")
                .gt("created_at", since.isoformat())
                .execute()
                .data
            ) or []
            items = [
                {"date": str(r.get("created_at") or "")[:10],
                 "title": r.get("title") or Path(r.get("file_path") or "").name}
                for r in rows
                # A meeting's own summary is listed under meetings already.
                if r.get("source_provider") != "fathom"
            ]
            out["sources"] = _capped(sorted(items, key=lambda i: i["date"], reverse=True))
        except Exception as exc:  # noqa: BLE001
            out["sources"] = {"error": str(exc)[:200]}

        try:
            rows = (
                client.table(Tables.COMMITMENTS)
                .select("description, due_date, direction, owner_name, created_at")
                .eq("project_id", project_id)
                .eq("status", "open")
                .gt("created_at", since.isoformat())
                .execute()
                .data
            ) or []
            items = [
                {"date": str(r.get("created_at") or "")[:10],
                 "text": (r.get("description") or "").strip(),
                 "due": r.get("due_date"),
                 "direction": r.get("direction"),
                 "owner": r.get("owner_name")}
                for r in rows
            ]
            out["commitments"] = _capped(sorted(items, key=lambda i: i["date"], reverse=True))
        except Exception as exc:  # noqa: BLE001
            out["commitments"] = {"error": str(exc)[:200]}

    if tenant_root is not None and dir_slug:
        try:
            out["decisions"] = _capped(_sprint_decisions(tenant_root, dir_slug, since))
        except Exception as exc:  # noqa: BLE001
            out["decisions"] = {"error": str(exc)[:200]}

    out["total"] = sum(
        v.get("count", 0) for v in out.values() if isinstance(v, dict)
    )
    return out


def _sprint_decisions(tenant_root: Path, dir_slug: str, since: date) -> list[dict]:
    """Dated decision bullets in the workstream's sprint files, after `since`."""
    from cp_engine.sprints import parse_sprint_file

    sprints = tenant_root / "sprints"
    if not sprints.is_dir():
        return []
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for week_dir in sorted(sprints.iterdir(), reverse=True):
        path = week_dir / f"{dir_slug}.md"
        if not path.is_file():
            continue
        try:
            parsed = parse_sprint_file(path)
        except Exception:  # noqa: BLE001 — one malformed file never sinks the read
            logger.warning("could not parse %s", path, exc_info=True)
            continue
        for entry in getattr(parsed, "decisions", ()) or ():
            when = _day(getattr(entry, "date", None))
            text = _COMMENT.sub("", getattr(entry, "text", "") or "").strip()
            if when is None or not text or when <= since:
                continue
            key = (when.isoformat(), text)
            if key in seen:
                continue
            seen.add(key)
            found.append({"date": when.isoformat(), "text": text})
    return sorted(found, key=lambda i: i["date"], reverse=True)


def render_since(block: dict) -> str:
    """Markdown for `cxp brief`: one line per kind, items beneath."""
    labels = (("meetings", "Meetings"), ("sources", "Sources ingested"),
              ("commitments", "Commitments opened"), ("decisions", "Decisions"))
    lines = [f"Since the summary ({block.get('since')}): "
             f"{block.get('total', 0)} item(s)."]
    for key, label in labels:
        part = block.get(key)
        if part is None:
            continue
        if "error" in part:
            lines.append(f"- **{label}:** could not read ({part['error']})")
            continue
        if not part["count"]:
            continue
        lines.append(f"- **{label} ({part['count']}):**")
        for item in part["items"]:
            text = item.get("title") or item.get("text") or ""
            lines.append(f"  - {item['date']} — {text}")
        if part["count"] > len(part["items"]):
            lines.append(f"  - … {part['count'] - len(part['items'])} more")
    return "\n".join(lines)
