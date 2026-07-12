"""将 OCR / 翻译缓存导出为竖排模版 DOCX。"""
from __future__ import annotations

import os
from typing import Literal

from generator.vertical_historical_docx_generator import vertical_historical_docx_generator as ocr_docx_gen
from generator.vertical_historical_docx_generator import vertical_historical_json_cleaner as ocr_cleaner
from generator.vertical_historical_docx_generator import (
    vertical_historical_translation_docx_generator as translation_docx_gen,
)
from generator.vertical_historical_docx_generator import (
    vertical_historical_translation_json_cleaner as translation_cleaner,
)
from services.cache_index_service import CacheIndexService
from services.cache_service import CacheService
from services.db_service import DbService
from services.document_storage_service import DocumentStorageService
from services.docx_sync_service import DocxSyncService
from utils.jacar_filename import extract_jacar_ref_from_path, parse_jacar_pdf_filename
from utils.reporting import ensure_dir, safe_filename

ExportKind = Literal["ocr", "translation"]
DOCX_OUTPUT_DIRNAME = "Docx_Output"


class VerticalHistoricalDocxExportService:
    def __init__(
        self,
        *,
        project_root: str | None = None,
        cache_service: CacheService | None = None,
        db_service: DbService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.cache_service = cache_service or CacheService()
        self.db_service = db_service or DbService()
        self.storage_service = DocumentStorageService(
            project_root=self.project_root,
            cache_service=self.cache_service,
        )
        self.cache_index_service = CacheIndexService(
            project_root=self.project_root,
            db_service=self.db_service,
        )
        self.docx_sync_service = DocxSyncService(
            project_root=self.project_root,
            cache_service=self.cache_service,
        )
        self._ocr_cache_dir = os.path.join(self.project_root, "OCR_Cache")
        self._translation_cache_dir = os.path.join(self.project_root, "Translation_Cache")
        self._downloads_root = os.path.join(self.project_root, "JACAR_Downloads")

    def default_output_root(self) -> str:
        return os.path.join(self.project_root, DOCX_OUTPUT_DIRNAME)

    def resolve_search_keyword(self, pdf_path: str) -> str:
        abs_pdf = os.path.abspath(pdf_path)
        ref = ""
        parts = parse_jacar_pdf_filename(abs_pdf)
        if parts and parts.ref:
            ref = parts.ref.strip().upper()
        if not ref:
            ref = extract_jacar_ref_from_path(abs_pdf)
        if ref:
            row = self.db_service.fetchone(
                """
                SELECT COALESCE(NULLIF(TRIM(search_keyword), ''), '') AS search_keyword
                FROM documents
                WHERE source = 'jacar' AND UPPER(native_id) = UPPER(?)
                LIMIT 1
                """,
                (ref,),
            )
            if row and str(row["search_keyword"] or "").strip():
                return str(row["search_keyword"]).strip()

        try:
            rel = os.path.relpath(abs_pdf, self._downloads_root)
            segments = rel.split(os.sep)
            if len(segments) > 1 and segments[0] not in {".", ".."}:
                return segments[0]
        except ValueError:
            pass
        return "未分类"

    def resolve_output_docx_path(self, pdf_path: str, kind: ExportKind) -> str:
        bundle = self.storage_service.resolve_bundle_from_pdf(pdf_path)
        if bundle.layout == "bundle_v1":
            artifact = "export_translation_docx" if kind == "translation" else "export_ocr_docx"
            return self.storage_service.resolve_write_path(bundle, artifact)  # type: ignore[arg-type]
        keyword = safe_filename(self.resolve_search_keyword(pdf_path)) or "未分类"
        basename = safe_filename(os.path.splitext(os.path.basename(pdf_path))[0])
        if kind == "translation":
            filename = f"{basename}_译文.docx"
        else:
            filename = f"{basename}.docx"
        out_dir = os.path.join(self.default_output_root(), keyword)
        ensure_dir(out_dir)
        return os.path.join(out_dir, filename)

    def cache_dir_for(self, kind: ExportKind) -> str:
        if kind == "ocr":
            return self._ocr_cache_dir
        return self._translation_cache_dir

    def build_cache_path(self, pdf_path: str, kind: ExportKind) -> str:
        bundle = self.storage_service.resolve_bundle_from_pdf(pdf_path)
        cache_path, _layout = self.storage_service.resolve_read_path_with_fallback(bundle, kind)
        return cache_path

    @staticmethod
    def cache_key_from_path(cache_path: str) -> str:
        return os.path.splitext(os.path.basename(cache_path))[0]

    def resolve_jacar_ref(self, pdf_path: str, cache_path: str) -> str:
        parts = parse_jacar_pdf_filename(pdf_path)
        if parts and parts.ref:
            return parts.ref.strip().upper()
        ref = extract_jacar_ref_from_path(pdf_path)
        if ref:
            return ref
        return self.cache_index_service.resolve_ref_from_cache(cache_path)

    def resolve_pages_for_export(
        self,
        *,
        pdf_path: str,
        kind: ExportKind,
        memory_pages: list[str] | None,
    ) -> tuple[str, list[str], dict]:
        """先落盘再读缓存；若磁盘为空则回退到内存 pages。"""
        cache_path = self.build_cache_path(pdf_path, kind)
        cache_key = self.cache_key_from_path(cache_path)
        meta = {
            "cache_path": cache_path,
            "cache_key": cache_key,
            "source": "disk",
        }

        if memory_pages:
            normalized = ["" if p is None else str(p) for p in memory_pages]
            if any(str(p).strip() for p in normalized):
                cache_path = self.storage_service.resolve_write_path(
                    self.storage_service.resolve_bundle_from_pdf(pdf_path),
                    kind,
                )
                self.cache_service.write_paged_cache(cache_path, normalized)
                disk_pages = self.cache_service.read_paged_cache(cache_path)
                if disk_pages:
                    meta["source"] = "memory_synced_to_disk"
                    return cache_path, disk_pages, meta

        disk_pages = self.cache_service.read_paged_cache(cache_path)
        if disk_pages and any(str(p).strip() for p in disk_pages):
            meta["source"] = "disk"
            return cache_path, disk_pages, meta

        if memory_pages:
            fallback, input_meta = ocr_cleaner.normalize_pages_list(memory_pages)
            if any(str(p).strip() for p in fallback):
                meta["source"] = "memory_fallback"
                meta.update(input_meta)
                return cache_path, fallback, meta

        return cache_path, [], meta

    def build_schema(
        self,
        *,
        kind: ExportKind,
        pdf_path: str,
        memory_pages: list[str] | None = None,
        title: str | None = None,
    ) -> dict:
        cache_path, pages, meta = self.resolve_pages_for_export(
            pdf_path=pdf_path,
            kind=kind,
            memory_pages=memory_pages,
        )
        if not pages or not any(str(p).strip() for p in pages):
            raise ValueError("导出内容为空：请先完成 OCR/翻译并保存后再试。")

        cache_key = self.cache_key_from_path(cache_path)
        jacar_ref = self.resolve_jacar_ref(pdf_path, cache_path)
        input_meta = {"export_source": meta.get("source", "unknown"), "input_format": meta.get("input_format", "paged_v1")}

        if kind == "ocr":
            schema = ocr_cleaner.build_schema_from_pages(
                pages,
                source_path=cache_path,
                cache_key=cache_key,
                title=title,
                db_path=self.db_service.db_path,
                project_root=self.project_root,
                jacar_ref_override=jacar_ref,
                input_meta=input_meta,
            )
            return self.docx_sync_service.attach_block_ids(schema, kind=kind)

        schema = translation_cleaner.build_schema_from_pages(
            pages,
            source_path=cache_path,
            cache_key=cache_key,
            title=title,
            db_path=self.db_service.db_path,
            project_root=self.project_root,
            jacar_ref_override=jacar_ref,
            input_meta=input_meta,
        )
        return self.docx_sync_service.attach_block_ids(schema, kind=kind)

    def export_to_docx(
        self,
        *,
        kind: ExportKind,
        pdf_path: str,
        output_docx: str,
        memory_pages: list[str] | None = None,
        title: str | None = None,
    ) -> dict:
        schema = self.build_schema(kind=kind, pdf_path=pdf_path, memory_pages=memory_pages, title=title)
        if not schema.get("pages"):
            raise ValueError("清洗后没有可导出的段落内容。")

        ensure_dir(os.path.dirname(os.path.abspath(output_docx)))

        if kind == "ocr":
            ocr_docx_gen.build_doc(schema, output_docx, flow_pages=False)
        else:
            translation_docx_gen.build_doc(
                schema,
                output_docx,
                flow_pages=False,
                merge_pages=True,
            )
        source = schema.get("source") or {}
        cache_path = source.get("ocr_cache" if kind == "ocr" else "translation_cache") or self.build_cache_path(pdf_path, kind)
        sync_path = self.docx_sync_service.write_sync_manifest(
            docx_path=output_docx,
            schema=schema,
            kind=kind,
            pdf_path=pdf_path,
            cache_path=cache_path,
        )

        try:
            self.cache_index_service.index_pdf(pdf_path)
        except Exception:
            pass

        return {
            "output_docx": output_docx,
            "page_count": len(schema.get("pages") or []),
            "title": (schema.get("source") or {}).get("title") or "",
            "jacar_ref": (schema.get("source") or {}).get("jacar_ref") or "",
            "search_keyword": self.resolve_search_keyword(pdf_path),
            "sync_path": sync_path,
        }
