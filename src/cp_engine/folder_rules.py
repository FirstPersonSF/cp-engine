"""Folder rules for Dropbox / Drive across the workstream tree (#396).

MC-2 snapshot 2026-10-30: Dropbox was "on" for 20 of 23 active workstreams
and bound for 12. "On, no folder" ingests nothing, and parent nodes had no
folder at all, so account-level material had no home. Four rules, one module:

1. **Inheritance.** A workstream with a source enabled but no folder bound
   reads its folder from its parent: ``<parent folder>/<child folder>`` —
   but ONLY when that subfolder is really there. The parent folder is listed
   (one level, read-only) and the child's subfolder is found by its job
   number or its exact name; nothing is ever constructed and assumed. Several
   matches is ambiguous and inherits nothing. The parent's own folder may
   itself be inherited, to any depth.

2. **Parent-node sources.** A parent node (account or program) ingests its
   own folder at ``scope='account'`` (`ProjectFolders.ingest_scope`), so every
   workstream of the company reads it. A child's folder nested inside the
   parent's is the CHILD's: the parent's listing drops it
   (`ProjectFolders.child_dropbox_roots` / `child_drive_folders`), so a job's
   files are never double-ingested as company documents.

3. **Health.** `unbound_workstreams` — active, ingestable workstreams with a
   source enabled, no folder bound, and no ancestor with a folder to inherit
   from. MC-2 only (no provider listing), so the daily line can run anywhere.

4. **Convention + audit.** `CONVENTION_ROOTS` names where a workstream's
   Dropbox folder is expected (`<root>/<full_job_name>`); `audit_folders`
   reports every active workstream against it, read-only, and
   `fill_sql` prints (never runs) the bindings Drew could apply.

Self-company workstreams WITH an agreement stay out of every rule: they are
not asset-ingest targets (`asset_ingest._out_of_scope`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cp_engine.mc2_db import Tables

SOURCES = ("dropbox", "drive")
ACTIVE_STATUSES = ("Deal", "Open")

# Where a workstream's Dropbox folder is expected, by company kind (#396).
# cnc-5239 is bound to `/Canonic/CAN 5239 Canonic Site`.
CONVENTION_ROOTS: dict[str, str] = {
    "client": "/1P Active Projects",
    "self-fpsf": "/1P Initiatives",
    "self-canonic": "/Canonic",
}

# Read for the audit and the health check. Explicit columns, never `*`.
FOLDER_AUDIT_COLUMNS = (
    "id, company_id, number, name, full_job_name, parent_id, mc_status, "
    "deal_stage, enable_google_drive, enable_dropbox, asset_ingest_folders, "
    "companies(code, kind)"
)

_MAX_DEPTH = 8  # ancestor walk guard (a parent_id cycle must not spin)


# ──────────────────────────────────────────────────────────────────────
#  Small helpers
# ──────────────────────────────────────────────────────────────────────


def code_for(row: dict) -> str:
    """The cp code for an MC-2 projects row (slugified `full_job_name`)."""
    from cp_engine.state import slug_full_job_name

    slug = slug_full_job_name(row.get("full_job_name"))
    if slug:
        return slug
    company = row.get("companies") or {}
    if isinstance(company, list):
        company = company[0] if company else {}
    prefix = (company.get("code") or "").lower() if isinstance(company, dict) else ""
    number = row.get("number")
    return f"{prefix}-{number}" if prefix and number is not None else str(row.get("id"))


def bound_folder(folders: Any, source: str) -> str | None:
    """The folder coordinate a ProjectFolders carries for `source`."""
    if source == "dropbox":
        return folders.mc_dropbox_folder_id
    return folders.google_drive_folder_id


def _enabled(folders: Any, source: str) -> bool:
    return folders.enable_dropbox if source == "dropbox" else folders.enable_google_drive


def _set_folder(folders: Any, source: str, value: str) -> None:
    if source == "dropbox":
        folders.mc_dropbox_folder_id = value
    else:
        folders.google_drive_folder_id = value


def match_subfolder(
    names: list[str], *, number: int | None, name: str | None,
    full_job_name: str | None = None,
) -> tuple[str | None, list[str]]:
    """Find a workstream's folder among real folder `names`.

    Returns ``(match, candidates)``: `match` is set only when exactly one
    folder qualifies. An exact (case-insensitive) name or full_job_name wins;
    otherwise the job number as a standalone token (`5239` in
    `CAN 5239 Canonic Site`, not in `15239`). Several candidates → no match,
    and the candidates say why.
    """
    wanted = {x.casefold() for x in (name, full_job_name) if x}
    exact = [n for n in names if n.casefold() in wanted]
    if len(exact) == 1:
        return exact[0], exact
    if number is not None:
        pat = re.compile(rf"(?<!\d){int(number)}(?!\d)")
        by_number = [n for n in names if pat.search(n)]
        if len(by_number) == 1:
            return by_number[0], by_number
        if by_number:
            return None, by_number
    return None, exact


# ──────────────────────────────────────────────────────────────────────
#  Provider listings (read-only)
# ──────────────────────────────────────────────────────────────────────


def list_subfolders(source: str, connector: Any, folder: str) -> list[tuple[str, str]]:
    """The immediate subfolders of `folder` as ``(name, coordinate)`` pairs.

    Dropbox: coordinate = `path_display`. Drive: coordinate = folder id.
    One level only; raises whatever the provider raises.
    """
    if source == "dropbox":
        out: list[tuple[str, str]] = []
        result = connector.dbx.files_list_folder(folder)
        while True:
            for entry in getattr(result, "entries", []) or []:
                # FolderMetadata carries no `size`; FileMetadata does.
                if hasattr(entry, "size"):
                    continue
                path = getattr(entry, "path_display", None)
                name = getattr(entry, "name", None)
                if path and name and type(entry).__name__ != "DeletedMetadata":
                    out.append((name, path))
            if not getattr(result, "has_more", False):
                break
            result = connector.dbx.files_list_folder_continue(result.cursor)
        return out
    from cp_engine.asset_ingest import _DRIVE_FOLDER_MIME

    items = connector._list_files_with_pagination(
        query=(f"'{folder}' in parents and mimeType='{_DRIVE_FOLDER_MIME}' "
               "and trashed=false"),
        fields="files(id,name)",
        page_size=100,
    )
    return [(i.get("name") or "", i.get("id")) for i in items if i.get("id")]


class _Listings:
    """Memoized `list_subfolders` for one run (errors memoized too)."""

    def __init__(self, connectors: dict[str, Any]):
        self.connectors = connectors
        self._memo: dict[tuple[str, str], Any] = {}

    def get(self, source: str, folder: str) -> list[tuple[str, str]]:
        key = (source, folder)
        if key not in self._memo:
            conn = self.connectors.get(source)
            if conn is None:
                self._memo[key] = RuntimeError(f"no {source} connector")
            else:
                try:
                    self._memo[key] = list_subfolders(source, conn, folder)
                except Exception as exc:  # noqa: BLE001 — memoized, re-raised below
                    self._memo[key] = exc
        value = self._memo[key]
        if isinstance(value, Exception):
            raise value
        return value


# ──────────────────────────────────────────────────────────────────────
#  Rule 1 — inheritance
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Inherited:
    """Outcome of an inheritance attempt for one source."""

    folder: str | None = None       # the resolved coordinate (path / id)
    from_code: str | None = None    # the ancestor whose folder it came from
    reason: str | None = None       # why nothing was inherited (None = no parent folder)


def inherit_folder(
    folders: Any,
    source: str,
    lookup: Callable[[str], Any],
    listings: _Listings,
    *,
    _depth: int = 0,
) -> Inherited:
    """Resolve `folders`' `source` folder from its ancestors (rule 1).

    `lookup(project_id) -> ProjectFolders | None` reads a parent (with
    `code` set); `listings` lists a folder's subfolders. Never invents a
    path: a parent folder whose listing has no unique match for this
    workstream inherits nothing, and `reason` says why.
    """
    parent_id = getattr(folders, "parent_id", None)
    if not parent_id or _depth >= _MAX_DEPTH:
        return Inherited()
    parent = lookup(parent_id)
    if parent is None:
        return Inherited()
    parent_code = getattr(parent, "code", None) or parent_id
    parent_folder = bound_folder(parent, source)
    if not parent_folder:
        up = inherit_folder(parent, source, lookup, listings, _depth=_depth + 1)
        if not up.folder:
            return Inherited(reason=up.reason)
        parent_folder = up.folder
    try:
        subs = listings.get(source, parent_folder)
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return Inherited(reason=f"{parent_code} folder unreadable "
                                f"({type(exc).__name__}: {exc})")
    by_name = dict(subs)
    match, candidates = match_subfolder(
        list(by_name), number=getattr(folders, "number", None),
        name=getattr(folders, "name", None),
        full_job_name=getattr(folders, "full_job_name", None),
    )
    if match:
        return Inherited(folder=by_name[match], from_code=parent_code)
    if candidates:
        return Inherited(reason=f"{parent_code} folder has several matches "
                                f"({', '.join(sorted(candidates))})")
    return Inherited(reason=f"no subfolder for this workstream in {parent_code}'s "
                            f"folder ({parent_folder})")


# ──────────────────────────────────────────────────────────────────────
#  Rules 1 + 2 at ingest time
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FolderRulesResult:
    notes: list[dict[str, str]] = field(default_factory=list)
    connectors: dict[str, Any] = field(default_factory=dict)


def _child_rows(client, project_id: str) -> list[dict]:
    return (
        client.table(Tables.PROJECTS)
        .select("id, number, name, full_job_name, mc_status")
        .eq("parent_id", project_id)
        .execute()
        .data
        or []
    )


def apply_folder_rules(
    client,
    folders: Any,
    *,
    connectors: dict[str, Any] | None = None,
    factories: dict[str, Callable[[], Any]] | None = None,
    lookup: Callable[[str], Any] | None = None,
) -> FolderRulesResult:
    """Apply inheritance (rule 1) and parent-node scoping (rule 2) to a
    resolved `ProjectFolders`, IN PLACE, before its files are listed.

    Connectors are built lazily (`factories`) and only when a rule needs a
    listing; the ones built come back in `.connectors` for reuse. Every
    outcome worth knowing leaves a note (`kind: "inherited"` for a folder
    taken from a parent). A failure here never raises: the run then proceeds
    exactly as it would have without the rule.
    """
    from cp_engine import asset_ingest as ai

    out = FolderRulesResult(connectors=dict(connectors or {}))
    if folders is None or ai._out_of_scope(folders):
        return out
    factories = factories or {"dropbox": ai._dropbox_connector, "drive": ai._drive_connector}

    def _conn(source: str):
        if out.connectors.get(source) is None and source in factories:
            try:
                out.connectors[source] = factories[source]()
            except Exception as exc:  # noqa: BLE001 — noted; the rule is skipped
                out.notes.append({"source": source,
                                  "note": f"folder rules skipped: {type(exc).__name__}: {exc}"})
                out.connectors[source] = None
        return out.connectors.get(source)

    class _Lazy(_Listings):
        def get(self, source, folder):  # build the connector on first use
            self.connectors[source] = _conn(source)
            return super().get(source, folder)

    listings = _Lazy(out.connectors)

    def _lookup(pid: str):
        parent = ai.resolve_project_folders_by_id(client, pid)
        return parent

    lookup = lookup or _lookup

    # Rule 1 — inherit an unbound, enabled source from the parent chain.
    if getattr(folders, "parent_id", None):
        for source in SOURCES:
            if not _enabled(folders, source) or bound_folder(folders, source):
                continue
            try:
                got = inherit_folder(folders, source, lookup, listings)
            except Exception as exc:  # noqa: BLE001 — never fail the run on a rule
                out.notes.append({"source": source, "note": (
                    f"folder inheritance failed: {type(exc).__name__}: {exc}")})
                continue
            if got.folder:
                _set_folder(folders, source, got.folder)
                folders.inherited_from[source] = got.from_code
                out.notes.append({"source": source, "kind": "inherited",
                                  "note": f"folder inherited from {got.from_code}: {got.folder}"})
            elif got.reason:
                out.notes.append({"source": source,
                                  "note": f"no inherited folder — {got.reason}"})

    # Rule 2 — a parent node's own folder ingests at account scope, minus
    # the subfolders that belong to its children.
    if not (bound_folder(folders, "dropbox") or bound_folder(folders, "drive")):
        return out
    try:
        children = _child_rows(client, folders.project_id)
    except Exception as exc:  # noqa: BLE001
        out.notes.append({"source": "config",
                          "note": f"children unreadable — parent-node rules skipped: {exc}"})
        return out
    if not isinstance(children, list) or not children:
        return out
    folders.is_parent_node = any(c.get("mc_status") != "Archived" for c in children)
    try:
        from cp_engine.mc2_bindings import fetch_binding_rows, hydrate_project_row

        grouped = fetch_binding_rows(client, project_ids=[c["id"] for c in children if c.get("id")])
    except Exception:  # noqa: BLE001 — a child's bound folder is then matched by name only
        grouped = {}
    for child in children:
        hydrate_project_row(child, grouped.get(child.get("id"), []))

    drop_roots: list[str] = []
    drive_names: list[str] = []
    for source in SOURCES:
        if not _enabled(folders, source):
            continue
        root = bound_folder(folders, source)
        if not root:
            continue
        try:
            subs = listings.get(source, root)
        except Exception as exc:  # noqa: BLE001
            out.notes.append({"source": source,
                              "note": f"child folders not separated (listing failed: {exc})"})
            subs = []
        by_name = dict(subs)
        by_coord = {coord: name for name, coord in subs}
        for child in children:
            child_bound = (child.get("mc_dropbox_folder_id") if source == "dropbox"
                           else child.get("google_drive_folder_id"))
            if source == "dropbox":
                if child_bound and child_bound.lower().startswith(root.rstrip("/").lower() + "/"):
                    drop_roots.append(child_bound)
                    continue
            elif child_bound and child_bound in by_coord:
                drive_names.append(by_coord[child_bound])
                continue
            match, _ = match_subfolder(list(by_name), number=child.get("number"),
                                       name=child.get("name"),
                                       full_job_name=child.get("full_job_name"))
            if match:
                if source == "dropbox":
                    drop_roots.append(by_name[match])
                else:
                    drive_names.append(match)
    folders.child_dropbox_roots = tuple(dict.fromkeys(drop_roots))
    folders.child_drive_folders = tuple(dict.fromkeys(drive_names))
    return out


def exclude_child_folders(folders: Any, refs: list) -> tuple[list, int]:
    """Drop refs under a child workstream's folder (rule 2). → (kept, dropped)."""
    roots = tuple(r.rstrip("/").lower() + "/" for r in getattr(folders, "child_dropbox_roots", ()))
    names = set(getattr(folders, "child_drive_folders", ()))
    if not roots and not names:
        return refs, 0
    kept = []
    for ref in refs:
        if ref.source == "dropbox" and ref.path and ref.path.lower().startswith(roots or ("\0",)):
            continue
        if ref.source == "drive" and ref.folder_path and ref.folder_path[0] in names:
            continue
        kept.append(ref)
    return kept, len(refs) - len(kept)


# ──────────────────────────────────────────────────────────────────────
#  Reading the tree (audit + health)
# ──────────────────────────────────────────────────────────────────────


def read_tree(client) -> dict[str, Any]:
    """Every projects row (archived too — an archived parent can still own a
    folder) as hydrated `ProjectFolders`, keyed by id. Two reads."""
    from cp_engine import asset_ingest as ai
    from cp_engine.mc2_bindings import fetch_binding_rows, hydrate_project_row

    rows = (client.table(Tables.PROJECTS).select(FOLDER_AUDIT_COLUMNS)
            .limit(5000).execute().data) or []
    grouped = fetch_binding_rows(client, project_ids=[r["id"] for r in rows if r.get("id")])
    tree: dict[str, Any] = {}
    for row in rows:
        folders = ai._row_to_folders(hydrate_project_row(row, grouped.get(row["id"], [])))
        folders.code = code_for(row)
        folders.mc_status = row.get("mc_status")
        tree[row["id"]] = folders
    for f in tree.values():
        f.is_parent_node = any(c.parent_id == f.project_id and c.mc_status != "Archived"
                               for c in tree.values())
    return tree


def _ancestor_has_folder(tree: dict[str, Any], folders: Any, source: str) -> str | None:
    seen: set[str] = set()
    pid = folders.parent_id
    while pid and pid not in seen and len(seen) < _MAX_DEPTH:
        seen.add(pid)
        parent = tree.get(pid)
        if parent is None:
            return None
        if bound_folder(parent, source):
            return parent.code
        pid = parent.parent_id
    return None


def unbound_workstreams(tree: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Rule 3: active, ingestable workstreams with a source enabled, no folder
    bound, and no ancestor folder to inherit from → ``[(code, [sources])]``.

    MC-2 only: an ancestor WITH a folder counts as inheritable here without
    listing it (ingest verifies the subfolder; `cxp folders` shows the
    outcome)."""
    from cp_engine import asset_ingest as ai

    out = []
    for f in tree.values():
        if f.mc_status not in ACTIVE_STATUSES or ai._out_of_scope(f):
            continue
        missing = [s for s in SOURCES
                   if _enabled(f, s) and not bound_folder(f, s)
                   and not _ancestor_has_folder(tree, f, s)]
        if missing:
            out.append((f.code, missing))
    return sorted(out)


# ──────────────────────────────────────────────────────────────────────
#  Rule 4 — convention + audit
# ──────────────────────────────────────────────────────────────────────


def suggested_folder(folders: Any) -> str | None:
    root = CONVENTION_ROOTS.get(folders.company_kind)
    if not root or not getattr(folders, "full_job_name", None):
        return None
    return f"{root}/{folders.full_job_name}"


@dataclass
class AuditRow:
    code: str
    label: str
    status: str
    project_id: str
    in_scope: bool
    enabled: dict[str, bool]
    bound: dict[str, str | None]
    bound_check: dict[str, str]           # "exists" | "missing" | "unchecked" | ""
    inherited: dict[str, str | None]      # "<folder> (from <code>)"
    inherit_note: dict[str, str | None]
    suggested: str | None
    # exists | found: <path> | ambiguous: … | missing | root missing | unchecked
    suggested_check: str
    found_path: str | None = None         # the real folder the suggestion resolved to


def _dropbox_path_exists(conn: Any, path: str) -> str:
    try:
        conn.dbx.files_get_metadata(path)
        return "exists"
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "not_found" in text:
            return "missing"
        return "unchecked"


def _label(tree: dict[str, Any], f: Any) -> str:
    from cp_engine.state import derive_label

    return derive_label(company_kind=f.company_kind,
                        parent_code=f.parent_id, has_agreement=f.has_agreement,
                        has_children=f.is_parent_node)


def audit_folders(
    client, *, connectors: dict[str, Any] | None = None, tree: dict[str, Any] | None = None,
) -> list[AuditRow]:
    """Every active workstream against the folder rules. Read-only.

    `connectors` maps "dropbox"/"drive" to a connector or None; with none,
    every provider-backed column reads "unchecked".
    """
    from cp_engine import asset_ingest as ai

    tree = tree if tree is not None else read_tree(client)
    connectors = connectors or {}
    listings = _Listings(connectors)
    dbx = connectors.get("dropbox")
    rows: list[AuditRow] = []
    for f in sorted(tree.values(), key=lambda x: x.code):
        if f.mc_status not in ACTIVE_STATUSES:
            continue
        in_scope = not ai._out_of_scope(f)
        enabled = {s: _enabled(f, s) for s in SOURCES}
        bound = {s: bound_folder(f, s) for s in SOURCES}
        bound_check = {s: "" for s in SOURCES}
        if bound["dropbox"]:
            bound_check["dropbox"] = (_dropbox_path_exists(dbx, bound["dropbox"])
                                      if dbx else "unchecked")
        if bound["drive"]:
            bound_check["drive"] = "unchecked"
        inherited: dict[str, str | None] = {s: None for s in SOURCES}
        inherit_note: dict[str, str | None] = {s: None for s in SOURCES}
        if in_scope:
            for s in SOURCES:
                if bound[s] or not f.parent_id:
                    continue
                if connectors.get(s) is None:
                    if _ancestor_has_folder(tree, f, s):
                        inherit_note[s] = "unchecked"
                    continue
                got = inherit_folder(f, s, tree.get, listings)
                if got.folder:
                    inherited[s] = f"{got.folder} (from {got.from_code})"
                else:
                    inherit_note[s] = got.reason
        suggested = suggested_folder(f)
        check, found = "unchecked", None
        if suggested and dbx is not None:
            root = CONVENTION_ROOTS[f.company_kind]
            try:
                subs = dict(listings.get("dropbox", root))
            except Exception as exc:  # noqa: BLE001
                check = "root missing" if "not_found" in str(exc) else "unchecked"
            else:
                match, cands = match_subfolder(list(subs), number=f.number, name=f.name,
                                               full_job_name=f.full_job_name)
                if match:
                    found = subs[match]
                    check = "exists" if found.lower() == suggested.lower() else f"found: {found}"
                elif cands:
                    check = "ambiguous: " + ", ".join(sorted(cands))
                else:
                    check = "missing"
        elif not suggested:
            check = ""
        rows.append(AuditRow(
            code=f.code, label=_label(tree, f), status=f.mc_status or "",
            project_id=f.project_id, in_scope=in_scope, enabled=enabled, bound=bound,
            bound_check=bound_check, inherited=inherited, inherit_note=inherit_note,
            suggested=suggested, suggested_check=check, found_path=found,
        ))
    return rows


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def fill_sql(rows: list[AuditRow]) -> list[str]:
    """The project_integrations statements Drew could apply — PRINTED, never run.

    INSERT for an in-scope workstream with Dropbox on, nothing bound, nothing
    inherited, and a convention folder that really exists. UPDATE for a bound
    Dropbox path that does not exist when the convention root holds exactly
    one folder for the workstream. Nothing else is guessed.
    """
    out: list[str] = []
    for r in rows:
        if not r.in_scope or not r.enabled["dropbox"] or not r.found_path:
            continue
        ref = _sql_str(json.dumps({"url": r.found_path}))
        if not r.bound["dropbox"] and not r.inherited["dropbox"]:
            out.append(
                f"-- {r.code}\n"
                "INSERT INTO public.project_integrations "
                "(project_id, service, external_ref, label)\n"
                # project_integrations has no unique key on (project_id,
                # service, label), so ON CONFLICT is rejected (42P10, found
                # applying the first fill 2026-10-30); guard with NOT EXISTS.
                f"SELECT {_sql_str(r.project_id)}, 'dropbox', {ref}::jsonb, ''\n"
                "WHERE NOT EXISTS (SELECT 1 FROM public.project_integrations "
                f"WHERE project_id = {_sql_str(r.project_id)} AND service = 'dropbox');"
            )
        elif r.bound["dropbox"] and r.bound_check["dropbox"] == "missing":
            out.append(
                f"-- {r.code}: bound path {r.bound['dropbox']!r} does not exist\n"
                f"UPDATE public.project_integrations SET external_ref = {ref}::jsonb, "
                "updated_at = now()\n"
                f"WHERE project_id = {_sql_str(r.project_id)} AND service = 'dropbox' "
                f"AND label = '' AND external_ref->>'url' = {_sql_str(r.bound['dropbox'])};"
            )
    return out


def render_audit(rows: list[AuditRow]) -> str:
    """Plain-text audit: one block per workstream."""
    lines: list[str] = []
    for r in rows:
        head = f"{r.code} ({r.label}, {r.status})"
        if not r.in_scope:
            lines.append(f"{head} — not an ingest target")
            continue
        lines.append(head)
        for s in SOURCES:
            on = "on " if r.enabled[s] else "off"
            if r.bound[s]:
                where = f"bound {r.bound[s]}"
                if r.bound_check[s]:
                    where += f" [{r.bound_check[s]}]"
            elif r.inherited[s]:
                where = f"inherited {r.inherited[s]}"
            else:
                where = "none"
                if r.inherit_note[s]:
                    where += f" (inherit: {r.inherit_note[s]})"
            lines.append(f"  {s:<7} {on}  {where}")
        if r.suggested and not r.bound["dropbox"] and not r.inherited["dropbox"] or (
            r.bound_check["dropbox"] == "missing"
        ):
            lines.append(f"  suggest {r.suggested} [{r.suggested_check}]")
    return "\n".join(lines)
