# Changelog

All notable changes to `cp-engine` are recorded here. The package follows [semver](https://semver.org/).

Tenants pin to a minor version (`engine = "~= 0.1"`). Patch updates flow automatically; minor bumps require explicit upgrade; major bumps require migration notes.

## v0.14.1 — 2026-05-28

### Added — diagnostic logging on all `/slack-action` 401 branches

`_verify_slack_signature` now emits `log.warning(...)` on every 401 reject path, with enough context to diagnose which of the 5 verification checks failed (missing/malformed header, missing/non-int timestamp, replay-window skew, or HMAC mismatch). For HMAC mismatches, logs the first 9 chars of expected vs provided signatures (safe: 6 hex chars of a single HMAC output don't compromise the secret), plus body and secret lengths so wrong-secret-entirely cases are distinguishable from body-encoding mismatches.

Triggered by a production debugging session: silent 401s on real Slack callbacks gave only the access-log line ("401 Unauthorized"), with the HTTPException detail string not surfaced anywhere actionable. Now the application logs name the exact failure mode.

No behavior change beyond logging. Same env vars.

## v0.14.0 — 2026-05-28

### Added — Interactive daily attention digest with Slack action buttons

Daily digest now emits Block Kit messages with one item per ask + risk and inline action buttons:

- **Risk buttons:** ✅ Resolve · 💤 Snooze 7d · 📅 Snooze until…
- **Non-ClickUp ask buttons:** ✅ Mark closed · 💤 Snooze 7d · 📅 Snooze until…
- **ClickUp-linked ask:** single "Open in ClickUp" link button. v0.12's bidirectional close-loop is the authoritative closure path; we deliberately avoid double sources of truth by hiding Resolve/Snooze on those.

Button clicks hit a new `POST /slack-action` endpoint that returns 200 immediately (Slack's 3-second ack window), then runs the cp plan + commit + push in a background asyncio task, then updates the original Slack message in-place via `response_url` so the buttons get replaced with "✅ Resolved · 7:42 AM · `commit_sha`".

### Added — Three new plan verbs

- `resolve-risk` — flips `[escalated · ...]` or `[watching · ...]` to `[resolved · ...]` matching by `cp_hash`. Stamps `<!-- cp:resolved-at=YYYY-MM-DD -->` and optional `<!-- cp:closed-by=<source> -->` audit markers. Hash-not-found is a silent no-op (matches Task 1.1 dedupe pattern) — Slack-button re-clicks on stale messages don't pollute `errors`.
- `snooze-ask` / `snooze-risk` — append (or replace) a `<!-- cp:snoozed-until=YYYY-MM-DD -->` HTML-comment marker on the bullet matching `cp_hash`. CRITICAL placement: marker is inserted BEFORE the `cp:hash=` marker, NOT after — `_OPEN_ASK_RE` and `_RISK_RE` anchor `$` after `cp:hash`, so trailing markers would silently break the digest scanner. Regression-tested.

Digest scanners (`_find_past_due_asks`, `_find_escalated_risks`) now check each matched bullet for the snooze marker and `continue` if `until > today`. The marker is stripped from the captured text before the digest UI renders it.

### Added — `_write_close_ask` hash branch (Slack-button entry point)

`_write_close_ask` now accepts an optional `hash` field. When present, matches by `cp:hash` marker; otherwise falls through to existing text/match substring (backward compat — v0.12's ClickUp pipeline keeps working unchanged).

### Added — `POST /slack-action` endpoint with async background dispatch

Receives Slack `block_actions` and `view_submission` payloads. Verifies Slack's `v0:<timestamp>:<body>` HMAC scheme against new `SLACK_SIGNING_SECRET` (with 5-minute timestamp replay window). Returns `200` immediately, then runs work in `_spawn_background(...)` (asyncio.create_task with strong-reference retention to defeat asyncio's weak-ref GC). All sync calls (git subprocess, slack_sdk WebClient, requests.post) wrap in `asyncio.to_thread` so the event loop stays free.

For the "Snooze until…" button, the modal must open within the 3-second `trigger_id` expiry window, so `views.open` runs inline (still via `asyncio.to_thread`). The actual snooze happens on the subsequent `view_submission` callback, which `_spawn_background`s the same way as block_actions.

The `_post_response_url_update` helper splices the actions block out of the original message and replaces it with a context block carrying the confirmation text — buttons get visually replaced in-place after the work completes. Non-2xx Slack responses are logged but never raise (fire-and-forget UX).

### Migration notes

- Tenants bump engine pin from `~= 0.13` to `~= 0.14`.
- **New required env vars on cp-engine-webhook Railway service:**
  - `SLACK_SIGNING_SECRET` — from the Slack app's "Basic Information" page (used by `/slack-action` for HMAC verification).
  - `SLACK_BOT_TOKEN` — `xoxb-...` bot token (used by `views.open` to launch the date-picker modal).
- **Slack app configuration:** enable Interactivity & Shortcuts, set Request URL to `https://cp-engine-production.up.railway.app/slack-action`.
- `[attention_digest].recipients` in `.cp-engine.toml` MUST be Slack user IDs (not channel IDs). `views.open`'s `trigger_id` is per-user — interactive buttons require a DM context. (Already true since v0.12; reasserting because v0.14 makes it load-bearing.)
- The CLI surface `cp attention-digest --post-to-slack` is unchanged. The daily-digest cron in cp tenant continues to work as-is.

### Out of scope (deferred)

- Slash-command entry point (`/cp snooze ibx-5167 1w`) — Block-Kit buttons cover the digest case; slash commands cover the "from any context" case, which isn't on the table yet.
- "Unsnooze" button — if Drew snoozes by mistake, hand-edit the marker out of the sprint file. Add only if the use case proves real.
- Channel posts — digest stays in DM. Channel posting requires different auth scopes + UX considerations.
- Audit trail UI — `cp:resolved-at` + `cp:closed-by=slack` markers are in the bullet markdown; a queryable audit page lives in dashboard territory, not engine territory.

## v0.13.0 — 2026-05-27

### Fixed — auto-ingest no longer drops content when target sprint file is missing

`execute_plan` now auto-scaffolds the target sprint file from the most recent prior sprint file for the same project when the target file is absent. Previously this was the single largest source of auto-ingest failures (5 of 12 historic failures pre-v0.13), all triggered by the late-in-week roll-forward in `_planning_monday` — Wed-Sun meetings tried to land in next week's sprint dir, which sync hadn't created yet.

- New `scaffold_from_prior(tenant_root, project_code, target_week_iso)` helper in `sprints.py`. Walks `sprints/*/<code>.md`, picks the most recent prior week, derives a minimal `ProjectState` from the prior file's frontmatter + `← [Project CP]`/`[Initiative CP]` navigation link (no MC-2 dependency), and renders the new file via the existing `render_sprint_scaffold` + Jinja templates. Supports 1p client engagements, FPSF/Canonic initiatives, and standalone repos.
- `execute_plan` calls `scaffold_from_prior` when `sprint_path.exists()` is False. Falls back to logging the error (as before) only when no prior sprint file exists for the project — first-ever-ingest edge case.

### Added — `POST /api/auto-ingest/runs/:run_id/rerun` for failed runs

Failed `auto_ingest_runs` rows are now re-runnable. The endpoint loads the row, extracts `meeting_id` + `project_codes`, and re-fires the pipeline via a new shared `_perform_auto_ingest` helper (extracted from `/api/auto-ingest`). Writes a NEW row (keeps the original as a failure record for diagnostics). HMAC-signed against `WEBHOOK_HMAC_SECRET`; only `status='failed'` rows may be re-fired.

The matching `fathom-meeting-sync` release adds a "Rerun" button on failed-status rows in the Auto-ingest activity panel. The dashboard's webhook-server proxy holds the HMAC secret and forwards to cp-engine-webhook.

Recovers historic Class 2 (Anthropic 529) + Class 3 (Anthropic usage-limit) failures, plus any remaining Class 1 failures from before auto-scaffold shipped.

### Migration notes

- Tenants bump engine pin from `~= 0.12` to `~= 0.13`.
- No new env vars or secrets on cp-engine.
- For the Rerun button to work end-to-end, deploy the matching fathom-meeting-sync release (server proxy + dashboard) and ensure `CP_AUTO_INGEST_URL` + `CP_AUTO_INGEST_HMAC_SECRET` are set on fathom-webhook-server (they already are if auto-ingest itself is working).

### Out of scope (deferred)

- Retry-with-backoff for Anthropic 529s — the rerun button gives manual recovery. Automate later if 529s recur.
- `rerun_of_id` column on `auto_ingest_runs` — inferable from order + project_codes for now. Add when a rerun-chain UI emerges.
- Rerun for non-failed runs — risky with hand-edited sprint files.

## v0.12.1 — 2026-05-27

### Fixed — `/clickup-task-closed` matches ClickUp's real webhook payload shape

Task 1.7 (shipped in v0.12.0) was built against an assumed payload shape that included the task `description` — we regexed the `[cp:hash=...]` trailer out of it to find the matching cp ask. ClickUp's real `taskStatusUpdated` payload doesn't include `description`, so the v0.12.0 endpoint would have 204'd on every real webhook call.

Refactored to match the actual payload:

- **Routing:** look up `cp_ask_hash` + project code from `clickup_task_proposals.clickup_task_id` (which the dashboard already stored at task-creation time) instead of trying to extract them from a `description` field that isn't there. One Supabase SELECT, no ClickUp API round-trip.
- **Signature header:** `X-Signature` (previously had `X-Webhook-Signature` wrong). Algorithm unchanged — HMAC-SHA256 hex against `CLICKUP_WEBHOOK_SECRET`.
- **Event filtering:** `taskStatusUpdated` fires on any status change. Now filter on `history_items[0].after.type == "closed"` so non-close transitions return 204 without doing any lookup or ingest work.
- Dropped `_lookup_project_for_hash` and the `_CP_HASH_RE` regex; added `_lookup_proposal_by_clickup_task_id(task_id) -> tuple[cp_hash, code] | None`. Tests rewritten to drive the new payload shape (7 cases covering happy path, non-closed status, missing task_id, bad HMAC, missing secret, orphan task, no-op rerun).

## v0.12.0 — 2026-05-27

### Added — Lever 1: bidirectional ClickUp (close a task → flip the cp ask)

When a ClickUp task created from a cp ask is marked done, cp now flips the matching ask to `[closed]` automatically. The full round-trip:

1. Fathom meeting auto-ingests. Each Fathom `action_item` becomes a deterministic `record-ask` in the sprint file with a `<!-- cp:hash=abc12345 -->` marker AND a row in `clickup_task_proposals` carrying the same `cp_ask_hash` column.
2. Dashboard reviewer approves the proposal. Dashboard's `buildDescription` appends `[cp:hash=abc12345]` to the ClickUp task description.
3. Someone marks the ClickUp task done. ClickUp fires the new `POST /clickup-task-closed` webhook. The handler regexes out the hash, looks up the owning project via `clickup_task_proposals.cp_ask_hash`, runs a `close-ask` plan with `closed_by=clickup`, commits + pushes.

**Code changes:**

- New helper `_action_items_to_ask_items` in `plan_from_transcript.py` converts Fathom's `action_items` JSONB into deterministic `record-ask` plan items. The hash recipe matches `ingest._content_hash(code, "record-ask", text)` exactly so `_write_ask`'s dedupe recognises re-ingests as no-ops AND the ClickUp round-trip works.
- `generate_plan()` gains an optional `action_items` kwarg. When provided, the helper-built items are merged into `plan["projects"][code]["record-ask"]` AFTER `_validate_plan` runs. **Architectural shift:** Fathom's structured `action_items` are now the canonical source of asks; the LLM still surfaces additional contextual asks from the transcript, but the action-items stream is authoritative and roundtrippable.
- `webhook/main.py`'s `/api/auto-ingest` fetches the meeting row ONCE (previously called twice — for ingest and artifact generation separately) and threads `action_items` through `_ingest_one_project`. Account-meeting and sprint-planning endpoints intentionally NOT updated — Fathom's `action_items` JSONB has no per-project attribution.
- `_write_close_ask` gains an optional `closed_by` field. When present, appends a trailing `<!-- cp:closed-by=<source> -->` HTML-comment marker on the flipped bullet for audit.
- New `_build_proposal_row` in `webhook/clickup_propose.py` populates `cp_ask_hash` using the same `_content_hash` recipe as ingest. Refactored from the inline row-build loop.
- New endpoint `POST /clickup-task-closed` on cp-engine-webhook. HMAC-validated against new env var `CLICKUP_WEBHOOK_SECRET` (separate from Fathom's `WEBHOOK_HMAC_SECRET` so they rotate independently). Returns 200 on success or orphan, 204 on no hash, 401 on bad signature.

**Schema change:**

- Supabase migration adds `clickup_task_proposals.cp_ask_hash text` (nullable) with a partial index on non-null values. Migration name: `add_cp_ask_hash_to_clickup_task_proposals`. Applied to MC-2's Supabase project.

**Companion repo:**

- `fathom-meeting-sync/fathom/clickup-tasks.js` reads `cp_ask_hash` from the proposal row and appends `[cp:hash=<8-hex>]` as the last line of the ClickUp task description. Backward compatible — proposals without a hash produce a description without the trailer.

### Added — Lever 2: daily attention digest

A new `cp attention-digest` CLI command scans the current sprint dir for past-due asks + recently-escalated risks and prints a markdown digest. With `--post-to-slack`, sends as a DM to configured recipients. A new GitHub Action `daily-digest.yml` runs daily at 14:00 UTC (7am PT) and posts to Drew automatically.

**Past-due definition:**
- `· by <date>` set AND `<date>` < today (immediately overdue), OR
- No `by` date AND asked at least N days ago (default 7 — stale).

**Escalated-risk window:** risks with `severity=escalated` AND `raised` within the last N days (default 7).

**Code changes:**

- New module `src/cp_engine/attention_digest.py` with `_find_past_due_asks`, `_find_escalated_risks`, `compose_digest`, `attention_digest` (orchestrator), and `_post_digest_to_recipients` (Slack DM helper).
- New CLI subcommand `cp attention-digest [--post-to-slack] [--recipient NAME] [--today YYYY-MM-DD]`. Default prints to stdout for local preview.
- New `post_dm(client, *, user_id, text)` helper in `src/cp_engine/slack.py`. Slack auto-opens a DM when `chat.postMessage` is called with `channel=<user_id>`.
- New `[attention_digest]` config block on `.cp-engine.toml`. All fields optional with sensible defaults. Type-validated via `CommittedConfigInvalid`. Mirrors how `[team]` and `[risk_categories]` are parsed.
- Orchestrator falls back to the previous ISO week's sprint dir when the current week's dir doesn't exist yet (Monday-morning case where no meeting has scaffolded the new sprint dir).

**Config shape:**

```toml
[attention_digest]
recipients = ["U12ABCDE"]            # Slack user IDs; empty disables --post-to-slack
past_due_threshold_days = 7          # no-by asks become "stale" after this many days
escalated_window_days = 7            # risks escalated within this window count as "recent"
allocation_cap_hours = 50            # reserved for Lever 3 (allocation watch); not yet used
post_when_clear = true               # post "all clear" affirmation when nothing's flagged
```

**Companion repo:**

- cp tenant gains `.github/workflows/daily-digest.yml`. Cron: `0 14 * * *`. Reuses existing secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SLACK_BOT_TOKEN`) — no new secrets needed.

### Migration notes

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.11` to `~= 0.12`.
2. Add the `[attention_digest]` block with at least `recipients = ["U..."]` if you want Slack DMs.
3. **For Lever 1:** set `CLICKUP_WEBHOOK_SECRET` on the cp-engine-webhook Railway service, subscribe ClickUp's "task closed" event to `https://cp-engine-production.up.railway.app/clickup-task-closed`, deploy the matching `fathom-meeting-sync` commit (the `[cp:hash=...]` description trailer).
4. **For Lever 2:** enable `daily-digest.yml` in the tenant's GitHub Actions. First scheduled fire happens at 14:00 UTC the day after enable.

### Known limitations

- **Initiative-sourced ClickUp tasks:** `clickup_propose._resolve_project` returns an initiative's `id` when `initiatives.enable_clickup=true`, but `clickup_task_proposals.project_id` has an FK constraint to `projects.id`. An initiative-sourced proposal would fail to insert today. **Latent today** (verified: zero orphan rows in production), but worth flagging — a fix needs either an alternate FK + lookup path OR a routing change in `clickup_propose`. Engagement-sourced ClickUp tasks (the vast majority) are unaffected.
- **Allocation watch:** `[attention_digest].allocation_cap_hours` is wired through config but the classifier always returns an empty list. Allocation is reserved for Lever 3+. The digest renders 0 lines for allocation today.
- **DST:** the daily-digest cron is at 14:00 UTC = 7am PT during DST. After standard time kicks in (Nov), the effective time shifts to 6am PT. If 7am-PT-year-round matters, dual-cron with idempotent posting is the right fix.

### Tests

- Test suite: 538 passing (was 483 at v0.11.0). 55 new tests across 7 files:
  - 5 in `test_plan_from_transcript.py` (action_items helper + merge)
  - 3 in `test_ingest.py` (close-ask closed_by marker)
  - 3 in `test_clickup_propose.py` (cp_ask_hash on proposal rows)
  - 8 in `test_webhook_clickup_closed.py` (new endpoint, including null-history-data safety)
  - 22 in `test_attention_digest.py` (classifiers, composer, orchestrator, sprint-dir fallback, recipients posting)
  - 3 in `test_slack.py` (post_dm helper)
  - 4 in `test_cli_attention_digest.py` (CLI subcommand)
  - 7 in `test_config.py` (attention_digest block + type validation)

## v0.11.0 — 2026-05-26

### Changed — Quick Resume becomes engine-managed (Lever 5)

**Structural change — tenants must opt in by bumping their `engine` pin AND accepting that auto-ingest will overwrite hand-written `**Current work:**` content over time.** Project `cp.md`'s `## Quick Resume` block becomes a fully engine-managed region wrapped in `<!-- cp-engine:start quick-resume -->` markers. Auto-ingest writes `**Current work:**`, `**Next up:**`, and `**Blockers:**` lines via three new scalar verbs on every meeting that touches the project. LLM is the source of truth.

**Trigger:** pipeline projects (Deal-stage) had blank `One-line summary` cells in master-cp because their template-placeholder `**Current work:**` lines never got hand-filled. Auto-managing the field fixes this class of staleness durably — pipeline projects never show empty cells once their first meeting fires; active projects' summaries stay fresh week-by-week instead of going stale between hand-edit passes.

**Engine changes:**

- `sync._ensure_quick_resume_markers` wraps the existing `## Quick Resume` section in engine markers on the cutover sync. Idempotent — runs once per project after the upgrade, then no-ops forever. Pre-existing hand-written content gets wrapped as-is; next auto-ingest will overwrite individual lines.
- `project-cp.md.j2` template scaffolds new projects with markers from the start.
- Three new ingest verbs (`current_work`, `next_up`, `blockers`) are **scalar** (single string per project, not list of dicts). `null` value means LLM declined to refresh — leave prior line alone.
- `ingest.execute_plan` branches on verb shape: scalar QR verbs write to project `cp.md` via `_write_quick_resume_verb`; existing list-typed verbs continue writing to sprint files. Both types appear in the same plan; both files appear in `files_written` on the same `[auto-ingest]` commit.
- `_resolve_project_cp_path` locates the project `cp.md` by code under any scope (handles 1p account-nested + FPSF/Canonic flat) without importing sync (avoids circular dependency).
- `plan_from_transcript` prompt templates (engagement + initiative) gain a Quick Resume schema block. The LLM evolves the prior `**Current work:**` / `**Next up:**` / `**Blockers:**` values with new signal from the meeting, returning `null` when the prior still describes state.

**Test surface:** 13 new tests (5 for sync marker wrapping, 1 for fresh scaffold, 7 for QR verbs). 481 passing total. No regressions.

### How tenants upgrade

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.10` to `~= 0.11`.
2. Run `cp sync` once. The cutover migration wraps `## Quick Resume` in markers across all project `cp.md` files (content unchanged on this pass).
3. Single commit: `[cutover] Quick Resume wrapped in engine-managed markers (v0.11.0)`.
4. Next auto-ingest fires per project → that project's Quick Resume gets first LLM-composed values. Over ~2 weeks of normal meeting flow, every active project transitions to LLM-composed Quick Resume content.

**Existing hand-written Quick Resume content WILL be overwritten** by the LLM's version on next auto-ingest. This is by design — Quick Resume is now machine-managed. Git history preserves prior hand-written versions; `git blame` recovers any specific line.

**Tenants who don't want auto-managed Quick Resume:** stay on `~= 0.10`. Auto-ingest still writes sprint files only; project `cp.md` Quick Resume stays hand-territory. Master-cp's One-line summary column still derives from hand-written `**Current work:**` lines.

**Rollback:** revert the tenant cutover commit + bump pin back to `~= 0.10`. Git history preserves the prior Quick Resume content.

Design: `cp/docs/plans/2026-05-26-quick-resume-engine-managed-design.md`.

## v0.10.1 — 2026-05-26

### Added — Sprint planning prep Step 0: empty-summary sweep

`CLAUDE.md.j2` template's Sprint planning prep playbook gets a new
Step 0: scan master-cp's active tables for empty `One-line summary`
cells and fill the corresponding project's `**Current work:**` line
before continuing prep. Runs FIRST so the rest of the prep walks a
fully-populated master-cp.

Triggered by a recurring pattern: pipeline projects (Deal stage) get
scaffolded with a template-placeholder `**Current work:**` line
(`_<what's in flight right now>_`) that `_derive_summary` correctly
reads as empty, leaving the master-cp summary cell blank until
someone hand-fills the line. The placeholder pattern means new
pipeline projects can sit summary-less indefinitely.

This is a process fix (CLAUDE.md guidance only) — engine logic
unchanged. A future v0.10.x can layer in a sync-side fallback
(synthesize `<Stage> — <Account> pipeline; owner <Name>` when the
template placeholder is present) so pipeline projects never show
empty cells even before sprint planning runs. Filed as backlog.

### How tenants upgrade

`~= 0.10` pins pick this up automatically (patch bump). Next `cp sync`
re-generates `CLAUDE.md` with the new playbook step.

## v0.10.0 — 2026-05-26

### Changed — sprint week numbering cuts over from Python `%W` to ISO 8601 (#7)

**Structural change — tenants must opt in by bumping their `engine` pin AND renaming `sprints/<W##>/` dirs.** Pre-v0.10.0 cp-engine used Python's `%W` for sprint week labels. For all of 2026, `%W` produces ISO_week − 1 — a one-week mismatch with calendars, Slack, Google Calendar, MC-2's financials surfaces, and any other tool a tenant operator opens. The discrepancy bit Drew on 2026-05-26 during sprint planning prep (mentally thinking "this is W22" while cp wrote into `sprints/2026-W21/`).

Internal contradictions already existed: `cli.py:_parse_week_iso` used `fromisocalendar` (ISO), `sprints.current_sprint_week_iso` used `%W`. So `cp slack-digest --week 2026-W22` would fetch ISO W22 but try to land it in `sprints/2026-W21/`. This release unifies on ISO 8601 throughout.

**Engine changes:**

- `sprints.current_sprint_week_iso` + `prior_sprint_week_iso`: `monday.strftime('%W')` → `monday.isocalendar()`. Uses ISO year (`isocalendar().year`) for correct Jan-boundary handling — Jan 1 2027 belongs to ISO `2026-W53` because that Friday is in 2026's last week per ISO.
- `aggregators._carry_forward_rollup`: same `%W` → `isocalendar()` fix for the horizon-decision-window math (was using a stale week number that disagreed with the file labels).
- Module docstrings in `sprints.py` + `ingest.py` updated to note the cutover.
- Anchor logic UNCHANGED: Mon/Tue → "this Monday's week", Wed-Sun → "next Monday's week". Only the number written into the label changes.

**Test surface:** 3 new tests in `test_sprints.py` (planning-anchor under ISO + new year-boundary coverage). ~100 hardcoded `2026-W##` test fixtures bumped +1. **468 passing total**, no regressions.

### How tenants upgrade

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.9` to `~= 0.10`.
2. Rename `sprints/2026-W##/` dirs +1 (since for 2026 every `%W` week = ISO − 1):
   ```bash
   for old in sprints/2026-W*; do
     [ -d "$old" ] || continue
     num=$(basename "$old" | sed 's/2026-W//')
     new=$(printf "W%02d" $((10#$num + 1)))
     git mv "$old" "sprints/2026-$new"
   done
   ```
3. Hand-fix cross-references in `weekly-cp.md` and any other handwritten files that mention `[W##]` or `2026-W##`. Engine-managed regions auto-refresh on next sync.
4. Run `cp sync` to verify; commit; push.

MC-2's `toWeekIso` flips to ISO concurrently (companion PR). Database values written pre-cutover stay as-is — they describe the week they were written for under the convention of that time. Cutover date for any audit: **2026-05-26**.

Design: `cp/docs/plans/2026-05-26-iso-week-cutover-design.md`.

## v0.9.1 — 2026-05-23

### Fixed — `link-local` and `capture-session` for multi-repo engagements (#5, #6)

`discover_cp_working_dirs` only globbed `_repo.md` (singular), so working dirs for multi-repo engagements (which have one `_repo-<name>.md` per linked repo, no singular file) were invisible to both `cp link-local` and `cp capture-session`. Captures for those source repos fell through to `cp/exceptions/` even though the repos WERE tracked in MC-2.

Concrete on the live tenant: 5 captures for `ggl-5136-events-calendar` had piled up in `cp/exceptions/` over ~2 weeks (2026-05-11 → 2026-05-23). Same shape affects any multi-repo engagement and `mc-2` / `market-leadership-scorecard` (linked from initiative dirs).

**Fix 1 (commit `c46f3a8`):** glob `_repo*.md` with a name-check guard so accidental matches like `_repository.md` are filtered. Each `_repo-<name>.md` yields its own `CpWorkingDir` entry pointing at the engagement dir; two source repos sharing one engagement dir each get a `.cp-link` targeting that shared dir.

**Fix 2 (commit `dbd0ba4`):** surfaced by Fix 1 — repos that have BOTH a standalone working dir AND appear as an initiative-linked `_repo-<name>.md` reference (e.g. `cp-engine` standalone in `firstpersonsf/cp-engine/` AND linked from Mission Control) yielded two `CpWorkingDir` entries with different paths; the prior dict-by-name builder silently picked whichever came last in rglob order — non-deterministic. Two-pass dedup now prefers the singular `_repo.md` (canonical standalone working dir) over `_repo-<name>.md` (pointer/reference), per CLAUDE.md design. Falls back to the linked-only entry when no standalone exists (`mc-2`'s case). Tie-breaking for unlikely edges uses sorted-path order so results are deterministic.

Live tenant before this release: ambiguous/missing entries, captures landing in `exceptions/`. After: 10 working dirs, each repo with exactly one canonical home, deterministic across runs.

Test coverage: 8 new tests in `tests/test_link_local.py`. **467 passing total (was 459 in v0.9.0)**, no regressions.

### How tenants upgrade

`~= 0.9` pins pick this up automatically (patch bump). No tenant action needed; no migration. The next `cp sync` / `cp capture-session` runs use the fixed discovery.

To verify the fix on a multi-repo tenant: `cp link-local` should now wire `.cp-link` files for repos whose engagement dir uses the `_repo-<name>.md` form, and `cp capture-session` from those source repos should write into the project's working dir rather than `cp/exceptions/`.

## v0.9.0 — 2026-05-22

### Changed — `1p/` scope is now per-account-nested (`1p/<company>/<project>/`)

**Structural change — tenants must opt in by bumping their `engine` pin and running `cp migrate-accounts`.** Triggered by the lack of any right home for cross-project account content (account-wide stakeholders, account-level decisions, multi-project client messages). The flat `1p/<project>/` layout meant a Janet email spanning ibx-5153 + ibx-5167 had to be shoved into one of the two project dirs; there was no surface for it.

After this release, each 1P account gets its own `cp.md` plus a `messages/` sibling for cross-project comms. FPSF and Canonic scopes are unchanged — they already nest by self-company at the scope level.

Engine support:

- New `company_slug(company_name)` and `account_scope_for(project)` in `state.py` — the single source of truth for the per-account layer. Client projects' working dirs land under `1p/<slug>/<dir_slug>/`; self-company projects (FPSF/Canonic) pass through unchanged. `_project_parent_dirs(scope)` yields the per-account subdirs that sync iterates for the deactivation sweep. Per-project inactive bin sits at `1p/<company>/inactive/`.
- New `account-cp.md.j2` template: anchor block, two engine-managed regions (`account-facts`, `projects`), and a handwritten skeleton for Quick Resume / Stakeholders / Account-wide decisions / Risks / Active research. Mirrors the project-cp.md hand-vs-engine discipline.
- New `render_account_cp`, `render_account_facts_body`, `render_account_projects_body` in `render.py`. Sync scaffolds each account's `cp.md` on first sync and re-splices the two engine regions on every subsequent sync; first-scaffold immediately re-splices itself to guarantee byte-equivalence with subsequent syncs (otherwise small whitespace differences between the template's `{{ block }}` spacing and the splicer's strip-and-rejoin made every next sync re-write the file, breaking the no-op promise).
- Master-cp `active-pipeline` + `active-1p` tables gain a leading **Account** column linking to each account's cp.md. Active engagements sort by `(account_slug, code)` so projects from the same account cluster (pipeline keeps stage-progression order). `_project_view` surfaces `account_display` + `account_link` for client projects; both are `None` for non-clients so the template renders an empty cell if it ever lands there.

Migration command:

- New `cp migrate-accounts [--dry-run]` CLI. Reads MC-2 once to build a `code → (slug, display)` map, plans `git mv` for every flat `1p/<dir>/`, hard-fails before any disk writes if any dir's code can't be resolved (no silent surprises, no junk-drawer `1p/unknown/`), executes the moves, absorbs `1p/_teleflex.md` (if present) into `1p/teleflex/cp.md` under a dated `## Legacy notes (migrated YYYY-MM-DD)` heading, runs `cp sync` so account `cp.md` files scaffold, then surfaces residue hints (stale `1p/<code>` references in `weekly-cp.md`) for the operator to hand-fix.
- Idempotent: re-running on an already-migrated tree is a no-op. Account-dir detection uses a `<prefix>-<digits>(-<slug>)?` shape check — dirs that don't look like a project code are silently skipped (account dirs, stray notes), while dirs that do match but can't resolve still hard-fail.
- Doesn't auto-commit: operator reviews with `git status` / `git diff` and commits manually. Mirrors `migrate-projects-flat`'s discipline.

Test coverage: 459 passing total (32 new for Phase 1 + Phase 2). The 20 existing path-assertion tests that broke when paths gained the per-account layer were fixed mechanically.

Live migration ran on the first cp tenant 2026-05-22 — 23 dirs into 7 account directories plus a hand-pre-positioned hexagon/. Idempotent `cp sync` post-migration reports `No changes (37 projects checked).`

Designs: `cp/docs/plans/2026-05-22-account-restructure-design.md` (engine support) and `cp/docs/plans/2026-05-22-migrate-accounts-design.md` (the migration command).

### How tenants upgrade

1. Bump `.cp-engine.toml`'s `[engine].version` pin from `~= 0.8` to `~= 0.9`.
2. `cp migrate-accounts --dry-run` to preview the moves and residue.
3. Resolve any unresolved dirs surfaced by the dry-run (fix MC-2 or hand-move the dir).
4. Run `cp migrate-accounts` for real in a quiet window (disable any sync/slack-digest crons that would race; don't tag Fathom meetings during the window).
5. Review with `git status` / `git diff`, hand-fix the residue list, commit.

## v0.8.16.6 — 2026-05-22

### Added — meeting count in the sprint-facts region

The deeper-transcripts pipeline writes per-meeting artifacts into each project's `meetings/` dir, but nothing pointed at them — the design deliberately omitted a central index, which left the folder easy to forget. This adds a discovery signal in the sprint file's `sprint-facts` region: a `Meetings` row showing how many meeting artifacts are dated within the sprint window, linking to the project's `meetings/` dir.

The row is sprint-scoped (matching the other `sprint-facts` rows) and emitted only when the count is greater than zero, so projects with no meetings get no dead link. It lives inside the `sprint-facts` region, which sync already re-splices every run, so it self-updates weekly with no new sync wiring.

- `count_sprint_meetings()`: pure helper counting `<YYYY-MM-DD>-<slug>.md` artifacts whose filename date falls in a sprint window. Filename-only — no file reads, no network; ignores `.txt` siblings and stray `.md`.
- `render_sprint_scaffold()`: new `meetings_this_sprint` param + relative link to the project's `meetings/` dir.
- `sprint-cp.md.j2` + `initiative-sprint.md.j2`: conditional `Meetings` row.
- `ensure_sprint_file()`: resolves the project's `meetings/` dir from `sprint_root.parent` and passes the windowed count through.

## v0.8.16.5 — 2026-05-19

### Added — `storyos-mc` sprint planning scope (Phase D.7)

Drew + Tony's standing weekly Canonic + Mission Control product/engineering sync was a mixed-scope meeting (StoryOS is Canonic; Mission Control is FPSF). The Phase D.5 sprint-planning scopes were per-org (`1p` / `fpsf` / `canonic`), so mixed-scope meetings either fanned too broadly (FPSF would hit all 5 initiatives) or had to fall back to a multi-tag work-session that lost the tenant-wide summary.

Add a new named scope `storyos-mc` that maps to a fixed pair of initiative codes via the new `_SCOPE_TO_EXPLICIT_CODES` mapping. `list_active_for_scope` now branches on scope shape: kind-based scopes (`1p`/`fpsf`/`canonic`) iterate `read_projects` and filter by `company_kind`; explicit-code scopes iterate the listed codes and look them up directly. Future named-pair scopes (e.g. when another standing combined meeting emerges) need only one line in `_SCOPE_TO_EXPLICIT_CODES` + `_SCOPE_TO_PSEUDO_COMPANY` + `_SCOPE_LABEL`.

Matching changes:
- MC-2 migration: `fathom_meetings.sprint_planning_scope` CHECK constraint adds `'storyos-mc'`.
- fathom-meeting-sync `VALID_SCOPES` includes `'storyos-mc'`; dashboard adds a fourth Sprint Planning button + chip labelMap entry.

## v0.8.16.4 — 2026-05-19

### Fixed — slack-rollup region wasn't being spliced into existing master-cp.md

v0.8.16.3 added the `slack-rollup` region to `_MASTER_REGIONS` + the first-pass exclusion list, but the second-pass `_write_if_changed` call hardcoded `splice_regions=("agenda", "sprint-facts-strip")` — so `slack-rollup` was never offered to the splicer. Result: on existing tenants the region's markers never appeared in master-cp.md and the rollup content never landed. Fixed by adding `"slack-rollup"` to the second-pass tuple. Schema-evolution rewrite path now triggers correctly when the existing master-cp.md is missing the marker.

## v0.8.16.3 — 2026-05-19

### Added — Phase D.6: Slack digest visibility in cross-cutting surfaces

The weekly Slack digest cron lands `### Slack digest` bullets in each project's current sprint file. Until now, those bullets were only visible inside each individual sprint file — sprint planning prep couldn't see them without opening every file.

- **New engine-managed region `## Recent Slack activity` in master-cp.md.** Reads the most recent `[<W##> · Slack]` bullet from each active project's current sprint file and surfaces them in a fold-out `<details>` block. Sorted by scope + project code. Hidden entirely when no project has a digest this week (quiet sprint, cron hasn't run, etc.).
- **New helper `cp_engine.render._compute_slack_rollup(tenant_root, projects, current_sprint_iso)`** does the extraction via regex (`_SLACK_DIGEST_RE`). Takes the last-matched bullet per file to be defensive against duplicate digest runs. Returns None when nothing matches.
- **Sync wires the new region into the post-sprint-files second pass** (alongside `agenda` and `sprint-facts-strip`) since it reads sprint file content.

### CLAUDE.md template — sprint planning prep section

New `## Sprint planning prep` section in Part 2 documents the prep workflow durably: read open asks, decisions, risks, **most recent Slack digest**, cross-cutting items from weekly-cp.md, and last-week allocations across all active items in the target scope. Then tag the Fathom recording for auto-ingest after the meeting runs.

4 new tests cover `_compute_slack_rollup`'s extraction, the "no projects have digests" case, missing sprint dirs, and the last-bullet-wins behavior for duplicate weeks.

## v0.8.16.2 — 2026-05-18

### Fixed — three coupled bugs surfaced by 1P sprint planning

Live failure path: tagging the 2026-05-18 1P Weekly Scrum as `sprint_planning_scope='1p'` ran `generate_sprint_planning_plan` against 20 active engagements; the response blew past the 4096-token cap and got truncated mid-output. Three fixes:

- **`_call_claude` max_tokens bumped to 16384.** The 4k ceiling was tuned for single-project plans; multi-project (account meetings, sprint planning) routinely needs 8k+. Anthropic bills on actual output, not max — no cost penalty for the headroom.
- **`_call_claude` raises a specific `PlanGenerationError` when `stop_reason == 'max_tokens'`.** Before, truncation cascaded silently into a YAML parse error downstream. Now the caller sees "response truncated at max_tokens — try a larger max_tokens budget or reduce prompt scope."
- **`_extract_yaml` recovers from truncated responses.** When the opening ` ```yaml ` fence is present but the closing fence isn't (truncation, model forgot, etc.), strip the opening fence and return the partial — let YAML parsing decide what to do with it, rather than dumping backticks into yaml.safe_load and getting a column-1 parse error.
- **`webhook._log_run_to_supabase` accepts a new `top_level_errors` parameter.** The two `status="failed"` paths (account / sprint-planning plan generation) now forward the AccountPlanError message into `auto_ingest_runs.errors` so the dashboard observability panel can show what actually went wrong. Before, `ingested=[]` failures landed with `errors=null` — opaque without Railway logs.

4 new tests cover the YAML extractor's three cases (complete fence, opening-only truncation, no fence, no-language-tag fence).

## v0.8.16.1 — 2026-05-15

### Changed — CLAUDE.md template restructured + updated for Phase D

The session-protocol doc was written pre-initiatives and pre-Phase-D. Several sections were stale (`is_internal` framing, working-tree example missing initiative dirs, no mention of account meetings, sprint planning, account summaries, or the auto-ingest pipeline at all).

Restructured into two clean parts:
- **Part 1 — What's in the tree.** Three kinds of work (engagements, initiatives, standalone repos), working tree layout (now showing initiatives + linked repos explicitly), the four meeting-assignment shapes (single project, account meeting, sprint planning, untagged), tenant-wide surfaces in `weekly-cp.md`, engagement-vs-initiative sprint file shape, local-link traversal.
- **Part 2 — How to read and edit it.** Reading modes, mode-switching, gatekeeper rule, trigger phrases, reference style (now with initiative + repo variants — "Updates on **Mission Control**?" not "**mission-control Mission Control**"), hand-written vs. engine-managed sections, deepening from transcript, word-count discipline.

The new tree example shows real initiative dirs (mission-control, storyos, first-person-website, etc.) so the layout is concrete. Reference style explicitly covers the three kinds.

No behavior change. Tenant CLAUDE.md regenerates on the next `cp sync`.

## v0.8.16 — 2026-05-15

### Added — Phase D.5 tenant-scope sprint planning

Sister concept to Phase D.4 account meetings. Where account meetings are per-company, sprint planning is per-scope: 1P sprint planning touches every active client engagement; FPSF/Canonic sprint planning touches every active initiative for that self-company. The user picks ONE scope; the webhook fetches the appropriate active project list at ingest time.

- **New helper `list_active_for_scope(config, scope)`** in `cp_engine.plan_from_account_meeting`. Three scopes mapped via `_SCOPE_TO_KIND`: `'1p'` → all active client engagements; `'fpsf'` → all active self-fpsf initiatives; `'canonic'` → all active self-canonic initiatives. Sister to `list_active_for_company`.
- **New `generate_sprint_planning_plan()`** mirrors `generate_account_plan()` but uses a sprint-planning-framed prompt and stamps the `account_summary`'s `company` field with a pseudo-code (`1p-clients`, `fpsf-internal`, `canonic-internal`) so it's distinguishable from per-account summaries in `weekly-cp.md`'s `## Account summaries` section.
- Server-side defaults stamp `company` + `week` on every emitted `account_summary` and `account_decisions` item so the prompt doesn't have to (defensive injection mirroring the account flow).

### Phase D.5 webhook

`POST /api/auto-ingest-sprint-planning` lives in `webhook/main.py`. Same auth, same per-project commit pattern as `/api/auto-ingest-account`. One additional commit lands the sprint-planning summary to `weekly-cp.md`. Payload: `{meeting_id, scope, transcript_text}` — much smaller than the account variant since scope is a literal three-value enum.

## v0.8.15 — 2026-05-14

### Added — Phase D.4 account meetings

Implements the engine half of `cp/docs/plans/2026-05-14-account-meetings.md`. Account meetings (weekly client syncs touching N engagements, weekly internal-team syncs touching N initiatives) now have first-class support: the user picks ONE company in the dashboard; the webhook fetches the active project list at ingest time and routes content per-project via one Claude call.

- **New verb `record-account-summary`** lands a paragraph bullet per `(company, week)` in `weekly-cp.md`'s new `## Account summaries` section. Section auto-creates if missing. Hash key embeds the week so re-runs are idempotent. The narrative companion to the existing one-liner `account_decisions` flow.
- **New module `cp_engine.plan_from_account_meeting`** — `generate_account_plan()` takes the company + transcript + active project list and asks Claude to route per-project verbs in a single pass. Output is the existing `cp ingest` multi-project plan shape PLUS an `account_summary` block PLUS the existing `account_decisions` block. Server-side injection stamps `company` + `week` on every account_* item so the prompt doesn't have to.
- **New helper `list_active_for_company(config, company_code)`** returns active engagements for client companies and active initiatives for self-fpsf/self-canonic companies, in one call.
- **`_validate_plan` accepts the new `account_summary` block** (single dict or one-element list, mirroring how `account_decisions` is shaped).
- **5 new tests** for the section auto-create, append-to-existing, idempotency, per-week dedup, and required-field validation.

### Phase D.4 webhook

The matching `POST /api/auto-ingest-account` endpoint lives in `webhook/main.py`. Same auth (HMAC), same Supabase observability, same per-project commit pattern as `/api/auto-ingest`. One additional commit lands `## Account summaries` to `weekly-cp.md` after all per-project commits.

## v0.8.14.1 — 2026-05-14

### Added — Initiative-shaped prompts for auto-ingest (Phase 2)

`plan_from_transcript._build_prompt` now branches on whether the target project is an engagement (`<3-letter>-<digits>` code) or an initiative (slug code). Initiative prompts:

- Drop `inbound` and `stakeholders` from the schema — no client side, no net-new external contacts.
- Emphasize `decisions`, `risks`, and `asks` (team-to-team open loops) as the load-bearing verbs.
- Use "internal workstream" framing instead of "client engagement."

The engagement path is unchanged.

New public helper: `plan_from_transcript._is_engagement_code(code)`. Used to discriminate without a separate `source` parameter.

This makes the auto-ingest webhook (`cp-engine-webhook`) correctly handle initiative meetings: when the dashboard's project-assignment dropdown (Phase D.1) writes `project_tags: ["mission-control"]`, the trigger fires, the webhook calls `generate_plan(project_code="mission-control")`, and the resulting plan is initiative-shaped.

## v0.8.14 — 2026-05-14

### Added — Internal initiatives as a first-class workstream

Implements Phase 1 of `docs/plans/2026-05-14-internal-initiatives.md`. Initiatives are internal workstreams (Mission Control, StoryOS, First Person Website, First Person Sales, Market Scorecard, First Person Operations) that don't fit the engagement shape (no client, no budget) and don't fit the standalone-repo shape (an initiative may span 0, 1, or N repos). They sit alongside engagements and repos as a third top-level entity.

- **`EntrySource` extended** with `"initiative"`. A ProjectState with `source="initiative"` is read from the new MC-2 `initiatives` table.
- **`sync_mc2.read_projects()`** now returns three streams: engagements + standalone repos + initiatives. Standalone repos are repos with both `project_id IS NULL` AND `initiative_id IS NULL` — initiative-linked repos surface as `_repo-<name>.md` under their initiative's working dir, mirroring engagement-linked repos.
- **New initiative templates** (`initiative-cp.md.j2`, `initiative-sprint.md.j2`). Slimmer than engagement templates — drop "Client communication" / "Outbound" / "Inbound" / "Stakeholders" (no client side), keep "Open asks" + "Slack digest" + "Decisions" + "Risks" + the standard sprint/notes structure. `render_project_cp` and `sprints.render_sprint_file` branch on `source == "initiative"` to pick the right template.
- **`master-cp.md` rollup** gains two new managed regions: `active-fpsf-initiatives` and `active-canonic-initiatives`. Initiatives appear in sibling tables below their scope's existing engagement/repo tables, with initiative-shaped columns (Code / Initiative / Owner / Status / Last touched / One-line summary / CP).
- **`cp slack-channels` + `cp slack-digest`** discriminate on the new `ChannelMapRow.kind` field (`"engagement"` vs `"initiative"`). The active-filter accepts `"Active"` (initiative status) alongside `"Deal" | "Open"` (engagement status). Initiatives with `slack_channel_ids` get the same weekly-digest treatment.
- **`cp list-active-projects`** surfaces initiatives as `source: "initiative"` rows. Used by the Fathom dashboard in Phase 2.
- **New initiative status vocabulary** in `status.py`: `INITIATIVE_STATUSES = ("Active", "On hold", "Done", "Archived")`. Parallel to `MC_STATUSES` for engagements. `is_active_initiative_status()` returns True only for `"Active"`.

### MC-2 migration

Migration `add_initiatives_table` (applied 2026-05-14) creates the `initiatives` table, adds `repos.initiative_id` FK, and seeds six rows: Mission Control + Market Scorecard + First Person Website / Sales / Operations (FPSF) and StoryOS (Canonic). Four repos linked: `mc-2` + `cp-engine` → mission-control; `storyos` → storyos; `market-leadership-scorecard` → market-scorecard.

### Operational notes

- Repos with `initiative_id` set no longer appear as top-level standalone-repo working dirs. Existing standalone-repo dirs for `mc-2`, `cp-engine`, `market-leadership-scorecard`, `storyos` are auto-deactivated to `<scope>/inactive/` on first sync after this migration; their content has been preserved there. The canonical home is now `<scope>/<initiative-code>/_repo-<name>.md`.
- The `canonic/storyos/` directory has a naming collision (initiative slug `storyos` matches the prior standalone-repo slug). The old `cp.md` and `_repo.md` remain alongside the new initiative-shaped scaffolding; clean up manually if desired.

## v0.8.13 — 2026-05-13

### Added — Multi-channel Slack digests

Projects can now have multiple Slack channels (e.g. a main channel + a `_team` internal channel). The digest pipeline fans out across all of them and produces one weekly bullet with one labeled paragraph per channel. Schema change on MC-2 (new `projects.slack_channel_ids` JSONB column) drives the fan-out; the legacy scalar `slack_channel_id` stays in place as a "primary channel" pointer for display purposes.

- **`fetch_channels(token, channel_ids, week_start)`** — new public function in `cp_engine.slack`. Fans out `fetch_week` across N channels, sharing one WebClient + user/channel-name caches across all calls. Returns a list of `FetchedChannel(channel_id, channel_name, messages)`. `fetch_week()` is now a thin single-channel wrapper.
- **`ChannelMapRow.channel_ids: tuple[str, ...]`** replaces the single `channel_id` field. The dataclass also exposes `primary_channel_id` + `primary_channel_name` for display; the digest pipeline does NOT special-case the primary.
- **`generate_slack_plan(channels=[...])`** takes a list of `FetchedChannel`s instead of a single channel + message list. The prompt describes each channel by name and asks Claude to emit one labeled paragraph per channel (e.g. `**#ibx_5167_ddi_platform_video**: <para>` followed by a blank line and `**#ibx_5167_ddi_platform_video_team**: <para>`) inside the single `slack_digest` entry's `text` field.
- **`cp slack-channels`** table format updated to show `#CH` (channel count) and a comma-separated list of channel IDs per project. JSON output now includes `channel_ids: [...]` + `primary_channel_id` + `primary_channel_name`.
- **`cp slack-fetch`** prints one `## Channel: <id> #<name>` section per channel for multi-channel projects.

### MC-2 migration

Migration `add_slack_channel_ids_array_to_projects` adds a `slack_channel_ids JSONB NOT NULL DEFAULT '[]'` column and backfills it from the existing scalar `slack_channel_id` (1-element array for each non-null row). The admin UI does not need to change immediately — the array is populated correctly by the migration, and additional channels can be added via SQL until the UI catches up.

### Breaking

- `ChannelMapRow.channel_id` removed; use `channel_ids[0]` or `primary_channel_id`. Internal API only — no external consumers.
- `generate_slack_plan` signature change: `channel_id` + `messages` → `channels: list[FetchedChannel]`. Internal API only.

## v0.8.12 — 2026-05-13

### Added — Weekly Slack digest pipeline

- **`cp slack-channels`** — debug command that lists every active engagement project alongside its MC-2 `slack_channel_id`, `enable_slack` flag, and `mc_status`. Used to spot projects that need a channel-id backfill before turning on the digest cron. Supports `--active-only` (Deal | Open) and `--format json`.
- **`cp slack-fetch --code <code> --week <YYYY-W##>`** — pulls one ISO week of top-level messages from a project's Slack channel. Filters bots, channel-join/leave/topic messages, and thread replies; resolves `<@U…>` mentions to display names. Caches user lookups for the run.
- **`cp slack-digest --week <YYYY-W##>`** — generates a `cp ingest` plan from one week of Slack chatter via Claude. Always emits a digest paragraph; emits `inbound`/`asks`/`decisions`/`risks` only when the chat content qualifies confidently. Multi-project mode (no `--code`) iterates every active project with `enable_slack=true` and a `slack_channel_id`, skipping channels with zero messages. With `--apply`, lands the plan via the existing `execute_plan` plumbing (idempotent via content-hash dedup).
- **`record-slack-digest` verb** in the ingest schema. Writes one bullet per `(project, week)` under `## Client communication / ### Slack digest`. Hash key embeds the week so the same digest text in two different weeks doesn't false-dedup; re-runs for the same week are no-ops. Subsection auto-creates if missing.
- **`execute_plan(week_iso=...)` override.** The Sunday cron runs in week N+1 but writes to week N's sprint files. The new optional parameter lets callers pin the target week explicitly instead of deriving it from `today`.
- **New runtime dependency: `slack-sdk>=3.27`.** Wraps the three Slack scopes the bot uses: `channels:history`, `channels:read`, `users:read`.

### Notes

- Slack credentials resolve from `os.environ` first, then `<mc-2 clone>/backend/.env` (same dotenv fallback pattern as `_load_supabase_creds`). The GitHub Actions cron in the cp tenant repo passes `SLACK_BOT_TOKEN` + `ANTHROPIC_API_KEY` as secrets.
- The sprint-file template now scaffolds the `### Slack digest` subsection alongside the existing Outbound/Open asks/Inbound/Stakeholders blocks.

## v0.8.11.1 — 2026-05-13

### Added

- **`TenantConfig.team`** field, populated from optional `[team]\nmembers = [...]` block in `.cp-engine.toml`. Used by `plan_from_transcript` so Claude doesn't auto-add internal team members as "new" project stakeholders during auto-ingest.
- **Account decisions in `plan_from_transcript` context.** Up to 25 most-recent decision titles from `weekly-cp.md` (account-level + cross-project) are surfaced in the prompt under "Recent account-level decisions." Prevents Claude from re-emitting account-wide commitments as project-level ones.

### Fixed

- **`cp ingest-from-transcript --show-prompt`** wasn't passing `config.team` to the prompt builder, so the team block always rendered as "(No team roster declared in tenant config.)" even when configured. The non-`--show-prompt` path was already correct.

## v0.8.11 — 2026-05-13

### Added — Phase C (auto-ingest) from cascade design doc

- **`cp ingest-from-transcript --project <code> --transcript <path>`** — new CLI command (Phase C.1). Generates a `cp ingest` plan from a transcript via Claude, prints YAML for review, or applies it with `--apply`. Requires `ANTHROPIC_API_KEY`. Used as the engine for the webhook service below; also useful standalone for "deepen this one transcript without typing the plan myself."
- **`webhook/` — cp-engine-webhook FastAPI service** (Phase C.2). HMAC-signed `POST /api/auto-ingest` endpoint that auto-ingests Fathom meetings into the cp tenant: HMAC verify → clone-on-each-request via SSH deploy key → Claude plan generation → `execute_plan` → commit `[auto-ingest] <code>: meeting <id-prefix>` → push. Deploys to Railway alongside `fathom-meeting-sync`. Per-call observability writes to the new `auto_ingest_runs` Supabase table (Phase C.4).
- **New runtime dependency: `anthropic>=0.40`.** Adds ~5 MB to the install footprint. The CLI never imports it unless `ingest-from-transcript` is invoked.

### Fixed

- **Anthropic error envelope.** `PlanGenerationError` now surfaces the actual SDK error body (including `BadRequestError` details like "credit balance too low") instead of the generic `Connection error`. Found via a real credit-balance issue during C.2 testing.

### Operational notes

- Production webhook URL: `https://cp-engine-production.up.railway.app`. See `webhook/README.md` for the deploy-key + Railway-env-var setup. Phase C.3 (the fathom-meeting-sync trigger) and Phase C.4 (the `auto_ingest_runs` observability table + dashboard view) live in the `fathom-meeting-sync` repo.

## v0.8.10.2 — 2026-05-13

### Fixed

- **`cp ingest --dry-run` summary now reports `account_decisions_count`.** Was silently absent from the JSON, making it impossible to confirm the plan parsed account-level decisions correctly.
- **Plugin manifests catch up to actual release version.** `plugin/plugin.json` and `.claude-plugin/marketplace.json` had been stuck at 0.8.2 since the last seven releases bypassed `scripts/release.py` and only bumped `pyproject.toml` + `__init__.py`. The `sync-cli-version.sh` SessionStart hook reads `plugin.json` as authoritative, so it was *downgrading* fresh installs back to 0.8.2 every session. Releases must use `scripts/release.py` going forward — never hand-bump version files.

## v0.8.10.1 — 2026-05-13

### Fixed

- **`cp fathom-fetch` JSON output now includes `meeting_type`.** Bug discovered immediately after v0.8.10 ship: the dataclass had `meeting_type`, the file header had `meeting_type`, but the CLI's JSON output dict was missing it. Plugin code that depends on the JSON to route by type couldn't see it. One-line fix.

## v0.8.10 — 2026-05-13

### Added — Phase B from cascade design doc

**Account-meeting ingest workflow.** Phase B of the meeting-type cascade design (`cp/docs/plans/2026-05-12-meeting-type-cascade.md`). Builds on Phase A's `meeting_type` field.

- **`/cp-ingest --account <company>` plugin mode.** New invocation that lists candidate `account-status` meetings for a company, lets the user pick one, fetches + classifies + plans + executes against it. Per-project plan template emphasizes per-project inbound/asks/decisions PLUS a new top-level `account_decisions` block for decisions that touch the whole client account but don't belong to any single project.
- **New `account_decisions` block in the plan YAML schema** — parallel to `themes` (tenant-wide content). Each item has `{text, company, date}` and lands as a numbered entry in `weekly-cp.md`'s handwritten Decisions list with the `(YYYY-MM-DD, source: account: <company-lower>)` suffix per Q5 from the cascade design doc.
- **`_write_account_decision()` internal function** — finds the highest existing decision number in `weekly-cp.md`, appends `<N+1>. **<text>** ...` before the first `cp-engine:start` marker (keeps the entry in the handwritten section, never inside engine-managed strip regions). Idempotent via content hash on `(company, "record-account-decision", text)`.
- **`cp list-active-projects --company <code>`** (case-insensitive) — filters by `company_code`. Used by `/cp-ingest --account` to scope active projects to the company's portfolio.
- **`cp fathom-list --type <meeting-type>`** — Click `--type` choice constrained to the 6-value taxonomy. Maps to `list_meetings(meeting_type=...)` which adds `.eq("meeting_type", ...)` to the Supabase query.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`.
- New plan-block: `account_decisions`. Existing plans without it work unchanged.
- `weekly-cp.md` decisions list grows when account-meeting ingests run. The cross-reference parser in `cp_engine.agenda.parse_weekly_decisions` already handles the `(date, source: <anything>)` suffix; `decisions_for_project` will pick up `source: account: <company>` entries automatically when projects of that company query for related decisions.

### Verification

- 8 new tests in `test_ingest.py` covering: account-decision write to weekly-cp.md, insertion before engine markers (handwritten section), idempotency, renumbering when no existing decisions, required-field validation (text + company), validation rejects non-list, valid alongside other plan blocks, errors when weekly-cp.md missing. Full suite: **388 passing**.

## v0.8.9 — 2026-05-12

### Added — Phase A from cascade design doc

**`meeting_type` field on Fathom meetings.** Phase A of the meeting-type cascade design (`cp/docs/plans/2026-05-12-meeting-type-cascade.md`). Foundation for Phases B (account-meeting ingest), C (project-meeting auto-ingest), and D (recurring-meeting cadence tracker).

- **Schema migration** (in fathom-meeting-sync repo): `migrations/02_meeting_type.sql` adds the `meeting_type TEXT NOT NULL DEFAULT 'untagged'` column to `fathom_meetings` with a CHECK constraint enforcing the 6-value taxonomy: `project-status`, `account-status`, `sprint-planning`, `work-session`, `1-1`, `untagged`. Indexed for filtered queries.
- **Classifier** (in fathom-meeting-sync repo): `extract-meeting-type.js` implements best-guess classification — title patterns first (sprint-planning + account-status are most distinctive), participant analysis second (2 FP-only → `1-1`; 3+ FP-only → `work-session`), project-tag fallback last (1 tag → `project-status`; 2+ → `untagged` for human review).
- **`FathomMeetingSummary` and `FathomMeetingFull` extended** with `meeting_type: str = "untagged"` field. `cp fathom-list` and `cp fathom-fetch` surface it in JSON output. `stage_transcript()` includes `# meeting_type:` in the staged file's metadata header.
- **No behavior change yet**: meeting_type drives auto-poll routing in Phase C and the cadence tracker in Phase D. v0.8.9 just makes the field available so downstream phases have something to read.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`.
- Requires the schema migration to be applied to the Supabase project (`mgheymslksfyhuvhmvmj`). Already applied during the Phase A session via the Supabase MCP.

### Verification

- 3 new tests in `test_fathom.py` covering meeting_type in stage_transcript header, FathomMeetingSummary.to_dict, and the default value. Full suite: **380 passing**.
- 27 new tests in fathom-meeting-sync's `__tests__/extract-meeting-type.test.js` covering title-pattern matching, participant-based classification, project-tag fallback, real W19 meeting titles, and the helper functions. Full fathom-meeting-sync suite: **67 passing**.
- Live verification of the Supabase migration: all 273 existing rows defaulted to `meeting_type='untagged'` as expected; CHECK constraint and index in place.

## v0.8.8.2 — 2026-05-12

### Added

- **`cp prep-agenda --summary`** — JSON output mode emitting structured metrics: week meta, themes/decisions counts, per-section coverage (Quick Resume / inbound / cross-referenced decisions), urgency counts, and `workload_by_owner` bucketed by *normalized* owner key. The `/cp-prep` plugin command consumes this for its highlights summary instead of parsing markdown.
- **`cp prep-agenda` auto-runs `cp sync` if master-cp's last sync is > 10 minutes old.** Avoids the "agenda shows yesterday's owner data" footgun. Opt out with `--no-sync` for environments where the network round-trip isn't acceptable. Threshold is `SYNC_STALENESS_THRESHOLD_MINUTES = 10` in `cp_engine.agenda`.
- New `normalize_owner()` helper collapses MC-2 owner-name variants ("Drew Fiero", "Drew", "Drew + Tony", "Drew and Tony", "Drew and Marcello" all → `"drew"`) for the workload summary. The rendered agenda preserves the literal MC-2 string for fidelity; only the bucketing key is normalized.
- New `master_cp_last_sync()` and `is_sync_stale()` helpers — read the `last-sync-timestamp` engine region in master-cp.md, compare against the threshold.

### Fixed

- **`cp prep-agenda` no longer warns "Skipping sprint file _agenda.md".** Earlier, the agenda generator's `_load_sprint_files` iterated `sprints/<W##>/*.md` and tried to parse `_agenda.md` itself as a sprint file (harmless warning, but noise). Fix: skip any `_`-prefixed file in the sprint folder, plus explicit blocklist for the three known sentinels (`README.md`, `_week.md`, `_agenda.md`).

### Verification

- 9 new tests in `test_agenda.py` covering: owner normalization (Drew variants → "drew", distinct first names stay distinct, empty/None handling), `master_cp_last_sync` parsing (missing file, real engine region), `is_sync_stale` (no master-cp → True; recent → False; > 1hr old → True; documented threshold matches constant). Full suite: **377 passing**.

## v0.8.8.1 — 2026-05-12

### Fixed

- **`cp prep-agenda` now strips `<!-- cp:hash=... -->` idempotency markers from rendered text.** Bug found in live verification: `/cp-ingest` writes bullets with trailing hash markers for re-run dedup; the v0.8.5 strip aggregators preserve those markers in the bullet text; the agenda renderer was passing them straight through to the rendered surface. Fix: new `_strip_hash_marker()` helper applied to themes, cross-cutting decisions, recent inbound, and open-asks bullets in the agenda. 1 new test in `test_agenda.py`. Total: 368 passing.

## v0.8.8 — 2026-05-12

### Added — Tier 2.4 from W19 retro: meeting-prep agent

**`cp prep-agenda` + `/cp-prep` plugin command.** Closes the loop the other direction from `/cp-ingest`: instead of capturing what happened in a meeting, prepares what should happen in one. Reads cp tenant state and assembles a project-grouped markdown agenda that bridges master-cp.md (too sparse) and per-project sprint files (too heavy). Per the W19 retro's Tier 2.4 motivation: would have prevented "ran out of time before Tony's projects" by surfacing per-owner workload up front.

- **`cp prep-agenda`** — engine verb. Default: full sprint planning agenda for all active projects. With `--projects ggl-5168,ibx-5167`: scoped agenda for the named projects only. With `--out <path>`: write to file (default stdout). Sources: master-cp project list + last-week allocations; weekly-cp.md decisions (parsed by `(YYYY-MM-DD, source: <code>)` regex); current-week sprint files (strip rollups + Horizon → Decisions due); per-project cp.md handwritten Quick Resume sections.
- **`/cp-prep` plugin command** — thin wrapper. Determines current planning week via cp-engine's v0.8.7.3 planning-week rule, generates the agenda to `sprints/<W##>/_agenda.md`, surfaces a brief summary (urgency flags, owner-workload callout) without dumping the full agenda inline.
- **Per-project block content (Option C from the design):** Quick Resume excerpt + recent inbound (last 3 from strip region) + open asks aged > 7d + decisions due this/next sprint (from sprint file Horizon section) + cross-referenced weekly-cp.md decisions matching the project code + stakeholders + discussion prompt (only when urgency signal exists).
- **Tenant-wide header:** themes (from `_week.md` `## Themes` sections, current + prior week) + cross-cutting decisions (from weekly-cp.md decisions-strip aggregator) + carry-forward (escalated risks, stale asks, decisions due — same data as master-cp.md `agenda` region).
- **New module:** `cp_engine.agenda` (~430 lines). Reuses existing `cp_engine.aggregators` (no duplicated logic).

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`. New verb is purely additive — no behavior change to existing commands.
- New optional file: `sprints/<W##>/_agenda.md`. Created only when `/cp-prep` runs. Idempotent (overwrites in place).
- Auto-commit deliberately not implemented — `/cp-prep` is a working artifact for the meeting; whether to commit is per-team preference.

### Verification

- 10 new tests in `test_agenda.py` covering: weekly-decision parsing (single source, multi-source, engine-marker truncation, empty input), `decisions_for_project` filtering (case-insensitive, multi-source matches), `extract_quick_resume` (real content, template-placeholder rejection, mixed content). Full suite: **367 passing**.
- Smoke-tested against the live cp tenant: parsed all 19 weekly-cp.md decisions; cross-references correctly match (#3+#5 → ggl-5136, #6+#7 → ggl-5168).

## v0.8.7.3 — 2026-05-12

### Changed — Tier 2.6 from W19 retro: sprint-week alignment with MC-2

- **`current_sprint_week_iso` now uses MC-2's planning-week rule.** Previously: always returned this calendar week's Monday (so Wed-Sun mid-sprint still labeled "this week"). Now matches MC-2's `planningWeekMonday()` in `frontend/src/components/sprint/WeekPicker.tsx`:
  - **Mon (0) + Tue (1)** → THIS week's Monday (e.g. Tue May 12 → W19)
  - **Wed (2) – Sun (6)** → NEXT week's Monday (e.g. Wed May 13 → W20)
- **Why:** the W19 retro flagged that "MC-2 shows W20, cp shows W19" mid-week. Drew's mental anchor is MC-2 — when partners open the planner mid-week, they're planning the *upcoming* sprint, not reviewing the current one. cp tenant labels now match that intent so `/cp-ingest` writes to the file representing "what we're planning right now."
- New helper `_planning_monday(now)` encapsulates the rule. `_monday_of()` retained as a pure utility (calendar-week Monday, no roll). `prior_sprint_week_iso` and `sprint_week_dates` both use the new anchor.

### Tenant impact

- **Behavior change visible mid-week.** On Wednesday morning, `cp sync` will now scaffold `sprints/<NEXT-week>/`, not refresh the previous folder. Sprint files written before the Wed-roll stay in their original week's folder (no data loss); aggregators read the last 4 weeks of sprint content so cross-sprint projections work normally.
- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`. Live tenant on this machine reinstalled at v0.8.7.3.

### Verification

- 3 new tests in `test_sprints.py` covering the full Mon-Sun cycle for `current_sprint_week_iso`, `prior_sprint_week_iso`, and `sprint_week_dates`. Pre-existing `test_sync.py` cases that hard-coded `datetime(2026, 5, 13)` (Wed) updated to use `datetime(2026, 5, 11)` (Mon) so the semantic intent ("plan this week") is preserved. Full suite: **357 passing**.

## v0.8.7.2 — 2026-05-12

### Changed

- **`cp fathom-fetch` filenames now use readable slugs.** Previously staged transcripts at `transcripts/incoming/<meeting-id>.txt` (UUID, unreadable in `ls`). Now stages as `<YYYY-MM-DD>-<slugified-title>.txt` (e.g. `2026-05-12-1p-weekly-scrum.txt`, `2026-05-12-sap-5174-vision-update-2026.txt`). On collision (common for repeated titles like "Impromptu Zoom Meeting"), appends `-2`, `-3` etc. Slug capped at 60 chars to keep filenames manageable across filesystems.
- Meeting id is preserved inside the file's metadata header — auto-poll's id-based idempotency in `.cp-engine/state.json` continues to work correctly even when filenames vary.

### Added

- 5 new tests covering: collision-suffix behavior across 3 same-title meetings, special-char slugging (`#sap_5174_vision_update` → `sap-5174-vision-update`), 60-char cap on long titles, missing-title fallback to "untitled". Full suite: **355 passing**.

## v0.8.7.1 — 2026-05-12

### Fixed

- **`cp fathom-fetch` now handles Supabase's JSONB transcript shape.** Bug discovered in live verification: the `fathom_meetings.transcript` column is a list of `{text, speaker, timestamp}` utterance dicts (not a flat string as initially assumed). v0.8.7 crashed with `TypeError: can only concatenate str (not "list") to str`. v0.8.7.1 introduces `_render_transcript_body()` which converts the JSONB list into the `MM:SS - Speaker / utterance text` format that `cp parse-transcript` expects. Defensive: still passes through string transcripts (legacy/future schema), returns `(no transcript)` placeholder for None/empty/bad shapes, skips garbage list entries silently. Strips leading `00:` from short timestamps so they match Fathom's typical export style. 4 new tests covering all four input shapes (Supabase JSONB list, string, empty, missing fields).

## v0.8.7 — 2026-05-12

### Added — Tier 1 Phase 1.3 (Fathom bridge)

**Third and final release of the v0.8.5/6/7 cluster from the [Tier 1 design doc](https://github.com/FirstPersonSF/cp/blob/main/docs/plans/2026-05-12-tier-1-design.md). With v0.8.7 shipped, the retro's full vision — "every transcript at every level keeps every artifact top of mind" — is operationally live for both file-sourced and Fathom-sourced transcripts.**

- **`cp fathom-list --since <ts>`** — emits JSON list of meetings from the `fathom_meetings` Supabase table (same project as MC-2; populated by the fathom-meeting-sync webhook). Default 50 rows, newest first. Reuses MC-2 credentials so no new auth surface.
- **`cp fathom-fetch <meeting-id>`** — pulls a single meeting's full transcript + metadata and stages it to `transcripts/incoming/<meeting-id>.txt` at the tenant root. Pass `--needs-review` to stage to `transcripts/needs-review/` instead. Includes a metadata header in the staged file (title, id, meeting_date, project_tags, duration).
- **`cp fathom-auto-poll`** — polls for new meetings since the last poll and stages them via the **confidence gate**: meetings with non-empty + non-`untagged` `project_tags` land in `transcripts/incoming/` for downstream `/cp-ingest`; meetings with no real tags land in `transcripts/needs-review/` for manual handling. Idempotent — `.cp-engine/state.json` tracks `last_polled_at` + `processed_ids`. `--dry-run` reports without writing.
- **`/cp-ingest --fathom <meeting-id>`** plugin mode — when the user passes `--fathom <id>` instead of a file path, the plugin runs `cp fathom-fetch` first to stage the transcript, then proceeds with the existing audit → classify → plan → confirm → execute flow.
- **`fathom-auto-poll.yml` GitHub Actions workflow** at `.github/workflows/` of the cp tenant. `workflow_dispatch:` only by default (cron is commented out as a starting point); flip to a cron schedule once you trust the auto-tagger + the confidence gate. Commits staged transcripts with `[fathom-ingest]` prefix matching the existing `[cp-sync]` convention.
- **`.gitignore` template gains `.cp-engine/`** entry so the per-tenant state file doesn't get committed.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install` or workflow run.
- New gitignored directory: `.cp-engine/` (holds `state.json`). Created on first auto-poll run.
- New gitignored directories (with content tracked when populated): `transcripts/incoming/` and `transcripts/needs-review/`. Created on first fetch.

### Verification

- 15 new tests in `test_fathom.py` covering: `has_good_tags` confidence-gate logic, state-file round-trip (load/save/round-trip; corrupt handling; preserves non-fathom top-level keys), `stage_transcript` (writes to correct dir, metadata header, handles empty transcript, needs-review flag). Full suite: **347 passing**.
- Live verification: see Step 9 of the v0.8.7 commit message. Real Fathom meeting fetched + ingested end-to-end against the cp tenant.

## v0.8.6.1 — 2026-05-12

### Fixed

- **`cp ingest` auto-creates missing sprint-file subsections.** Bug discovered during live verification: sprint files scaffolded before v0.8.5 (e.g. the existing W19 files) don't have `### Stakeholders` inside `## Client communication` — so `cp ingest`'s stakeholder write failed with `subsection '### Stakeholders' not found inside '## Client communication'`. v0.8.6.1 makes `_append_bullet_to_subsection` auto-insert a missing subsection at the end of its parent section before appending the bullet. Same bootstrap behavior as `_ensure_strip_markers` for project cp.md regions.

## v0.8.6 — 2026-05-12

### Added — Tier 1 Phase 1.1 (cp ingest verbs + /cp-ingest plugin)

**Per the [Tier 1 design](https://github.com/FirstPersonSF/cp/blob/main/docs/plans/2026-05-12-tier-1-design.md), this is the second of three releases in the v0.8.5/6/7 cluster. v0.8.5 added the engine-managed regions; v0.8.6 adds the verbs that write into them. v0.8.7 will bridge fathom-meeting-sync.**

- **`cp parse-transcript <path>`** — audits a Fathom-style transcript and emits JSON: speakers, duration, audio gaps (>= 2 min by default, configurable via `--gap-threshold`), action items, and mentioned project codes (via `--codes` flag). **Critical retro fix:** flags the kind of audio gaps that caused the W19 deepening's "Tony absent" misattribution.
- **`cp list-active-projects`** — emits JSON list of active projects from MC-2 for transcript classification: `code, name, company_code, company_name, owner, scope, source`. Filterable by `--scope`. Used by the plugin to give Claude the candidate-projects list during classification.
- **`cp ingest --plan <file>`** — validates a YAML plan against the schema and executes it atomically. Plan format documented in the design doc; supports 7 verbs (`record-inbound`, `record-ask`, `close-ask`, `add-decision`, `record-risk`, `record-stakeholder`, `record-theme`) plus shorthand names (`inbound`, `asks`, `decisions`, etc.). Idempotent — each appended bullet gets a trailing `<!-- cp:hash=<sha8> -->` content-hash comment; re-running the same plan is a no-op (counts as `skipped_duplicate`). `--dry-run` validates and reports without writing.
- **`cp write-region <file> <region>`** — escape-hatch verb that splices arbitrary content into an engine-managed region. Logs a warning so routine use is visible. For cases the structured verbs don't cover.
- **`/cp-ingest <transcript-path>` plugin command** — Claude-orchestrated workflow: audit → classify → plan → confirm → execute → log. Saves the executed plan to `sprints/<W##>/_ingest-log/<timestamp>.yaml` as an audit trail. Does **not** commit/push automatically — user reviews and commits.
- **New `cp_engine.ingest` module** — holds `parse_transcript`, `execute_plan`, and the seven internal write functions. Single source of truth for the verb behavior.
- **New dependency: `pyyaml>=6.0`** — for plan parsing.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next `uv tool install`. No tenant config changes.
- The v0.8.5 regions and templates are required precursors — `cp ingest` writes bracket-formatted bullets into the new subsections (`### Stakeholders`, etc.) and the v0.8.5 aggregators project them up into the strip regions on next sync.
- Workflow change: instead of hand-writing 13 sprint files for a sprint planning, you can now run `/cp-ingest sprints/2026-W19/<transcript>.txt`. The protocol-defined trigger phrase `deepen from transcript` is now backed by deterministic engine verbs instead of Claude improvising.

### Verification

- 16 new tests in `test_ingest.py` covering parse_transcript (speakers, gaps, action items, mentioned codes, dedup), plan validation (schema, unknown verbs, shorthand names), and execute_plan (write semantics, idempotency, cross-cutting flag, missing-sprint-file error handling, close-ask flip). Full suite: **332 passing**.

## v0.8.5.1 — 2026-05-12

### Fixed

- **HTML escaping in strip-region output.** `_PROJECT_STRIPS_TEMPLATE` and `_WEEKLY_STRIPS_TEMPLATE` (the ad-hoc Jinja templates used for the new engine-managed regions) were rendered via `env.from_string()`, which doesn't apply the env's filename-based `select_autoescape(disabled_extensions=("md", "j2"))` — so apostrophes and ampersands were rendered as `&#39;` and `&amp;` in cp tenant artifacts. v0.8.5.1 introduces a shared `_render_strip_template()` helper that constructs the Jinja `Template` with `autoescape=False` explicitly. Existing escaped output gets corrected on the next sync that produces a content change (no automatic rewrite of files that haven't changed otherwise).
- **Lenient bracket-bullet parsing.** The W19 hand-deepenings often wrapped brackets in backticks (`` - `[open · 2026-05-08 · Prosperous Health]` text ``). The pre-v0.8.5 `_BRACKET_RE` only matched bare brackets (`- [meta] text`), silently dropping backtick-wrapped entries — exactly the format-fragility risk the retro flagged. v0.8.5.1 updates the regex to tolerate optional backtick wrapping on either side of the bracket. **Side effect:** all bracket-formatted asks/risks/horizon/decisions from the W19 hand-deepenings now parse correctly and feed the new aggregators.

## v0.8.5 — 2026-05-12

### Added — Tier 1 Phase 1.2 (engine-managed regions for transcript ingest)

**Per the [Tier 1 design](https://github.com/FirstPersonSF/cp/blob/main/docs/plans/2026-05-12-tier-1-design.md), this is the first of three releases that together deliver the "every transcript at every level keeps every artifact top of mind" vision from the W19 retro. v0.8.5 adds the engine-managed regions that v0.8.6's `cp ingest` verbs will write to. Backward-compatible additions only — tenants pinned `~= 0.8` pick this up automatically.**

- **Four new engine-managed regions in per-project `cp.md`**: `inbound-strip`, `recent-decisions-strip`, `open-asks-strip`, `stakeholders-strip`. All four aggregate handwritten content from the project's sprint files into the project's durable view. Time windows: last 4 weeks for inbound + decisions; all-time for open asks (until closed) and stakeholders (deduplicated by name, most-recent role/context wins).
- **Three new engine-managed regions in `weekly-cp.md`**: `themes-strip`, `decisions-strip`, `carry-forward-strip`. Themes pulled from the new `sprints/<W##>/_week.md` (last 2 weeks); cross-cutting decisions from sprint files (last 4 weeks); carry-forward from open asks aged > 7 days, escalated risks, and decisions due in next +2 sprints. **`weekly-cp.md` is no longer "untouched by sync"** — it's now mostly handwritten with three engine-managed regions inside.
- **New file: `sprints/<W##>/_week.md`** scaffolded on first sync of each week. Holds week-scope handwritten content (themes, attendance, meta) that doesn't belong to any single project. Like `weekly-cp.md`, created once and never overwritten.
- **Sprint-file template gains `### Stakeholders` subsection** inside `## Client communication`. Old W18/W19 sprint files don't have this subsection — they just render empty stakeholder strips until new sprint files have content.
- **New `cp_engine.aggregators` module** with `aggregate_project_strips`, `aggregate_tenant_strips`, and `carry_forward_rollup`. The last is shared between `master-cp.md`'s existing `agenda` region and the new `weekly-cp.md` `carry-forward-strip` (refactor: `_compute_agenda_rollup` in `render.py` now delegates to the shared function).
- **New state types**: `Stakeholder`, `Theme`, `DecisionEntry` (with `cross_cutting: bool` flag). `SprintFile` extended with `stakeholders` and `decisions` fields.
- **New parsers**: `_parse_stakeholders`, `_parse_decisions` (bracket-formatted, distinct from the legacy numbered-list freeform decisions in `MeetingNotes.decisions`), `parse_themes_from_week_file`.
- **Bootstrap path for existing tenants**: sync injects the new strip markers into existing `weekly-cp.md` and project `cp.md` files (anchored before `## Active research` for weekly; after `tracked-issues` end marker for projects). Idempotent — already-present markers are skipped.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed.
- First sync after upgrading: every active project's `cp.md` gains four new strip regions (initially with placeholder content since no v0.8.6 verbs have written into source yet); `weekly-cp.md` gains three new strip regions; current week's sprint folder gains `_week.md`. Handwritten content in any of these files is preserved.
- Empty regions are normal until v0.8.6 ships — they show "_No X captured yet._" placeholders rather than real aggregated content. Existing W19 sprint files don't yet have bracket-formatted stakeholders/decisions/inbound, so the regions will look mostly empty until new transcripts get deepened.

### Verification

- 13 new tests in `test_sprints.py` (parsers for stakeholders, decisions, themes) and a new `test_aggregators.py` (project + tenant aggregation, dedup, time windows, horizon-target filtering). Full suite: **316 passing**.
- Existing `test_weekly_cp_is_pure_skeleton` updated to reflect the new shape (renamed to `test_weekly_cp_template_has_three_engine_regions`); behavior of master-cp.md `agenda` region preserved across the shared-aggregator refactor.

## v0.8.4 — 2026-05-11

### Added

- **Linked repos in engagement project working dirs.** When an MC-2 `repos` row points at an engagement project (`repos.project_id` is non-null), `cp sync` now writes a `_repo-<repo-name>.md` file into the engagement's working dir for each linked repo. Previously this design promise (called out in `state.py:134` and `sync_mc2.py:12`) had no implementation — the data sat in MC-2 but never surfaced anywhere. **Concrete win:** `cp/1p/ggl-5136-go-safety-website/` now picks up `_repo-ggl-5136-events-calendar.md` on next sync, surfacing Tony's OnFire calendar app with its GitHub URL and any matching `[local-repos.<user>]` clone paths. Closes [#3](https://github.com/FirstPersonSF/cp-engine/issues/3).
- New `LinkedRepo` dataclass on `cp_engine.state` (also re-exported from `cp_engine` and `cp_engine.sync`). New `render_linked_repo_md()` renderer. New `linked-repo.md.j2` template — distinct from `repo.md.j2` so the file's framing makes the *linked* relationship explicit ("Linked source repository" header + named parent engagement) rather than treating it as a primary working-dir record.
- `_engagement_row_to_state` now populates `ProjectState.linked_repos` from the new PostgREST embed (`repos!project_id(...)`). `_parse_linked_repos` filters Inactive repos and skips rows missing org/name.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed.
- Engagement working dirs that have linked repos in MC-2 will gain new `_repo-<name>.md` files. Engagement working dirs without linked repos are unchanged.

### Verification

- 12 new tests in `test_sync_mc2.py` (linked-repo parsing: happy path, Inactive filter, missing org/name, sort order, empty/invalid payload, default status, end-to-end via `_engagement_row_to_state`) and `test_render.py` (linked-repo template: GitHub link + engagement context, local-clone lines, no-clones fallback, no-description fallback). Full suite: 303 passing.

## v0.8.3 — 2026-05-11

### Fixed

- **`cp sync` now auto-loads `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` from `<mc-2 clone>/backend/.env`** when they're absent from the environment. Previously every fresh shell hit `Sync failed: MC-2 backend requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment.` and required `set -a; source .../mc-2/backend/.env; set +a` before each session. The clone path is read from `TenantConfig.local_repos["mc-2"]` (per-machine, gitignored — already required for `cp link-local` and `cp capture-session`), so no new config is needed. Env vars still take precedence, preserving the CI/GitHub Actions path. When the file fallback is used, `cp sync` prints a one-line note to stderr (`Loaded SUPABASE_* from /path/to/mc-2/backend/.env`) so the implicit dependency stays visible. Closes #2.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed, provided `[local-repos]."mc-2"` is set in `.cp-engine.local.toml` (it already is for anyone who's run `cp init` since v0.4).

### Verification

- New tests in `test_sync_mc2.py` cover the four cases: env present (no file read), env absent + .env present (loads from file, prints stderr note), neither source has the keys (raises `BackendUnavailable` with both paths named), and "no MC-2 clone configured" message variant. Full suite passing.

## v0.8.2 — 2026-05-11

### Fixed

- **Completes the v0.8.1 fix at the right layer.** v0.8.1 patched the orchestrator (`_is_active_for_sprint`) but `sync.py` was already pre-filtering the project list with `not is_internal and is_active_status(status)` before passing it down. That stripped FPSF/Canonic repos before the orchestrator could include them. v0.8.2 removes the upstream pre-filter and lets the orchestrator own the rule end-to-end. Verified on real tenant data: the next sync writes `mc-2.md`, `cp-engine.md`, `storyos.md`, etc. alongside the client engagement files.

### Verification

- Updated `test_sync_tenant_writes_sprint_files_for_active_projects` to cover six cases: active engagement (included), holding engagement (excluded), internal engagement (excluded), FPSF repo with status=Active (included), Canonic repo with status=Active (included), inactive repo (excluded). 287 tests passing.

## v0.8.1 — 2026-05-11

### Fixed

- **Sprint files are now generated for FPSF and Canonic projects.** The v0.8.0 orchestrator filtered the active set with `is_active_status` only, which understands MC-2's `Deal`/`Open` vocabulary but not the literal `Active` status used by repo-source projects (FPSF internal tooling, Canonic projects). The result on real tenant data: only client engagements got sprint files; internal projects were silently skipped. Fix mirrors `render.py`'s `is_active` rule — engagement → `is_active_status` + `not is_internal`; repo → `status == "Active"` — so the sprints directory now contains a file for every project the master CP also surfaces.

### Tenant impact

- Tenants pinned at `engine = "~= 0.8"` pick this up on next sync. No config changes needed.
- The next sync after upgrading will write sprint files for FPSF/Canonic projects that were previously missing. Expected to add ~5–7 new files per tenant on the next sync.

### Verification

- New regression test `test_ensure_sprint_files_includes_repo_source_active_projects` locks in the four cases: engagement (Open) → included; FPSF repo (Active) → included; Canonic repo (Active) → included; inactive repo → excluded. Full suite: 287 passing.

## v0.8.0 — 2026-05-10

### Added

- **Sprint files.** `cp sync` now writes a per-project sprint markdown file at `sprints/<YYYY-W##>/<project-code>.md` for every active, non-internal project. Each file scaffolds the partners' weekly review with both engine-managed sections (sprint facts, where-it-stands, carry-forward from the prior sprint) and hand-written sections (client communication, dependencies & risks, this sprint's plan, 4–8 week horizon, meeting notes). Hand-written content is preserved across re-syncs; only engine-managed regions refresh.
- **Master agenda + facts strip.** `master-cp.md` gains a top-of-page Agenda rollup (escalated risks, stale client asks > 7 days, horizon decisions due within 2 sprints) and a sprint-totals facts strip (total hours, per-person hours, active count, stale asks, escalated risks, decisions due, prior sprint). Both regions aggregate across all parsed sprint files.
- **Project CP "Current sprint" block.** Each project's `cp.md` gains a `current-sprint` engine-managed region linking to the active sprint file with a top-3 asks/risks summary. Legacy CPs without the marker get the markers seeded automatically.
- **Sprint column in master active tables.** Each active-project row in master CP now links to that project's sprint file alongside its CP link.
- **Auto-generated section summaries.** Each active section in master CP gets a one-line italic summary like _Three deals in flight_, _Four engagements in delivery_.
- **Sprint-index README.** `sprints/<YYYY-W##>/README.md` is engine-rendered each sync with a launch table: project · allocation · asks · risks · decisions due.
- **Mode 4 deepening contract.** `CLAUDE.md` documents that `deepen from transcript` writes meeting notes, decisions, new asks, outbound drafts, and risk updates into the sprint file (not project `cp.md`) when one exists for the current week.
- **`risk_categories` config.** Tenants can override the default risk-category vocabulary (`contract`, `pricing`, `people`, `technical`, `scope`, `timeline`) via a `[risk_categories]` block in `.cp-engine.toml`.
- **Per-project `contacts`.** `[[projects]]` blocks now accept a `contacts = [{name, role}, ...]` array surfaced in the sprint file's client-communication section.
- **`cp parse-sprint <path> [--json]`.** New CLI subcommand emits a parsed `SprintFile` as one-line summary or full JSON for downstream consumers (mc-2 integration, debugging, future tooling).

### Out of scope (deferred)

- mc-2 capacity overlay, web-rendered viewer, and any direct read path from mc-2 into the cp tenant repo. The sprint markdown is the source of truth; mc-2 integration is a separate exploration tracked in `docs/plans/2026-05-10-sprint-files-design.md`.
- Inline editing UI. Sprint files remain markdown-edited.
- Per-project sprint-hour budgets / project-level capacity.

### Tenant impact

- Pin `engine = "~= 0.8"` to pick up sprint-files generation.
- Existing `cp.md` files get a new `current-sprint` engine region inserted on next sync; hand-written content outside engine markers is preserved.
- Run a sync inside the sprint window (any day Mon–Sun of the upcoming sprint week) to populate `sprints/<current-week>/` for the first time. Re-syncs are idempotent and only refresh engine regions.

### Verification

- 286 tests passing across `tests/test_state.py`, `tests/test_sprints.py`, `tests/test_sync.py`, `tests/test_render.py`, `tests/test_config.py`, `tests/test_cli_parse_sprint.py`.
- Round-trip test: `render_sprint_scaffold` → `parse_sprint_file` confirms parser/template parity.
- Idempotency tests: re-running `ensure_sprint_file` on identical input produces identical bytes; sync no-op semantics preserved (engine regions only re-write when content changes).

See `docs/plans/2026-05-10-sprint-files-design.md` for design rationale and `docs/plans/2026-05-10-sprint-files-plan.md` for the phased implementation plan.

## v0.7.4 — 2026-05-09

### Fixed

- **Sprint allocation window now anchors on the upcoming sprint-planning Monday.** Previously, `_last_week_monday()` always returned the Monday of the calendar week BEFORE today's calendar week — meaning when sync ran on Saturday May 9, master-cp.md showed allocations for Apr 27 - May 3, even though Monday's sprint planning meeting (May 11) is meant to review the week of May 4 - May 10. Fix: anchor on the upcoming Monday (or today if today IS Monday) and look back 7 days. Window now matches what sprint planning will actually discuss.

### Behavior change

- **On Tue-Sun:** window is `[next_Monday - 7, next_Monday - 1]` (the week ending the day before Monday's sprint planning).
- **On Monday:** window is `[today - 7, today - 1]` (the week just ended, which is what the meeting reviews).
- **Before the fix:** all days returned the previous calendar week — too early by 7 days for Tue-Sat sprint prep.

### Verification

8 new tests in `test_sync.py` covering: Monday anchor, Tue-Sat anchor on next Monday, weekend prep, consistency Sat→Sun→Mon, post-meeting flip Mon→Tue, date-or-datetime input handling.

### Tenant impact

Runner picks up v0.7.4 on next sync via the existing `~= 0.3` pin. master-cp.md will start showing the corrected window on the next cron tick.

## v0.7.3 — 2026-05-09

### Changed

- **Content-only mode (`--working-dir`) now sweeps the entire working dir on commit.** Previously, `cp capture-session --working-dir <wd>` staged only the session file + `cp.md` — same narrow scope as source-repo mode. That left synthesis docs, transcripts, and hand-written notes the user added during the session uncommitted, which forced Claude to ask "should I commit X?" friction on every capture. Now the engine `git add`s everything trackable inside the working dir (filtered by `.gitignore`, so binaries and `.DS_Store` are auto-excluded).
- **Source-repo mode is unchanged.** The narrow scope (v0.4.3) still applies for source-code projects: only the session file + `cp.md` are committed, since the source repo is where hand-written work lives, not the cp working dir.
- **`/cp-summarize` slash command** explicitly tells Claude not to ask the user about untracked files in the working dir for Mode B — the engine has already decided. Just relay the CLI's "Also committed N other file(s)" output to the user as part of the summary.

### Added

- **`CaptureResult.extra_files_committed: tuple[Path, ...]`** — populated in content-only mode with the list of files swept up beyond the session file + cp.md. The CLI prints this as a one-line "Also committed N other file(s)" plus a relative-path list.
- **`cp_engine.capture_session._trackable_paths_under(working_dir, tenant_root)`** — internal helper that runs `git ls-files --others --exclude-standard --modified -- <working-dir>/` to enumerate files git would track if `git add`-ed. Filters by `.gitignore` for free.
- **4 new tests** for the sweep behavior: text content under the working dir gets committed, binaries are excluded by `.gitignore`, the sweep does NOT cross project boundaries (other projects' dirty state is left alone), and source-repo mode keeps its narrow scope (regression guard against accidentally broadening it).

### Why

For 1P engagements without a source code repo (`cp/1p/<engagement>/`), the working dir IS where all the work lives — synthesis docs, meeting transcripts, reference materials, the `cp.md` itself. Asking "should I commit X?" on every session is friction that makes no sense: the answer is always "yes, commit the text content, exclude the binaries." The `.gitignore` already enforces the binary exclusion; the new sweep just stops asking.

### Notes

- Smoke-tested against `cp/1p/ibx-5153-ai-campaign/` (real 1P engagement with `Reference Materials/` full of `.pptx` and `.docx` files plus one `.md`): the `.md` got swept up, the binaries didn't, the working dir is now clean.
- The behavior is scoped to one project's working dir at a time. Dirty state in other projects' working dirs is NOT pulled in (verified by test).

## v0.7.2 — 2026-05-09

### Added

- **`cp capture-session --working-dir <path>`** — new mode for **content-only projects** (1P engagements without a separate source code repo). Skips all source-repo + `.cp-link` resolution and writes the session summary directly to `<working-dir>/sessions/`. The cp tenant root is found by walking up for `.cp-engine.toml`, so `--cp-tenant` isn't needed. Mutually exclusive with `--source-repo`.
- **`cp_engine.capture_session.capture_session_in_working_dir`** — public Python API for the same. New `WorkingDirNotInTenant` exception when the supplied path isn't inside any cp tenant clone.
- **5 new tests** covering the content-only path: writes to `sessions/`, updates `cp.md`'s Last session line, rejects working dirs outside any tenant, rejects non-existent paths, and (end-to-end) commits exactly the session file + cp.md without sweeping unrelated dirty state into the commit.

### Changed

- **`/cp-summarize` slash command** auto-routes to the new mode. Detects three cases by comparing `pwd` to `git rev-parse --show-toplevel`:
  - **Mode A** — `pwd` is inside a source code repo (the common case). Original behavior; uses `--source-repo`.
  - **Mode B** — `pwd` is inside a cp tenant working dir (content-only project). New behavior; uses `--working-dir`.
  - **Mode C** — `pwd` is the cp tenant root itself. Tells the user to `cd` into a project dir.

### Why

Content-only projects (e.g. `1p/ibx-5153-ai-campaign/`) have no source code repo to anchor `.cp-link` against. The original `/cp-summarize` flow assumed every project has a code repo, so capturing a session from a content-only working dir required hand-driving the file write, the cp.md update, and the git commit. Now `/cp-summarize` Just Works regardless of project type.

### Notes

- Refactored the existing `capture_session()` to extract a shared `_write_to_working_dir()` helper. Behavior-preserving — both modes write the session file, update cp.md, and commit + push the same way.
- The `_session_commit_does_not_include_unrelated_uncommitted_state` discipline (v0.4.3) carries through unchanged: only the new session file and (if updated) the project's `cp.md` are staged.

## v0.7.1 — 2026-05-09

### Changed

- **`<scope>/archived/` renamed to `<scope>/inactive/`.** Projects often flip back to active (engagements paused and resumed, internal flag toggled, status changed) — "inactive" captures that better than "archived" (which suggests a one-way trip). The reactivation logic (already present since v0.4) is now visible in the directory name. Spread of changes:
  - `state.ARCHIVED_DIR_NAME` → `INACTIVE_DIR_NAME`; `archived_root()` → `inactive_root()`; `archived_dir()` → `inactive_dir()`.
  - `sync._archive_stale_cps` → `_deactivate_stale_cps`; `SyncResult.files_archived` → `files_deactivated`.
  - `cp sync` CLI output: "archived X" → "deactivated X".
  - `_repo.md` discovery in `link_local` skips both `inactive/` and the legacy `archived/` (so unmigrated tenants still work).
  - `cp migrate-projects-flat` now reads pre-v0.7 `<scope>/projects/archived/` as input and writes `<scope>/inactive/` as output (rename + flatten in one step).

### Cleared documentation debt

- **`actions/sync` GitHub Action** rewritten to mirror what `cp/.github/workflows/sync.yml` does inline: install `packaging`, run inline pin resolver, install cp-engine at the resolved tag, run `cp sync`, commit + push. The previous version had a hardcoded `pip install cp-engine` (no PyPI), referenced a `cp-sync` branch that doesn't exist, and had a `TODO: implement commit + push` left in. Now a future second tenant can `uses: FirstPersonSF/cp-engine/actions/sync@v0.7.1` instead of copy-pasting the inline resolver.
- **Reading-mode descriptions** in `cp_engine.modes` and the rendered `CLAUDE.md` updated from v0.2-era `projects/<code>.md` paths to v0.7's `<scope>/<dir_slug>/cp.md`. Mode 4's "every active project CP" glob is now `<scope>/*/cp.md` (so it naturally excludes `inactive/`).
- **Spec v02** had 11 stale `projects/<code>.md` references in the modes section, master-CP section, and architecture diagram. Bulk-rewrote to the v0.7 path shape. The v01 amendments and v02-architecture-change docs in `docs/specs/history/` are intentionally not touched (they're historical).

### Tenant migration

- **Existing tenants with no archived projects** (e.g. `cp` today): runner picks up v0.7.1 on the next sync via the existing `~= 0.3` pin; no manual action needed.
- **Existing tenants with archived projects in pre-v0.7 layout**: re-run `cp migrate-projects-flat`. The command now reads `<scope>/projects/archived/<dir>/` (legacy name) and writes `<scope>/inactive/<dir>/`.
- **Existing tenants in v0.7 layout with archived projects**: no automated path. Manual `git mv <scope>/archived <scope>/inactive` per scope. (Out of scope to automate for v0.7.1 — affects no current tenant.)

## v0.7.0 — 2026-05-09

### Changed

- **Working-tree layout drops the redundant `projects/` segment.** Project working dirs now live at `<tenant>/<scope>/<dir_slug>/` instead of `<tenant>/<scope>/projects/<dir_slug>/`. Archived projects move from `<scope>/projects/archived/<dir>/` to `<scope>/archived/<dir>/`. The old `projects/` segment was inherited from when the spec called for separate `cp-1p` / `cp-firstpersonsf` / `cp-canonic` tenants (where the inner `projects/` was the only child of the tenant root). With all three scopes consolidated into one `cp` tenant, the segment was empty container — every `firstpersonsf/projects/` had only `firstpersonsf/projects/cp-engine/` (etc.) inside it.
- **Path-construction is centralized in `cp_engine.state`.** New helpers `scope_root()`, `working_dir()`, `archived_root()`, `archived_dir()` are the single source of truth for working-tree paths. Sync, render, link-local, capture-session, and project-context all route through them.
- **`master-cp.md` table links** now point at `<scope>/<dir>/cp.md` instead of `<scope>/projects/<dir>/cp.md`. Re-rendered automatically on next `cp sync`.

### Added

- **`cp migrate-projects-flat`** — one-shot migration command. For each scope, `git mv`s every `<scope>/projects/<dir>` to `<scope>/<dir>` and `<scope>/projects/archived/` to `<scope>/archived/`. Removes the now-empty `projects/` parent. Rewrites `.cp-link` files in linked source repos so they point at the new paths. Idempotent: re-running on an already-migrated tree is a no-op. Refuses to run on a dirty working tree. Detects collisions (destination already exists) and aborts loudly rather than silently merging.
- **`cp_engine.migrate_flat`** module with full unit-test coverage (10 tests): clean-tree pre-flight, idempotency, single-scope move, multi-scope move, archive subdir handling, mixed already-migrated/not-yet-migrated state, collision detection (live + archive), git-history preservation under `git log --follow`.

### Tenant migration

Existing tenants must run `cp migrate-projects-flat` once to convert their working tree:

```sh
cd /path/to/cp-tenant
cp migrate-projects-flat
git status   # review the staged moves
git commit -m "v0.7 layout: drop projects/ segment from working dirs"
git push
```

The `[engine].version` constraint in `.cp-engine.toml` continues to admit v0.7 if it's `~= 0.3` (or any constraint that allows 0.7.x). The runner picks up v0.7 on next sync via `cp resolve-engine-pin` (added in v0.6); no workflow file edit needed.

### Why now

Single-user / single-tenant on this machine: cheap to migrate, easier to do before any second user adopts the system. The empty `projects/` directories were visible noise in every `ls`, and the path shape `firstpersonsf/projects/cp-engine/` made readers think there must be a sibling alongside `projects/` (there wasn't). v0.7 makes the layout match the conceptual model: scope dir → project dir, no intermediate.

## v0.6.0 — 2026-05-09

### Added

- **`scripts/release.py`** is the canonical way to cut a release. Reads the new version from the command line; bumps `pyproject.toml`, `src/cp_engine/__init__.py`, `plugin/plugin.json`, `.claude-plugin/marketplace.json` atomically; runs `pytest` and `python -m build`; commits, tags `v<X>`, and pushes. Pre-flight checks (clean working tree, on `main`, CHANGELOG section drafted, tag doesn't already exist) all pass before any file is touched. `--dry-run` runs the checks without modifying anything.
- **`SessionStart` hook in the plugin** auto-installs the matching `cp` CLI when the plugin and CLI versions drift. Runs on every Claude Code session in a project where the plugin is loaded; fast (~50ms) when versions match, runs `uv tool install --force --from git+...@v<plugin-version>` when they don't. Per spec v03: prints errors but never blocks session start — the next `/cp-summarize` will fail loud with `EngineVersionMismatch` if the install fails, which is the existing safety net.
- **`cp resolve-engine-pin`** CLI subcommand resolves a tenant's `[engine].version` constraint to the highest matching git tag on the engine repo. `--format` controls output (`tag`, `pip-spec`, `json`). Backed by a new `cp_engine.pin_resolver` module with full unit tests.
- **`cp_engine.pin_resolver`** module — `read_constraint`, `list_remote_tags`, `resolve`, `resolve_for_tenant`. Pure functions; no PyPI, no caching, no auth. Uses `git ls-remote --tags` to enumerate available versions.

### Changed

- **`src/cp_engine/__init__.py`'s `__version__`** is now bumped by the release script. Reading it from `importlib.metadata` was considered (single-source-of-truth via package metadata) but rejected because Python 3.14 silently returns `None` for missing/broken metadata, which would collide with `config.py`'s version-check logic.

### Tenant migration

- Tenants should update `.github/workflows/sync.yml` to install cp-engine via inline pin resolution instead of a hardcoded git tag. The `cp` tenant got this update as part of v0.6.0 release prep. Until updated, runners continue to install whatever tag the workflow file names — no breakage, just no longer self-updating.
- The plugin's new SessionStart hook activates on next `/plugin update cp-engine`. No action required; users get the auto-install behavior automatically once they've updated the plugin once.

### Why

Before v0.6: a release required manually bumping four version strings (pyproject, plugin.json, marketplace.json, CHANGELOG), tagging, asking every user to run two install commands (`/plugin update` AND `uv tool install --force`), and editing each tenant's `sync.yml`. Four places where versions could drift, no automated check that they hadn't. After v0.6: one script for release, one command (`/plugin update`) for users, automatic propagation to runners via the tenant pin. See `docs/specs/cp-engine-spec-v03-version-distribution.md` for the full reasoning.

## v0.5.2 — 2026-05-09

### Added

- **`/cp-context` slash command** is now functional (was a stub through v0.5.1). Run from inside a cp working dir, prints a 7-day timeline merging git commits from the linked source repo's local clone with session captures from the working dir's `sessions/` directory. Claude reads the timeline and synthesizes "what's been happening on this project?" without the user having to ask for raw output.
- **`cp project-context` CLI command** powers the slash command but is also useful standalone. Defaults to a 7-day window; `--days <N>` overrides. `--user <name>` picks a specific `[local-repos.<user>]` entry; without it the command picks the first user whose configured path exists on this machine (the natural "running on Drew's machine, find Drew's clone" heuristic).
- **`cp_engine.project_context`** module — `project_context(working_dir, user, days, now)` returns a `ContextResult` with `commits: tuple[CommitEntry, ...]` and `sessions: tuple[SessionEntry, ...]`. Pure-Python plumbing; the slash command's markdown is a thin wrapper.

### Notes

- When no local clone is reachable on this machine, `cp project-context` returns sessions only (commits empty) rather than erroring — gives the user *some* context even if the git history isn't available locally.
- One-liner extraction for session entries reuses the same `### What we did` heuristic that drives `cp.md` Last session line updates, so summaries stay consistent across surfaces.

## v0.5.1 — 2026-05-08

### Fixed

- **`cp capture-session` auto-recovers from push rejection.** Previously, when a `[cp-sync]` cron commit landed on origin between captures, `git push` was rejected as non-fast-forward and the command silently reported "(push skipped or failed)" while leaving the session commit only on the local clone. Now: detects the rejection, runs `git pull --rebase`, retries the push once. Real push failures (network, auth, hook rejection, rebase conflict) raise `PushFailed` with the underlying stderr.
- **CLI output distinguishes pushed / rebased+pushed / skipped / failed.** "(push skipped or failed)" was misleading because it conflated three different states. Now: pushed-and-clean → "Committed X and pushed."; rebased then pushed → "Committed X, rebased on top of upstream, and pushed."; commit=False or push=False → "Committed X (push skipped)."; failure → raises `PushFailed` (no longer reaches the success-output branch).

### Added

- **`CaptureResult.push_rebased: bool`** — True when the first push was rejected and the auto-rebase + retry succeeded. Lets callers distinguish a clean push from one that needed recovery.
- **`PushFailed` exception** — raised by `capture_session()` when push fails for a reason auto-rebase can't recover from. Includes the underlying git stderr in the message so the user knows what to fix.

## v0.5.0 — 2026-05-08

### Added

- **Committed multi-user `[local-repos.<user>]` schema in `.cp-engine.toml`.** Each tenant member declares their own per-machine clone paths in a section keyed by their name (`[local-repos.drew]`, `[local-repos.tony]`, etc.). The runner reads these (unlike the gitignored `.cp-engine.local.toml`'s `[local-repos]`), so the rendered `_repo.md` includes one `**Local clone (User):**` line per user who has the repo. Free-form user keys; no validation against a known users list.
- **`TenantConfig.local_repos_by_user`** — `Mapping[str, Mapping[str, str]]` exposing the new committed map. Outer key is user; inner is repo-name → path string. Paths are NOT resolved (they don't exist on the runner; they're for display).

### Changed

- **`render_repo_md` signature: `local_clone_path` → `local_clones_by_user`.** Old kwarg accepted a single `Path | None` for the current machine's clone path; new kwarg accepts a `dict[str, str] | None` mapping user → path. Sync now passes the per-user map for the current repo, derived from `config.local_repos_by_user`.
- **`_repo.md` template** renders one line per user: `**Local clone (Drew):** /...` then `**Local clone (Tony):** /...` etc., followed by `(per .cp-engine.toml [local-repos.<user>])`. The "for each user listed above..." prose replaces v0.4's single-clone wording.
- **CLAUDE.md template** updated to explain the two-tier model: committed `[local-repos.<user>]` for rendering and team-wide visibility, gitignored `[local-repos]` for `cp link-local` and `cp capture-session` self-healing.

### Why

v0.4's enrichment relied on the gitignored `[local-repos]`, which the GitHub Action runner can't see. The cron sync would always produce `_repo.md` files without local clone paths, overwriting any local-only enrichment on the next push. v0.5 separates "committed paths for display" from "per-machine paths for behavior" so both work.

### Migration

Existing tenants without `[local-repos.<user>]` sections render `_repo.md` in the v0.3.3 shape (no clone paths) — no breakage. To opt in, add a section per user to your tenant's `.cp-engine.toml` and run a sync.

## v0.4.3 — 2026-05-08

### Fixed

- **`[session]` commits no longer sweep up unrelated uncommitted state.** Previously `cp capture-session` ran `git add .` from the cp tenant root, which opportunistically committed any pre-existing dirty files in the tree alongside the actual session capture. Now it stages only the files this capture wrote: the new session file and (if updated) the project's `cp.md`. Pre-existing dirty state stays uncommitted for the human to handle.

  Caught in real use: a `/cp-summarize` run picked up 11 files of pre-existing sync output in addition to the intended 2-file session change, producing a `[session] cp-engine: ...` commit whose contents were mostly unrelated to the session.

## v0.4.2 — 2026-05-08

### Fixed

- **`actions/sync` composite action runs on Node.js 24.** Bumped `actions/checkout` from v4 to v6 and `actions/setup-python` from v5 to v6. GitHub deprecates Node.js 20 on its runners June 2 2026; v6 of both actions runs on Node.js 24 and is the future-proof pin.

## v0.4.1 — 2026-05-08

### Fixed

- **`cp capture-session` now enforces the cp tenant's engine pin.** Previously the command bypassed `config.load()` (it doesn't need the merged config to do its job), which meant a stale cp-engine binary would silently produce wrong-format output against a newer-pinned tenant. The check now runs after destination resolution but before any writes — a stale install fails loud with `EngineVersionMismatch` and no half-formed session file is created.
- **`EngineVersionMismatch` message includes upgrade instructions.** Lists the system-wide (`uv tool install --force --from <repo> cp-engine`) and project-local (`uv pip install -e <repo>`) options. Prompted by a real failure: a `.venv/bin/cp` got stuck at v0.1.0 across many engine releases, and the slash command silently dispatched to it.

### Added

- **`config.enforce_engine_version_for_tenant(tenant_root)`** — public lightweight helper that reads just `[engine].version` from `.cp-engine.toml` and runs the existing constraint check. Used by `capture_session()`; available for any future command that needs to validate the install without loading the full merged config.

## v0.4.0 — 2026-05-08

### Added

- **Two-way local linkage between cp tenants and source repos.** A new `[local-repos]` section in `.cp-engine.local.toml` maps GitHub repo names to local clone paths (per-machine, gitignored). The engine reads this map to enrich `_repo.md` and to discover where each tracked repo lives on disk.
- **`cp link-local` CLI command.** Reads `[local-repos]`, walks the cp tenant tree to locate matching `_repo.md` files, writes `<source-repo>/.cp-link` containing the absolute path of the corresponding cp working dir, and adds `.cp-link` to `<source-repo>/.git/info/exclude`. Idempotent. Validates that each configured path is a git repo whose `origin` remote matches the entry name.
- **`cp capture-session` CLI command.** Writes a session summary back to the cp tree from inside a source repo. Resolves the cp working dir via `<source-repo>/.cp-link`, self-heals if the linked path is stale (re-resolves from the source repo's git remote against the cp tenant's `_repo.md` files), writes `<wd>/sessions/<YYYY-MM-DD>-<HHMM>-<user>.md` with a counter-suffix on collision, updates `cp.md`'s `**Last session:**` line without touching other Quick Resume content, then commits and pushes the cp clone. Falls back to writing `<cp-tenant>/exceptions/` when the source repo isn't a tracked project.
- **`/cp-summarize` Claude Code slash command** at `plugin/commands/cp-summarize.md`. Thin wrapper over `cp capture-session` that drafts the session summary using a loose template (Session header, What we did, Decisions, Open threads, Next), writes it to a temp file, and shells out to the CLI. Distributed via the marketplace manifest at `.claude-plugin/marketplace.json`: install with `/plugin marketplace add FirstPersonSF/cp-engine` then `/plugin install cp-engine@cp-engine`.
- **Engine-managed exceptions README.** When `<cp-tenant>/exceptions/` exists, sync regenerates `exceptions/README.md` with a splice region (`exceptions-list`) listing the last 30 days of exception files (newest first, parsed from filename or mtime).
- **Master-cp `exceptions-summary` line.** When the tenant has 1+ exception in the last 7 days, master-cp.md surfaces a one-line "**Exceptions:** {N} this week" pointer. Region is always present so the splicer can find it; body is empty when the count is zero.
- **`_repo.md` enriched with local clone path.** When `[local-repos]` has an entry for a project's repo name, the rendered file surfaces `**Local clone:** <absolute path>` and points Claude at the local clone for activity questions. Without the entry, the v0.3.3 shape is preserved.
- **CLAUDE.md template gains a "Local-link traversal" section** explaining both directions of the link (cp → source via `_repo.md`, source → cp via `.cp-link`) and pointing at `/cp-summarize`.

### Changed

- **`render_master_cp` accepts `exceptions_count: int = 0`** for the new surface line. `count_exceptions_in_window` (also new) does the lookup; sync.py wires it.
- **`render_repo_md` accepts `local_clone_path: Path | None = None`** for the enriched output. Sync.py looks up via `config.local_repos.get(project.repo_name)`.
- **`TenantConfig` gains `local_repos: Mapping[str, Path]`** (defaults to empty MappingProxyType). Tenants without `[local-repos]` continue to work unchanged.

### Distribution

The Claude Code plugin lives at `/plugin/` inside this repo and is excluded from both the wheel and the sdist via `[tool.hatch.build.targets.sdist].exclude`. Install via `/plugin add github:FirstPersonSF/cp-engine?path=plugin`; updates pull from the same repo.

### Notes

- The slash command's design is **MC-2-free at runtime** — both the linked path resolution and the self-heal walk read filesystem only (cp tenant's committed `.cp-engine.toml` and rendered `_repo.md` files).
- `[local-repos]` is keyed by GitHub repo name, separate from the existing `[repos]` table (which keys by project code). This lets users link non-project repos like `cp-engine` itself or unregistered libraries.

## v0.2.4 — 2026-05-08

### Added

- **Sprint allocations from MC-2.** Sync now reads `public.sprint_allocations` for last week (Monday-starting) and renders in two places:
  - **Per-row allocation line** appended to each non-internal project row in the active sections, formatted as `_Last week: Tony 4h, Marcello 8h (12h total)._`. Skips entirely if zero hours that week. Renders via `<br>` inside the Project cell so it stays in markdown table.
  - **Per-person workload rollup** as a new section at the bottom of master-cp.md. Includes ALL allocations (engagements + internal admin) split into two columns. Surfaces who's spending time on what without polluting per-row data.
- **`MC2Backend.read_allocations(config, week_start)`** — new method returning `WeeklyAllocations` (per-project + per-person rollup).
- **New state shapes:** `PersonHours`, `ProjectAllocation`, `PersonRollup`, `WeeklyAllocations`.
- New engine-managed region: `last-week-workload`.

### Fixed

- **`splice_managed_region` produces matching output for empty bodies.** Previously, splicing an empty body produced `start\n\nend` (two newlines) while a fresh full-write produced `start\nend` (one newline). After splice, second-sync no-op detection failed for any region whose body could be empty (e.g. workload section with no allocations). Now both paths produce `start\nend` for empty bodies.

### Notes

"This week" is intentionally NOT surfaced — it's incomplete on Monday morning when sync runs and gets entered live during the partners' review session itself. Only last week is shown, providing a clean "what just happened" view.

## v0.2.3 — 2026-05-08

### Added

- **Auto-summary from project CP content.** Sync now derives each project's master-CP one-line summary by reading the project CP's hand-written sections — first preference: `## Quick Resume`'s "Current work:" line; fallback: first non-placeholder paragraph in `## Current Work`. Pure Python heuristic, no LLM call. Returns None when content is still placeholder; the column activates the moment a human writes content. ≤120 char cap enforced.
- **`summary.derive_from_project_cp(file_path)`** — new public API that drives the auto-summary.
- **1P split into Pipeline + Active subtables.** Pipeline section first (Status=Deal, sorted by stage progression Inquiry → Negotiation → Contract), Active second (Status=Open). Each section's columns are optimized for its question — Pipeline shows Stage; Active drops Status and Stage as redundant.

### Changed

- New engine-managed region `active-pipeline` in master-cp.md.
- The `active-1p` region now contains only Open engagements (was: all client work). Existing tenants will see schema-evolution recovery fire on first v0.2.3 sync (the existing master-cp.md gets full-rewritten because `active-pipeline` doesn't exist in v0.2.2-rendered files).

## v0.2.2 — 2026-05-08

### Added

- **`cp refresh-pristine`** CLI command. Re-scaffolds project CPs that still contain the placeholder marker (i.e. never been hand-edited) using the current template. Edited CPs are NEVER touched. Use this once after upgrading the engine when you want the template improvements to land in already-scaffolded files. `--dry-run` shows what would change without writing.
- **`project-facts` engine-managed region** in project CPs. Sits near the top, surfaces Code / Status / Owner / Stage / Budget / Client (engagement) or Code / Status / Owner / GitHub / Description (repo) plus Last touched. Means humans opening a project CP see the metadata without needing the master CP loaded.

### Fixed

- **Doubled H1 in project CPs.** Previously `# GGL-5168 GGL 5168 Activation — Project CP` (code prepended to name that already contained the same prefix). Now `# GGL 5168 Activation — Project CP`. Repo CPs were even worse: `# MC-2 mc-2 — Project CP` → now `# mc-2 — Project CP`.
- **Engagement-shaped sections on repo CPs.** Repos got a "Stakeholders" section that doesn't fit. Repos now get "Committers" instead; engagements keep "Stakeholders".
- **Stale Provenance line on existing CPs.** Now updated when `cp refresh-pristine` runs against pristine files.

### Notes for tenants on v0.2.x

After upgrading the engine, run `cp refresh-pristine` once to refresh the template shape across pristine project CPs. Engaged-with CPs (anyone has filled in Quick Resume, Decisions, etc.) are left alone permanently — that's hand-written content the engine never touches.

## v0.2.1 — 2026-05-08

### Fixed

- **Schema-evolution recovery in `_write_if_changed`.** When the existing master-cp.md is missing one or more expected splice regions (typically because the engine version bumped and added new regions like v0.1's `active-table` → v0.2's `active-1p`/`active-fpsf`/`active-canonic`), sync now full-rewrites instead of raising `MarkerMissing`. Logs a warning so the recovery is visible. Caught when upgrading the cp tenant from v0.1.4 → v0.2.0 — the existing master-cp.md had v0.1 markers that didn't match v0.2's region names.

## v0.2.0 — 2026-05-08

### Added

- **`sync_mc2` reads two source streams.** Engagement projects (`public.projects`) AND standalone repos (`public.repos WHERE project_id IS NULL`). Both unify into `tuple[ProjectState, ...]` returned from `read_projects()`. Repos linked to engagements are intentionally excluded — their info enriches the parent engagement's project CP, not the master index.
- **`ProjectState` gains `source` + `company_kind` discriminators** plus engagement-only fields (`deal_stage`, `budget`) and repo-only fields (`github_org`, `repo_name`, `description`).
- **Master CP renders three sections** grouped by `companies.kind`:
  - 1P — client engagements, engagement-shape table (Code | Project | Status | Stage | Owner | Budget | Last touched | Summary | CP)
  - First Person — self-fpsf repos, repo-shape table (Repo | Status | Owner | Description | Last touched | GitHub | CP)
  - Canonic — self-canonic repos, same repo-shape
- Engine-managed regions renamed: `active-table` → `active-1p`, plus new `active-fpsf` and `active-canonic` regions.

### Changed

- The "tenant" model is conceptually collapsed: one CP repo serves all three audiences (1P / FPSF / Canonic), filtered at render time rather than at repo level. The `[tenant]` block in `.cp-engine.toml` still exists but is generic.
- Smoke-tested against the real MC-2 with 21 engagements + 5 standalone repos rendered correctly.

### Migration notes for tenant repos pinned `~= 0.1`

This is a minor bump (0.1.x → 0.2.0) and tenants pinned to `~= 0.1` will NOT auto-upgrade. Tenants should:
1. Bump pin to `~= 0.2` in `.cp-engine.toml` `[engine].version`
2. Bump GitHub Action workflow to `pip install "git+https://github.com/FirstPersonSF/cp-engine.git@v0.2.0"`
3. On next sync, the master CP regenerates with three sections. Existing project CPs are untouched.

## v0.1.4 — 2026-05-08

### Fixed

- `sync` no longer writes `master-cp.md` when the only change between syncs is the `last-sync-timestamp` region. Previously the hourly cron produced a one-line commit every run even when MC-2 hadn't changed — 24 noise commits per day. The engine now compares non-cosmetic regions only; timestamp refreshes piggyback on real changes, never stand alone. When MC-2 *has* changed, the new sync clock is written alongside the real diff (no stale-timestamp risk).

### Changed

- `_write_if_changed` (internal) gains a `cosmetic_regions` parameter for regions whose contents differ every sync by definition. Currently only `last-sync-timestamp` is cosmetic; the parameter is generic so future timestamp-shaped regions can opt in.

## v0.1.3 — 2026-05-07

### Fixed

- `config.load` no longer raises `LocalConfigMissing` when committed `.cp-engine.toml` has no `[[projects]]` entries — there's nothing to map, so the local file is treated as optional. This unblocks CI runners (gitignored local file) and mc-2-backend tenants that read their project list from MC-2 directly rather than from committed config. Surfaced when standing up cp-1p's hourly Action.

## v0.1.2 — 2026-05-07

### Fixed

- `sync` now archives project CPs whose source-of-truth project has dropped out of view (archived in MC-2, deleted, or flipped to `is_internal=true`). The CP file moves from `projects/<code>.md` to `projects/archived/<code>.md`. Hand-edited content is preserved (move/rename, not regenerate). If `projects/archived/<code>.md` already exists, the engine logs a warning and skips rather than overwriting. ([cp-1p#1] surfaced this when 30+ legacy projects were bulk-archived in MC-2.)

### Changed

- `SyncResult` gains a `files_archived: tuple[Path, ...]` field. `no_op` is now true iff both `files_written` and `files_archived` are empty. CLI `cp sync` reports archived files in the same output block as written files.

## v0.1.1 — 2026-05-07

### Fixed

- `sync` no longer scaffolds project CPs for `is_internal=true` projects. The renderer was already correctly excluding them from `master-cp.md`, but the orchestrator was creating CP files for them — leaving inconsistent state on disk vs. what the rendered master CP showed. Surfaced while standing up cp-1p.

## v0.1.0 — 2026-05-07

First release. The framework for First Person and Canonic CP corpora.

### What works

- `cp_engine.config`: `.cp-engine.toml` + `.cp-engine.local.toml` merger with fail-loud semantics, engine_version constraint enforcement (via `packaging.SpecifierSet`), symlink resolution for Dropbox-backed working dirs, drift warnings for orphan local entries
- `cp_engine.render`: full renderers (`master-cp`, `weekly-cp`, `project-cp`, `CLAUDE.md`) plus `splice_managed_region` with HTML-comment markers and impossible-to-misuse splice semantics (raises on missing/duplicate/inverted markers)
- `cp_engine.sync`: orchestrator with Backend `Protocol`; `mc-2` backend reads MC-2's `projects` table via Supabase using company-prefixed `<prefix>-<number>` canonical IDs (legacy rows without `company_id` fall back to `<number>`)
- `cp_engine.init`: interactive `cp init` writes `.cp-engine.local.toml`, strict path validation with up to 3 retries, tomlkit round-trip preserves user comments
- `cp_engine.status`: canonical vocabulary `Deal | Open | Holding | Closed | Archived` with per-status active flag map; mirror of `mc-2/backend/src/status.py`
- `cp_engine.modes`: the four reading-mode contracts (index-only / single-project / sprint / weekly review) as data
- `cp_engine.summary`: ≤120-char enforcement for master-CP one-liners
- CLI: `cp init`, `cp sync`, `cp render`
- Templates: `master-cp.md.j2`, `weekly-cp.md.j2`, `project-cp.md.j2`, `CLAUDE.md.j2`
- GitHub Action skeleton at `actions/sync/action.yml`

### Tested

- 87 unit tests
- End-to-end smoke test against the real MC-2 database (57 projects)

### Spec

- Canonical: `docs/specs/cp-engine-spec-v02.md`
- v01 + amendments + architecture-change preserved at `docs/specs/history/`

### Deferred to v0.2

- `github-issues` backend (for `cp-canonic`)
- `cp status` (read-only diff preview)
- CI drift check between `mc-2/status.ts`, `mc-2/status.py`, `cp_engine/status.py`
- Per-issue `tracked-issues` content in project CPs
