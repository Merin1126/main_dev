from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _trace_dir(project_root: str | Path) -> Path:
    return Path(project_root) / "Gemini_Trace"


def _choose_current_trace(trace_dir: Path) -> Path | None:
    if not trace_dir.exists():
        return None
    today_name = f"gemini_trace_{datetime.now().strftime('%Y%m%d')}.jsonl"
    today_path = trace_dir / today_name
    if today_path.exists():
        return today_path
    candidates = sorted(trace_dir.glob("gemini_trace_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def convert_current_trace_to_md(project_root: str | Path) -> Path:
    trace_dir = _trace_dir(project_root)
    src = _choose_current_trace(trace_dir)
    if not src:
        raise FileNotFoundError("未找到可转换的 Trace 日志（Gemini_Trace/*.jsonl）")

    events: list[dict[str, Any]] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {"event": "parse_error", "raw_line": raw}
        events.append(obj)

    by_event: dict[str, int] = {}
    by_task: dict[str, int] = {}
    for e in events:
        et = _safe_text(e.get("event")) or "unknown"
        tk = _safe_text(e.get("task")) or "unknown"
        by_event[et] = by_event.get(et, 0) + 1
        by_task[tk] = by_task.get(tk, 0) + 1

    out = src.with_suffix(".md")
    lines: list[str] = []
    lines.append(f"# Gemini Trace 可读报告：`{src.name}`")
    lines.append("")
    lines.append("## 概览")
    lines.append(f"- 总事件数：{len(events)}")
    lines.append(f"- 任务分布：{', '.join([f'{k}={v}' for k, v in by_task.items()]) or '无'}")
    lines.append(f"- 事件分布：{', '.join([f'{k}={v}' for k, v in by_event.items()]) or '无'}")
    lines.append("")
    lines.append("## 事件时间线")

    for idx, e in enumerate(events, start=1):
        event = _safe_text(e.get("event")) or "unknown"
        ts = _safe_text(e.get("ts"))
        task = _safe_text(e.get("task"))
        screen = _safe_text(e.get("screen"))
        page = e.get("page_index")
        file_name = _safe_text(e.get("file_name"))
        model = _safe_text(e.get("model_name"))
        session_id = _safe_text(e.get("chat_session_id"))
        lines.append(f"### {idx}. {event}")
        lines.append(f"- 时间：{ts or 'N/A'}")
        lines.append(f"- 任务/页面：{task or 'N/A'} / {screen or 'N/A'}")
        lines.append(f"- 文件与页码：{file_name or 'N/A'} / {page if page is not None else 'N/A'}")
        if model:
            lines.append(f"- 模型：{model}")
        if session_id:
            lines.append(f"- 会话ID：`{session_id}`")
        if event == "request_prepared":
            lines.append(f"- 输入类型：{_safe_text(e.get('input_kind')) or 'N/A'}")
            lines.append("")
            lines.append("#### Prompt")
            lines.append("```text")
            lines.append(_safe_text(e.get("prompt_text")))
            lines.append("```")
            src_text = _safe_text(e.get("source_text"))
            if src_text:
                lines.append("")
                lines.append("#### Source Text")
                lines.append("```text")
                lines.append(src_text)
                lines.append("```")
        elif event == "response_received":
            lines.append(f"- 耗时(ms)：{_safe_text(e.get('elapsed_ms')) or 'N/A'}")
            lines.append(f"- Token摘要：{json.dumps(e.get('usage_summary', {}), ensure_ascii=False)}")
            lines.append("")
            lines.append("#### Response")
            lines.append("```text")
            lines.append(_safe_text(e.get("response_text")))
            lines.append("```")
        elif event == "chat_session_started":
            lines.append(f"- 响应格式：{_safe_text(e.get('response_mime_type')) or 'N/A'}")
            lines.append(f"- 温度：{_safe_text(e.get('temperature')) or 'N/A'}")
            lines.append("")
            lines.append("#### System Instruction")
            lines.append("```text")
            lines.append(_safe_text(e.get("system_instruction")))
            lines.append("```")
        elif event == "chat_session_observed":
            lines.append(f"- create后可见history条数：{_safe_text(e.get('observed_history_count')) or '0'}")
            has_reaction = _safe_text(e.get("has_model_reaction")) or "False"
            lines.append(f"- 是否观察到模型对 system 的显式反应：{has_reaction}")
            reaction = _safe_text(e.get("model_reaction_text"))
            if reaction:
                lines.append("")
                lines.append("#### Observed Model Reaction")
                lines.append("```text")
                lines.append(reaction)
                lines.append("```")
            else:
                lines.append("")
                lines.append("> 注：当前 SDK 的 `chats.create(...)` 通常不会立即产出模型文本；system 生效体现在后续 turn 行为中。")
        elif event == "chat_turn_prepared":
            lines.append(f"- 输入类型：{_safe_text(e.get('input_kind')) or 'chat_text'}")
            lines.append(f"- 发送前history条数：{_safe_text(e.get('history_count_before')) or 'N/A'}")
            lines.append("")
            lines.append("#### Turn Prompt")
            lines.append("```text")
            lines.append(_safe_text(e.get("turn_prompt")))
            lines.append("```")
        elif event == "chat_turn_response":
            lines.append(f"- 耗时(ms)：{_safe_text(e.get('elapsed_ms')) or 'N/A'}")
            lines.append(
                f"- history条数(before -> after)："
                f"{_safe_text(e.get('history_count_before')) or 'N/A'} -> "
                f"{_safe_text(e.get('history_count_after')) or 'N/A'}"
            )
            lines.append(f"- Token摘要：{json.dumps(e.get('usage_summary', {}), ensure_ascii=False)}")
            lines.append("")
            lines.append("#### Turn Response")
            lines.append("```text")
            lines.append(_safe_text(e.get("response_text")))
            lines.append("```")
        elif event == "chat_turn_error":
            lines.append(f"- 错误：{_safe_text(e.get('error'))}")
        elif event == "cache_write":
            lines.append(f"- 缓存类型：{_safe_text(e.get('cache_kind'))}")
            lines.append(f"- 缓存路径：`{_safe_text(e.get('cache_path'))}`")
            lines.append(f"- 文件大小(bytes)：{_safe_text(e.get('cache_file_size'))}")
            lines.append("")
            lines.append("#### 写入内容预览")
            lines.append("```text")
            lines.append(_safe_text(e.get("content_preview")))
            lines.append("```")
        elif event == "request_error":
            lines.append(f"- 错误：{_safe_text(e.get('error'))}")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def delete_current_converted_trace_md(project_root: str | Path) -> Path | None:
    trace_dir = _trace_dir(project_root)
    src = _choose_current_trace(trace_dir)
    if not src:
        return None
    md_path = src.with_suffix(".md")
    if not md_path.exists():
        return None
    md_path.unlink()
    return md_path
