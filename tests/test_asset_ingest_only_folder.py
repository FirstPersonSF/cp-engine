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


def test_only_folder_partial_match_narrows():
    # "Carol" should select the "Carol Decks" allowed entry (same contains rule
    # _matches_allowlist uses), so the scan narrows to that folder.
    eff = _effective_allowlist("Carol", ("Carol Decks",))
    # effective allowlist must still MATCH files under Carol Decks but nothing else;
    # the simplest correct effective value is ("Carol",) which _matches_allowlist
    # will contains-match against "Carol Decks" segments.
    assert eff == ("Carol",)


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
