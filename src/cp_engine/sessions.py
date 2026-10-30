"""Several cp sessions at once, safely (#394).

Before this module, two Claude Code sessions in one tenant clone could hurt
each other three ways:

* **Unscoped commits.** A wrap-up that staged the whole tree committed the
  other session's half-finished files under its own message.
* **Shared generated files.** Two `cxp sync` runs at once both rewrote
  `master-cp.md`, the sprint READMEs and `.cp-engine/paths.json`, and both
  wrote MC-2 (ask imports, commitment expiry). Nothing serialised them.
* **No awareness.** Neither session knew the other existed.

The four pieces here answer those:

1. **`tenant_lock`** — an exclusive `flock` on a file in the repo's *common*
   git dir, so every worktree of one clone shares it. `flock` is released by
   the kernel when its holder dies, so a crashed sync can never wedge the
   tenant: a lock file left behind by a dead process is simply re-acquired.
   The file carries the holder's pid, host, purpose, project and start time,
   so a waiter can name who it is waiting for, and how long they have held it.
2. **Scoped commits** — `session_paths` picks, from `git status`, only the
   files a session on given workstream(s) owns: the working dir (minus any
   nested workstream dir), that workstream's sprint files, `improvements.md`,
   and paths named explicitly. `commit_session` commits exactly those with
   `git commit -- <paths>`, which ignores anything else that happens to be
   staged.
3. **Per-session worktrees** — `start_session` makes a git worktree of the
   tenant on a fresh `cp-session/<code>-<stamp>` branch from `origin/main`,
   beside the tenant (not inside it, so Claude Code does not load the
   tenant's `CLAUDE.md` twice). `end_session` refuses to remove one whose
   work is not on `origin/main` yet.
4. **A registry** — `cp-sessions.json` in the common git dir: who is working
   where. Worktree sessions register at start; plain Claude Code sessions
   register from the SessionStart hook and leave from SessionEnd. Stale
   entries (worktree gone, holder pid dead, hook entry a day old) are pruned
   on every read.

`push_session` is the one push path for both a session branch and `main`:
fetch, merge (never rebase — `--ours`/`--theirs` swap meaning mid-rebase)
`origin/main`, prove with `merge_check` that no `cp:hash` bullet was lost,
then push `HEAD:main`.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

try:  # POSIX only; cp runs on macOS and Linux.
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = "cp-engine-sync.lock"
REGISTRY_NAME = "cp-sessions.json"
REGISTRY_LOCK_NAME = "cp-sessions.lock"
SESSION_BRANCH_PREFIX = "cp-session/"

# How long a second `cxp sync` waits for the first before giving up. A full
# sync is usually well under a minute; waiting longer than this hides a hung
# holder. Override with CXP_LOCK_WAIT (seconds).
DEFAULT_LOCK_WAIT = 30.0
# A holder older than this is named as possibly hung in the refusal. The
# kernel already frees the lock of a DEAD holder; a LIVE one cannot be broken
# safely from outside, only reported.
DEFAULT_LOCK_STALE = timedelta(minutes=30)
# A hook-registered session whose Claude Code pid could not be found has no
# liveness signal but its age.
HOOK_ENTRY_MAX_AGE = timedelta(hours=24)

# Gitignored per-machine files a new worktree needs to behave like the tenant
# clone. Symlinked, so an edit in either place is the same file.
LOCAL_FILES = (".cp-engine.local.toml", ".claude/settings.local.json")


class SessionError(RuntimeError):
    """A session operation refused, with a message fit to print as-is."""


# ──────────────────────────────────────────────────────────────────────
#  git plumbing
# ──────────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise SessionError(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def git_common_dir(root: Path) -> Path | None:
    """The git dir every worktree of this clone shares, or None (not git)."""
    proc = _git(root, "rev-parse", "--git-common-dir", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    path = Path(proc.stdout.strip())
    if not path.is_absolute():
        path = (root / path)
    return path.resolve()


def toplevel(path: Path) -> Path | None:
    proc = _git(path, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def main_worktree(root: Path) -> Path | None:
    """The clone's primary checkout (first entry of `git worktree list`)."""
    proc = _git(root, "worktree", "list", "--porcelain", check=False)
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):]).resolve()
    return None


def current_branch(root: Path) -> str | None:
    proc = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return proc.stdout.strip() or None


def push_target(root: Path) -> tuple[str, str]:
    """(remote, branch) a session's work lands on.

    The upstream of the current branch when it has one — a session branch
    tracks `origin/main` — else `origin/main`.
    """
    proc = _git(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
    )
    up = proc.stdout.strip()
    if proc.returncode == 0 and "/" in up:
        remote, branch = up.split("/", 1)
        return remote, branch
    return "origin", "main"


def push_refspec_args(root: Path) -> list[str]:
    """Extra `git push` args so a branch whose name differs from its
    upstream's (a `cp-session/…` branch tracking `origin/main`) pushes to
    that upstream. Empty when plain `git push` already does the right thing.
    """
    proc = _git(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
    )
    up = proc.stdout.strip()
    branch = current_branch(root)
    if proc.returncode != 0 or "/" not in up or branch is None:
        return []
    remote, up_branch = up.split("/", 1)
    if up_branch == branch:
        return []
    return [remote, f"HEAD:{up_branch}"]


def _is_ancestor(root: Path, a: str, b: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _state_dir(root: Path) -> Path:
    """Where the lock and the registry live: the common git dir, so every
    worktree of the clone sees one copy. A non-git tenant (tests, a fresh
    `cxp init`) falls back to its own `.cp-engine/`."""
    common = git_common_dir(root)
    if common is not None:
        return common
    d = root / ".cp-engine"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ──────────────────────────────────────────────────────────────────────
#  1. The sync/render lock
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LockHolder:
    pid: int | None
    host: str | None
    purpose: str | None
    project: str | None
    worktree: str | None
    started_at: str | None

    def age(self, now: datetime | None = None) -> timedelta | None:
        ts = _parse_ts(self.started_at)
        return None if ts is None else (now or _now()) - ts

    def describe(self, now: datetime | None = None) -> str:
        age = self.age(now)
        mins = f"{int(age.total_seconds() // 60)} min" if age is not None else "?"
        bits = [self.purpose or "a cxp command", f"pid {self.pid or '?'}"]
        if self.host:
            bits[-1] += f" on {self.host}"
        if self.project:
            bits.append(f"project {self.project}")
        if self.worktree:
            bits.append(f"in {self.worktree}")
        bits.append(f"running {mins}")
        return ", ".join(bits)


class LockHeld(SessionError):
    def __init__(self, holder: LockHolder | None, waited: float, stale: bool):
        self.holder = holder
        self.waited = waited
        self.stale = stale
        who = holder.describe() if holder else "another cxp process (no holder info)"
        msg = (
            f"Another cxp sync/render holds the tenant lock: {who}. "
            f"Waited {waited:.0f}s. Run it again once that finishes."
        )
        if stale and holder and holder.pid:
            msg += (
                f" It has held the lock longer than "
                f"{int(DEFAULT_LOCK_STALE.total_seconds() // 60)} min; if it is "
                f"hung, stop it (`kill {holder.pid}`) — a dead holder's lock "
                "frees itself."
            )
        super().__init__(msg)


def _read_holder(path: Path) -> LockHolder | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return LockHolder(
        pid=data.get("pid"),
        host=data.get("host"),
        purpose=data.get("purpose"),
        project=data.get("project"),
        worktree=data.get("worktree"),
        started_at=data.get("started_at"),
    )


def lock_path(root: Path) -> Path:
    return _state_dir(root) / LOCK_NAME


def read_lock_holder(root: Path) -> LockHolder | None:
    """Who holds the tenant lock right now, or None when it is free.

    Free means `flock` is acquirable: a lock file whose recorded holder died
    still holds that holder's info, and is reported free here."""
    path = lock_path(root)
    if not path.exists() or fcntl is None:
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_holder(path)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextmanager
def tenant_lock(
    root: Path,
    *,
    purpose: str,
    project: str | None = None,
    wait: float | None = None,
    stale_after: timedelta = DEFAULT_LOCK_STALE,
    poll: float = 0.5,
) -> Iterator[LockHolder]:
    """Hold the tenant's sync/render lock for the body of the `with`.

    Waits up to `wait` seconds (default `CXP_LOCK_WAIT`, else 30) for a
    current holder, then raises `LockHeld` naming it. Read-only commands
    (`cxp render --check`, `cxp status`) must not take it.
    """
    if wait is None:
        try:
            wait = float(os.environ.get("CXP_LOCK_WAIT", DEFAULT_LOCK_WAIT))
        except ValueError:
            wait = DEFAULT_LOCK_WAIT
    path = lock_path(root)
    me = LockHolder(
        pid=os.getpid(),
        host=socket.gethostname(),
        purpose=purpose,
        project=project,
        worktree=str(toplevel(root) or root),
        started_at=_now().isoformat(timespec="seconds"),
    )
    if fcntl is None:  # pragma: no cover — no flock: run unguarded, as before
        yield me
        return

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - start
                if waited >= wait:
                    holder = _read_holder(path)
                    age = holder.age() if holder else None
                    raise LockHeld(
                        holder, waited, stale=bool(age and age > stale_after)
                    ) from None
                time.sleep(poll)
        # Ours now. Whatever the file said was a dead holder's leftover.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(asdict(me)).encode("utf-8"))
        os.fsync(fd)
        try:
            yield me
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ──────────────────────────────────────────────────────────────────────
#  4. The session registry
# ──────────────────────────────────────────────────────────────────────


@dataclass
class SessionEntry:
    id: str
    worktree: str
    kind: str  # "worktree" (cxp session start) | "hook" (SessionStart hook)
    project: str | None = None
    branch: str | None = None
    pid: int | None = None
    host: str | None = None
    started_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))
    claude_session: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "SessionEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def label(self) -> str:
        return self.project or "no project yet"


def registry_path(root: Path) -> Path:
    return _state_dir(root) / REGISTRY_NAME


def entry_is_stale(entry: SessionEntry, now: datetime | None = None) -> bool:
    now = now or _now()
    if not Path(entry.worktree).exists():
        return True
    local = entry.host in (None, socket.gethostname())
    if entry.pid and local and not pid_alive(entry.pid):
        return True
    if entry.kind == "hook" and not entry.pid:
        ts = _parse_ts(entry.started_at)
        if ts is None or now - ts > HOOK_ENTRY_MAX_AGE:
            return True
    return False


@contextmanager
def _registry_txn(root: Path) -> Iterator[list[SessionEntry]]:
    """Read-modify-write of the registry under its own short flock, written
    atomically (temp file + replace) so a reader never sees half a file."""
    state = _state_dir(root)
    path = state / REGISTRY_NAME
    fd = os.open(state / REGISTRY_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        entries = [
            SessionEntry.from_dict(d) for d in raw if isinstance(d, dict) and d.get("id")
        ]
        before = [asdict(e) for e in entries]
        yield entries
        after = [asdict(e) for e in entries]
        if after != before:
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def list_sessions(root: Path, *, prune: bool = True) -> list[SessionEntry]:
    """Active sessions of this clone, stale ones pruned (and dropped from
    the file) unless `prune=False`."""
    with _registry_txn(root) as entries:
        if prune:
            entries[:] = [e for e in entries if not entry_is_stale(e)]
        return list(entries)


def register(root: Path, entry: SessionEntry) -> SessionEntry:
    with _registry_txn(root) as entries:
        entries[:] = [e for e in entries if e.id != entry.id and not entry_is_stale(e)]
        entries.append(entry)
    return entry


def unregister(root: Path, entry_id: str) -> bool:
    with _registry_txn(root) as entries:
        n = len(entries)
        entries[:] = [e for e in entries if e.id != entry_id]
        return len(entries) != n


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def others_active(root: Path) -> list[SessionEntry]:
    """Sessions other than the caller's. In a session worktree, "mine" is
    that worktree's entry; in a shared checkout the caller is assumed to be
    one of the entries registered there, so one of them is discounted."""
    here = toplevel(root) or root
    entries = list_sessions(root)
    mine_wt = [e for e in entries if e.kind == "worktree" and _same_path(e.worktree, here)]
    if mine_wt:
        return [e for e in entries if e is not mine_wt[0] and not _same_path(e.worktree, here)]
    same = [e for e in entries if _same_path(e.worktree, here)]
    rest = [e for e in entries if not _same_path(e.worktree, here)]
    return rest + same[1:]


def others_line(root: Path, *, committed_only_mine: bool) -> str | None:
    others = others_active(root)
    if not others:
        return None
    here = toplevel(root) or root
    parts = []
    for e in others:
        where = "this checkout" if _same_path(e.worktree, here) else e.worktree
        parts.append(f"{e.label()} ({where})")
    n = len(others)
    line = f"{n} other cp session{'s' if n != 1 else ''} active: {'; '.join(parts)}"
    if committed_only_mine:
        line += "; only your files were committed."
    return line


def _claude_pid(start: int | None = None) -> int | None:
    """The Claude Code process this hook runs under: the nearest ancestor
    whose command name contains `claude`. None when it cannot be found."""
    pid = start or os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        out = proc.stdout.strip()
        if proc.returncode != 0 or not out:
            return None
        ppid_s, _, comm = out.partition(" ")
        if "claude" in Path(comm.strip()).name.lower():
            return pid
        try:
            pid = int(ppid_s)
        except ValueError:
            return None
    return None


def hook_register(
    root: Path, claude_session: str, *, pid: int | None = None
) -> SessionEntry:
    """SessionStart: record this Claude Code session. Inside a session
    worktree, attach to that worktree's entry rather than adding a second."""
    here = toplevel(root) or root
    pid = pid if pid is not None else _claude_pid()
    with _registry_txn(root) as entries:
        entries[:] = [e for e in entries if not entry_is_stale(e)]
        for e in entries:
            if e.kind == "worktree" and _same_path(e.worktree, here):
                e.claude_session = claude_session
                return e
        entry = SessionEntry(
            id=f"claude:{claude_session}",
            worktree=str(here),
            kind="hook",
            pid=pid,
            host=socket.gethostname(),
            claude_session=claude_session,
        )
        entries[:] = [e for e in entries if e.id != entry.id]
        entries.append(entry)
        return entry


def hook_unregister(root: Path, claude_session: str) -> None:
    """SessionEnd: drop the hook entry; a worktree entry only loses its
    Claude session (the worktree lives until `cxp session end`)."""
    with _registry_txn(root) as entries:
        keep = []
        for e in entries:
            if e.claude_session == claude_session:
                if e.kind == "worktree":
                    e.claude_session = None
                    keep.append(e)
                continue
            keep.append(e)
        entries[:] = keep


# ──────────────────────────────────────────────────────────────────────
#  2. Scoped commits
# ──────────────────────────────────────────────────────────────────────


def changed_paths(root: Path) -> list[str]:
    """Every path `git status` reports changed or untracked (not ignored),
    repo-relative. Both sides of a rename are listed."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        status, path = rec[:2], rec[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            if i < len(fields) and fields[i]:
                paths.append(fields[i])
            i += 1
    return paths


def _resolve_dirs(root: Path, codes: list[str]) -> list[Path]:
    from cp_engine.spine import SpineDirNotFound, find_spine_dir

    dirs = []
    for code in codes:
        try:
            dirs.append(find_spine_dir(root, code).resolve())
        except SpineDirNotFound as exc:
            raise SessionError(str(exc)) from exc
    return dirs


_SPRINT_FILE_RE = re.compile(r"^sprints/[^/]+/(?P<name>[^/]+)\.md$")


def session_paths(
    root: Path, codes: list[str], extra: tuple[str, ...] | list[str] = ()
) -> tuple[list[str], list[str]]:
    """(paths this session owns, other changed paths left alone).

    Owned: each workstream's working dir — but not a nested workstream's
    dir inside it (a child has its own `cp.md`, and may have its own
    session) unless that child is named too; that workstream's sprint file
    in every `sprints/<week>/`; `improvements.md`; and each `extra` path
    (a file, or a directory prefix).
    """
    root = root.resolve()
    dirs = _resolve_dirs(root, codes)
    rel_dirs = [d.relative_to(root).as_posix() for d in dirs]
    names = {d.name for d in dirs}
    extras = [e.strip("/") for e in extra if e.strip("/")]

    def nested_elsewhere(path: str, base: str) -> bool:
        # Any directory strictly between `base` and the file that is itself
        # a workstream dir (carries cp.md) belongs to that workstream.
        parts = path[len(base) + 1:].split("/")[:-1]
        cur = base
        for part in parts:
            cur = f"{cur}/{part}"
            if cur in rel_dirs:
                return False  # a named workstream claims it directly
            if (root / cur / "cp.md").exists():
                return True
        return False

    def owned(path: str) -> bool:
        if path == "improvements.md":
            return True
        if any(path == e or path.startswith(e + "/") for e in extras):
            return True
        m = _SPRINT_FILE_RE.match(path)
        if m and m.group("name") in names:
            return True
        for base in rel_dirs:
            if path.startswith(base + "/") and not nested_elsewhere(path, base):
                return True
        return False

    mine: list[str] = []
    others: list[str] = []
    for p in changed_paths(root):
        (mine if owned(p) else others).append(p)
    return sorted(set(mine)), sorted(set(others) - set(mine))


@dataclass
class CommitResult:
    sha: str | None
    committed: list[str]
    left_alone: list[str]


def commit_session(
    root: Path,
    codes: list[str],
    message: str,
    extra: tuple[str, ...] | list[str] = (),
) -> CommitResult:
    """Commit only the session's paths. `git commit -- <paths>` commits
    exactly those, leaving anything else that is staged (another session's
    `git add`) staged and uncommitted."""
    mine, others = session_paths(root, codes, extra)
    if not mine:
        return CommitResult(None, [], others)
    present = [p for p in mine if (root / p).exists() or (root / p).is_symlink()]
    gone = [p for p in mine if p not in present]
    if present:
        _git(root, "--literal-pathspecs", "add", "-A", "--", *present)
    if gone:  # deleted (or the old side of a rename): drop from the index
        _git(root, "--literal-pathspecs", "rm", "-q", "--cached",
             "--ignore-unmatch", "--", *gone)
    _git(root, "--literal-pathspecs", "commit", "-q", "-m", message, "--", *mine)
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return CommitResult(sha, mine, others)


# ──────────────────────────────────────────────────────────────────────
#  Push: fetch → merge → merge-check → push HEAD:main
# ──────────────────────────────────────────────────────────────────────


class MergeConflict(SessionError):
    def __init__(self, files: list[str]):
        self.files = files
        super().__init__(
            "Merging origin left conflicts in: " + ", ".join(files) + ". "
            "Resolve by region: inside cp-engine:start/end markers take ours "
            "(sync regenerates them); inspect everything else — never loop "
            "`git checkout --ours`. Then `cxp merge-check`, commit, and run "
            "this again."
        )


class ContentLost(SessionError):
    def __init__(self, lost):
        self.lost = lost
        lines = [f"  {x.path}: cp:hash={x.hash} {x.snippet}" for x in lost[:20]]
        super().__init__(
            f"merge-check: {len(lost)} ingest bullet(s) on origin are missing "
            "from the tree after the merge — not pushed. Restore them, commit, "
            "run again:\n" + "\n".join(lines)
        )


@dataclass
class PushResult:
    pushed: bool
    merged: bool
    target: str
    attempts: int


def push_session(root: Path, *, attempts: int = 3) -> PushResult:
    """Land HEAD on the upstream (origin/main for a session branch).

    Fetch; if origin moved, `git merge --no-edit` it (refusing to continue
    past a conflict); `merge_check` against origin so no webhook bullet was
    dropped; then `git push <remote> HEAD:<branch>`. A push that loses a
    race with another pusher goes round again, up to `attempts` times.
    """
    from cp_engine.merge_check import MergeCheckError, check_merge

    remote, branch = push_target(root)
    ref = f"{remote}/{branch}"
    merged = False
    for attempt in range(1, attempts + 1):
        _git(root, "fetch", "--quiet", remote)
        if _is_ancestor(root, "HEAD", ref):
            return PushResult(False, merged, ref, attempt)  # nothing of ours to land
        if not _is_ancestor(root, ref, "HEAD"):
            proc = _git(root, "merge", "--no-edit", ref, check=False)
            if proc.returncode != 0:
                conflicted = _git(
                    root, "diff", "--name-only", "--diff-filter=U", check=False
                ).stdout.split()
                if conflicted:
                    raise MergeConflict(conflicted)
                raise SessionError(
                    f"git merge {ref} refused: {(proc.stderr or proc.stdout).strip()} "
                    "— uncommitted changes from another session may be in the "
                    "way; a session worktree (`cxp session start <code>`) avoids this."
                )
            merged = True
            try:
                lost, _ = check_merge(root, ref=ref)
            except MergeCheckError as exc:
                raise SessionError(f"merge-check could not run: {exc}") from exc
            if lost:
                raise ContentLost(lost)
        proc = _git(root, "push", "--quiet", remote, f"HEAD:{branch}", check=False)
        if proc.returncode == 0:
            return PushResult(True, merged, ref, attempt)
        err = proc.stderr or ""
        if "non-fast-forward" not in err and "fetch first" not in err and "rejected" not in err:
            raise SessionError(f"git push failed: {err.strip()}")
    raise SessionError(
        f"git push to {ref} lost the race {attempts} times — run it again."
    )


# ──────────────────────────────────────────────────────────────────────
#  3. Per-session worktrees
# ──────────────────────────────────────────────────────────────────────


def sessions_dir(main: Path) -> Path:
    """Where session worktrees go: `CXP_SESSIONS_DIR`, else a sibling of
    the tenant clone, `<tenant>-sessions/`. Beside the tenant, not inside
    it: Claude Code loads every ancestor's CLAUDE.md, so a worktree inside
    the tenant would load the protocol twice."""
    env = os.environ.get("CXP_SESSIONS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return main.parent / f"{main.name}-sessions"


def start_session(
    root: Path, code: str, *, now: datetime | None = None, fetch: bool = True
) -> SessionEntry:
    main = main_worktree(root)
    if main is None:
        raise SessionError(f"{root} is not a git checkout — no worktree to make.")
    wd = _resolve_dirs(main, [code])[0]
    full = wd.name
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = sessions_dir(main) / f"{full}-{stamp}"
    branch = f"{SESSION_BRANCH_PREFIX}{full}-{stamp}"
    if path.exists():
        raise SessionError(f"{path} already exists.")
    if fetch:
        _git(main, "fetch", "--quiet", "origin")
    if _git(main, "rev-parse", "--verify", "--quiet", "origin/main", check=False).returncode:
        raise SessionError("No origin/main to start from — is `origin` set?")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(main, "worktree", "add", "--quiet", "--track", "-b", branch, str(path), "origin/main")
    for rel in LOCAL_FILES:
        src = main / rel
        dst = path / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
    return register(
        main,
        SessionEntry(
            id=branch,
            worktree=str(path.resolve()),
            kind="worktree",
            project=full,
            branch=branch,
            host=socket.gethostname(),
        ),
    )


def _find_worktree_entry(root: Path, code: str | None) -> SessionEntry:
    entries = [e for e in list_sessions(root, prune=False) if e.kind == "worktree"]
    if code:
        hits = [
            e for e in entries
            if e.project and (e.project == code or e.project.startswith(code + "-"))
        ]
        if not hits:
            raise SessionError(f"No session worktree registered for {code}.")
        if len(hits) > 1:
            raise SessionError(
                f"{len(hits)} session worktrees for {code} — run `cxp session end` "
                "from inside the one to close: "
                + ", ".join(e.worktree for e in hits)
            )
        return hits[0]
    here = toplevel(root) or root
    for e in entries:
        if _same_path(e.worktree, here):
            return e
    raise SessionError(
        f"{here} is not a registered session worktree. Run this from inside "
        "one, or name it: `cxp session end <code>`. `cxp session list` shows them."
    )


def end_session(root: Path, code: str | None = None, *, force: bool = False) -> tuple[SessionEntry, Path]:
    """Remove a session worktree and its branch once its work is on origin.

    Refuses (unless `force`) when the worktree has uncommitted changes, or
    commits `origin/main` does not contain. Returns (entry, main checkout).
    """
    entry = _find_worktree_entry(root, code)
    main = main_worktree(root) or root
    wt = Path(entry.worktree)
    links = [rel for rel in LOCAL_FILES if (wt / rel).is_symlink()]
    if wt.exists():
        dirty = [p for p in changed_paths(wt) if p not in links]
        if dirty and not force:
            raise SessionError(
                f"{len(dirty)} uncommitted file(s) in {wt}: "
                + ", ".join(dirty[:10])
                + ". Commit and push them (`cxp session commit <code> -m ...`), "
                "or pass --force to discard them."
            )
        remote, branch = push_target(wt)
        fetched = _git(wt, "fetch", "--quiet", remote, check=False).returncode == 0
        if not fetched and not force:
            raise SessionError(
                f"Could not fetch {remote} to check the work is pushed — "
                "try again online, or pass --force."
            )
        if not force and not _is_ancestor(wt, "HEAD", f"{remote}/{branch}"):
            ahead = _git(wt, "rev-list", "--count", f"{remote}/{branch}..HEAD").stdout.strip()
            raise SessionError(
                f"{ahead} commit(s) in {wt} are not on {remote}/{branch} yet. "
                "Push them (`cxp session commit <code> -m ...` pushes), or pass "
                "--force to throw them away."
            )
        for rel in links:
            (wt / rel).unlink()
        args = ["worktree", "remove"] + (["--force"] if force else []) + [str(wt)]
        _git(main, *args)
    _git(main, "worktree", "prune", check=False)
    if entry.branch:
        _git(main, "branch", "-D", entry.branch, check=False)
    unregister(main, entry.id)
    return entry, main
