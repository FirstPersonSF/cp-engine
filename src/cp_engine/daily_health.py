"""The daily health line — one Slack message a day (architecture plan step 3).

WHY. Silent failure is attached to 37 of cp's 130 recorded defects. #194 ran
three months; #263 ate 39 account summaries over 18 sync runs; CI was red for
a week through ~15 releases. In every case the signal existed somewhere —
a runs table, a workflow page, a Sentry event — and nobody was looking at it.
This puts the signals in front of the partners once a day, in one place,
terse enough to read in five seconds.

SHAPE. One line per check, label 1–3 words, numbers, ✅/⚠️:

    ✅ Sync · 06:12 ok
    ⚠️ Ingest 24h · 4 ok · 1 partial · 1 failed

THE LINE CARRIES NO ERROR TEXT (house UI rule 1: labels and segments are 1–3
words, never sentences). Every segment after the label is a number, a time,
or a word or three. The error behind a ⚠️ — the exception, the last failed
run's message — goes in ``Check.detail["error"]``: `cxp health --json`, the
cron route's response + run row (``check_errors``) and the logs carry it; the
Slack line never does.

Every check is a function returning a `Check`. A check that cannot read its
source does NOT disappear and does NOT read healthy: it renders ⚠️ with a
short reason (``⚠️ CI main · unreadable``). A health report that goes quiet
when its inputs break is the defect class it exists to catch.

Inputs are injected (`client`, `fetch_json`, `github`) so tests pin the exact
states; `gather()` wires the real ones. Read-only everywhere.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cp_engine.clock import tenant_now, tenant_timezone
from cp_engine.mc2_db import Tables

OK = "✅"
WARN = "⚠️"

HOSTED_HEALTH_URL = "https://cp.mc-2.1p.is/health"
ENGINE_REPO = "FirstPersonSF/cp-engine"
ENGINE_CI_WORKFLOW = "tests.yml"
TENANT_SYNC_WORKFLOW = "sync.yml"
DEFAULT_TENANT_REPO = "FirstPersonSF/cp"

_ERR_CHARS = 300  # detail["error"] only — never the rendered line

# Not in `mc2_db.Tables` yet: the hosted server vendors that class verbatim
# and the drift test pins it. Move it there when step 1 (hosted imports the
# engine) deletes the vendor tree. Created by webhook/migrations/04.
WEBHOOK_RUNS = Tables.WEBHOOK_RUNS


@dataclass
class Check:
    label: str
    ok: bool
    text: str
    detail: dict = field(default_factory=dict)

    def render(self) -> str:
        return f"{OK if self.ok else WARN} {self.label} · {self.text}"


def _clip(text: Any, n: int = _ERR_CHARS) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _why(exc: BaseException | str) -> str:
    return _clip(exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}")


def _unreadable(label: str, exc: BaseException | str, short: str = "unreadable",
                **detail) -> Check:
    """⚠️ with a ≤3-word reason on the line; the full reason in detail."""
    return Check(label, False, short, {**detail, "error": _why(exc)})


_NO_CLIENT = "no MC-2 client (SUPABASE_URL / key unset)"


def _local(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tenant_timezone())


def _hhmm(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "?"
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


# ── GitHub ────────────────────────────────────────────────────────────────

GitHubGet = Callable[[str], Any]


def github_get(path: str) -> Any:
    """GET api.github.com<path>. Token from GH_TOKEN/GITHUB_TOKEN/GH_PAT; falls
    back to the `gh` CLI (a local dry run). Raises on failure — callers
    render the failure, they never hide it."""
    # The workflow's own token (`GITHUB_REPO_TOKEN: ${{ github.token }}`) can
    # read this repo's Actions but no other; GH_PAT reads cp-engine but not
    # necessarily the tenant (it 404'd on the tenant's sync runs). Use the
    # repo's own token for the repo the job runs in, GH_PAT elsewhere.
    own_repo = os.environ.get("GITHUB_REPOSITORY")
    own_token = os.environ.get("GITHUB_REPO_TOKEN")
    if own_repo and own_token and path.startswith(f"/repos/{own_repo}/"):
        token = own_token
    else:
        token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                 or os.environ.get("GH_PAT"))
    if token:
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — fixed host
            return json.loads(resp.read().decode("utf-8"))
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gh api failed (no token in env)")
    return json.loads(out.stdout)


def _latest_run(github: GitHubGet, repo: str, workflow: str, branch: str | None = None) -> dict | None:
    q = "?per_page=1" + (f"&branch={branch}" if branch else "")
    data = github(f"/repos/{repo}/actions/workflows/{workflow}/runs{q}")
    runs = (data or {}).get("workflow_runs") or []
    return runs[0] if runs else None


SYNC_COMMIT_PREFIX = "[cp-sync]"


def last_sync_commit(tenant_root: Path | None) -> tuple[datetime | None, str | None]:
    """(committer time, short sha) of the newest `[cp-sync]` commit in the
    tenant checkout's history — (None, None) when there is none to see.

    The fallback source for the Sync line when the Actions API is unreadable
    (Railway has no job token; the webhook's GH_PAT 404s on the tenant). It is
    a WEAKER claim than a run conclusion: sync commits only when the render
    changed something, and a failed run leaves no commit. The line says which
    source it used.
    """
    if tenant_root is None:
        return None, None
    try:
        out = subprocess.run(
            ["git", "-C", str(tenant_root), "log", "-1", "--fixed-strings",
             f"--grep={SYNC_COMMIT_PREFIX}", "--format=%cI %h"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    line = out.stdout.strip() if out.returncode == 0 else ""
    if not line:
        return None, None
    ts, _, sha = line.partition(" ")
    return _local(ts), sha or None


def check_sync(github: GitHubGet, tenant_repo: str, now: datetime,
               tenant_root: Path | None = None) -> Check:
    """Last tenant sync workflow run: time and conclusion.

    Source order: the Actions API (run conclusion — the strong claim), then,
    when that is unreadable and a checkout is given, the newest `[cp-sync]`
    commit in it (`via git`, with the Actions reason). Neither readable → ⚠️.
    """
    label = "Sync"
    try:
        run = _latest_run(github, tenant_repo, TENANT_SYNC_WORKFLOW)
    except Exception as exc:  # noqa: BLE001 — rendered as ⚠️, never hidden
        actions_err = f"Actions: {_why(exc)}"
        when, sha = last_sync_commit(tenant_root)
        if tenant_root is None:
            return _unreadable(label, actions_err, source="none")
        if when is None:
            return _unreadable(
                label, f"{actions_err}; no {SYNC_COMMIT_PREFIX} commit in checkout",
                source="none")
        age_h = (now - when).total_seconds() / 3600
        text = f"{_hhmm(when, now)} · via git"
        if age_h > 26:
            text += f" · {int(age_h)}h old"
        return Check(label, age_h <= 26, text,
                     {"source": "git", "sha": sha, "error": actions_err})
    if run is None:
        return Check(label, False, "no runs", {"source": "actions"})
    when = _local(run.get("updated_at") or run.get("created_at"))
    concl = run.get("conclusion") or run.get("status") or "?"
    age_h = (now - when).total_seconds() / 3600 if when else None
    ok = concl == "success" and age_h is not None and age_h <= 26
    text = f"{_hhmm(when, now)} {'ok' if concl == 'success' else concl}"
    if concl == "success" and age_h is not None and age_h > 26:
        text += f" · {int(age_h)}h old"
    return Check(label, ok, text, {"source": "actions", "conclusion": concl,
                                   "url": run.get("html_url")})


def check_ci(github: GitHubGet, now: datetime) -> Check:
    """cp-engine main's latest test run."""
    label = "CI main"
    try:
        run = _latest_run(github, ENGINE_REPO, ENGINE_CI_WORKFLOW, branch="main")
    except Exception as exc:  # noqa: BLE001
        return _unreadable(label, exc)
    if run is None:
        return Check(label, False, "no runs")
    concl = run.get("conclusion") or run.get("status") or "?"
    when = _local(run.get("updated_at") or run.get("created_at"))
    text = f"{'green' if concl == 'success' else concl} ({_hhmm(when, now)})"
    return Check(label, concl == "success", text, {"url": run.get("html_url")})


# ── MC-2 run tables ───────────────────────────────────────────────────────


def _since(now: datetime, hours: int = 24) -> str:
    return (now.astimezone(UTC) - timedelta(hours=hours)).isoformat()


def check_ingest(client, now: datetime) -> Check:
    """auto_ingest_runs in the last 24h: ok / partial / failed + last error.

    PARTIAL = status 'success' that still carries errors (the #194 shape) or
    side-step warnings (`warnings` column, or the pre-migration
    ``plan_summary._warnings`` fold)."""
    label = "Ingest 24h"
    if client is None:
        return _unreadable(label, _NO_CLIENT, "no client")
    since = _since(now)
    try:
        try:
            rows = (client.table(Tables.AUTO_INGEST_RUNS)
                    .select("status, errors, warnings, plan_summary, created_at")
                    .gte("created_at", since).order("created_at", desc=True)
                    .limit(500).execute().data) or []
        except Exception as exc:  # noqa: BLE001 — only a missing `warnings` column is tolerated
            if "warnings" not in str(exc):
                raise
            rows = (client.table(Tables.AUTO_INGEST_RUNS)
                    .select("status, errors, plan_summary, created_at")
                    .gte("created_at", since).order("created_at", desc=True)
                    .limit(500).execute().data) or []
    except Exception as exc:  # noqa: BLE001
        return _unreadable(label, exc)
    ok = partial = failed = noop = 0
    last_err = None
    for r in rows:
        st = r.get("status")
        warns = r.get("warnings") or ((r.get("plan_summary") or {}).get("_warnings")
                                      if isinstance(r.get("plan_summary"), dict) else None)
        if st == "failed":
            failed += 1
        elif st == "skipped_no_op" and not warns:
            noop += 1
        elif r.get("errors") or warns:
            partial += 1
        else:
            ok += 1
        if last_err is None and (st == "failed" or r.get("errors") or warns):
            last_err = (r.get("errors") or warns or ["(failed, no error text)"])[0]
    parts = [f"{ok} ok"]
    if noop:
        parts.append(f"{noop} no-op")
    parts += [f"{partial} partial", f"{failed} failed"]
    text = " · ".join(parts)
    if not rows:
        text = "0 runs"
    detail = {"ok": ok, "partial": partial, "failed": failed, "noop": noop}
    if last_err:
        detail["error"] = _clip(last_err)
    return Check(label, failed == 0 and partial == 0, text, detail)


def check_webhook(client, now: datetime) -> Check:
    """Webhook run failures in the last 24h across every run table.

    `webhook_runs` (every POST, step 3) plus the domain tables that predate it
    (spine_promote_runs, asset_ingest_runs). A missing ledger table is itself
    a ⚠️ — it means migration 04 has not been applied and the routes are
    writing their ledger rows into an ERROR log."""
    label = "Webhook 24h"
    if client is None:
        return _unreadable(label, _NO_CLIENT, "no client")
    since = _since(now)
    failed = partial = rejected = 0
    last_err = None
    notes: list[str] = []
    errors: list[str] = []
    try:
        rows = (client.table(WEBHOOK_RUNS)
                .select("route, status, error, created_at")
                .gte("created_at", since).in_("status", ["failed", "partial", "rejected"])
                .order("created_at", desc=True).limit(500).execute().data) or []
        for r in rows:
            st = r.get("status")
            failed += st == "failed"
            partial += st == "partial"
            rejected += st == "rejected"
            if last_err is None and st in ("failed", "partial"):
                last_err = f"{r.get('route')}: {r.get('error') or st}"
    except Exception as exc:  # noqa: BLE001 — named, not hidden
        msg = str(exc)
        notes.append("ledger missing" if ("webhook_runs" in msg and (
            "does not exist" in msg or "42P01" in msg or "PGRST205" in msg))
            else "ledger unreadable")
        errors.append(f"{WEBHOOK_RUNS}: {_why(exc)}")
    for table, col, short in ((Tables.SPINE_PROMOTE_RUNS, "started_at", "promote runs"),
                              (Tables.ASSET_INGEST_RUNS, "started_at", "asset runs")):
        try:
            rows = (client.table(table).select(f"status, error, {col}")
                    .gte(col, since).eq("status", "failed")
                    .order(col, desc=True).limit(200).execute().data) or []
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{short} unreadable")
            errors.append(f"{table}: {_why(exc)}")
            continue
        failed += len(rows)
        if rows and last_err is None:
            last_err = f"{table}: {rows[0].get('error') or 'failed'}"
    parts = [f"{failed} failed", f"{partial} partial"]
    if rejected:
        parts.append(f"{rejected} rejected")
    text = " · ".join(parts + notes)
    detail = {"failed": failed, "partial": partial, "rejected": rejected}
    if last_err:
        errors.insert(0, _clip(last_err))
    if errors:
        detail["error"] = "; ".join(errors)
    return Check(label, failed == 0 and partial == 0 and not notes, text, detail)


def check_unbound(client) -> Check:
    """Live spine elements left unbound, and the important-but-floating subset
    (the `spine_lint` finding: important, unbound, serves nothing)."""
    label = "Spine unbound"
    if client is None:
        return _unreadable(label, _NO_CLIENT, "no client")
    try:
        rows = (client.table(Tables.SPINE_SUBSTANCE)
                .select("project_id, est_item_id, important, serves")
                .eq("binding", "unbound").eq("status", "live").eq("archived", False)
                .limit(5000).execute().data) or []
    except Exception as exc:  # noqa: BLE001
        return _unreadable(label, exc)
    elements = {(r.get("project_id"), r.get("est_item_id")) for r in rows}
    floating = {(r.get("project_id"), r.get("est_item_id")) for r in rows
                if r.get("important") and not (r.get("serves") or [])}
    text = f"{len(elements)}"
    if floating:
        text += f" · {len(floating)} floating"
    # Unbound context is normal; only important-and-floating is a finding.
    return Check(label, not floating, text,
                 {"unbound": len(elements), "floating": len(floating)})


# ── tenant tree ───────────────────────────────────────────────────────────

_STALE_RE = re.compile(r"⚠️ _summary (\d+)d stale_")
_PARTIAL_RE = re.compile(r"⚠️ _partial refresh_")


def check_stale_summaries(tenant_root: Path | None) -> Check:
    """Stale + partially-refreshed Exec Summaries, as the last sync rendered
    them into master-cp.md (sync owns the computation; this only counts)."""
    label = "Stale summaries"
    if tenant_root is None:
        return _unreadable(label, "no tenant checkout", "no checkout")
    try:
        text = (tenant_root / "master-cp.md").read_text(encoding="utf-8")
    except OSError as exc:
        return _unreadable(label, exc)
    stale = [int(m) for m in _STALE_RE.findall(text)]
    partial = len(_PARTIAL_RE.findall(text))
    out = f"{len(stale)}"
    if stale:
        out += f" (max {max(stale)}d)"
    if partial:
        out += f" · {partial} partial"
    return Check(label, not stale and not partial, out,
                 {"stale": len(stale), "partial": partial})


# ── hosted server + copies-disagree ───────────────────────────────────────


def fetch_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — our own host
        return json.loads(resp.read().decode("utf-8"))


def check_hosted(health: dict | None, error: str | None = None) -> Check:
    label = "Hosted"
    if health is None:
        return _unreadable(label, error or "no response", "unreachable")
    version = str(health.get("server_version") or "?").rsplit("/", 1)[-1]
    tools = health.get("tool_count")
    read = (health.get("read_endpoint") or {}).get("tool_count")
    ok = health.get("status") == "healthy" and health.get("deps_ok", True) is not False
    text = f"{version} · {tools} tools"
    if read is not None:
        text += f" · {read} read"
    detail = {"version": version}
    if health.get("deps_ok") is False:
        text += " · deps failing"
        detail["error"] = f"deps: {_clip(health.get('deps'))}"
    elif health.get("status") != "healthy":
        status = str(health.get("status") or "?")
        # One word on the line; anything longer is a sentence → detail.
        text += f" · {status}" if len(status.split()) == 1 and len(status) <= 20 \
            else " · unhealthy"
        detail["error"] = f"status: {_clip(status)}"
    return Check(label, ok, text, detail)


def check_copies(health: dict | None, engine_version: str, engine_week: str) -> Check:
    """Cheap copies-disagree checks: hosted vs engine version, and hosted vs
    engine sprint week when /health reports one (`sprint_week`). A field the
    hosted server does not report renders `n/a` — never as agreement."""
    label = "Copies"
    if health is None:
        return Check(label, False, f"engine {engine_version} · {engine_week} · hosted n/a")
    hv = str(health.get("server_version") or "?").rsplit("/", 1)[-1]
    hw = health.get("sprint_week")
    disagreements = []
    if hv != engine_version:
        disagreements.append(f"version {hv}≠{engine_version}")
    if hw and hw != engine_week:
        disagreements.append(f"week {hw}≠{engine_week}")
    if disagreements:
        return Check(label, False, " · ".join(disagreements))
    week = engine_week + ("" if hw else " (hosted n/a)")  # ≤ 3 words
    return Check(label, True, f"agree · {hv} · {week}")


# ── schedule gate ─────────────────────────────────────────────────────────

MORNING_HOUR = 5  # tenant-local hour the line is meant to land in


def is_morning_slot(cron: str, now: datetime, hour: int = MORNING_HOUR) -> bool:
    """Is THIS scheduled slot the one that lands at `hour` tenant-local time?

    GitHub cron is UTC and has no DST, so the workflow schedules two slots an
    hour apart and each run passes the cron expression that fired it
    (`github.event.schedule`). The gate reads the slot's INTENDED time, not
    the time the runner woke up — runner lag (hours, measured) must not move
    or duplicate the post. Exactly one of the pair matches on any date.
    """
    parts = cron.split()
    minute, hr = int(parts[0]), int(parts[1])
    utc_today = now.astimezone(UTC).date()
    intended = datetime(utc_today.year, utc_today.month, utc_today.day,
                        hr, minute, tzinfo=UTC)
    return intended.astimezone(tenant_timezone()).hour == hour


# ── compose ───────────────────────────────────────────────────────────────


@dataclass
class HealthReport:
    when: datetime
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> str:
        bad = sum(not c.ok for c in self.checks)
        head = (f"*cp health* · {self.when.strftime('%a %m-%d %H:%M')} · "
                + ("all clear" if not bad else f"{bad} need a look"))
        return "\n".join([head, *(c.render() for c in self.checks)])

    def errors(self) -> dict[str, str]:
        """{label: error} for every check carrying one — what the line leaves
        out. Logs, `--json` and the cron run row carry it; Slack does not."""
        return {c.label: c.detail["error"] for c in self.checks if c.detail.get("error")}

    def to_dict(self) -> dict:
        return {"when": self.when.isoformat(), "ok": self.ok,
                "checks": [{"label": c.label, "ok": c.ok, "text": c.text, **c.detail}
                           for c in self.checks]}


def _tenant_repo(tenant_root: Path | None) -> str:
    if tenant_root is not None:
        try:
            url = subprocess.run(["git", "-C", str(tenant_root), "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
            if m:
                return m.group(1)
        except (OSError, subprocess.SubprocessError):
            return DEFAULT_TENANT_REPO
    return DEFAULT_TENANT_REPO


def gather(
    *,
    tenant_root: Path | None,
    client=None,
    github: GitHubGet = github_get,
    hosted_fetch: Callable[[str], dict] = fetch_json,
    hosted_url: str = HOSTED_HEALTH_URL,
    now: datetime | None = None,
) -> HealthReport:
    """Run every check. Each check renders its own failure; none raises."""
    import cp_engine
    from cp_engine.sprints import current_sprint_week_iso

    now = now or tenant_now()
    if now.tzinfo is None:  # tenant_now() is tenant-local wall time, naive
        now = now.replace(tzinfo=tenant_timezone())
    hosted: dict | None = None
    hosted_err = None
    try:
        hosted = hosted_fetch(hosted_url)
    except Exception as exc:  # noqa: BLE001 — rendered by check_hosted
        hosted_err = f"{type(exc).__name__}: {exc}"
    checks = [
        check_sync(github, _tenant_repo(tenant_root), now, tenant_root),
        check_ingest(client, now),
        check_webhook(client, now),
        check_hosted(hosted, hosted_err),
        check_ci(github, now),
        check_unbound(client),
        check_stale_summaries(tenant_root),
        check_copies(hosted, cp_engine.__version__, current_sprint_week_iso(now)),
    ]
    return HealthReport(when=now, checks=checks)


def post(report: HealthReport, *, config, client, channel: str | None = None) -> str:
    """Post the health line, with the dates loop's bot token
    (`slack.load_slack_token`). Destination, first set wins: `channel`
    (`cxp health --channel`), env `CP_HEALTH_CHANNEL`, then the partners'
    channel (MC-2 app_config `dates_loop_partners_channel`). Raises when no
    destination is set or the post fails: a health line that silently didn't
    post is the thing it exists to prevent."""
    from cp_engine import slack as slack_mod
    from cp_engine.dates_loop import _partners_channel

    errors: list[str] = []
    channel = channel or os.environ.get("CP_HEALTH_CHANNEL") or None
    if not channel and client is not None:
        channel = _partners_channel(client, errors)
    if not channel:
        raise slack_mod.SlackError(
            "partners channel unset — set MC-2 app_config "
            f"`dates_loop_partners_channel`{'; ' + errors[0] if errors else ''}"
        )
    from slack_sdk import WebClient

    web = WebClient(token=slack_mod.load_slack_token(config))
    return slack_mod.post_channel(web, channel_id=channel, text=report.render())
