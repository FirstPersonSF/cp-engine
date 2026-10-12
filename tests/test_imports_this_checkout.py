"""The suite imports THIS checkout's code, not the shared venv's (#357).

The root `conftest.py` refuses to start a run that would import another
tree's `cp_engine`; this test states the same fact where it can be run on its
own, and covers the webhook too (its tests put `webhook/` on `sys.path`
relative to their own file, so it must resolve inside this checkout).
"""
from __future__ import annotations

import sys
from pathlib import Path


def test_cp_engine_is_imported_from_this_checkout(request):
    import cp_engine

    root = request.config.rootpath.resolve()
    where = Path(cp_engine.__file__).resolve()
    assert where.is_relative_to(root / "src"), (
        f"cp_engine from {where}, not {root / 'src'}"
    )


def test_webhook_modules_resolve_inside_this_checkout(request):
    root = request.config.rootpath.resolve()
    webhook = root / "webhook"
    if str(webhook) not in sys.path:
        sys.path.insert(0, str(webhook))
    import pipeline

    assert Path(pipeline.__file__).resolve().is_relative_to(webhook)
