"""缓存文件 ↔ JACAR Ref 索引：支持从哈希缓存名反向定位史料。"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from services.cache_service import CacheService
from services.db_service import DbService
from utils.jacar_filename import extract_jacar_ref_from_path, parse_jacar_pdf_filename
from utils.reporting import discover_pdf_files

CACHE_KIND_OCR = "ocr"
CACHE_KIND_ANALYSIS = "analysis"
CACHE_KIND_TRANSLATION = "translation"
CACHE_KIND_ANALYSIS_CONTEXT = "analysis_context"

_PAGED_CACHE_KINDS = (
    (CACHE_KIND_OCR, "OCR_Cache"),
    (CACHE_KIND_ANALYSIS, "Analysis_Cache"),
    (CACHE_KIND_TRANSLATION, "Translation_Cache"),
)


@dataclass(frozen=True)
class CacheIndexLookup:
    native_id: str
    document_id: str
    pdf_path: str
    cache_kind: str
    cache_path: str
    is_orphan: bool = False


@dataclass
class CacheIndexRebuildStats:
    pdfs_scanned: int = 0
    entries_written: int = 0
    orphans_found: int = 0
    notes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"扫描 PDF：{self.pdfs_scanned} 个",
            f"写入索引：{self.entries_written} 条",
            f"孤儿缓存（无法反查 Ref）：{self.orphans_found} 条",
        ]
        if self.notes:
            lines.append("")
            lines.append("备注：")
            lines.extend(f"  · {n}" for n in self.notes[:20])
            if len(self.notes) > 20:
                lines.append(f"  · … 另有 {len(self.notes) - 20} 条")
        return lines


class CacheIndexService:
    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
        cache_service: CacheService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.db_service = db_service or DbService()
        self.cache_service = cache_service or CacheService()
        self.downloads_root = os.path.join(self.project_root, "JACAR_Downloads")

    @staticmethod
    def cache_key_digest(pdf_path: str, *, mtime_ns: int, size: int) -> str:
        raw = f"{pdf_path}|{mtime_ns}|{size}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def infer_cache_kind(cache_path: str) -> str | None:
        norm = os.path.normpath(cache_path)
        name = os.path.basename(norm).lower()
        parent = os.path.basename(os.path.dirname(norm))
        if name.endswith(".context.json"):
            return CACHE_KIND_ANALYSIS_CONTEXT
        if not name.endswith(".txt"):
            return None
        if parent == "OCR_Cache":
            return CACHE_KIND_OCR
        if parent == "Analysis_Cache":
            return CACHE_KIND_ANALYSIS
        if parent == "Translation_Cache":
            return CACHE_KIND_TRANSLATION
        return None

    def _resolve_path(self, path: str) -> str:
        if not path:
            return ""
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.project_root, path))

    def _resolve_ref_and_document(self, pdf_path: str) -> tuple[str, str]:
        ref = ""
        parts = parse_jacar_pdf_filename(pdf_path)
        if parts and parts.ref:
            ref = parts.ref.strip().upper()
        if not ref:
            ref = extract_jacar_ref_from_path(pdf_path)
        document_id = self.db_service.make_document_id("jacar", ref) if ref else ""
        return ref, document_id

    def _planned_cache_paths(self, pdf_path: str) -> list[tuple[str, str]]:
        if not os.path.isfile(pdf_path):
            return []
        stat = os.stat(pdf_path)
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size
        digest = self.cache_key_digest(pdf_path, mtime_ns=mtime_ns, size=size)
        planned: list[tuple[str, str]] = []
        for kind, dirname in _PAGED_CACHE_KINDS:
            cache_dir = os.path.join(self.project_root, dirname)
            cache_path = self.cache_service.build_cache_path_from_stat(
                pdf_path,
                cache_dir,
                mtime_ns=mtime_ns,
                size=size,
            )
            planned.append((kind, cache_path))
            if kind == CACHE_KIND_ANALYSIS:
                planned.append(
                    (
                        CACHE_KIND_ANALYSIS_CONTEXT,
                        self.cache_service.build_context_sidecar_path(cache_path),
                    )
                )
        return planned

    def _delete_rows_for_document(self, document_id: str) -> None:
        if not document_id:
            return
        self.db_service.execute(
            "DELETE FROM document_cache_index WHERE document_id = ?",
            (document_id,),
        )

    def _upsert_row(
        self,
        *,
        document_id: str,
        native_id: str,
        cache_kind: str,
        cache_path: str,
        pdf_path: str,
        cache_key: str,
        pdf_mtime: int | None,
        pdf_size: int | None,
        is_present: bool,
        is_orphan: bool,
    ) -> None:
        now = self.db_service.utc_now_iso()
        basename = os.path.basename(cache_path)
        self.db_service.execute(
            """
            INSERT INTO document_cache_index (
                document_id, native_id, cache_kind, cache_path, cache_basename,
                pdf_path, cache_key, pdf_mtime, pdf_size, is_present, is_orphan, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_kind, cache_basename) DO UPDATE SET
                document_id = excluded.document_id,
                native_id = excluded.native_id,
                cache_path = excluded.cache_path,
                pdf_path = excluded.pdf_path,
                cache_key = excluded.cache_key,
                pdf_mtime = excluded.pdf_mtime,
                pdf_size = excluded.pdf_size,
                is_present = excluded.is_present,
                is_orphan = excluded.is_orphan,
                indexed_at = excluded.indexed_at
            """,
            (
                document_id or None,
                native_id or "",
                cache_kind,
                cache_path,
                basename,
                pdf_path or "",
                cache_key or "",
                pdf_mtime,
                pdf_size,
                1 if is_present else 0,
                1 if is_orphan else 0,
                now,
            ),
        )

    def index_pdf(self, pdf_path: str) -> int:
        """为单条 PDF 刷新 OCR/Analysis/Translation 缓存索引。返回写入条数。"""
        abs_pdf = self._resolve_path(pdf_path)
        if not os.path.isfile(abs_pdf):
            return 0
        ref, document_id = self._resolve_ref_and_document(abs_pdf)
        if document_id:
            self._delete_rows_for_document(document_id)

        stat = os.stat(abs_pdf)
        digest = self.cache_key_digest(abs_pdf, mtime_ns=stat.st_mtime_ns, size=stat.st_size)
        written = 0
        for cache_kind, cache_path in self._planned_cache_paths(abs_pdf):
            self._upsert_row(
                document_id=document_id,
                native_id=ref,
                cache_kind=cache_kind,
                cache_path=cache_path,
                pdf_path=abs_pdf,
                cache_key=digest,
                pdf_mtime=int(stat.st_mtime_ns),
                pdf_size=int(stat.st_size),
                is_present=os.path.isfile(cache_path),
                is_orphan=False,
            )
            written += 1
        return written

    def rebuild_all(self, *, pdf_root: str | None = None) -> CacheIndexRebuildStats:
        """全量重建：先按 PDF 正向索引，再扫描缓存目录登记孤儿文件。"""
        stats = CacheIndexRebuildStats()
        root = os.path.abspath(pdf_root or self.downloads_root)
        pdf_paths = discover_pdf_files(root) if os.path.isdir(root) else []
        stats.pdfs_scanned = len(pdf_paths)

        with self.db_service.transaction():
            self.db_service.execute("DELETE FROM document_cache_index")
            known: set[tuple[str, str]] = set()
            for pdf_path in pdf_paths:
                abs_pdf = os.path.abspath(pdf_path)
                ref, document_id = self._resolve_ref_and_document(abs_pdf)
                if not ref:
                    stats.notes.append(f"无法解析 Ref，已跳过：{os.path.basename(abs_pdf)}")
                    continue
                try:
                    stat = os.stat(abs_pdf)
                except OSError as exc:
                    stats.notes.append(f"无法 stat PDF：{abs_pdf} ({exc})")
                    continue
                digest = self.cache_key_digest(abs_pdf, mtime_ns=stat.st_mtime_ns, size=stat.st_size)
                for cache_kind, cache_path in self._planned_cache_paths(abs_pdf):
                    present = os.path.isfile(cache_path)
                    self._upsert_row(
                        document_id=document_id,
                        native_id=ref,
                        cache_kind=cache_kind,
                        cache_path=cache_path,
                        pdf_path=abs_pdf,
                        cache_key=digest,
                        pdf_mtime=int(stat.st_mtime_ns),
                        pdf_size=int(stat.st_size),
                        is_present=present,
                        is_orphan=False,
                    )
                    known.add((cache_kind, os.path.basename(cache_path)))
                    stats.entries_written += 1

            for dirname in ("OCR_Cache", "Analysis_Cache", "Translation_Cache"):
                cache_dir = os.path.join(self.project_root, dirname)
                if not os.path.isdir(cache_dir):
                    continue
                for name in os.listdir(cache_dir):
                    abs_path = os.path.join(cache_dir, name)
                    if not os.path.isfile(abs_path):
                        continue
                    cache_kind = self.infer_cache_kind(abs_path)
                    if not cache_kind:
                        continue
                    key = (cache_kind, name)
                    if key in known:
                        continue
                    self._upsert_row(
                        document_id="",
                        native_id="",
                        cache_kind=cache_kind,
                        cache_path=abs_path,
                        pdf_path="",
                        cache_key="",
                        pdf_mtime=None,
                        pdf_size=None,
                        is_present=True,
                        is_orphan=True,
                    )
                    known.add(key)
                    stats.orphans_found += 1
                    stats.entries_written += 1

        return stats

    def lookup_by_cache_path(self, cache_path: str) -> CacheIndexLookup | None:
        resolved = self._resolve_path(cache_path)
        if not resolved:
            return None
        row = self.db_service.fetchone(
            """
            SELECT document_id, native_id, pdf_path, cache_kind, cache_path, is_orphan
            FROM document_cache_index
            WHERE cache_path = ? OR cache_basename = ?
            LIMIT 1
            """,
            (resolved, os.path.basename(resolved)),
        )
        return self._row_to_lookup(row)

    def lookup_by_cache_basename(self, cache_basename: str, *, cache_kind: str | None = None) -> CacheIndexLookup | None:
        name = os.path.basename((cache_basename or "").strip())
        if not name:
            return None
        if cache_kind:
            row = self.db_service.fetchone(
                """
                SELECT document_id, native_id, pdf_path, cache_kind, cache_path, is_orphan
                FROM document_cache_index
                WHERE cache_basename = ? AND cache_kind = ?
                LIMIT 1
                """,
                (name, cache_kind),
            )
        else:
            row = self.db_service.fetchone(
                """
                SELECT document_id, native_id, pdf_path, cache_kind, cache_path, is_orphan
                FROM document_cache_index
                WHERE cache_basename = ?
                LIMIT 1
                """,
                (name,),
            )
        return self._row_to_lookup(row)

    def resolve_ref_from_cache(self, cache_path: str) -> str:
        """从缓存文件路径或哈希文件名反查 JACAR Ref；找不到或孤儿缓存返回空字符串。"""
        hit = self.lookup_by_cache_path(cache_path)
        if hit is None or hit.is_orphan or not hit.native_id:
            return ""
        return hit.native_id

    @staticmethod
    def _row_to_lookup(row) -> CacheIndexLookup | None:
        if row is None:
            return None
        native_id = str(row["native_id"] or "").strip()
        is_orphan = bool(int(row["is_orphan"] or 0))
        if is_orphan or not native_id:
            return CacheIndexLookup(
                native_id="",
                document_id=str(row["document_id"] or ""),
                pdf_path=str(row["pdf_path"] or ""),
                cache_kind=str(row["cache_kind"] or ""),
                cache_path=str(row["cache_path"] or ""),
                is_orphan=True,
            )
        return CacheIndexLookup(
            native_id=native_id,
            document_id=str(row["document_id"] or ""),
            pdf_path=str(row["pdf_path"] or ""),
            cache_kind=str(row["cache_kind"] or ""),
            cache_path=str(row["cache_path"] or ""),
            is_orphan=False,
        )
