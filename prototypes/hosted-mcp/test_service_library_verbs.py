"""`list_services` / `get_service` — the library reads that make IDs checkable.

The whole point of moving the Service Library into MC-2 (2026-09-07) was
that a document cannot enforce its own corrections. `[A.064]` Information
Architecture was deprecated in an Addendum and still sat unmarked in the
list, so anyone reading top-down selected a retired service.

These verbs are where that stops being possible: a deprecated item is not
returned by default, and an unknown Reference ID comes back as an explicit
`found: false` with an instruction not to cite it.

Source-shape assertions, in the house style of `test_degradation_visible.py`
— the live paths need Supabase and a caller JWT to exercise for real.

    python -m pytest prototypes/hosted-mcp/test_service_library_verbs.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).parent / "server.py").read_text()


def body_of(func: str) -> str:
    start = SRC.index(f"def {func}(")
    rest = SRC[start:]
    nxt = re.search(r"\n(?:@mcp_server\.tool\(\)\n)?def ", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


# ──────────────────────────────────────────────────────────────────────
#  Registration
# ──────────────────────────────────────────────────────────────────────


def test_both_verbs_are_registered_as_tools() -> None:
    for verb in ("list_services", "get_service"):
        assert f"@mcp_server.tool()\ndef {verb}(" in SRC, f"{verb} not registered"


def test_both_run_under_the_callers_identity() -> None:
    """Never the service key — RLS is the authorization story here."""
    for verb in ("list_services", "get_service"):
        assert "user_client()" in body_of(verb)
        assert "service" not in body_of(verb).split("user_client()")[0][-200:].lower() \
            or "SUPABASE_SERVICE" not in body_of(verb)


def test_both_read_the_estimator_schema() -> None:
    for verb in ("list_services", "get_service"):
        assert 'schema("estimator")' in body_of(verb)


# ──────────────────────────────────────────────────────────────────────
#  The deprecation guard — the reason this exists
# ──────────────────────────────────────────────────────────────────────


def test_list_services_excludes_deprecated_by_default() -> None:
    body = body_of("list_services")
    assert 'include_deprecated: bool = False' in SRC, "default must be False"
    assert 'eq("status", "active")' in body, \
        "the default query must filter to active only"


def test_list_services_can_still_trace_a_deprecated_id() -> None:
    """Excluded from selection, but findable — an ID in an old proposal
    still needs to be explicable."""
    body = body_of("list_services")
    assert 'in_("status", ["active", "deprecated"])' in body


def test_a_deprecated_item_is_flagged_loudly_when_returned() -> None:
    for verb in ("list_services", "get_service"):
        assert '"DEPRECATED"' in body_of(verb), \
            f"{verb} must mark a deprecated item as such"


def test_get_service_warns_against_citing_a_deprecated_item() -> None:
    body = body_of("get_service")
    assert "never cite" in body.lower()


# ──────────────────────────────────────────────────────────────────────
#  The fabrication guard
# ──────────────────────────────────────────────────────────────────────


def test_unknown_ref_id_returns_found_false_not_an_empty_success() -> None:
    """A silent empty result reads as 'nothing matched'; the caller needs
    to know the ID does not exist at all."""
    body = body_of("get_service")
    assert '"found": False' in body


def test_unknown_ref_id_instructs_against_inventing_one() -> None:
    body = body_of("get_service")
    assert "do not cite it" in body.lower() or "not cite" in body.lower()
    assert "propose a" in body.lower()


def test_empty_list_result_says_do_not_invent() -> None:
    body = body_of("list_services")
    assert "invent" in body.lower()


def test_malformed_ref_id_is_rejected_rather_than_guessed() -> None:
    body = body_of("get_service")
    assert "must start with" in body


# ──────────────────────────────────────────────────────────────────────
#  Degradation stays visible (house rule, 2026-08-26 audit)
# ──────────────────────────────────────────────────────────────────────


def test_neither_verb_raises_on_backend_failure() -> None:
    for verb in ("list_services", "get_service"):
        body = body_of(verb)
        assert "except Exception" in body, f"{verb} must degrade, not raise"


def test_list_services_reports_what_it_could_not_read() -> None:
    """A partial read must not look like a complete one."""
    body = body_of("list_services")
    assert 'errors' in body and 'result["errors"] = errors' in body


def test_get_service_returns_the_error_text_not_a_bare_none() -> None:
    body = body_of("get_service")
    assert 'f"lookup failed: {exc}"' in body


# ──────────────────────────────────────────────────────────────────────
#  Contract details
# ──────────────────────────────────────────────────────────────────────


def test_definitions_are_returned_verbatim_under_a_stable_key() -> None:
    """The caller quotes this exactly and adds a reframing line beneath;
    it must not be reshaped between here and there."""
    assert '"definition": row.get("short_description")' in body_of("get_service")


def test_guidance_is_surfaced_when_present() -> None:
    """The Addendum's per-item nuance is the whole reason two similar
    items can be told apart."""
    for verb in ("list_services", "get_service"):
        assert "guidance" in body_of(verb)


def test_library_version_is_reported() -> None:
    """The old Doc's version stamps disagreed with its filename; a caller
    must always be able to say which library it read."""
    body = body_of("list_services")
    assert "service_library_meta" in body
    assert '"library_version"' in body


def test_brackets_are_tolerated_on_ref_id() -> None:
    """Callers copy '[A.001]' straight out of a proposal."""
    assert 'strip("[]")' in body_of("get_service")


def test_kind_filter_is_validated_not_silently_ignored() -> None:
    body = body_of("list_services")
    assert "kind must be" in body
