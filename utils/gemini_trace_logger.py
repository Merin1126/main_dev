from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_TRACE_LOCK = threading.Lock()


def _trace_dir(project_root: str) -> Path:
    path = Path(project_root) / "Gemini_Trace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _trace_file_path(project_root: str) -> Path:
    day = datetime.now().strftime("%Y%m%d")
    return _trace_dir(project_root) / f"gemini_trace_{day}.jsonl"


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def append_trace_event(project_root: str, event: dict[str, Any]) -> str:
    """Append one JSONL trace event and return trace file path."""
    out = _trace_file_path(project_root)
    payload = dict(event)
    payload.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with _TRACE_LOCK:
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return str(out)


def build_cache_event(
    *,
    project_root: str,
    screen_name: str,
    task_name: str,
    pdf_path: str | None,
    page_index: int | None,
    cache_path: str,
    cache_kind: str,
    content: str,
    include_full_text: bool,
) -> dict[str, Any]:
    preview = content if include_full_text else content[:1200]
    return {
        "event": "cache_write",
        "screen": screen_name,
        "task": task_name,
        "selected_pdf_path": pdf_path,
        "page_index": page_index,
        "cache_kind": cache_kind,
        "cache_path": cache_path,
        "cache_file_size": _safe_size(cache_path),
        "content_preview": preview,
        "content_length": len(content),
        "full_text_logged": include_full_text,
        "trace_project_root": project_root,
    }
