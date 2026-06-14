"""Integration test for the shell-spine mirror folded into `cp sync`.

Drives `sync_tenant` with a fake backend that returns one engagement
ProjectState (with `mc2_id` set) and exposes a fake Supabase client via
`shell_client()`. After sync, the fake client's `shell_elements` store must
contain the elements found on disk under that project's `shell/` dir.

Reuses the established fixtures from `test_sync.py` (make_config) and the
`_FakeClient`/`_FakeTable` fake-Supabase pattern from `test_shell_sync.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cp_engine import ProjectState, sync_tenant
from cp_engine.sync import Backend

from tests.test_shell_sync import _FakeClient
from tests.test_sync import make_config


def _write_shell_el(project_dir: Path, layer: str, name: str, eid: str,
                    project: str, **fm) -> None:
    """Write one shell element under <project_dir>/shell/<layer>/<name>.md.

    Mirrors the `_write_el` helper in test_shell_sync.py but targets an
    arbitrary project dir (sync scaffolds clients at 1p/<acct>/<code>/).
    """
    d = project_dir / "shell" / layer
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"id: {eid}", f"project: {project}", f"layer: {layer}",
             f"title: {name}", "status: active", "last_touched: 2026-06-13"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    (d / f"{name}.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n")


class _ShellBackend(Backend):
    """Fake backend that returns one project AND exposes a fake Supabase
    client for the shell-spine mirror to write into."""

    def __init__(self, states: tuple[ProjectState, ...], client: _FakeClient) -> None:
        self._states = states
        self._client = client

    def read_projects(self, config) -> tuple[ProjectState, ...]:
        return self._states

    def shell_client(self):
        return self._client


class _ExplodingClient:
    """A fake Supabase client whose `.table()` raises — used to prove the
    mirror never touches the client when a project has no mc2_id."""

    store: dict = {}

    def table(self, name):  # noqa: ANN001
        raise AssertionError("mirror should not run when mc2_id is None")


def _make_engagement(code: str, mc2_id: str | None = "x") -> ProjectState:
    return ProjectState(
        code=code,
        name=code,  # name == code keeps the dir slug == bare code
        source="engagement",
        company_kind="client",
        company_code="GGL",
        company_name="Google",
        status="Open",
        is_internal=False,
        owner="drew",
        last_touched=datetime(2026, 6, 13, tzinfo=timezone.utc),
        deadline=None,
        mc2_id=mc2_id,
    )


def test_sync_mirrors_shell_spine_into_backend_client(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    # The engagement scaffolds at 1p/google/<code>/. Pre-create its shell/
    # elements on disk there so the mirror has something to reconcile.
    code = "ggl-5168"
    proj_dir = tmp_path / "1p/google" / code
    _write_shell_el(proj_dir, "Deliverables", "pos", f"{code}/deliverable/pos",
                    project=code)
    _write_shell_el(proj_dir, "Research", "iv1", f"{code}/research/iv1",
                    project=code)

    client = _FakeClient()
    state = _make_engagement(code, mc2_id="uuid-ggl-5168")
    backend = _ShellBackend((state,), client)

    sync_tenant(config, backend_factory=lambda _: backend)

    rows = client.store.get("shell_elements", [])
    assert {r["element_id"] for r in rows} == {
        f"{code}/deliverable/pos",
        f"{code}/research/iv1",
    }
    # Every mirrored row is keyed to this project's MC-2 uuid.
    assert all(r["project_id"] == "uuid-ggl-5168" for r in rows)


def test_sync_skips_mirror_when_no_mc2_id(tmp_path: Path) -> None:
    """A project without an mc2_id (default None) must skip the mirror
    entirely — shell_client() should never be consulted."""
    config = make_config(tmp_path)
    code = "ggl-5168"
    proj_dir = tmp_path / "1p/google" / code
    _write_shell_el(proj_dir, "Research", "iv1", f"{code}/research/iv1",
                    project=code)

    # The backend hands back a client that explodes if `.table()` is ever
    # called — so the test fails loudly if the mirror runs despite mc2_id=None.
    client = _ExplodingClient()
    state = _make_engagement(code, mc2_id=None)
    backend = _ShellBackend((state,), client)

    # Completes without error ⇒ shell_client()/`.table()` was never consulted.
    sync_tenant(config, backend_factory=lambda _: backend)

    assert client.store.get("shell_elements", []) == []
