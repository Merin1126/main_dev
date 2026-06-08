"""OCR 页级缓存文本：layout_analysis 与 transcription 拆分与合并。"""
from __future__ import annotations

import re

_LAYOUT_RE = re.compile(
    r"<layout_analysis>\s*(.*?)\s*</layout_analysis>",
    flags=re.IGNORECASE | re.DOTALL,
)
_TRANSCRIPTION_RE = re.compile(
    r"<transcription>\s*(.*?)\s*</transcription>",
    flags=re.IGNORECASE | re.DOTALL,
)


def page_has_ocr_xml_structure(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_TRANSCRIPTION_RE.search(raw) or _LAYOUT_RE.search(raw))


def extract_layout_analysis(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _LAYOUT_RE.search(raw)
    if not match:
        return ""
    return (match.group(1) or "").strip()


def extract_transcription_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _TRANSCRIPTION_RE.search(raw)
    if not match:
        return raw
    return (match.group(1) or "").strip()


def compose_ocr_page_xml(*, layout: str, transcription: str) -> str:
    layout_body = (layout or "").strip()
    trans_body = (transcription or "").strip()
    return (
        "<layout_analysis>\n"
        f"{layout_body}\n"
        "</layout_analysis>\n\n"
        "<transcription>\n"
        f"{trans_body}\n"
        "</transcription>"
    )
