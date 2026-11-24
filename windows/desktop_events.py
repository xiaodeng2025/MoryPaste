"""Small messages on the desktop-owned receiver stdout pipe."""
from __future__ import annotations

import re
import threading


PREFIX = "MORYPASTE_EVENT "
_line = re.compile(r"(devices|files|legacy|file-complete|legacy-complete)(?: ([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}))?")
_output_lock = threading.Lock()


def emit(kind: str, task_id: str = "") -> None:
    try:
        with _output_lock:
            print(PREFIX + kind + (" " + task_id if task_id else ""), flush=True)
    except (OSError, ValueError):
        # A closed desktop pipe must never change transfer or pairing results.
        pass


def parse(line: str) -> tuple[str, str] | None:
    match = _line.fullmatch(line.removeprefix(PREFIX)) if line.startswith(PREFIX) else None
    if not match:
        return None
    kind, task_id = match.groups()
    if kind.endswith("-complete") != bool(task_id):
        return None
    return kind, task_id or ""
