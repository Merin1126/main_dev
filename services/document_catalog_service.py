"""史料目录：从 SQLite 列出条目、健康探测、重命名与目录导出。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from services.cache_service import CacheService
from services.document_audit_service import DocumentAuditService
from services.document_rename_service import DocumentRenameService, resolve_existing_pdf_path
from services.db_service import DbService
from services.html_preview_service import HtmlPreviewService
from utils.catalog_export import export_catalog_docx, export_catalog_pdf
from utils.jacar_filename import parse_jacar_pdf_filename
from utils.reporting import safe_filename

SortKey = Literal["keyword", "title", "ref", "updated_at"]
SortDir = Literal["asc", "desc"]
HealthTier = Literal["ok", "warn", "error"]
HealthFilter = Literal["全部", "正常", "警告", "严重"]


@dataclass
class CatalogEntry:
    document_id: str
    source: str
    native_id: str
    title: str
    search_keyword: str
    level2_name: str
    parent_name: str
    repo_name: str
    scale: int | None
    status: str
    updated_at: str
    pdf_path: str
    sidecar_path: str
    standard_filename: bool = False
    pdf_ok: bool = False
    sidecar_ok: bool = False
    ocr_ok: bool = False
    analysis_ok: bool = False
    translation_ok: bool = False
    health_note: str = ""

    @property
    def ref(self) -> str:
        return self.native_id

    @property
    def keyword(self) -> str:
        return self.search_keyword or "未分类"

    def health_tier(self) -> HealthTier:
        if not self.pdf_ok:
            return "error"
        if not self.standard_filename:
            return "error"
        if not self.sidecar_ok or not (self.ocr_ok and self.analysis_ok):
            return "warn"
        return "ok"

    def status_icon(self) -> str:
        tier = self.health_tier()
        if tier == "error":
            return "🔴"
        if tier == "warn":
            return "🟡"
        return "🟢"

    def health_status_label(self) -> str:
        tier = self.health_tier()
        if tier == "error":
            return "严重"
        if tier == "warn":
            return "警告"
        return "正常"

    @property
    def is_abnormal(self) -> bool:
        return self.health_tier() != "ok"


class DocumentCatalogService:
    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.db_service = db_service or DbService()
        self.cache_service = CacheService()
        self.rename_service = DocumentRenameService(
            project_root=self.project_root,
            db_service=self.db_service,
        )
        self.audit_service = DocumentAuditService(self.db_service)
        self.html_preview_service = HtmlPreviewService(
            project_root=self.project_root,
            db_service=self.db_service,
        )
        self._ocr_dir = os.path.join(self.project_root, "OCR_Cache")
        self._analysis_dir = os.path.join(self.project_root, "Analysis_Cache")
        self._translation_dir = os.path.join(self.project_root, "Translation_Cache")

    def default_catalog_dir(self) -> str:
        path = os.path.join(self.project_root, "Reports", "catalog")
        os.makedirs(path, exist_ok=True)
        return path

    def list_keywords(self) -> list[str]:
        rows = self.db_service.fetchall(
            """
            SELECT DISTINCT COALESCE(NULLIF(TRIM(search_keyword), ''), '未分类') AS kw
            FROM documents
            WHERE source = 'jacar'
            ORDER BY kw
            """
        )
        return [str(r["kw"]) for r in rows]

    def fetch_entries(
        self,
        *,
        search_text: str = "",
        keyword_filter: str = "全部",
        sort_key: SortKey = "keyword",
        sort_dir: SortDir = "asc",
        probe_health: bool = True,
    ) -> list[CatalogEntry]:
        rows = self.db_service.fetchall(
            """
            SELECT
                d.document_id,
                d.source,
                d.native_id,
                COALESCE(d.title, '') AS title,
                COALESCE(NULLIF(TRIM(d.search_keyword), ''), '未分类') AS search_keyword,
                COALESCE(d.level2_name, '') AS level2_name,
                COALESCE(d.parent_name, '') AS parent_name,
                COALESCE(d.repo_name, '') AS repo_name,
                d.scale,
                d.status,
                d.updated_at,
                fp.path AS pdf_path,
                fs.path AS sidecar_path
            FROM documents d
            LEFT JOIN files fp ON fp.document_id = d.document_id AND fp.kind = 'pdf'
            LEFT JOIN files fs ON fs.document_id = d.document_id AND fs.kind = 'sidecar'
            WHERE d.source = 'jacar' AND d.status != 'failed'
            """
        )
        entries: list[CatalogEntry] = []
        needle = (search_text or "").strip().lower()
        for row in rows:
            entry = CatalogEntry(
                document_id=str(row["document_id"] or ""),
                source=str(row["source"] or ""),
                native_id=str(row["native_id"] or ""),
                title=str(row["title"] or ""),
                search_keyword=str(row["search_keyword"] or "未分类"),
                level2_name=str(row["level2_name"] or ""),
                parent_name=str(row["parent_name"] or ""),
                repo_name=str(row["repo_name"] or ""),
                scale=int(row["scale"]) if row["scale"] is not None else None,
                status=str(row["status"] or ""),
                updated_at=str(row["updated_at"] or ""),
                pdf_path=str(row["pdf_path"] or ""),
                sidecar_path=str(row["sidecar_path"] or ""),
            )
            if keyword_filter and keyword_filter != "全部" and entry.search_keyword != keyword_filter:
                continue
            if needle and not self._matches_search(entry, needle):
                continue
            if probe_health:
                self._probe_health(entry)
            entries.append(entry)

        reverse = sort_dir == "desc"
        if sort_key == "title":
            entries.sort(key=lambda e: (e.title.lower(), e.native_id), reverse=reverse)
        elif sort_key == "ref":
            entries.sort(key=lambda e: e.native_id.upper(), reverse=reverse)
        elif sort_key == "updated_at":
            entries.sort(key=lambda e: e.updated_at, reverse=reverse)
        else:
            entries.sort(
                key=lambda e: (e.search_keyword, e.title.lower(), e.native_id),
                reverse=reverse,
            )
        return entries

    @staticmethod
    def matches_health_filter(entry: CatalogEntry, health_filter: str) -> bool:
        if health_filter == "全部":
            return True
        tier = entry.health_tier()
        if health_filter == "正常":
            return tier == "ok"
        if health_filter == "警告":
            return tier == "warn"
        if health_filter == "严重":
            return tier == "error"
        return True

    def reprobe_entries(self, entries: list[CatalogEntry]) -> None:
        for entry in entries:
            self._probe_health(entry)

    @staticmethod
    def count_health_tiers(entries: list[CatalogEntry]) -> dict[str, int]:
        counts = {"ok": 0, "warn": 0, "error": 0}
        for entry in entries:
            counts[entry.health_tier()] += 1
        return counts

    @staticmethod
    def _matches_search(entry: CatalogEntry, needle: str) -> bool:
        hay = " ".join(
            [
                entry.title,
                entry.native_id,
                entry.search_keyword,
                entry.level2_name,
                entry.parent_name,
                entry.repo_name,
                os.path.basename(entry.pdf_path),
            ]
        ).lower()
        return needle in hay

    def _resolve_path(self, path: str) -> str:
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)

    def _probe_health(self, entry: CatalogEntry) -> None:
        pdf_resolved = ""
        if entry.pdf_path:
            pdf_resolved = self._resolve_path(entry.pdf_path)
            if not os.path.isfile(pdf_resolved):
                pdf_resolved = resolve_existing_pdf_path(pdf_resolved) or ""
        if not pdf_resolved and entry.native_id:
            downloads = os.path.join(self.project_root, "JACAR_Downloads")
            ref = entry.native_id.upper()
            if os.path.isdir(downloads):
                for base, _, names in os.walk(downloads):
                    for name in names:
                        if not name.lower().endswith(".pdf"):
                            continue
                        if ref in name.upper():
                            pdf_resolved = os.path.join(base, name)
                            break
                    if pdf_resolved:
                        break

        entry.pdf_path = pdf_resolved or entry.pdf_path
        entry.pdf_ok = bool(pdf_resolved and os.path.isfile(pdf_resolved))
        entry.standard_filename = bool(
            pdf_resolved and parse_jacar_pdf_filename(pdf_resolved) is not None
        )

        side = self._resolve_path(entry.sidecar_path)
        if entry.pdf_ok and not os.path.isfile(side):
            side = os.path.splitext(pdf_resolved)[0] + ".json"
        entry.sidecar_ok = bool(side and os.path.isfile(side))
        entry.sidecar_path = side if entry.sidecar_ok else entry.sidecar_path

        notes: list[str] = []
        if not entry.pdf_ok:
            notes.append("无 PDF")
        elif not entry.standard_filename:
            notes.append("非标准文件名")
        if entry.pdf_ok:
            try:
                entry.ocr_ok = os.path.isfile(
                    self.cache_service.build_cache_path(pdf_resolved, self._ocr_dir)
                )
                entry.analysis_ok = os.path.isfile(
                    self.cache_service.build_cache_path(pdf_resolved, self._analysis_dir)
                )
                entry.translation_ok = os.path.isfile(
                    self.cache_service.build_cache_path(pdf_resolved, self._translation_dir)
                )
            except OSError:
                pass
        if not entry.sidecar_ok:
            notes.append("无 sidecar JSON")
        entry.health_note = "；".join(notes)

    def rename_entry_title(self, entry: CatalogEntry, new_title: str):
        return self.rename_entry_metadata(entry, title=new_title)

    def rename_entry_metadata(
        self,
        entry: CatalogEntry,
        *,
        title: str | None = None,
        level2: str | None = None,
        parent: str | None = None,
        repo: str | None = None,
    ):
        pdf = entry.pdf_path
        if not pdf or not os.path.isfile(pdf):
            pdf = resolve_existing_pdf_path(pdf or entry.native_id) or ""
        if not pdf:
            from services.document_rename_service import DocumentRenameResult

            return DocumentRenameResult(False, "无法定位 PDF 文件。", old_pdf_path=entry.pdf_path)
        return self.rename_service.rename_metadata(
            pdf,
            title=title,
            level2=level2,
            parent=parent,
            repo=repo,
            audit_source="catalog_ui",
        )

    def fetch_audit_logs(self, entry: CatalogEntry, *, limit: int = 30) -> list[dict]:
        return self.audit_service.fetch_for_document(entry.document_id, limit=limit)

    def format_audit_logs(self, entry: CatalogEntry, *, limit: int = 30) -> str:
        records = self.fetch_audit_logs(entry, limit=limit)
        return self.audit_service.format_log_lines(records)

    def generate_html_preview(
        self,
        *,
        enrich: bool = False,
        enrich_cat: str | None = None,
        release: bool = False,
    ) -> str:
        return self.html_preview_service.generate_report_html(
            enrich=enrich,
            enrich_cat=enrich_cat,
            release=release,
        )

    def export_catalog(
        self,
        entries: list[CatalogEntry],
        *,
        fmt: Literal["docx", "pdf", "both"],
        output_dir: str | None = None,
        filter_summary: str = "",
    ) -> dict[str, str]:
        out_dir = os.path.abspath(output_dir or self.default_catalog_dir())
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = safe_filename(f"史料目录_{ts}")
        result: dict[str, str] = {}
        rows = [self._entry_to_export_row(e) for e in entries]
        meta = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(entries),
            "filter_summary": filter_summary or "全部 JACAR 条目",
        }
        if fmt in {"docx", "both"}:
            path = os.path.join(out_dir, f"{base}.docx")
            export_catalog_docx(path, rows=rows, meta=meta)
            result["docx"] = path
        if fmt in {"pdf", "both"}:
            path = os.path.join(out_dir, f"{base}.pdf")
            export_catalog_pdf(path, rows=rows, meta=meta)
            result["pdf"] = path
        return result

    def _entry_to_export_row(self, entry: CatalogEntry) -> dict[str, Any]:
        pdf_rel = entry.pdf_path
        try:
            if pdf_rel:
                pdf_rel = os.path.relpath(pdf_rel, self.project_root)
        except ValueError:
            pass
        cache_flags = []
        if entry.ocr_ok:
            cache_flags.append("OCR")
        if entry.analysis_ok:
            cache_flags.append("Ana")
        if entry.translation_ok:
            cache_flags.append("Trans")
        return {
            "keyword": entry.search_keyword,
            "ref": entry.native_id,
            "title": entry.title,
            "level2": entry.level2_name,
            "parent": entry.parent_name,
            "repo": entry.repo_name,
            "scale": entry.scale if entry.scale is not None else "",
            "status": entry.status,
            "pdf": pdf_rel or "",
            "sidecar": "有" if entry.sidecar_ok else "",
            "cache": " ".join(cache_flags) if cache_flags else "",
            "note": entry.health_note,
        }
