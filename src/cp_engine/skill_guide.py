"""The skill guide: what each cp skill does and how to start it (#395).

People did not know which cp skills exist, what they do, or the words that
start them. Every surface that answers that question reads this module, so the
answer is the same everywhere:

  * `/cp-help` (plugin command) → `cxp skills [NAME]` → `render_table` /
    `render_detail`;
  * the start-of-session hint → `cxp skills --hint` → `render_hint`;
  * the shareable guide page → `cxp skills --html PATH` → `render_html`;
  * the tenant `CLAUDE.md` trigger table → `claude_md_rows` (render.py).

ONE SOURCE: each skill's own frontmatter. Plugin commands
(`plugin/commands/*.md`) and plugin skills (`plugin/skills/*/SKILL.md`) carry
optional fields beside `name`/`description`:

    label: wrap up            # 1-3 words; the name a person knows it by
    summary: Close out ...    # one plain-English line
    triggers:                 # phrases a person says to start it
      - wrap up
    example: wrap up          # one concrete example
    hint: false               # leave it out of the one-line session hint

Tenant skills (`.claude/skills/*/SKILL.md`) are read live and usually carry
only `name` + `description`; every field falls back to what the description
says (its first clause; quoted phrases after "Use when …").

The plugin directory is not in the wheel, so an installed `cxp` reads a
committed SNAPSHOT of the plugin frontmatter (`data/plugin_skills.json`),
unless `CLAUDE_PLUGIN_ROOT` points at a live plugin (the SessionStart hook
runs with it set). `tests/test_skill_guide.py` fails when the snapshot drifts
from the plugin files; regenerate with
`python -m cp_engine.skill_guide --write-snapshot`.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

SNAPSHOT = Path(__file__).parent / "data" / "plugin_skills.json"
REPO_PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
TENANT_SKILLS_DIR = Path(".claude") / "skills"

HINT_MAX = 200
SUMMARY_MAX = 90
HINT_PREFIX = "cp skills: "
HINT_SUFFIX = " — /cp-help for details"

# Order the plugin entries the way a person meets them, not alphabetically.
_ORDER = (
    "cp-wrapup", "cp-prep", "build-stakeholder", "proposal-architect",
    "rfp-authoring", "cp-wrap", "cp-help", "cp-tools",
)


@dataclass(frozen=True)
class Skill:
    name: str
    kind: str  # "command" | "skill" | "tenant"
    label: str
    summary: str
    triggers: tuple[str, ...] = ()
    example: str = ""
    hint: bool = True
    source: str = ""
    description: str = field(default="", compare=False)

    @property
    def slash(self) -> str | None:
        """The slash form, when there is one a person can type."""
        return f"/{self.name}" if self.kind in ("command", "tenant") else None

    def start(self) -> str:
        """How to start it, in one short phrase: at most two ways."""
        parts = []
        if self.slash:
            parts.append(f"`{self.slash}`")
        parts += [f'"{t}"' for t in self.triggers]
        return " or ".join(parts[:2]) if parts else f"`/{self.name}`"


# ── reading ─────────────────────────────────────────────────────────────


def read_frontmatter(path: Path) -> dict:
    """The YAML header of a skill/command file; {} when there is none.

    A header YAML cannot parse (an unquoted `: ` inside a description is the
    usual cause) still yields `name` and `description` line by line — the same
    two-field reader the hosted `list_skills` verb uses — so one bad file does
    not drop a skill from the guide."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    head = text[3:end]
    try:
        data = yaml.safe_load(head)
        if isinstance(data, dict):
            return data
    except yaml.YAMLError:
        pass
    out: dict = {}
    for line in head.splitlines():
        m = re.match(r"^(name|description):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


_USE_WHEN = re.compile(r"\b(?:Use|Triggers?)\b[^.]*?\b(?:when|whenever|on)\b", re.I)
_QUOTED = re.compile(r'"([^"]{2,80})"|“([^”]{2,80})”')


def triggers_from_description(desc: str) -> tuple[str, ...]:
    """Quoted phrases from the "Use when … says "x", "y"" part of a
    description. Nothing quoted → no triggers (the slash form still works)."""
    m = _USE_WHEN.search(desc)
    if not m:
        return ()
    tail = desc[m.start():]
    found = []
    for a, b in _QUOTED.findall(tail):
        phrase = (a or b).strip()
        if phrase and phrase not in found:
            found.append(phrase)
    return tuple(found)


def summary_from_description(desc: str) -> str:
    """The description's first clause, without parentheticals, capped."""
    s = re.sub(r"\s*\([^)]*\)", "", desc or "").strip()
    s = re.split(r"\s+[—–]\s+|(?<=[a-z0-9)])\.\s|\s+Use (?:when|whenever)\b", s, maxsplit=1)[0]
    s = s.strip().rstrip(".")
    if len(s) > SUMMARY_MAX:
        s = s[: SUMMARY_MAX - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def _as_tuple(v) -> tuple[str, ...]:
    if isinstance(v, str):
        return (v.strip(),) if v.strip() else ()
    if isinstance(v, list):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return ()


def skill_from_file(path: Path, kind: str, rel_to: Path | None = None) -> Skill:
    fm = read_frontmatter(path)
    default_name = path.stem if kind == "command" else path.parent.name
    name = str(fm.get("name") or default_name).strip()
    desc = str(fm.get("description") or "").strip()
    triggers = _as_tuple(fm.get("triggers")) or triggers_from_description(desc)
    label = str(fm.get("label") or "").strip() or (
        triggers[0] if kind != "tenant" and triggers else name.replace("-", " ")
    )
    summary = str(fm.get("summary") or "").strip() or summary_from_description(desc)
    slash = f"/{name}" if kind in ("command", "tenant") else None
    example = str(fm.get("example") or "").strip() or (
        triggers[0] if triggers else (slash or name)
    )
    hint = fm.get("hint", True) is not False
    try:
        source = str(path.relative_to(rel_to)) if rel_to else path.name
    except ValueError:
        source = path.name
    return Skill(
        name=name, kind=kind, label=label, summary=summary, triggers=triggers,
        example=example, hint=hint, source=source, description=desc,
    )


def read_plugin(plugin_root: Path) -> list[Skill]:
    """Every plugin command and skill, from the files themselves."""
    out: list[Skill] = []
    cmds = plugin_root / "commands"
    if cmds.is_dir():
        out += [skill_from_file(p, "command", plugin_root) for p in sorted(cmds.glob("*.md"))]
    sk = plugin_root / "skills"
    if sk.is_dir():
        out += [
            skill_from_file(p, "skill", plugin_root)
            for p in sorted(sk.glob("*/SKILL.md"))
        ]
    rank = {n: i for i, n in enumerate(_ORDER)}
    return sorted(out, key=lambda s: (rank.get(s.name, len(rank)), s.name))


def read_tenant(tenant_root: Path) -> list[Skill]:
    """Tenant skills under `.claude/skills/<name>/SKILL.md`, read live."""
    base = tenant_root / TENANT_SKILLS_DIR
    if not base.is_dir():
        return []
    return [
        skill_from_file(p, "tenant", tenant_root)
        for p in sorted(base.glob("*/SKILL.md"))
    ]


def _skill_from_dict(d: dict) -> Skill:
    return Skill(**{**d, "triggers": tuple(d.get("triggers") or ())})


def snapshot_data(skills: list[Skill]) -> list[dict]:
    rows = []
    for s in skills:
        d = asdict(s)
        d["triggers"] = list(s.triggers)
        d.pop("description", None)
        rows.append(d)
    return rows


def load_snapshot() -> list[Skill]:
    try:
        return [_skill_from_dict(d) for d in json.loads(SNAPSHOT.read_text("utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def plugin_skills(plugin_root: Path | None = None) -> list[Skill]:
    """Plugin entries: an explicit root, then `CLAUDE_PLUGIN_ROOT`, then the
    committed snapshot (what an installed `cxp` has)."""
    for cand in (plugin_root, os.environ.get("CLAUDE_PLUGIN_ROOT")):
        if cand and (Path(cand) / "commands").is_dir():
            return read_plugin(Path(cand))
    return load_snapshot()


def find_tenant_root(start: Path | None = None) -> Path | None:
    d = (start or Path.cwd()).resolve()
    for p in (d, *d.parents):
        if (p / ".cp-engine.toml").is_file():
            return p
    return None


def all_skills(plugin_root: Path | None = None, tenant_root: Path | None = None) -> list[Skill]:
    built_in = plugin_skills(plugin_root)
    names = {s.name for s in built_in}
    tenant = read_tenant(tenant_root) if tenant_root else []
    return built_in + [s for s in tenant if s.name not in names]


def find(skills: list[Skill], query: str) -> Skill | None:
    q = query.strip().lstrip("/").lower()
    q = q.split(":", 1)[-1]  # "cp-engine:cp-wrapup" → "cp-wrapup"
    for s in skills:
        if q in (s.name.lower(), s.label.lower()):
            return s
    for s in skills:
        if any(q == t.lower() for t in s.triggers):
            return s
    return None


# ── rendering ───────────────────────────────────────────────────────────


def render_hint(skills: list[Skill], limit: int = HINT_MAX) -> str:
    """ONE line naming every skill a person might start, by label."""
    labels = [s.label for s in skills if s.hint]
    shown = list(labels)
    while shown:
        more = len(labels) - len(shown)
        body = " · ".join(shown) + (f" · +{more} more" if more else "")
        line = HINT_PREFIX + body + HINT_SUFFIX
        if len(line) <= limit:
            return line
        shown.pop()
    return (HINT_PREFIX + f"{len(labels)} available" + HINT_SUFFIX)[:limit]


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_table(skills: list[Skill]) -> str:
    """The `/cp-help` table: one row per skill."""
    lines = [
        "| Skill | What it does | Start with | Example |",
        "|---|---|---|---|",
    ]
    for s in skills:
        lines.append(
            f"| **{_cell(s.label)}** | {_cell(s.summary)} | {_cell(s.start())} "
            f"| `{_cell(s.example)}` |"
        )
    tenant = sum(1 for s in skills if s.kind == "tenant")
    lines.append("")
    lines.append(
        f"{len(skills)} skills ({tenant} from this tenant's `.claude/skills/`). "
        "`/cp-help <skill>` for one in detail."
    )
    return "\n".join(lines)


_KIND = {"command": "Plugin command", "skill": "Plugin skill", "tenant": "Tenant skill"}


def render_detail(s: Skill) -> str:
    lines = [f"## {s.label}", "", s.summary, ""]
    lines.append(f"- **Kind:** {_KIND.get(s.kind, s.kind)} (`{s.name}`)")
    if s.slash:
        lines.append(f"- **Type:** `{s.slash}`")
    if s.triggers:
        lines.append("- **Or say:** " + " · ".join(f'"{t}"' for t in s.triggers))
    lines.append(f"- **Example:** `{s.example}`")
    if s.source:
        lines.append(f"- **Source:** `{s.source}`")
    if s.description and s.description != s.summary:
        lines += ["", s.description]
    return "\n".join(lines)


def claude_md_rows(skills: list[Skill], existing: str) -> list[dict]:
    """Rows for the tenant CLAUDE.md trigger table: every plugin entry with a
    trigger, minus any whose first trigger already has a hand-written row in
    `existing` (the template source). The drift test checks the result."""
    rows = []
    for s in skills:
        if s.kind == "tenant" or not s.triggers:
            continue
        phrase = s.triggers[0]
        if f"| `{phrase}` |" in existing:
            continue
        how = f"run `{s.slash}`" if s.slash else f"the `{s.name}` skill"
        rows.append({"phrase": phrase, "action": f"{s.summary.rstrip('.')} — {how}."})
    return rows


_CSS = """
:root{--bg:#fbfaf8;--surface:#ffffff;--text:#1d1d1f;--muted:#6b6b70;--line:#e6e4df;
--accent:#2f5bd3;--chip:#eef2fc;--chip-text:#22408f;--code:#f3f2ee}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#141416;--surface:#1c1c1f;
--text:#ececef;--muted:#9a9aa2;--line:#2c2c31;--accent:#8aa7ff;--chip:#24293a;--chip-text:#c3d0ff;--code:#26262b}}
:root[data-theme="dark"]{--bg:#141416;--surface:#1c1c1f;--text:#ececef;--muted:#9a9aa2;--line:#2c2c31;
--accent:#8aa7ff;--chip:#24293a;--chip-text:#c3d0ff;--code:#26262b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px 64px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}
.lede{color:var(--muted);margin:0 0 32px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:32px 0 12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin:0 0 12px}
.card h3{font-size:18px;margin:0 0 4px}
.card p{margin:0 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}
.chip{background:var(--chip);color:var(--chip-text);border-radius:999px;padding:2px 10px;
font-size:14px;overflow-wrap:anywhere}
.ex{font-size:14px;color:var(--muted);margin:0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em;background:var(--code);
border-radius:6px;padding:1px 6px;overflow-wrap:anywhere}
footer{color:var(--muted);font-size:13px;margin-top:40px}
"""


def _card(s: Skill) -> str:
    e = html.escape
    chips = []
    if s.slash:
        chips.append(f'<span class="chip"><code>{e(s.slash)}</code></span>')
    chips += [f'<span class="chip">“{e(t)}”</span>' for t in s.triggers]
    return (
        '<section class="card">'
        f"<h3>{e(s.label)}</h3>"
        f"<p>{e(s.summary)}</p>"
        f'<div class="chips">{"".join(chips)}</div>'
        f'<p class="ex">Example: <code>{e(s.example)}</code></p>'
        "</section>"
    )


def render_html(skills: list[Skill], generated: str = "") -> str:
    """A self-contained guide page: no external assets, light and dark,
    one column that reads on a phone."""
    groups = [
        ("Built in", [s for s in skills if s.kind != "tenant"]),
        ("This tenant", [s for s in skills if s.kind == "tenant"]),
    ]
    body = []
    for title, items in groups:
        if not items:
            continue
        body.append(f"<h2>{html.escape(title)}</h2>")
        body += [_card(s) for s in items]
    foot = "Generated from each skill's own frontmatter by <code>cxp skills --html</code>"
    if generated:
        foot += f" · {html.escape(generated)}"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>cp Skills</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n<main>\n"
        "<h1>cp Skills</h1>\n"
        '<p class="lede">Say the words, or type the command, in a Claude Code '
        "session in your cp folder.</p>\n"
        + "\n".join(body)
        + f"\n<footer>{foot}</footer>\n</main>\n</body>\n</html>\n"
    )


def to_json(skills: list[Skill]) -> str:
    return json.dumps(snapshot_data(skills), indent=2, ensure_ascii=False)


def write_snapshot(plugin_root: Path = REPO_PLUGIN) -> Path:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(to_json(read_plugin(plugin_root)) + "\n", encoding="utf-8")
    return SNAPSHOT


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    if sys.argv[1:] == ["--write-snapshot"]:
        print(write_snapshot())
    else:
        print("usage: python -m cp_engine.skill_guide --write-snapshot", file=sys.stderr)
        sys.exit(2)
