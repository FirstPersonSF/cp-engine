"""Fail-loudly helpers (architecture plan step 3).

WHY THIS EXISTS. Silent failure is attached to 37 of cp's 130 recorded
defects — #194 discarded 1,375 bullets over three months while every run
logged success. The common shape: a best-effort ``except`` degrades to
``logger.warning``, and outside ``cxp sync`` (the only command that installs
a handler, `sync._WarningCounter`, #197/#212) the CLI configures no logging
at all, so the warning is discarded unread. The run then reports success.

Two tools, one rule — *whoever gets the result must be able to see the
failure*:

- `Warnings` — an explicit, append-only list a function returns (or attaches
  to its result) so the caller renders it. Prefer this when you own the
  result shape.
- `captured_warnings()` — a context manager that collects every WARNING+
  record from the ``cp_engine`` logger for the duration of a CLI command, so
  a command whose helpers only ``logger.warning`` can still print what went
  wrong and exit nonzero-or-flagged. It never suppresses the records.

`print_warnings` is the one renderer: it writes to stderr (``print`` is what
reaches the user in this codebase; see project_cp_engine_print_vs_logger).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO


class Warnings(list):
    """A list of human-readable failure notes carried back to the caller."""

    def add(self, where: str, exc: BaseException | str) -> str:
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        note = f"{where}: {text}"
        self.append(note)
        return note


class _Collector(logging.Handler):
    MAX_RETAINED = 50

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.count += 1
            if len(self.messages) < self.MAX_RETAINED:
                self.messages.append(f"[{record.name}] {record.getMessage()}")
        except Exception:  # noqa: BLE001 — a broken collector must not break the command
            self.count += 0

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802,D102
        return None


@contextmanager
def captured_warnings(logger_name: str = "cp_engine") -> Iterator[_Collector]:
    """Collect WARNING+ records emitted under ``logger_name`` for one command."""
    collector = _Collector()
    lg = logging.getLogger(logger_name)
    lg.addHandler(collector)
    try:
        yield collector
    finally:
        lg.removeHandler(collector)


def print_warnings(
    messages: list[str] | tuple[str, ...],
    *,
    count: int | None = None,
    stream: TextIO | None = None,
    label: str = "warnings",
) -> None:
    """Render failure notes to stderr. No-op when there are none."""
    total = count if count is not None else len(messages)
    if not total:
        return
    out = stream or sys.stderr
    print(f"⚠ {total} {label}:", file=out)
    for m in messages:
        print(f"  - {m}", file=out)
    if total > len(messages):
        print(f"  … and {total - len(messages)} more", file=out)
