from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services import CacheService, PdfService


_FENCED_JSON_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", flags=re.DOTALL)


@dataclass
class ReportDocumentEntry:
    pdf_path: str
    pdf_rel_path: str
    folder: str
    page_count: int
    ocr_cache_path: str
    analysis_cache_path: str
    ocr_pages: int
    analysis_pages: int
    ocr_complete: bool
    analysis_complete: bool
    analysis_json_pages: int
    ready: bool
    issues: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "pdf_rel_path": self.pdf_rel_path,
            "folder": self.folder,
            "page_count": self.page_count,
            "ocr_cache_path": self.ocr_cache_path,
            "analysis_cache_path": self.analysis_cache_path,
            "ocr_pages": self.ocr_pages,
            "analysis_pages": self.analysis_pages,
            "ocr_complete": self.ocr_complete,
            "analysis_complete": self.analysis_complete,
            "analysis_json_pages": self.analysis_json_pages,
            "ready": self.ready,
            "issues": self.issues,
        }


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "untitled"


def extract_transcription_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    m = re.search(
        r"<transcription>\s*(.*?)\s*</transcription>",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return (m.group(1) if m else raw).strip()


def clean_possible_fenced_json(text: str) -> str:
    raw = (text or "").strip()
    m = _FENCED_JSON_RE.match(raw)
    if not m:
        return raw
    return (m.group(1) or "").strip()


def parse_analysis_page_json(text: str) -> tuple[dict[str, Any] | None, str]:
    cleaned = clean_possible_fenced_json(text)
    if not cleaned:
        return None, ""
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None, cleaned
    if not isinstance(payload, dict):
        return None, cleaned
    return payload, cleaned


def discover_pdf_files(pdf_root: str) -> list[str]:
    results: list[str] = []
    if not os.path.isdir(pdf_root):
        return results
    for base, _, files in os.walk(pdf_root):
        for name in files:
            if name.lower().endswith(".pdf"):
                results.append(os.path.join(base, name))
    results.sort()
    return results


def build_report_entry(
    *,
    project_root: str,
    pdf_root: str,
    pdf_path: str,
    cache_service: CacheService,
    pdf_service: PdfService,
) -> ReportDocumentEntry:
    ocr_dir = os.path.join(project_root, "OCR_Cache")
    analysis_dir = os.path.join(project_root, "Analysis_Cache")
    ocr_cache_path = cache_service.build_cache_path(pdf_path, ocr_dir)
    analysis_cache_path = cache_service.build_cache_path(pdf_path, analysis_dir)

    page_count = pdf_service.count_pages(pdf_path)
    ocr_pages = cache_service.read_paged_cache(ocr_cache_path)
    analysis_pages = cache_service.read_paged_cache(analysis_cache_path)

    issues: list[str] = []
    if page_count <= 0:
        issues.append("PDF 页数为 0 或无法读取")
    if len(ocr_pages) != page_count:
        issues.append(f"OCR 页数不匹配: {len(ocr_pages)}/{page_count}")
    if len(analysis_pages) != page_count:
        issues.append(f"Analysis 页数不匹配: {len(analysis_pages)}/{page_count}")

    ocr_usable_count = sum(1 for p in ocr_pages if extract_transcription_text(p).strip())
    analysis_usable_count = 0
    analysis_json_pages = 0
    for p in analysis_pages:
        parsed, cleaned = parse_analysis_page_json(p)
        if parsed is not None:
            analysis_json_pages += 1
            analysis_usable_count += 1
        elif cleaned.strip():
            analysis_usable_count += 1

    if len(ocr_pages) == page_count and ocr_usable_count != page_count:
        issues.append(f"OCR 存在空页: 可用 {ocr_usable_count}/{page_count}")
    if len(analysis_pages) == page_count and analysis_usable_count != page_count:
        issues.append(f"Analysis 存在空页: 可用 {analysis_usable_count}/{page_count}")

    ocr_complete = len(ocr_pages) == page_count and ocr_usable_count == page_count
    analysis_complete = len(analysis_pages) == page_count and analysis_usable_count == page_count
    ready = ocr_complete and analysis_complete

    pdf_rel_path = os.path.relpath(pdf_path, project_root)
    try:
        rel_to_pdf_root = os.path.relpath(pdf_path, pdf_root)
    except ValueError:
        rel_to_pdf_root = os.path.basename(pdf_path)
    first_part = rel_to_pdf_root.split(os.sep)[0] if rel_to_pdf_root else ""
    folder = first_part if first_part and first_part not in {".", ".."} else "未分类"
    return ReportDocumentEntry(
        pdf_path=pdf_path,
        pdf_rel_path=pdf_rel_path,
        folder=folder,
        page_count=page_count,
        ocr_cache_path=ocr_cache_path,
        analysis_cache_path=analysis_cache_path,
        ocr_pages=len(ocr_pages),
        analysis_pages=len(analysis_pages),
        ocr_complete=ocr_complete,
        analysis_complete=analysis_complete,
        analysis_json_pages=analysis_json_pages,
        ready=ready,
        issues=issues,
    )


def write_json(path: str, payload: dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_index(index_path: str) -> dict[str, Any]:
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("索引文件格式无效：根节点必须是对象。")
    return data
