"""A minimal `cp_engine` for the hosted container.

NOT the real package. The Dockerfile ships `server.py` and `observability.py`
only, so the four wrap-up verbs' call-time
`from cp_engine.<module> import ...` raised ModuleNotFoundError in production
while every local test passed — the test suite runs inside the cp-engine repo,
where the real package is importable.

What ships here is the closure those verbs actually need: the lint modules
copied VERBATIM, plus shims (each marked `VENDORED` in its first line) carrying
only the symbols the copies import. `prototypes/hosted-mcp/test_vendor_drift.py`
derives both lists from this tree and fails if any copied module or shimmed
symbol stops matching its source, or if a shim references a name it does not
define (#287).
"""
