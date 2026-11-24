"""Bounded, read-only projection of existing diagnostic events for the desktop UI."""
from __future__ import annotations

import json
from pathlib import Path


def recent_receipts(directory: Path, limit: int = 6) -> list[dict]:
    records = []
    remaining = 4 * 1024 * 1024
    for path in sorted(directory.glob("windows-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if remaining <= 0:
            break
        try:
            with path.open("rb") as stream:
                size = path.stat().st_size
                start = max(0, size - remaining)
                stream.seek(start)
                if start:
                    stream.readline()
                payload = stream.read(remaining)
            remaining -= len(payload)
            for line in payload.splitlines():
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append(record)
                except (ValueError, UnicodeError):
                    continue
        except OSError:
            continue
    records.sort(key=lambda r: r.get("timestamp", ""))
    tasks = {}
    successful = {}
    for record in records:
        task = record.get("task_id")
        if not task:
            continue
        index = record.get("image_index", 1)
        key = (task, index)
        info = tasks.setdefault(key, {"taskId": task, "imageIndex": index})
        for source, target in (("device_name", "device"), ("device", "device"), ("mime", "mime"),
                               ("backend", "backend"), ("bytes_received", "bytes"), ("actual_bytes", "bytes")):
            if record.get(source) is not None:
                info[target] = record[source]
        if record.get("event") == "RECEIVE_FINISHED" and record.get("status") == "ok":
            successful[key] = {**info, "timestamp": record.get("timestamp"), "imageCount": record.get("image_count", 1)}
            if record.get("receive_mode") == "batch":
                successful[key]["receiveMode"] = "batch"
    return sorted(successful.values(), key=lambda r: r.get("timestamp", ""), reverse=True)[:limit]
