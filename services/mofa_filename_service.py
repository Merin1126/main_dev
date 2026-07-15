"""Human-readable, machine-stable filenames for MOFA archive artifacts."""
from __future__ import annotations

import os
import re
import unicodedata


MAX_FILENAME_BYTES = 240
_MOFA_ID_RE = re.compile(r"MOFA_[A-Z0-9_-]+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_REPLACEMENTS = str.maketrans(
    {
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
)


def sanitize_mofa_title(title: str) -> str:
    """Return a readable title that is safe on macOS, Windows, and Linux."""
    value = unicodedata.normalize("NFC", str(title or "")).translate(_REPLACEMENTS)
    value = _CONTROL_RE.sub(" ", value)
    value = _SPACE_RE.sub(" ", value).strip(" .")
    return value or "无题史料"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    encoded = value.encode("utf-8")[: max(0, max_bytes)]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip(" .")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def _filename(title: str, native_id: str, marker: str = "") -> str:
    clean_title = sanitize_mofa_title(title)
    clean_id = str(native_id or "").strip().upper() or "MOFA_UNKNOWN"
    suffix = f" [{clean_id}]{marker}.pdf"
    title_budget = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    clean_title = _truncate_utf8(clean_title, title_budget) or "无题史料"
    return f"{clean_title}{suffix}"


def build_mofa_pdf_filename(title: str, native_id: str) -> str:
    """Build ``标题 [MOFA_ID].pdf`` for the archival PDF."""
    return _filename(title, native_id)


def build_mineru_input_filename(title: str, native_id: str) -> str:
    """Build a readable MinerU input name with an embedded stable identity."""
    return _filename(title, native_id, " single-pages")


def build_mineru_input_filename_from_pdf(pdf_path: str) -> str:
    """Derive a single-page input name from an already normalized MOFA PDF."""
    stem = os.path.splitext(os.path.basename(pdf_path))[0].strip() or "document"
    suffix = " single-pages.pdf"
    budget = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    return f"{_truncate_utf8(stem, budget)}{suffix}"


def build_mineru_chunk_filename_from_pdf(
    input_pdf_path: str,
    start_page: int,
    end_page: int,
    *,
    page_number_width: int = 4,
) -> str:
    """Derive ``... single-pages p0001-p0200.pdf`` from a MinerU input PDF."""
    start = int(start_page)
    end = int(end_page)
    if start <= 0 or end < start:
        raise ValueError("invalid MinerU chunk page range")
    width = max(4, int(page_number_width), len(str(end)))
    stem = os.path.splitext(os.path.basename(input_pdf_path))[0].strip() or "document"
    range_marker = f" p{start:0{width}d}-p{end:0{width}d}"
    native_match = _MOFA_ID_RE.search(stem)
    if native_match:
        title = stem[: native_match.start()].rstrip(" [")
        return _filename(
            title,
            native_match.group(0).upper(),
            f" single-pages{range_marker}",
        )
    suffix = f"{range_marker}.pdf"
    budget = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    return f"{_truncate_utf8(stem, budget)}{suffix}"


def extract_mofa_native_id(value: str) -> str:
    """Extract the MOFA ID from an input or MinerU result folder name."""
    match = _MOFA_ID_RE.search(str(value or ""))
    return match.group(0).upper() if match else ""
