"""Tests for cp_engine.plan_from_transcript — prompt-shaping logic.

The actual Claude call is integration-level (needs ANTHROPIC_API_KEY)
and is covered by live smoke tests. Here we lock down the pure
helpers: engagement-vs-initiative discrimination and prompt template
selection.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.plan_from_transcript import _build_prompt, _is_engagement_code


def test_is_engagement_code_recognizes_standard_shape() -> None:
    assert _is_engagement_code("ggl-5168") is True
    assert _is_engagement_code("ibx-5153") is True
    assert _is_engagement_code("hex-5184") is True
    assert _is_engagement_code("tel-5113") is True
    # 4-letter prefix (rare but legal in the regex).
    assert _is_engagement_code("aaaa-1234") is True


def test_is_engagement_code_rejects_initiative_slugs() -> None:
    assert _is_engagement_code("mission-control") is False
    assert _is_engagement_code("storyos") is False
    assert _is_engagement_code("first-person-website") is False
    assert _is_engagement_code("market-scorecard") is False


def test_is_engagement_code_rejects_garbage() -> None:
    assert _is_engagement_code("") is False
    assert _is_engagement_code("ggl") is False
    assert _is_engagement_code("GGL-5168") is False  # uppercase rejected
    assert _is_engagement_code("ggl-XX") is False
    assert _is_engagement_code(None) is False  # type: ignore[arg-type]


def test_build_prompt_engagement_keeps_client_verbs() -> None:
    """Engagement prompts include the client-side verbs (inbound,
    stakeholders) since engagements have a client to communicate with."""
    prompt = _build_prompt(
        transcript="2026-05-12 - Meeting about ggl-5168\nDrew: hi",
        project_context="(no context)",
        project_code="ggl-5168",
        transcript_path=Path("/tmp/t.txt"),
        team=("drew", "tony"),
    )
    assert "inbound:" in prompt
    assert "stakeholders:" in prompt
    assert "things the client said" in prompt


def test_build_prompt_initiative_drops_client_verbs() -> None:
    """Initiative prompts strip inbound + stakeholders from the schema
    since there's no client side; team roster is already known."""
    prompt = _build_prompt(
        transcript="2026-05-14 - Mission Control sync\nDrew: hi",
        project_context="(no context)",
        project_code="mission-control",
        transcript_path=Path("/tmp/t.txt"),
        team=("drew", "tony"),
    )
    # The schema block omits inbound + stakeholders entirely.
    schema_section = prompt.split("# Schema you must produce")[1].split(
        "# Rules"
    )[0]
    assert "inbound:" not in schema_section
    assert "stakeholders:" not in schema_section
    # Internal-specific phrasing is present.
    assert "INTERNAL meeting" in prompt or "internal workstream" in prompt.lower()
    # Decisions/risks/asks remain.
    assert "decisions:" in schema_section
    assert "risks:" in schema_section
    assert "asks:" in schema_section


# ──────────────────────────────────────────────────────────────────────
#  _extract_yaml — fenced code block handling, including truncation
# ──────────────────────────────────────────────────────────────────────


def test_extract_yaml_handles_complete_fenced_block() -> None:
    from cp_engine.plan_from_transcript import _extract_yaml
    response = '```yaml\nprojects:\n  ggl-5168:\n    asks: []\n```'
    out = _extract_yaml(response)
    assert out == 'projects:\n  ggl-5168:\n    asks: []'


def test_extract_yaml_handles_truncated_response_with_open_fence_only() -> None:
    """Real bug from 2026-05-18: a multi-project plan response was
    truncated at max_tokens mid-output. The opening ```yaml fence was
    present but the closing fence wasn't, so the extractor fell through
    to its no-fence branch and returned the raw response with backticks
    intact. yaml.safe_load then threw an opaque column-1 parse error.
    Now the extractor strips the opening fence and returns the partial."""
    from cp_engine.plan_from_transcript import _extract_yaml
    truncated = (
        '```yaml\n'
        'projects:\n'
        '  slt-5175:\n'
        '    inbound:\n'
        '      - text: "Art texted Marcello back from'  # ← chopped here
    )
    out = _extract_yaml(truncated)
    # Backticks must be gone; partial YAML returned (caller decides what
    # to do with it — typically a yaml.safe_load partial parse).
    assert not out.startswith('```')
    assert out.startswith('projects:')
    assert 'slt-5175' in out


def test_extract_yaml_handles_no_fence_at_all() -> None:
    from cp_engine.plan_from_transcript import _extract_yaml
    response = 'projects:\n  ggl-5168: {}\n'
    out = _extract_yaml(response)
    assert out == 'projects:\n  ggl-5168: {}'


def test_extract_yaml_strips_yaml_language_tag_variants() -> None:
    from cp_engine.plan_from_transcript import _extract_yaml
    # Some responses use ``` (no language tag) instead of ```yaml.
    response = '```\nprojects: {}\n```'
    assert _extract_yaml(response) == 'projects: {}'


# ──────────────────────────────────────────────────────────────────────
#  _action_items_to_ask_items — Fathom action_items → record-ask plan
# ──────────────────────────────────────────────────────────────────────


def test_action_items_to_ask_items_emits_one_record_ask_per_item():
    from cp_engine.ingest import _content_hash
    from cp_engine.plan_from_transcript import _action_items_to_ask_items

    action_items = [
        {
            "description": "Confirm ISCI code with Jennifer",
            "assignee": {"name": "Drew", "email": "drew@firstperson.is"},
        },
        {"description": "Send Q3 invoice", "assignee": {}},
    ]
    items = _action_items_to_ask_items(
        code="ggl-5168", action_items=action_items, today_iso="2026-05-27"
    )
    assert len(items) == 2
    assert items[0]["verb"] == "record-ask"
    assert items[0]["text"] == "Confirm ISCI code with Jennifer"
    assert items[0]["who"] == "Drew"
    assert items[0]["date"] == "2026-05-27"
    # Hash matches the ingest recipe exactly — guards against recipe drift
    # that would break the ClickUp ↔ cp round-trip (_write_ask, clickup_propose,
    # ClickUp-close webhook all rely on this exact hash).
    expected_hash_0 = _content_hash("ggl-5168", "record-ask", "Confirm ISCI code with Jennifer")
    assert items[0]["hash"] == expected_hash_0
    # Second item uses a different text under the same code/verb — confirms
    # the recipe is deterministic on text, not just on code/verb.
    expected_hash_1 = _content_hash("ggl-5168", "record-ask", "Send Q3 invoice")
    assert items[1]["hash"] == expected_hash_1


def test_action_items_to_ask_items_skips_empty_descriptions():
    from cp_engine.plan_from_transcript import _action_items_to_ask_items

    items = _action_items_to_ask_items(
        code="ggl-5168",
        action_items=[{"description": ""}, {"description": "  "}, {"description": "Real ask"}],
        today_iso="2026-05-27",
    )
    assert len(items) == 1
    assert items[0]["text"] == "Real ask"
