from cp_engine.asset_ingest import _effective_allowlist


def test_none_only_folder_returns_configured_unchanged():
    assert _effective_allowlist(None, ("Carol Decks", "Client Assets")) == (
        "Carol Decks",
        "Client Assets",
    )
    assert _effective_allowlist(None, ()) == ()


def test_only_folder_in_allowlist_narrows_to_it():
    assert _effective_allowlist("Carol Decks", ("Carol Decks", "Client Assets")) == (
        "Carol Decks",
    )


def test_only_folder_fragment_not_permitted():
    # "Carol" is a mere FRAGMENT of allowed "Carol Decks" — permitting it would
    # WIDEN the allowlist (it would match "Carol Photos", "Carolina HR", …), so
    # it must NOT be permitted → match-nothing sentinel.
    from cp_engine.asset_ingest import _matches_allowlist, FileRef

    eff = _effective_allowlist("Carol", ("Carol Decks",))
    assert eff != ()  # NOT () which means "all"
    f = FileRef(
        source="drive",
        id="1",
        name="x",
        mime_type=None,
        size=None,
        modified=None,
        folder_path=("Carol Decks",),
    )
    assert _matches_allowlist(f, eff) is False


def test_only_folder_super_name_permitted():
    # A super-name that CONTAINS an allowed entry stays subset-safe → permitted.
    assert _effective_allowlist("Carol Decks Archive", ("Carol Decks",)) == (
        "Carol Decks Archive",
    )


def test_effective_is_subset_of_original_never_widens():
    from cp_engine.asset_ingest import _matches_allowlist, FileRef

    configured = ("Carol Decks",)
    eff = _effective_allowlist("Carol", configured)  # a fragment → must not widen
    # segments the ORIGINAL allowlist does NOT match must also be unmatched by effective
    for seg in ("Carol Photos", "Carolina Confidential", "Caroline HR"):
        f = FileRef(
            source="drive",
            id="1",
            name="x",
            mime_type=None,
            size=None,
            modified=None,
            folder_path=(seg,),
        )
        if not _matches_allowlist(f, configured):
            assert not _matches_allowlist(f, eff), f"effective widened to {seg!r}"


def test_only_folder_not_permitted_scans_nothing():
    eff = _effective_allowlist("Secret", ("Carol Decks",))
    # must match NOTHING — assert it's a sentinel that no real folder segment contains
    # (NOT () which would mean "all"). Assert eff != () and eff is the match-nothing sentinel.
    assert eff != ()
    # the sentinel matches no real segment:
    from cp_engine.asset_ingest import _matches_allowlist, FileRef

    f = FileRef(
        source="drive",
        id="1",
        name="x",
        mime_type=None,
        size=None,
        modified=None,
        folder_path=("Carol Decks",),
    )
    assert _matches_allowlist(f, eff) is False


def test_empty_configured_means_all_so_only_folder_applies_alone():
    # configured () = "all allowed"; only_folder alone then applies.
    assert _effective_allowlist("Anything", ()) == ("Anything",)
