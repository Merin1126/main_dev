from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from docx import Document

from services.cache_service import CacheService
from utils.ocr_page_text import (
    compose_ocr_page_xml,
    extract_layout_analysis,
    page_has_ocr_xml_structure,
)

DocxSyncKind = Literal["ocr", "translation"]

BLOCK_MARKER_RE = re.compile(r"\[\[HRS_BLOCK:([A-Za-z0-9_.:-]+)\]\]")


@dataclass
class DocxSyncPreview:
    docx_path: str
    sync_path: str
    changed_blocks: int
    changed_pages: list[int]
    missing_blocks: int
    unmapped_paragraphs: int
    updated_pages: list[str]


class DocxSyncService:
    """Write/read DOCX block markers and sync edited DOCX text back to paged caches."""

    def __init__(self, *, project_root: str, cache_service: CacheService | None = None) -> None:
        self.project_root = os.path.abspath(project_root)
        self.cache_service = cache_service or CacheService()

    @staticmethod
    def sync_path_for_docx(docx_path: str) -> str:
        return f"{docx_path}.sync.json"

    @staticmethod
    def marker_for_block(block_id: str) -> str:
        return f"[[HRS_BLOCK:{block_id}]]"

    def attach_block_ids(self, schema: dict, *, kind: DocxSyncKind) -> dict:
        pages = schema.get("pages") or []
        if not isinstance(pages, list):
            return schema
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            source_page_index = int(page.get("source_page_index", page_index))
            blocks = page.get("paragraphs") or []
            if not isinstance(blocks, list):
                continue
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict) or block.get("role") == "page_number":
                    continue
                block_id = f"p{source_page_index + 1:04d}-b{block_index + 1:04d}"
                block["block_id"] = block_id
                block["sync_kind"] = kind
                block["sync_page_index"] = source_page_index
                block["sync_block_index"] = block_index
        return schema

    def write_sync_manifest(
        self,
        *,
        docx_path: str,
        schema: dict,
        kind: DocxSyncKind,
        pdf_path: str,
        cache_path: str,
    ) -> str:
        blocks: dict[str, dict] = {}
        exported_blocks, _unmapped = self._read_docx_blocks(docx_path)
        pages = schema.get("pages") or []
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            source_page_index = int(page.get("source_page_index", page_index))
            for block_index, block in enumerate(page.get("paragraphs") or []):
                if not isinstance(block, dict):
                    continue
                block_id = str(block.get("block_id") or "").strip()
                if not block_id:
                    continue
                source_text = str(block.get("text") or "")
                blocks[block_id] = {
                    "page_index": source_page_index,
                    "block_index": block_index,
                    "source_text": source_text,
                    "display_text": exported_blocks.get(block_id, source_text),
                }
        payload = {
            "schema_version": 2,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "pdf_path": pdf_path,
            "cache_path": cache_path,
            "docx_path": docx_path,
            "blocks": blocks,
        }
        sync_path = self.sync_path_for_docx(docx_path)
        with open(sync_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return sync_path

    def preview_docx_changes(
        self,
        *,
        docx_path: str,
        cache_path: str,
        kind: DocxSyncKind,
    ) -> DocxSyncPreview:
        sync_path = self.sync_path_for_docx(docx_path)
        if not os.path.isfile(docx_path):
            raise FileNotFoundError(docx_path)
        if not os.path.isfile(sync_path):
            raise FileNotFoundError(
                f"未找到 DOCX 同步索引：{sync_path}\n请先重新确认并导出文档。"
            )

        with open(sync_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        blocks = manifest.get("blocks") or {}
        if not isinstance(blocks, dict) or not blocks:
            raise ValueError("DOCX 同步索引为空，请重新导出文档。")
        schema_version = int(manifest.get("schema_version") or 1)
        if schema_version < 2 or any(
            "source_text" not in meta or "display_text" not in meta
            for meta in blocks.values()
            if isinstance(meta, dict)
        ):
            raise ValueError("DOCX 同步索引版本过旧。请重新确认并导出文档后，再编辑新 DOCX 并同步。")
        manifest_kind = str(manifest.get("kind") or "")
        if manifest_kind and manifest_kind != kind:
            raise ValueError(f"DOCX 类型不匹配：索引为 {manifest_kind}，当前页面为 {kind}。")

        docx_blocks, unmapped = self._read_docx_blocks(docx_path)
        current_pages = self.cache_service.read_paged_cache(cache_path)
        max_page_index = max(int(meta.get("page_index", 0)) for meta in blocks.values())
        if len(current_pages) <= max_page_index:
            current_pages.extend([""] * (max_page_index + 1 - len(current_pages)))

        page_to_blocks: dict[int, list[tuple[int, str, str]]] = {}
        changed_blocks = 0
        changed_page_indices: set[int] = set()
        missing_blocks = 0
        for block_id, meta in blocks.items():
            page_index = int(meta.get("page_index", 0))
            block_index = int(meta.get("block_index", 0))
            source_text = str(meta.get("source_text", meta.get("text", "")) or "")
            display_text = str(meta.get("display_text", meta.get("text", source_text)) or "")
            if block_id in docx_blocks:
                edited_text = docx_blocks[block_id].strip()
                if edited_text != display_text.strip():
                    changed_blocks += 1
                    changed_page_indices.add(page_index)
                    current = self._apply_docx_edit_to_source(
                        source_text=source_text,
                        display_text=display_text,
                        edited_text=edited_text,
                    )
                else:
                    current = source_text
            else:
                current = source_text
                missing_blocks += 1
            page_to_blocks.setdefault(page_index, []).append((block_index, block_id, current))

        updated_pages = list(current_pages)
        changed_pages: list[int] = []
        for page_index, items in page_to_blocks.items():
            if page_index not in changed_page_indices:
                continue
            items.sort(key=lambda x: x[0])
            page_text = "\n\n".join(text for _idx, _bid, text in items if text.strip())
            if kind == "ocr":
                raw = str(current_pages[page_index] if page_index < len(current_pages) else "")
                if page_has_ocr_xml_structure(raw):
                    page_text = compose_ocr_page_xml(
                        layout=extract_layout_analysis(raw),
                        transcription=page_text,
                    )
            if page_index >= len(updated_pages):
                updated_pages.extend([""] * (page_index + 1 - len(updated_pages)))
            if updated_pages[page_index] != page_text:
                updated_pages[page_index] = page_text
                changed_pages.append(page_index)

        return DocxSyncPreview(
            docx_path=docx_path,
            sync_path=sync_path,
            changed_blocks=changed_blocks,
            changed_pages=changed_pages,
            missing_blocks=missing_blocks,
            unmapped_paragraphs=unmapped,
            updated_pages=updated_pages,
        )

    def apply_docx_changes(
        self,
        *,
        preview: DocxSyncPreview,
        cache_path: str,
    ) -> str:
        return self.cache_service.write_paged_cache(cache_path, preview.updated_pages)

    def _read_docx_blocks(self, docx_path: str) -> tuple[dict[str, str], int]:
        doc = Document(docx_path)
        blocks: dict[str, str] = {}
        unmapped = 0
        for paragraph in doc.paragraphs:
            raw_text = paragraph.text or ""
            match = BLOCK_MARKER_RE.search(raw_text)
            if not match:
                if raw_text.strip():
                    unmapped += 1
                continue
            block_id = match.group(1)
            text = BLOCK_MARKER_RE.sub("", raw_text).strip()
            blocks[block_id] = text
        return blocks, unmapped

    def _apply_docx_edit_to_source(
        self,
        *,
        source_text: str,
        display_text: str,
        edited_text: str,
    ) -> str:
        """Apply only user edits from DOCX display text to the pre-export source text."""
        if edited_text == display_text:
            return source_text
        if len(source_text) != len(display_text):
            return self._apply_docx_edit_with_boundary_map(source_text, display_text, edited_text)

        pieces: list[str] = []
        cursor = 0
        matcher = SequenceMatcher(a=display_text, b=edited_text, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            pieces.append(source_text[cursor:i1])
            if tag in {"replace", "insert"}:
                pieces.append(edited_text[j1:j2])
            cursor = i2
        pieces.append(source_text[cursor:])
        return "".join(pieces)

    def _apply_docx_edit_with_boundary_map(
        self,
        source_text: str,
        display_text: str,
        edited_text: str,
    ) -> str:
        source_to_display = self._build_display_to_source_boundaries(source_text, display_text)
        pieces: list[str] = []
        cursor = 0
        matcher = SequenceMatcher(a=display_text, b=edited_text, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            s1 = source_to_display(i1)
            s2 = source_to_display(i2)
            pieces.append(source_text[cursor:s1])
            if tag in {"replace", "insert"}:
                pieces.append(edited_text[j1:j2])
            cursor = s2
        pieces.append(source_text[cursor:])
        return "".join(pieces)

    def _build_display_to_source_boundaries(self, source_text: str, display_text: str):
        if not display_text:
            return lambda _idx: 0

        source_norm = [self._char_key(ch) for ch in source_text]
        display_norm = [self._char_key(ch) for ch in display_text]
        char_map: dict[int, int] = {}
        matcher = SequenceMatcher(a=source_norm, b=display_norm, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(j2 - j1):
                    char_map[j1 + offset] = i1 + offset
            elif tag == "replace" and (i2 - i1) == (j2 - j1):
                for offset in range(j2 - j1):
                    char_map[j1 + offset] = i1 + offset

        def map_boundary(display_index: int) -> int:
            if display_index <= 0:
                return 0
            if display_index >= len(display_text):
                return len(source_text)
            if display_index in char_map:
                return char_map[display_index]
            before = [idx for idx in char_map if idx < display_index]
            if before:
                nearest = max(before)
                return min(len(source_text), char_map[nearest] + (display_index - nearest))
            after = [idx for idx in char_map if idx > display_index]
            if after:
                nearest = min(after)
                return max(0, char_map[nearest] - (nearest - display_index))
            return round(display_index * len(source_text) / len(display_text))

        return map_boundary

    @staticmethod
    def _char_key(ch: str) -> str:
        vertical_map = {
            "︵": "(",
            "︶": ")",
            "︷": "{",
            "︸": "}",
            "︹": "[",
            "︺": "]",
            "︻": "【",
            "︼": "】",
            "﹁": "「",
            "﹂": "」",
            "﹃": "『",
            "﹄": "』",
            "︑": "、",
            "︒": "。",
            "︐": "，",
            "︔": "；",
            "︰": "：",
            "︕": "！",
            "︖": "？",
        }
        return vertical_map.get(ch, ch)
