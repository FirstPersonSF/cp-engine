"""The Drive account is named where a sharing gap would otherwise read as a missing file.

2026-10-30: the webhook, hosted MCP and a laptop all read Drive as a service
account no project folder was shared with. `cxp folders` said "not found" for
nine folders that existed and `pull_document_comments` said "Forbidden"; neither
said which account Drive had refused.
"""

import sys
import types

from cp_engine import folder_rules as fr
from cp_engine import project_sources as ps

SA = "reader@proj.iam.gserviceaccount.com"


class _Exec:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def execute(self):
        if self.error:
            raise self.error
        return self.value


class _Service:
    def __init__(self, *, cred_email=None, about_email=None, about_error=None,
                 comments_error=None):
        self._http = types.SimpleNamespace(
            credentials=types.SimpleNamespace(service_account_email=cred_email)
            if cred_email else None)
        self._about = (about_email, about_error)
        self._comments_error = comments_error

    def about(self):
        email, error = self._about
        return types.SimpleNamespace(get=lambda **k: _Exec(
            {"user": {"emailAddress": email}} if email else {}, error))

    def comments(self):
        return types.SimpleNamespace(list=lambda **k: _Exec({"comments": []},
                                                           self._comments_error))


class _Conn:
    def __init__(self, service):
        self.service = service


def test_account_from_the_credential_without_a_network_call():
    svc = _Service(cred_email=SA, about_error=AssertionError("must not call Drive"))
    assert fr.drive_account(_Conn(svc)) == SA


def test_account_falls_back_to_drive_about():
    assert fr.drive_account(_Conn(_Service(about_email=SA))) == SA


def test_account_unknown_is_none_never_a_raise():
    assert fr.drive_account(None) is None
    assert fr.drive_account(_Conn(_Service(about_error=RuntimeError("offline")))) is None

    class _NoService:
        @property
        def service(self):
            raise RuntimeError("auth failed")

    assert fr.drive_account(_NoService()) is None


def _row(check):
    return fr.AuditRow(
        code="ibx-1-x", label="job", status="Open", project_id="p1", in_scope=True,
        enabled={"dropbox": False, "drive": True},
        bound={"dropbox": None, "drive": "fid-1"},
        bound_check={"dropbox": "", "drive": check},
        inherited={"dropbox": None, "drive": None},
        inherit_note={"dropbox": None, "drive": None},
        suggested=None, suggested_check="", found_path=None,
    )


def test_audit_names_the_account_and_the_likely_cause():
    text = fr.render_audit([_row("not found")], drive_account=SA)
    assert text.splitlines()[0] == f"Drive checked as {SA}"
    assert f"bound fid-1 [not found: deleted, or not shared with {SA}]" in text


def test_audit_without_an_account_reads_as_before():
    text = fr.render_audit([_row("not found")])
    assert "Drive checked as" not in text
    assert "bound fid-1 [not found]" in text
    assert "[exists]" in fr.render_audit([_row("exists")], drive_account=SA)


def _install_connector(monkeypatch, service):
    class _Connector:
        def __init__(self, *a, **k):
            self.service = service

        def _authenticate(self):
            pass

    mod = types.ModuleType("cloud_storage.google_drive_connector")
    mod.GoogleDriveConnector = _Connector
    monkeypatch.setitem(sys.modules, "cloud_storage.google_drive_connector", mod)


def test_comment_read_failure_names_the_account(monkeypatch):
    _install_connector(monkeypatch, _Service(cred_email=SA,
                                             comments_error=PermissionError("Forbidden")))
    try:
        ps._drive_comments("g1")
    except RuntimeError as exc:
        msg = str(exc)
    else:
        raise AssertionError("a refused read must raise, never return []")
    assert "PermissionError: Forbidden" in msg and f"read as {SA}" in msg


def test_comment_read_failure_without_an_account_is_unchanged(monkeypatch):
    _install_connector(monkeypatch, _Service(comments_error=PermissionError("Forbidden"),
                                             about_error=RuntimeError("offline")))
    try:
        ps._drive_comments("g1")
    except PermissionError as exc:
        assert str(exc) == "Forbidden"
    else:
        raise AssertionError("expected the original error")
