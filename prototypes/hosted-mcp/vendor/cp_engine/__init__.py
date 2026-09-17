"""A minimal `cp_engine` for the hosted container.

NOT the real package. The Dockerfile ships `server.py` and `observability.py`
only, so the four wrap-up verbs' call-time
`from cp_engine.<module> import ...` raised ModuleNotFoundError in production
while every local test passed — the test suite runs inside the cp-engine repo,
where the real package is importable.

What ships here is the closure those verbs actually need: the four lint modules
copied VERBATIM, plus two shims (`mc2_db.Tables`, `dates_loop`'s TTL clock)
carrying only the symbols they import. `tests/test_vendor_drift.py` fails if any
copied module or shimmed symbol stops matching its source.
"""
