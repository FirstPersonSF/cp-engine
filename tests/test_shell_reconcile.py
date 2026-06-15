from cp_engine.shell_sync import reconcile_field

# reconcile_field(field, current_value, current_field_state, new_value, now_iso)
#   -> (value_to_store, new_field_state, review_flag_or_None)

def test_proposed_field_updates_freely():
    val, state, flag = reconcile_field(
        "status", "active", "proposed", "dormant", "2026-06-20T00:00:00Z")
    assert val == "dormant"
    assert state == "proposed"
    assert flag is None

def test_confirmed_same_value_is_noop():
    val, state, flag = reconcile_field(
        "status", "active", "confirmed", "active", "2026-06-20T00:00:00Z")
    assert val == "active"
    assert state == "confirmed"
    assert flag is None

def test_confirmed_different_value_keeps_confirmed_and_flags():
    val, state, flag = reconcile_field(
        "status", "active", "confirmed", "dormant", "2026-06-20T00:00:00Z")
    assert val == "active"
    assert state == "confirmed"
    assert flag == {"field": "status", "was": "active",
                    "now": "dormant", "at": "2026-06-20T00:00:00Z"}

def test_absent_field_state_treated_as_proposed():
    val, state, flag = reconcile_field(
        "stage", "first", None, "revised", "2026-06-20T00:00:00Z")
    assert val == "revised"
    assert state == "proposed"
    assert flag is None
