"""A rejected Supabase key fails `cxp` with ONE clear line, not a traceback.

On 2026-10-22 Supabase disabled MC-2's legacy API keys. Every local `cxp`
command that read MC-2 died with a long postgrest/pydantic traceback ending
in `APIError: {'message': 'JSON could not be generated', 'code': 401, ...}`.
These tests drive a real command (`commitments-sweep`) through click's
CliRunner with a fake Supabase client whose `execute()` raises that payload,
and resolve creds through the REAL resolver (env / mc-2 .env), so the message
names where the key came from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from postgrest.exceptions import APIError

from cp_engine.cli import main

FAKE_KEY = "eyJhbGciOiJIUzI1NiJ9.SECRET-PAYLOAD-DO-NOT-PRINT.sig"
FAKE_URL = "https://stub.supabase.co"

LEGACY_PAYLOAD = {
    "message": "JSON could not be generated",
    "code": 401,
    "hint": "Refer to full message for details",
    "details": (
        'b\'{"message":"Legacy API keys are disabled","hint":"Your legacy API '
        "keys (anon, service_role) were disabled on 2026-10-22. Re-enable them "
        "in the Supabase dashboard, or use the new publishable and secret API "
        'keys."}\''
    ),
}

INVALID_KEY_PAYLOAD = {
    "message": "Invalid API key",
    "code": "401",
    "hint": "Double check your Supabase `anon` or `service_role` API key.",
    "details": None,
}

NON_AUTH_PAYLOAD = {
    "message": 'column commitments.bogus does not exist',
    "code": "42703",
    "hint": None,
    "details": None,
}


class _RaisingQuery:
    def __init__(self, payload: dict):
        self._payload = payload

    def __getattr__(self, _name):
        # select/eq/is_/order/... all chain back to self.
        return lambda *a, **k: self

    def execute(self):
        raise APIError(self._payload)


class _RaisingClient:
    def __init__(self, payload: dict):
        self._payload = payload

    def table(self, _name):
        return _RaisingQuery(self._payload)

    def rpc(self, *_a, **_k):
        return _RaisingQuery(self._payload)


def _tenant(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    return tmp_path


def _install_client(monkeypatch, payload: dict) -> None:
    import supabase

    monkeypatch.setattr(
        supabase, "create_client", lambda url, key: _RaisingClient(payload)
    )


def _clear_supabase_env(monkeypatch) -> None:
    # setenv first so monkeypatch records the ORIGINAL state and restores it:
    # the .env rung exports what it resolves into os.environ, and a bare
    # delenv of an absent var records nothing — the export would leak into
    # later tests.
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"):
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)


def _run(tenant: Path, monkeypatch, *args: str):
    monkeypatch.chdir(tenant)
    return CliRunner().invoke(main, list(args))


def _assert_one_clean_line(result) -> str:
    out = result.output  # CliRunner mixes stderr into output by default
    assert FAKE_KEY not in out
    assert "SECRET-PAYLOAD" not in out
    assert "Traceback" not in out
    assert "APIError" not in out
    lines = [ln for ln in out.splitlines() if ln.startswith("cxp: MC-2 rejected")]
    assert len(lines) == 1, out
    return lines[0]


def test_legacy_keys_disabled_exits_3_with_one_line_naming_env(
    tmp_path, monkeypatch
) -> None:
    _clear_supabase_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", FAKE_KEY)
    _install_client(monkeypatch, LEGACY_PAYLOAD)

    result = _run(_tenant(tmp_path), monkeypatch, "commitments-sweep", "--all")

    assert result.exit_code == 3, result.output
    # The handler converted the error: CliRunner saw a clean SystemExit, not
    # an escaped APIError.
    assert isinstance(result.exception, SystemExit)
    line = _assert_one_clean_line(result)
    assert "Legacy API keys are disabled" in line
    assert "SUPABASE_SERVICE_KEY env var" in line
    assert "sb_secret_" in line


def test_invalid_api_key_names_the_dotenv_source(tmp_path, monkeypatch) -> None:
    _clear_supabase_env(monkeypatch)
    tenant = _tenant(tmp_path / "tenant")
    clone = tmp_path / "mc-2"
    (clone / "backend").mkdir(parents=True)
    env_file = clone / "backend" / ".env"
    env_file.write_text(
        f"SUPABASE_URL={FAKE_URL}\nSUPABASE_SERVICE_KEY={FAKE_KEY}\n",
        encoding="utf-8",
    )
    (tenant / ".cp-engine.local.toml").write_text(
        f'[local-repos]\n"mc-2" = "{clone}"\n', encoding="utf-8"
    )
    _install_client(monkeypatch, INVALID_KEY_PAYLOAD)

    result = _run(tenant, monkeypatch, "commitments-sweep", "--all")

    assert result.exit_code == 3, result.output
    line = _assert_one_clean_line(result)
    assert "Invalid API key" in line
    assert str(env_file) in line


def test_non_auth_api_error_still_propagates(tmp_path, monkeypatch) -> None:
    _clear_supabase_env(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", FAKE_KEY)
    _install_client(monkeypatch, NON_AUTH_PAYLOAD)

    result = _run(_tenant(tmp_path), monkeypatch, "commitments-sweep", "--all")

    assert isinstance(result.exception, APIError), result.output
    assert result.exception.code == "42703"
    assert "cxp: MC-2 rejected" not in result.output


@pytest.mark.parametrize(
    "payload, expected",
    [
        (LEGACY_PAYLOAD, "Legacy API keys are disabled"),
        (INVALID_KEY_PAYLOAD, "Invalid API key"),
        ({"message": "JWT expired", "code": 401, "hint": None, "details": None}, "JWT expired"),
        (NON_AUTH_PAYLOAD, None),
        ({"message": "permission denied for table x", "code": "42501"}, None),
    ],
)
def test_classifier(payload, expected) -> None:
    from cp_engine.mc2_db import supabase_key_rejection

    assert supabase_key_rejection(APIError(payload)) == expected


def test_classifier_follows_the_exception_chain() -> None:
    from cp_engine.mc2_db import supabase_key_rejection

    try:
        try:
            raise APIError(LEGACY_PAYLOAD)
        except APIError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert supabase_key_rejection(outer) == "Legacy API keys are disabled"
