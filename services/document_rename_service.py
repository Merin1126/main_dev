"""史料 PDF 标题重命名：同步本地缓存、SQLite、Database_JSON 与汇报导出文件。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from services.cache_service import CacheService
from services.db_service import DbService
from services.document_audit_service import DocumentAuditService
from services.report_service import ReportService
from utils.jacar_filename import (
    JacarFilenameParts,
    parse_jacar_pdf_filename,
    refs_equal,
)
from utils.jacar_sidecar import patch_jacar_download_sidecar, sidecar_path_for_pdf
from utils.reporting import extract_jacar_ref_from_path


def resolve_existing_pdf_path(pdf_path: str) -> str | None:
    """解析磁盘上真实 PDF 路径（精确匹配；失败时在同目录按 JACAR 编号匹配）。"""
    candidate = os.path.abspath(pdf_path)
    if os.path.isfile(candidate):
        return candidate
    directory = os.path.dirname(candidate)
    ref = extract_jacar_ref_from_path(candidate)
    if not ref or not os.path.isdir(directory):
        return None
    ref_upper = ref.upper()
    hits: list[str] = []
    for name in os.listdir(directory):
        if not name.lower().endswith(".pdf"):
            continue
        if ref_upper in name.upper():
            hits.append(os.path.join(directory, name))
    if len(hits) == 1:
        return os.path.abspath(hits[0])
    if not hits:
        return None
    base_hint = os.path.basename(candidate).lower()
    for path in sorted(hits):
        if os.path.basename(path).lower() == base_hint:
            return os.path.abspath(path)
    return os.path.abspath(sorted(hits)[0])


@dataclass
class DocumentRenameResult:
    success: bool
    message: str
    old_pdf_path: str = ""
    new_pdf_path: str = ""
    moved_cache_files: list[str] = field(default_factory=list)
    renamed_exports: list[str] = field(default_factory=list)
    updated_json_files: int = 0
    sidecar_json_renamed: bool = False
    db_path_updated: bool = False
    db_document_updated: bool = False
    db_sidecar_path_updated: bool = False


class DocumentRenameService:
    """仅改「」内标题（及拼装文件名其它固定段），JACAR Ref 必须不变。"""

    def __init__(
        self,
        *,
        project_root: str | None = None,
        cache_service: CacheService | None = None,
        db_service: DbService | None = None,
    ) -> None:
        root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.project_root = os.path.abspath(root)
        self.cache_service = cache_service or CacheService()
        self.db_service = db_service or DbService()
        self.report_service = ReportService(project_root=self.project_root)
        self._cache_dirs = [
            os.path.join(self.project_root, "OCR_Cache"),
            os.path.join(self.project_root, "Analysis_Cache"),
            os.path.join(self.project_root, "Translation_Cache"),
        ]
        self._json_dir = os.path.join(self.project_root, "Database_JSON")

    def build_new_path(self, old_pdf_path: str, new_title: str) -> tuple[str, JacarFilenameParts]:
        return self.build_new_path_from_fields(old_pdf_path, title=new_title)

    def build_new_path_from_fields(
        self,
        old_pdf_path: str,
        *,
        title: str | None = None,
        level2: str | None = None,
        parent: str | None = None,
        repo: str | None = None,
    ) -> tuple[str, JacarFilenameParts]:
        parts = parse_jacar_pdf_filename(old_pdf_path)
        if parts is None:
            raise ValueError(
                "当前文件名不符合 HRS 标准格式，无法重命名。\n"
                "请保持：二级分类：「标题」、JACAR Ref. XXXX（…）、『卷名』（馆藏）"
            )
        updated = JacarFilenameParts(
            level2=(level2 if level2 is not None else parts.level2).strip(),
            title=(title if title is not None else parts.title).strip(),
            ref=parts.ref,
            image_range=parts.image_range,
            parent=(parent if parent is not None else parts.parent).strip(),
            repo=(repo if repo is not None else parts.repo).strip(),
        )
        for label, value in (
            ("二级分类", updated.level2),
            ("标题", updated.title),
            ("卷名", updated.parent),
            ("馆藏", updated.repo),
        ):
            if not value:
                raise ValueError(f"{label}不能为空。")
        new_name = updated.build_pdf_filename()
        new_path = os.path.join(os.path.dirname(os.path.abspath(old_pdf_path)), new_name)
        return new_path, updated

    def rename_title(self, old_pdf_path: str, new_title: str) -> DocumentRenameResult:
        return self.rename_metadata(old_pdf_path, title=new_title, audit_source="sidebar")

    def rename_metadata(
        self,
        old_pdf_path: str,
        *,
        title: str | None = None,
        level2: str | None = None,
        parent: str | None = None,
        repo: str | None = None,
        audit_source: str = "catalog_ui",
    ) -> DocumentRenameResult:
        resolved = resolve_existing_pdf_path(old_pdf_path)
        if not resolved:
            return DocumentRenameResult(
                False,
                f"PDF 文件不存在或无法定位：\n{old_pdf_path}",
                old_pdf_path=old_pdf_path,
            )
        old_pdf_path = resolved

        try:
            new_pdf_path, new_parts = self.build_new_path_from_fields(
                old_pdf_path,
                title=title,
                level2=level2,
                parent=parent,
                repo=repo,
            )
        except ValueError as exc:
            return DocumentRenameResult(False, str(exc), old_pdf_path=old_pdf_path)

        if os.path.normpath(new_pdf_path) == os.path.normpath(old_pdf_path):
            return DocumentRenameResult(
                True,
                "元数据未变化，无需重命名。",
                old_pdf_path=old_pdf_path,
                new_pdf_path=old_pdf_path,
            )

        if os.path.exists(new_pdf_path):
            return DocumentRenameResult(
                False,
                f"目标文件已存在：\n{os.path.basename(new_pdf_path)}",
                old_pdf_path=old_pdf_path,
                new_pdf_path=new_pdf_path,
            )

        old_sidecar = sidecar_path_for_pdf(old_pdf_path)
        new_sidecar = sidecar_path_for_pdf(new_pdf_path)
        if os.path.isfile(old_sidecar) and os.path.exists(new_sidecar):
            if os.path.normpath(old_sidecar) != os.path.normpath(new_sidecar):
                return DocumentRenameResult(
                    False,
                    f"目标 Sidecar JSON 已存在：\n{os.path.basename(new_sidecar)}",
                    old_pdf_path=old_pdf_path,
                    new_pdf_path=new_pdf_path,
                )

        old_parts = parse_jacar_pdf_filename(old_pdf_path)
        if old_parts is None:
            return DocumentRenameResult(False, "无法解析当前文件名。", old_pdf_path=old_pdf_path)
        if not refs_equal(old_parts.ref, new_parts.ref):
            return DocumentRenameResult(
                False,
                "JACAR 编号发生变化，本功能不支持修改 Ref。",
                old_pdf_path=old_pdf_path,
                new_pdf_path=new_pdf_path,
            )

        old_ref = extract_jacar_ref_from_path(old_pdf_path)
        cache_moves = self._plan_cache_moves(old_pdf_path, new_pdf_path)
        export_moves = self._plan_export_moves(old_pdf_path, new_pdf_path)

        completed_moves: list[tuple[str, str]] = []

        sidecar_renamed = False

        try:
            os.rename(old_pdf_path, new_pdf_path)
            if os.path.isfile(old_sidecar):
                os.rename(old_sidecar, new_sidecar)
                completed_moves.append((old_sidecar, new_sidecar))
                patch_jacar_download_sidecar(new_sidecar, new_parts)
                sidecar_renamed = True

            for src, dst in cache_moves:
                if os.path.isfile(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.exists(dst):
                        raise OSError(f"缓存目标已存在: {dst}")
                    os.rename(src, dst)
                    completed_moves.append((src, dst))

            renamed_exports: list[str] = []
            for src, dst in export_moves:
                if os.path.isfile(src) and not os.path.exists(dst):
                    os.rename(src, dst)
                    renamed_exports.append(dst)

            json_count = self._patch_database_json_metadata(old_ref, new_parts)
            db_files_ok = self._update_sqlite_file_path(
                old_pdf_path, new_pdf_path, old_ref, kind="pdf"
            )
            db_sidecar_ok = False
            if sidecar_renamed:
                db_sidecar_ok = self._update_sqlite_file_path(
                    old_sidecar, new_sidecar, old_ref, kind="sidecar"
                )
            db_doc_ok = self._update_sqlite_document_fields(
                old_ref,
                new_parts,
                sidecar_path=new_sidecar if sidecar_renamed else None,
            )

            self._write_audit_log(
                old_parts=old_parts,
                new_parts=new_parts,
                old_pdf_path=old_pdf_path,
                new_pdf_path=new_pdf_path,
                source=audit_source,
            )

            return DocumentRenameResult(
                success=True,
                message=self._success_message(
                    new_pdf_path,
                    moved=len(completed_moves),
                    exports=len(renamed_exports),
                    json_files=json_count,
                    sidecar_renamed=sidecar_renamed,
                    db_files=db_files_ok,
                    db_document=db_doc_ok,
                    db_sidecar=db_sidecar_ok,
                ),
                old_pdf_path=old_pdf_path,
                new_pdf_path=new_pdf_path,
                moved_cache_files=[dst for _, dst in completed_moves],
                renamed_exports=renamed_exports,
                updated_json_files=json_count,
                sidecar_json_renamed=sidecar_renamed,
                db_path_updated=db_files_ok,
                db_document_updated=db_doc_ok,
                db_sidecar_path_updated=db_sidecar_ok,
            )
        except Exception as exc:
            self._rollback(completed_moves, new_pdf_path, old_pdf_path)
            return DocumentRenameResult(
                False,
                f"重命名失败（已尝试回滚）：{exc}",
                old_pdf_path=old_pdf_path,
                new_pdf_path=new_pdf_path,
            )

    def _write_audit_log(
        self,
        *,
        old_parts: JacarFilenameParts,
        new_parts: JacarFilenameParts,
        old_pdf_path: str,
        new_pdf_path: str,
        source: str,
    ) -> None:
        try:
            audit = DocumentAuditService(self.db_service)
            document_id = self._resolve_document_id_for_ref(old_parts.ref) or ""
            audit.log_rename(
                document_id=document_id,
                native_id=old_parts.ref,
                before=DocumentAuditService.parts_snapshot(old_parts),
                after=DocumentAuditService.parts_snapshot(new_parts),
                pdf_path_before=old_pdf_path,
                pdf_path_after=new_pdf_path,
                source=source,
            )
        except Exception:
            pass

    def _plan_cache_moves(self, old_pdf: str, new_pdf: str) -> list[tuple[str, str]]:
        stat = os.stat(old_pdf)
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size
        pairs: list[tuple[str, str]] = []
        for cache_dir in self._cache_dirs:
            old_txt = self.cache_service.build_cache_path_from_stat(
                old_pdf, cache_dir, mtime_ns=mtime_ns, size=size
            )
            new_txt = self.cache_service.build_cache_path_from_stat(
                new_pdf, cache_dir, mtime_ns=mtime_ns, size=size
            )
            if old_txt != new_txt:
                pairs.append((old_txt, new_txt))
            old_side = self.cache_service.build_context_sidecar_path(old_txt)
            new_side = self.cache_service.build_context_sidecar_path(new_txt)
            if old_side != new_side:
                pairs.append((old_side, new_side))
        return pairs

    def _plan_export_moves(self, old_pdf: str, new_pdf: str) -> list[tuple[str, str]]:
        moves: list[tuple[str, str]] = []
        comp_dir = self.report_service.default_comparison_dir()
        sum_dir = self.report_service.default_summary_dir()
        for old_p, new_p in (
            (
                self.report_service.expected_comparison_docx_path(old_pdf, comp_dir),
                self.report_service.expected_comparison_docx_path(new_pdf, comp_dir),
            ),
            (
                self.report_service.expected_summary_md_path(old_pdf, sum_dir),
                self.report_service.expected_summary_md_path(new_pdf, sum_dir),
            ),
        ):
            if os.path.normpath(old_p) != os.path.normpath(new_p):
                moves.append((old_p, new_p))
        return moves

    def _patch_database_json_metadata(self, ref: str, parts: JacarFilenameParts) -> int:
        if not ref or not os.path.isdir(self._json_dir):
            return 0
        prefix = f"JACAR_{ref}_p"
        auto_cite = (
            f"{parts.level2}：「{parts.title}」、JACAR Ref. {parts.ref}"
            f"（{parts.image_range}）、『{parts.parent}』（{parts.repo}）"
        )
        count = 0
        for name in os.listdir(self._json_dir):
            if not name.lower().endswith(".json"):
                continue
            if not name.upper().startswith(prefix.upper()):
                continue
            path = os.path.join(self._json_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                cite = data.get("Citation_Metadata")
                if not isinstance(cite, dict):
                    cite = {}
                cite["Level2_Name"] = parts.level2
                cite["Doc_Title"] = parts.title
                cite["JACAR_Ref"] = parts.ref
                cite["Parent_Volume"] = parts.parent
                cite["Repository"] = parts.repo
                cite["Auto_Citation"] = auto_cite
                cite.pop("Error", None)
                data["Citation_Metadata"] = cite
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                count += 1
            except (OSError, json.JSONDecodeError):
                continue
        return count

    @staticmethod
    def _merge_metadata_json(existing_raw: str | None, parts: JacarFilenameParts) -> str:
        meta: dict = {}
        if existing_raw:
            try:
                loaded = json.loads(existing_raw)
                if isinstance(loaded, dict):
                    meta = loaded
            except json.JSONDecodeError:
                meta = {}
        auto_cite = (
            f"{parts.level2}：「{parts.title}」、JACAR Ref. {parts.ref}"
            f"（{parts.image_range}）、『{parts.parent}』（{parts.repo}）"
        )
        meta["Title"] = parts.title
        meta["Doc_Title"] = parts.title
        meta["Level2_Name"] = parts.level2
        meta["Parent_Name"] = parts.parent
        meta["Repo_Name"] = parts.repo
        meta["Ref_Code"] = parts.ref
        meta["Auto_Citation"] = auto_cite
        cite = meta.get("Citation_Metadata")
        if not isinstance(cite, dict):
            cite = {}
        cite["Level2_Name"] = parts.level2
        cite["Doc_Title"] = parts.title
        cite["JACAR_Ref"] = parts.ref
        cite["Parent_Volume"] = parts.parent
        cite["Repository"] = parts.repo
        cite["Auto_Citation"] = auto_cite
        cite.pop("Error", None)
        meta["Citation_Metadata"] = cite
        return json.dumps(meta, ensure_ascii=False)

    def _resolve_document_id_for_ref(self, ref: str) -> str | None:
        if not ref:
            return None
        document_id = self.db_service.make_document_id("jacar", ref)
        row = self.db_service.fetchone(
            "SELECT document_id FROM documents WHERE document_id = ? LIMIT 1",
            (document_id,),
        )
        if row:
            return str(row["document_id"])
        row = self.db_service.fetchone(
            """
            SELECT document_id FROM documents
            WHERE source = 'jacar' AND UPPER(native_id) = UPPER(?)
            LIMIT 1
            """,
            (ref,),
        )
        if row:
            return str(row["document_id"])
        return None

    def _update_sqlite_document_fields(
        self,
        ref: str,
        parts: JacarFilenameParts,
        *,
        sidecar_path: str | None = None,
    ) -> bool:
        """同步 documents.title / level2_name / parent_name / repo_name / metadata_json。"""
        document_id = self._resolve_document_id_for_ref(ref)
        if not document_id:
            return False
        try:
            row = self.db_service.fetchone(
                "SELECT metadata_json FROM documents WHERE document_id = ? LIMIT 1",
                (document_id,),
            )
            existing_meta = None
            if row and row["metadata_json"] is not None:
                existing_meta = str(row["metadata_json"])
            if sidecar_path and os.path.isfile(sidecar_path):
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    sidecar_data = json.load(f)
                if isinstance(sidecar_data, dict):
                    metadata_json = json.dumps(sidecar_data, ensure_ascii=False)
                else:
                    metadata_json = self._merge_metadata_json(existing_meta, parts)
            else:
                metadata_json = self._merge_metadata_json(existing_meta, parts)
            now = self.db_service.utc_now_iso()
            with self.db_service.transaction():
                cur = self.db_service.execute(
                    """
                    UPDATE documents SET
                        title = ?,
                        level2_name = ?,
                        parent_name = ?,
                        repo_name = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE document_id = ?
                    """,
                    (
                        parts.title,
                        parts.level2,
                        parts.parent,
                        parts.repo,
                        metadata_json,
                        now,
                        document_id,
                    ),
                )
            return bool(cur.rowcount and cur.rowcount > 0)
        except Exception:
            return False

    def _update_sqlite_file_path(self, old_path: str, new_path: str, ref: str, *, kind: str) -> bool:
        try:
            cur = self.db_service.execute(
                "UPDATE files SET path = ? WHERE kind = ? AND path = ?",
                (new_path, kind, old_path),
            )
            if cur.rowcount and cur.rowcount > 0:
                return True
            if not ref:
                return False
            document_id = self._resolve_document_id_for_ref(ref)
            if not document_id:
                return False
            cur2 = self.db_service.execute(
                "UPDATE files SET path = ? WHERE document_id = ? AND kind = ?",
                (new_path, document_id, kind),
            )
            return bool(cur2.rowcount and cur2.rowcount > 0)
        except Exception:
            return False

    @staticmethod
    def _rollback(moves: list[tuple[str, str]], new_pdf: str, old_pdf: str) -> None:
        for src, dst in reversed(moves):
            if os.path.isfile(dst) and not os.path.isfile(src):
                try:
                    os.rename(dst, src)
                except OSError:
                    pass
        if os.path.isfile(new_pdf) and not os.path.isfile(old_pdf):
            try:
                os.rename(new_pdf, old_pdf)
            except OSError:
                pass

    @staticmethod
    def _success_message(
        new_pdf: str,
        *,
        moved: int,
        exports: int,
        json_files: int,
        sidecar_renamed: bool,
        db_files: bool,
        db_document: bool,
        db_sidecar: bool,
    ) -> str:
        lines = [f"已重命名为：\n{os.path.basename(new_pdf)}"]
        if sidecar_renamed:
            lines.append("已同步重命名并更新同目录下的抓取元数据 JSON（.json）。")
        else:
            lines.append("未找到配对的抓取元数据 JSON，仅重命名了 PDF。")
        if moved:
            lines.append(f"已迁移 {moved} 个本地缓存文件（OCR / 分析 / 翻译）。")
        else:
            lines.append("未发现需迁移的本地页级缓存。")
        if exports:
            lines.append(f"已同步重命名 {exports} 个汇报导出文件。")
        if json_files:
            lines.append(f"已更新 {json_files} 个 Database_JSON 分析分页 JSON 中的出处元数据。")
        if db_sidecar:
            lines.append("已更新 SQLite 中 sidecar 文件路径。")
        if db_document:
            lines.append("已更新 SQLite 中的标题（title）及出处相关字段。")
        elif db_files:
            lines.append("已更新数据库中的 PDF 路径（未找到 documents 记录，未更新标题）。")
        elif not db_files:
            lines.append("未在 SQLite 中找到该条 JACAR 记录（PDF 与缓存仍已重命名）。")
        return "\n".join(lines)
