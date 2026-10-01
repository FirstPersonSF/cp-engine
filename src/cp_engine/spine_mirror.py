"""The generated ``spine/`` view: rendered from MC-2, guarded against hand edits.

Architecture plan step 4c (decided by Drew, 2026-10-01): **spine elements are
owned by MC-2.** Every file under a workstream's ``spine/`` — and every file
under an account's ``_stakeholders/`` — is rendered from ``spine_substance``
rows on each sync. Nothing on disk flows back up: the disk→MC-2 substance push
that used to run here is gone, so a hand edit can no longer overwrite a
human-confirmed MC-2 field (it did, silently, nine times — every one of the
substance files whose ``framing`` differed held the OLDER text).

THE QUESTION THE GUARD ANSWERS is the region guard's, asked of a whole file:
"did the engine write what is on disk now?" — not "does it differ from what
MC-2 says now" (it differs whenever MC-2 moved). Each generated directory keeps
a manifest, ``.generated.json``, mapping every file the engine wrote to the
`region_guard.digest` of the text it wrote:

* on-disk digest == recorded → the engine wrote it; replace or remove silently.
* recorded but different     → a hand edit. The text is QUARANTINED through
  `region_guard.report` (``exceptions/region-edits/``, committed with the
  render, loud warning naming the file), then replaced or removed.
* not recorded ("unstamped") → no claim either way, exactly as for an
  unstamped region: a file the render produces is overwritten and recorded; a
  file the render does NOT produce is quarantined before it is removed, since
  nothing says the engine ever wrote it (that is how a hand-written card such
  as the slt-5196 Morgan Wright dossier would otherwise vanish).

Exempt, never generated and never reaped: frozen snapshots (``*.snapshots/``,
write-once by ``cxp spine snapshot``; MC-2 indexes them but holds no body), and
the legacy ``Retrospective/meeting-history.md`` until
``scripts/archive/move_meeting_history.py`` moves it to the workstream root.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from cp_engine import region_guard
from cp_engine.authored_mirror import authored_slug, render_element
from cp_engine.mc2_db import Tables

logger = logging.getLogger(__name__)

MANIFEST_NAME = ".generated.json"
#: The `region` label a whole-file edit is quarantined under.
GUARD_REGION = "spine-file"
_HINT = (
    "spine/ is generated from MC-2 — change the element there "
    "(add_spine_version / set_spine_element), never the file."
)

# Columns the renderer reads off each spine_substance row. Explicit, never '*'.
RENDER_SELECT = (
    "id, project_code, est_item_id, est_item_kind, phase, binding, layer, "
    "placement, serves, version_label, version_date, status, framing, body, "
    "sources, origin, scope, archived, rel_path"
)
_STEPS_SELECT = "est_item_id, position, title, status, step_date, note"


def _is_exempt(rel: Path) -> bool:
    parts = rel.parts
    if any(p.endswith(".snapshots") for p in parts[:-1]):
        return True
    return parts == ("Retrospective", "meeting-history.md")


class MirrorDir:
    """One generated directory (a ``spine/`` or a ``_stakeholders/``)."""

    def __init__(self, root: Path, *, writer: str,
                 warnings_out: list[str] | None = None):
        self.root = root
        self.writer = writer
        self.warnings_out = warnings_out
        self.claimed: set[str] = set()
        self.foreign: list[region_guard.ForeignEdit] = []
        self._manifest_path = root / MANIFEST_NAME
        self.manifest: dict[str, str] = {}
        try:
            loaded = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.manifest = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            pass
        self._initial = dict(self.manifest)

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def claim(self, path: Path) -> None:
        """Keep ``path`` out of the reap without writing it (a render that
        failed must never turn into deletion of the last good copy — #197)."""
        self.claimed.add(self._rel(path))

    def _report(self, path: Path, discarded: str, replacement: str) -> None:
        edit = region_guard.report(
            source=path, region=GUARD_REGION, discarded=discarded,
            replacement=replacement, writer=self.writer, hint=_HINT,
        )
        self.foreign.append(edit)
        if self.warnings_out is not None:
            self.warnings_out.append(
                f"hand edit in generated {edit.path} replaced"
                + (f"; preserved in {edit.quarantined}" if edit.quarantined
                   else "; COULD NOT PRESERVE IT")
            )

    def write(self, path: Path, text: str) -> bool:
        """Write ``text`` to ``path`` (guarded). True when the bytes changed."""
        rel = self._rel(path)
        self.claimed.add(rel)
        if path.exists():
            have = path.read_text(encoding="utf-8")
            if have == text:
                self.manifest[rel] = region_guard.digest(text)
                return False
            recorded = self.manifest.get(rel)
            if recorded is not None and recorded != region_guard.digest(have):
                self._report(path, have, text)
            elif recorded is None:
                logger.info("generated spine: first render of %s replaces "
                            "an unstamped file", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.manifest[rel] = region_guard.digest(text)
        return True

    def reap(self) -> list[Path]:
        """Remove every ``*.md`` the render did not claim. Engine-written
        files go silently; anything else is quarantined first."""
        removed: list[Path] = []
        if not self.root.is_dir():
            return removed
        for md in sorted(self.root.rglob("*.md")):
            rel_path = md.relative_to(self.root)
            rel = rel_path.as_posix()
            if rel in self.claimed or _is_exempt(rel_path):
                continue
            try:
                have = md.read_text(encoding="utf-8")
            except OSError:
                continue
            recorded = self.manifest.get(rel)
            if recorded is None or recorded != region_guard.digest(have):
                self._report(
                    md, have,
                    "(file removed — no MC-2 element renders it; import it "
                    "into MC-2 if it still matters)",
                )
            md.unlink()
            removed.append(md)
            self.manifest.pop(rel, None)
            parent = md.parent
            while parent != self.root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        for rel in list(self.manifest):
            if not (self.root / rel).exists():
                self.manifest.pop(rel)
        return removed

    def save(self) -> None:
        if self.manifest == self._initial and (
            self._manifest_path.exists() or not self.manifest
        ):
            return
        if not self.manifest:
            self._manifest_path.unlink(missing_ok=True)
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(
            json.dumps(self.manifest, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._initial = dict(self.manifest)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "item"


def distilled_path(project_dir: Path, row: dict) -> Path:
    """Where a distilled (work-item) element renders: its row's ``rel_path``
    when that names a file under ``spine/`` (outside ``_authored/``), else
    ``spine/<phase-slug>/<item-slug>.md`` — the shape the promote path mints."""
    rel = str(row.get("rel_path") or "")
    parts = Path(rel).parts
    if (rel.endswith(".md") and len(parts) >= 3 and parts[0] == "spine"
            and parts[1] != "_authored" and ".." not in parts):
        return project_dir / rel
    phase = row.get("phase")
    return (project_dir / "spine" / (_slugify(phase) if phase else "unbound")
            / f"{_slugify(str(row.get('est_item_id') or ''))}.md")


def element_path(project_dir: Path, rows: list[dict]) -> Path:
    first = rows[0]
    eid = str(first["est_item_id"])
    if first.get("origin") == "authored":
        return project_dir / "spine" / "_authored" / f"{authored_slug(eid)}.md"
    return distilled_path(project_dir, first)


def _fetch(client, table: str, cols: str, **eq) -> list[dict]:
    q = client.table(table).select(cols)
    for k, v in eq.items():
        q = q.eq(k, v)
    return q.execute().data or []


def plan_project_files(rows: list[dict], project_dir: Path):
    """``([(path, family, est_item_id, rows)], [collision warnings])`` — the
    files a render of these spine_substance rows produces under ``spine/``.
    Account-scope rows and retired (all-archived) elements produce none.
    Shared by the render and by the hand-written-card importer, so "what the
    render would produce" cannot drift between them."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if (r.get("scope") or "project") == "account":
            continue
        fam = "authored" if r.get("origin") == "authored" else "distilled"
        groups.setdefault((fam, str(r["est_item_id"])), []).append(r)
    plan, collisions = [], []
    taken: dict[Path, str] = {}
    for (fam, eid), group in sorted(groups.items()):
        if all(r.get("archived") for r in group):
            continue  # retired → not rendered → reaped
        path = element_path(project_dir, group)
        if path in taken:
            collisions.append(
                f"spine render: {eid} and {taken[path]} both render to "
                f"{path.relative_to(project_dir)}; kept the first")
            continue
        taken[path] = eid
        plan.append((path, fam, eid, group))
    return plan, collisions


def render_project_spine(
    client,
    *,
    project_id: str,
    project_code: str,
    project_dir: Path,
    writer: str = "cxp sync",
    warnings_out: list[str] | None = None,
) -> int:
    """Render the project's whole ``spine/`` from MC-2, guarded. Returns the
    number of element files rendered (written or already current).

    Rows are read by ``project_id`` (the stable uuid), so a stale
    ``project_code`` on a row — the two archived ``GGL-london-…`` rows — still
    renders into this workstream's directory. Account-scope rows are skipped:
    they render under the account's ``_stakeholders/`` (`render_account_dir`).
    A read failure RAISES before anything is touched — an empty fetch must
    never be read as "every element was deleted" and reap the directory."""
    rows = _fetch(client, Tables.SPINE_SUBSTANCE, RENDER_SELECT,
                  project_id=project_id)
    steps_by_item: dict[str, list[dict]] = {}
    try:
        for s in _fetch(client, Tables.SPINE_STEPS, _STEPS_SELECT,
                        project_id=project_id):
            steps_by_item.setdefault(s["est_item_id"], []).append(s)
    except Exception as exc:  # noqa: BLE001 — steps degrade, the render doesn't
        logger.warning("spine steps unreadable for %s; rendering without "
                       "them: %s", project_code, exc)
        if warnings_out is not None:
            warnings_out.append(f"steps not rendered: {exc}")

    mirror = MirrorDir(project_dir / "spine", writer=writer,
                       warnings_out=warnings_out)
    rendered = 0
    plan, collisions = plan_project_files(rows, project_dir)
    for msg in collisions:
        logger.warning(msg)
        if warnings_out is not None:
            warnings_out.append(msg)
    for path, fam, eid, group in plan:
        try:
            text = render_element(
                est_item_id=eid, rows=group, steps=steps_by_item.get(eid),
                kind="context" if fam == "authored" else None, path=path,
            )
        except Exception as exc:  # noqa: BLE001 — one bad element
            mirror.claim(path)
            msg = f"spine element not rendered for {project_code} / {eid}: {exc}"
            logger.warning(msg)
            if warnings_out is not None:
                warnings_out.append(msg)
            continue
        mirror.write(path, text)
        rendered += 1
    # An account-scope element renders under _stakeholders/, so its old
    # project-side copy is reaped here — unless the account render cannot
    # produce it either (zero/two live, #198): then the project copy is the
    # last file on either side and stays (#197).
    account: dict[str, list[dict]] = {}
    for r in rows:
        if (r.get("scope") or "project") == "account" and r.get("origin") == "authored":
            account.setdefault(str(r["est_item_id"]), []).append(r)
    for eid, group in account.items():
        if all(r.get("archived") for r in group):
            continue
        try:
            render_element(est_item_id=eid, rows=group, kind="context")
        except Exception:  # noqa: BLE001 — unrenderable → keep the copy
            mirror.claim(project_dir / "spine" / "_authored" / f"{authored_slug(eid)}.md")
    mirror.reap()
    mirror.save()
    return rendered


def render_account_dir(
    client,
    *,
    company_id: str,
    stakeholders_dir: Path,
    writer: str = "cxp sync",
    warnings_out: list[str] | None = None,
) -> int:
    """Render a company's account-scope elements into ``_stakeholders/``
    (guarded like ``spine/``). Every project of the company converges on the
    same files. A pre-promotion copy of an element under a project's
    ``spine/_authored/`` is that project's render's to reap (account rows are
    not rendered there)."""
    rows = _fetch(client, Tables.SPINE_SUBSTANCE, RENDER_SELECT,
                  company_id=company_id, scope="account", origin="authored")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r["est_item_id"]), []).append(r)
    mirror = MirrorDir(stakeholders_dir, writer=writer, warnings_out=warnings_out)
    written = failed = 0
    for eid, group in sorted(groups.items()):
        if all(r.get("archived") for r in group):
            continue
        path = stakeholders_dir / f"{authored_slug(eid)}.md"
        try:
            text = render_element(est_item_id=eid, rows=group, kind="context",
                                  path=path)
        except Exception as exc:  # noqa: BLE001 — one bad element (#198)
            failed += 1
            mirror.claim(path)
            msg = f"account element not rendered for {eid}: {exc}"
            logger.warning(msg)
            if warnings_out is not None:
                warnings_out.append(msg)
            continue
        mirror.write(path, text)
        written += 1
    if failed:
        logger.warning("account render into %s: %d element(s) skipped, %d "
                       "written", stakeholders_dir, failed, written)
    mirror.reap()
    mirror.save()
    return written
