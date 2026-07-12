"""对照 JACAR_Downloads 磁盘上的 PDF，刷新 SQLite documents / files 索引。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from services.cache_index_service import CacheIndexService
from services.db_service import DbService
from services.document_rename_service import DocumentRenameService
from utils.jacar_sidecar import sidecar_path_for_pdf
from utils.jacar_filename import parse_jacar_pdf_filename
from utils.reporting import discover_pdf_files, extract_jacar_ref_from_path


def _safe_int_from_image_range(image_range: str) -> int | None:
    if not image_range:
        return None
    nums = [int(x) for x in re.findall(r"\d+", image_range)]
    return max(nums) if nums else None


def _search_keyword_from_pdf(pdf_path: str, downloads_root: str) -> str | None:
    try:
        rel = os.path.relpath(pdf_path, downloads_root)
    except ValueError:
        return None
    parts = rel.split(os.sep)
    if len(parts) > 1 and parts[0] not in {".", ".."}:
        return parts[0]
    return None


@dataclass
class DiskSyncStats:
    pdfs_scanned: int = 0
    documents_updated: int = 0
    documents_created: int = 0
    pdf_paths_updated: int = 0
    unparseable_pdfs: int = 0
    duplicate_ref_on_disk: int = 0
    missing_files_reset: int = 0
    db_rows_relinked: int = 0
    notes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"扫描 PDF：{self.pdfs_scanned} 个",
            f"更新 documents 记录：{self.documents_updated} 条",
            f"新建 documents 记录：{self.documents_created} 条",
            f"更新 PDF 路径 (files)：{self.pdf_paths_updated} 条",
            f"按 Ref 重新关联库记录：{self.db_rows_relinked} 条",
            f"无法解析标准文件名的 PDF：{self.unparseable_pdfs} 个",
            f"磁盘上 Ref 重复（后者覆盖）：{self.duplicate_ref_on_disk} 个",
            f"库中已下载但磁盘缺失（已重置为 discovered）：{self.missing_files_reset} 条",
        ]
        if self.notes:
            lines.append("")
            lines.append("备注：")
            lines.extend(f"  · {n}" for n in self.notes[:20])
            if len(self.notes) > 20:
                lines.append(f"  · … 另有 {len(self.notes) - 20} 条")
        return lines


class DbDiskSyncService:
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
        self.downloads_root = os.path.join(self.project_root, "JACAR_Downloads")

    def sync_from_disk(self, *, fix_missing_files: bool = True) -> DiskSyncStats:
        stats = DiskSyncStats()
        if not os.path.isdir(self.downloads_root):
            stats.notes.append(f"史料目录不存在：{self.downloads_root}")
            return stats

        pdf_paths = discover_pdf_files(self.downloads_root)
        stats.pdfs_scanned = len(pdf_paths)
        ref_to_pdf: dict[str, str] = {}

        for pdf_path in pdf_paths:
            abs_pdf = os.path.abspath(pdf_path)
            parts = parse_jacar_pdf_filename(abs_pdf)
            ref = parts.ref.strip() if parts else extract_jacar_ref_from_path(abs_pdf)
            if not ref:
                stats.unparseable_pdfs += 1
                continue

            ref_key = ref.upper()
            if ref_key in ref_to_pdf and ref_to_pdf[ref_key] != abs_pdf:
                stats.duplicate_ref_on_disk += 1
                stats.notes.append(
                    f"Ref {ref} 对应多份 PDF，已使用：{os.path.basename(abs_pdf)}"
                )
            ref_to_pdf[ref_key] = abs_pdf

            created, updated, path_updated = self._upsert_jacar_pdf_record(
                abs_pdf, parts, ref, downloads_root=self.downloads_root
            )
            if created:
                stats.documents_created += 1
            if updated:
                stats.documents_updated += 1
            if path_updated:
                stats.pdf_paths_updated += 1

        if fix_missing_files:
            stats.db_rows_relinked, reset_count = self._relink_and_prune_db(ref_to_pdf)
            stats.missing_files_reset = reset_count

        try:
            cache_stats = CacheIndexService(project_root=self.project_root).rebuild_all()
            stats.notes.append(
                f"缓存 Ref 索引：{cache_stats.entries_written} 条（孤儿 {cache_stats.orphans_found}）"
            )
        except Exception as exc:
            stats.notes.append(f"缓存 Ref 索引重建失败：{exc}")

        return stats

    def _upsert_jacar_pdf_record(
        self,
        pdf_path: str,
        parts,
        ref: str,
        *,
        downloads_root: str,
    ) -> tuple[bool, bool, bool]:
        """返回 (created, updated, path_updated)。"""
        document_id = self.db_service.make_document_id("jacar", ref)
        existed = self.db_service.fetchone(
            "SELECT document_id, title, metadata_json FROM documents WHERE document_id = ? LIMIT 1",
            (document_id,),
        )
        created = existed is None

        title = parts.title if parts else os.path.splitext(os.path.basename(pdf_path))[0]
        level2 = parts.level2 if parts else None
        parent = parts.parent if parts else None
        repo = parts.repo if parts else None
        scale = _safe_int_from_image_range(parts.image_range) if parts else None
        keyword = _search_keyword_from_pdf(pdf_path, downloads_root)

        existing_meta = None
        if existed and existed["metadata_json"] is not None:
            existing_meta = str(existed["metadata_json"])
        metadata_json_str = existing_meta
        if parts:
            metadata_json_str = DocumentRenameService._merge_metadata_json(existing_meta, parts)
        metadata_obj: dict | None = None
        if metadata_json_str:
            try:
                loaded = json.loads(metadata_json_str)
                if isinstance(loaded, dict):
                    metadata_obj = loaded
            except json.JSONDecodeError:
                metadata_obj = None

        status = "downloaded" if os.path.isfile(pdf_path) else "discovered"
        self.db_service.upsert_document(
            source="jacar",
            native_id=ref,
            title=title,
            repo_name=repo,
            level2_name=level2,
            parent_name=parent,
            scale=scale,
            search_keyword=keyword,
            metadata=metadata_obj,
            status=status,
        )
        old_file = self.db_service.fetchone(
            "SELECT path FROM files WHERE document_id = ? AND kind = 'pdf' LIMIT 1",
            (document_id,),
        )
        path_updated = old_file is None
        if old_file and old_file["path"] is not None:
            old_p = str(old_file["path"])
            old_abs = old_p if os.path.isabs(old_p) else os.path.join(self.project_root, old_p)
            path_updated = os.path.normpath(old_abs) != os.path.normpath(pdf_path)

        sidecar_path = sidecar_path_for_pdf(pdf_path)
        sidecar_arg = sidecar_path if os.path.isfile(sidecar_path) else None
        self.db_service.mark_downloaded_with_files(
            source="jacar",
            native_id=ref,
            pdf_path=pdf_path,
            sidecar_path=sidecar_arg,
        )

        updated = False
        if existed:
            old_title = str(existed["title"] or "")
            updated = (
                old_title != title
                or (metadata_json_str or "") != (str(existed["metadata_json"] or ""))
            )
        return created, updated, path_updated

    def _relink_and_prune_db(self, ref_to_pdf: dict[str, str]) -> tuple[int, int]:
        """将库中 JACAR 记录与磁盘 PDF 对齐；清理硬盘已丢失的 downloaded 记录。"""
        relinked = 0
        reset_count = 0
        rows = self.db_service.fetchall(
            """
            SELECT d.document_id, d.native_id, d.status, f.path AS pdf_path
            FROM documents d
            LEFT JOIN files f ON d.document_id = f.document_id AND f.kind = 'pdf'
            WHERE d.source = 'jacar'
            """
        )
        for row in rows:
            document_id = str(row["document_id"] or "")
            native_id = str(row["native_id"] or "").strip()
            ref_key = native_id.upper()
            status = str(row["status"] or "")
            pdf_path = str(row["pdf_path"] or "").strip()
            resolved = ""
            if pdf_path:
                resolved = pdf_path if os.path.isabs(pdf_path) else os.path.join(self.project_root, pdf_path)
            disk_pdf = ref_to_pdf.get(ref_key)

            if disk_pdf and os.path.isfile(disk_pdf):
                if not resolved or not os.path.isfile(resolved) or os.path.normpath(resolved) != os.path.normpath(
                    disk_pdf
                ):
                    parts = parse_jacar_pdf_filename(disk_pdf)
                    ref = parts.ref.strip() if parts else native_id
                    self._upsert_jacar_pdf_record(
                        disk_pdf, parts, ref, downloads_root=self.downloads_root
                    )
                    relinked += 1
                continue

            if status in {"downloaded", "completed"} and (not resolved or not os.path.isfile(resolved)):
                now = self.db_service.utc_now_iso()
                with self.db_service.transaction():
                    self.db_service.execute(
                        "UPDATE documents SET status = ?, updated_at = ? WHERE document_id = ?",
                        ("discovered", now, document_id),
                    )
                    self.db_service.execute(
                        "DELETE FROM files WHERE document_id = ? AND kind = 'pdf'",
                        (document_id,),
                    )
                reset_count += 1

        return relinked, reset_count
