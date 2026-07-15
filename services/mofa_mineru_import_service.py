"""Import and monitor MinerU desktop results for the local MOFA corpus."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

import fitz

from services.db_service import DbService
from services.mofa_filename_service import extract_mofa_native_id
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService
from services.mofa_pdf_split_service import MofaMineruInputPart, MofaPdfSplitService


_RESULT_REQUIRED_FILES = ("full.md", "layout.json")
_IGNORED_DIRS = {"images", "data", "__pycache__"}
_TEMP_SUFFIXES = (".part", ".tmp", ".download")
_SAFE_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass(frozen=True)
class MofaMineruMetadata:
    page_count: int
    mineru_version: str
    backend: str
    effort: str
    ocr_enabled: bool | None
    origin_pdf_path: str


@dataclass(frozen=True)
class MofaMineruImportResult:
    source_dir: str
    status: str
    native_id: str = ""
    title: str = ""
    raw_dir: str = ""
    match_method: str = ""
    page_count: int = 0
    chunk_index: int = 0
    chunk_count: int = 0
    chunk_start: int = 0
    chunk_end: int = 0
    total_pages: int = 0
    message: str = ""


@dataclass(frozen=True)
class MofaMineruBatchResult:
    results: tuple[MofaMineruImportResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.results)

    @property
    def imported(self) -> int:
        return self.count("imported")

    @property
    def skipped(self) -> int:
        return self.count("skipped")

    @property
    def unmatched(self) -> int:
        return self.count("unmatched")

    @property
    def ambiguous(self) -> int:
        return self.count("ambiguous")

    @property
    def invalid(self) -> int:
        return self.count("invalid")

    @property
    def failed(self) -> int:
        return self.count("failed")


class MofaMineruImportService:
    """Validate, identify, deduplicate, and atomically copy MinerU exports."""

    WATCH_DIR_SETTING = "watch_dir"

    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
        library_service: MofaLibraryService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.db = db_service or DbService()
        self.library = library_service or MofaLibraryService(
            project_root=self.project_root,
            db_service=self.db,
        )
        self.splitter = MofaPdfSplitService()

    @staticmethod
    def _is_result_dir(path: str) -> bool:
        return (
            not os.path.isfile(os.path.join(path, "import_manifest.json"))
            and all(os.path.isfile(os.path.join(path, name)) for name in _RESULT_REQUIRED_FILES)
        )

    def discover_result_dirs(self, root_dir: str) -> list[str]:
        """Find complete-looking MinerU result roots without descending into images."""
        root = os.path.abspath(os.path.expanduser(root_dir or ""))
        if not os.path.isdir(root):
            return []
        if self._is_result_dir(root):
            return [root]
        results: list[str] = []
        for current, dirnames, _filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".") and name not in _IGNORED_DIRS
            ]
            if current != root and self._is_result_dir(current):
                results.append(current)
                dirnames[:] = []
        return sorted(results)

    @staticmethod
    def directory_snapshot(path: str) -> tuple[int, int, int]:
        """Return (file count, total bytes, newest mtime_ns) for watcher stability."""
        count = total = newest = 0
        for current, dirnames, filenames in os.walk(path):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                try:
                    stat = os.stat(os.path.join(current, name))
                except OSError:
                    continue
                count += 1
                total += int(stat.st_size)
                newest = max(newest, int(stat.st_mtime_ns))
        return count, total, newest

    @staticmethod
    def _origin_pdf(result_dir: str) -> str:
        names = sorted(
            name
            for name in os.listdir(result_dir)
            if name.lower().endswith("_origin.pdf")
        )
        return os.path.join(result_dir, names[0]) if names else ""

    def read_metadata(self, result_dir: str) -> MofaMineruMetadata:
        result_dir = os.path.abspath(result_dir)
        missing = [
            name for name in _RESULT_REQUIRED_FILES if not os.path.isfile(os.path.join(result_dir, name))
        ]
        if missing:
            raise ValueError(f"缺少 MinerU 结果文件：{', '.join(missing)}")
        if any(
            name.lower().endswith(_TEMP_SUFFIXES)
            for name in os.listdir(result_dir)
        ):
            raise ValueError("MinerU 结果仍包含临时下载文件，可能尚未写入完成")
        content_lists = [
            name
            for name in os.listdir(result_dir)
            if name.lower().endswith(("_content_list.json", "_content_list_v2.json"))
        ]
        if not content_lists:
            raise ValueError("缺少 MinerU content_list JSON")
        layout_path = os.path.join(result_dir, "layout.json")
        try:
            with open(layout_path, "r", encoding="utf-8") as stream:
                layout = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"layout.json 无法读取：{exc}") from exc
        if not isinstance(layout, dict) or not isinstance(layout.get("pdf_info"), list):
            raise ValueError("layout.json 缺少 pdf_info 页面列表")
        page_count = len(layout["pdf_info"])
        if page_count <= 0:
            raise ValueError("MinerU 结果没有可导入页面")
        ocr_value = layout.get("_ocr_enable")
        return MofaMineruMetadata(
            page_count=page_count,
            mineru_version=str(layout.get("_version_name") or ""),
            backend=str(layout.get("_backend") or ""),
            effort=str(layout.get("_effort") or ""),
            ocr_enabled=bool(ocr_value) if ocr_value is not None else None,
            origin_pdf_path=self._origin_pdf(result_dir),
        )

    @staticmethod
    def _pdf_page_count(path: str) -> int:
        document = fitz.open(path)
        try:
            return len(document)
        finally:
            document.close()

    @staticmethod
    def _pdf_fingerprint(path: str) -> tuple[int, tuple[str, ...]]:
        document = fitz.open(path)
        try:
            page_count = len(document)
            if page_count <= 0:
                return 0, ()
            page_indexes = sorted({0, page_count // 2, page_count - 1})
            fingerprints: list[str] = []
            for page_index in page_indexes:
                page = document[page_index]
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(0.25, 0.25),
                    colorspace=fitz.csGRAY,
                    alpha=False,
                )
                digest = hashlib.sha256()
                digest.update(f"{pixmap.width}x{pixmap.height}:".encode("ascii"))
                digest.update(pixmap.samples)
                fingerprints.append(digest.hexdigest())
            return page_count, tuple(fingerprints)
        finally:
            document.close()

    def _match_entry(
        self,
        result_dir: str,
        metadata: MofaMineruMetadata,
    ) -> tuple[MofaLibraryEntry | None, MofaMineruInputPart | None, str, str]:
        entries = self.library.list_entries(item_kind="")
        result_name = os.path.basename(result_dir)
        named_id = extract_mofa_native_id(result_name)
        named_range = self.splitter.extract_chunk_range(result_name)
        if named_id:
            entry = next((item for item in entries if item.native_id.upper() == named_id), None)
            if entry is None:
                return None, None, "native_id", f"文件名中的 {named_id} 不在 MOFA 本地目录缓存中"
            if not entry.split_pdf_exists:
                return None, None, "native_id", "对应史料尚无 MinerU 单页 PDF，无法校验页面"
            parts = self.splitter.load_input_parts(entry.bundle_dir, entry.split_pdf_path)
            if named_range:
                part = next(
                    (
                        value
                        for value in parts
                        if (value.start_page, value.end_page) == named_range
                    ),
                    None,
                )
                if part is None:
                    return (
                        None,
                        None,
                        "native_id+page_range",
                        f"文件名页段 p{named_range[0]}-p{named_range[1]} 不在 chunk_manifest.json 中",
                    )
                method = "native_id+page_range"
            elif len(parts) == 1:
                part = parts[0]
                method = "native_id"
            elif metadata.origin_pdf_path:
                fingerprint = self._pdf_fingerprint(metadata.origin_pdf_path)
                matched = [
                    value
                    for value in parts
                    if value.page_count == metadata.page_count
                    and self._pdf_fingerprint(value.path) == fingerprint
                ]
                if len(matched) != 1:
                    return (
                        None,
                        None,
                        "native_id+page_fingerprint",
                        "该史料有多个 MinerU 分段，但结果名缺少页码范围且无法唯一匹配",
                    )
                part = matched[0]
                method = "native_id+page_fingerprint"
            else:
                return (
                    None,
                    None,
                    "native_id",
                    "该史料有多个 MinerU 分段，结果名必须保留 p0001-p0200 页码范围",
                )
            if part.page_count != metadata.page_count:
                return (
                    None,
                    None,
                    method,
                    f"页段匹配，但页面数不一致：结果 {metadata.page_count}，输入 {part.page_count}",
                )
            if metadata.origin_pdf_path and self._pdf_fingerprint(
                metadata.origin_pdf_path
            ) != self._pdf_fingerprint(part.path):
                return None, None, method, "MOFA ID 匹配，但 origin.pdf 与对应输入页段的指纹不一致"
            return entry, part, method, ""

        if not metadata.origin_pdf_path:
            return None, None, "page_fingerprint", "匿名结果缺少 *_origin.pdf，无法安全定位"
        origin_fingerprint = self._pdf_fingerprint(metadata.origin_pdf_path)
        candidates: list[tuple[MofaLibraryEntry, MofaMineruInputPart]] = []
        for entry in entries:
            if not entry.split_pdf_exists:
                continue
            for part in self.splitter.load_input_parts(entry.bundle_dir, entry.split_pdf_path):
                try:
                    if part.page_count != metadata.page_count:
                        continue
                    if self._pdf_fingerprint(part.path) == origin_fingerprint:
                        candidates.append((entry, part))
                except (OSError, RuntimeError, ValueError):
                    continue
        if len(candidates) == 1:
            entry, part = candidates[0]
            return entry, part, "page_fingerprint", ""
        if len(candidates) > 1:
            ids = "、".join(f"{item.native_id}:{part.range_label}" for item, part in candidates[:5])
            return None, None, "page_fingerprint", f"页面指纹匹配到多个候选：{ids}"
        return None, None, "page_fingerprint", "没有找到页数及页面指纹一致的 MinerU 输入页段"

    @staticmethod
    def _signature_files(result_dir: str) -> list[str]:
        selected: list[str] = []
        for name in os.listdir(result_dir):
            lower = name.lower()
            if name in {"full.md", "layout.json", "block_list.json"} or lower.endswith(
                ("_content_list.json", "_content_list_v2.json")
            ):
                path = os.path.join(result_dir, name)
                if os.path.isfile(path):
                    selected.append(path)
        return sorted(selected, key=lambda path: os.path.basename(path))

    def result_signature(self, result_dir: str, metadata: MofaMineruMetadata) -> str:
        digest = hashlib.sha256()
        for path in self._signature_files(result_dir):
            name = os.path.basename(path)
            role = re.sub(r"^[0-9a-f-]{20,}_", "", name, flags=re.IGNORECASE)
            digest.update(role.encode("utf-8"))
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        if metadata.origin_pdf_path:
            page_count, fingerprints = self._pdf_fingerprint(metadata.origin_pdf_path)
            digest.update(str(page_count).encode("ascii"))
            for value in fingerprints:
                digest.update(value.encode("ascii"))
        return digest.hexdigest()

    @staticmethod
    def _safe_component(value: str, fallback: str) -> str:
        clean = _SAFE_COMPONENT_RE.sub("-", value.strip()).strip("-._")
        return clean or fallback

    def _run_dir_name(
        self,
        metadata: MofaMineruMetadata,
        signature: str,
        input_part: MofaMineruInputPart,
    ) -> str:
        version = self._safe_component(metadata.mineru_version, "unknown")
        backend = self._safe_component(metadata.backend, "backend")
        effort = self._safe_component(metadata.effort, "effort")
        range_marker = f"_{input_part.range_label}" if input_part.count > 1 else ""
        return f"mineru-{version}_{backend}-{effort}{range_marker}_{signature[:16]}"

    def _existing_run(self, document_id: str, signature: str):
        return self.db.fetchone(
            """
            SELECT * FROM mofa_mineru_runs
            WHERE document_id = ? AND result_signature = ?
            LIMIT 1
            """,
            (document_id, signature),
        )

    @staticmethod
    def _build_import_manifest(
        entry: MofaLibraryEntry,
        metadata: MofaMineruMetadata,
        input_part: MofaMineruInputPart,
        *,
        document_id: str,
        source_dir: str,
        signature: str,
        match_method: str,
        imported_at: str,
    ) -> dict:
        return {
            "schema_version": 1,
            "document_id": document_id,
            "native_id": entry.native_id,
            "title": entry.title,
            "source_dir": source_dir,
            "result_signature": signature,
            "match_method": match_method,
            "imported_at": imported_at,
            "mineru": {
                "version": metadata.mineru_version,
                "backend": metadata.backend,
                "effort": metadata.effort,
                "ocr_enabled": metadata.ocr_enabled,
                "page_count": metadata.page_count,
                "origin_pdf": os.path.basename(metadata.origin_pdf_path),
            },
            "input": {
                "path": input_part.path,
                "sha256": input_part.sha256,
                "chunk_index": input_part.index,
                "chunk_count": input_part.count,
                "chunk_start": input_part.start_page,
                "chunk_end": input_part.end_page,
                "chunk_pages": input_part.page_count,
                "total_pages": input_part.total_pages,
            },
            "page_mapping": {
                "mineru_local_first_page": 1,
                "mineru_local_last_page": input_part.page_count,
                "global_first_page": input_part.start_page,
                "global_last_page": input_part.end_page,
                "formula": "global_page = chunk_start + mineru_local_page - 1",
            },
            "source_preserved": True,
        }

    def _record_run(
        self,
        entry: MofaLibraryEntry,
        metadata: MofaMineruMetadata,
        input_part: MofaMineruInputPart,
        *,
        source_dir: str,
        raw_dir: str,
        signature: str,
        match_method: str,
        imported_at: str,
        manifest: dict,
    ) -> None:
        document_id = self.db.upsert_document(
            source="mofa",
            native_id=entry.native_id,
            title=entry.title,
            repo_name="日本外務省",
            level2_name="日本外交文書",
            parent_name=entry.volume_label,
            viewer_url=entry.catalog_url,
            search_keyword="MOFA史料库",
            status="downloaded",
        )
        run_id = f"{entry.native_id}_{signature[:16]}"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_mineru_runs(
                    run_id, document_id, native_id, result_signature,
                    source_dir, raw_dir, match_method, mineru_version,
                    backend, effort, ocr_enabled, page_count, imported_at,
                    metadata_json, input_sha256, chunk_index, chunk_count,
                    chunk_start, chunk_end, total_pages
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, result_signature) DO UPDATE SET
                    source_dir=excluded.source_dir,
                    raw_dir=excluded.raw_dir,
                    imported_at=excluded.imported_at,
                    metadata_json=excluded.metadata_json,
                    input_sha256=excluded.input_sha256,
                    chunk_index=excluded.chunk_index,
                    chunk_count=excluded.chunk_count,
                    chunk_start=excluded.chunk_start,
                    chunk_end=excluded.chunk_end,
                    total_pages=excluded.total_pages
                """,
                (
                    run_id,
                    document_id,
                    entry.native_id,
                    signature,
                    source_dir,
                    raw_dir,
                    match_method,
                    metadata.mineru_version,
                    metadata.backend,
                    metadata.effort,
                    None if metadata.ocr_enabled is None else int(metadata.ocr_enabled),
                    metadata.page_count,
                    imported_at,
                    json.dumps(manifest, ensure_ascii=False),
                    input_part.sha256,
                    input_part.index,
                    input_part.count,
                    input_part.start_page,
                    input_part.end_page,
                    input_part.total_pages,
                ),
            )

    @staticmethod
    def _copy_ignore(_directory: str, names: Iterable[str]) -> set[str]:
        return {
            name
            for name in names
            if name.startswith(".") or name.lower().endswith(_TEMP_SUFFIXES)
        }

    def import_result_dir(self, result_dir: str) -> MofaMineruImportResult:
        source_dir = os.path.abspath(os.path.expanduser(result_dir))
        try:
            metadata = self.read_metadata(source_dir)
        except Exception as exc:
            return MofaMineruImportResult(source_dir, "invalid", message=str(exc))
        try:
            entry, input_part, match_method, match_error = self._match_entry(
                source_dir,
                metadata,
            )
        except Exception as exc:
            return MofaMineruImportResult(
                source_dir,
                "failed",
                page_count=metadata.page_count,
                message=f"定位史料时失败：{exc}",
            )
        if entry is None or input_part is None:
            status = "ambiguous" if "多个候选" in match_error else "unmatched"
            return MofaMineruImportResult(
                source_dir,
                status,
                match_method=match_method,
                page_count=metadata.page_count,
                message=match_error,
            )
        raw_dir = ""
        created_archive = False
        try:
            signature = self.result_signature(source_dir, metadata)
            document_id = self.db.make_document_id("mofa", entry.native_id)
            existing = self._existing_run(document_id, signature)
            if existing is not None and os.path.isdir(str(existing["raw_dir"])):
                return MofaMineruImportResult(
                    source_dir,
                    "skipped",
                    native_id=entry.native_id,
                    title=entry.title,
                    raw_dir=str(existing["raw_dir"]),
                    match_method=match_method,
                    page_count=metadata.page_count,
                    chunk_index=input_part.index,
                    chunk_count=input_part.count,
                    chunk_start=input_part.start_page,
                    chunk_end=input_part.end_page,
                    total_pages=input_part.total_pages,
                    message="相同 MinerU 结果已导入",
                )

            raw_root = os.path.join(entry.bundle_dir, "mineru", "raw")
            os.makedirs(raw_root, exist_ok=True)
            if not input_part.sha256:
                input_part = MofaMineruInputPart(
                    path=input_part.path,
                    sha256=self.splitter._sha256(input_part.path),
                    index=input_part.index,
                    count=input_part.count,
                    start_page=input_part.start_page,
                    end_page=input_part.end_page,
                    page_count=input_part.page_count,
                    total_pages=input_part.total_pages,
                )
            run_dir_name = self._run_dir_name(metadata, signature, input_part)
            raw_dir = os.path.join(raw_root, run_dir_name)
            imported_at = self.db.utc_now_iso()
            manifest = self._build_import_manifest(
                entry,
                metadata,
                input_part,
                document_id=document_id,
                source_dir=source_dir,
                signature=signature,
                match_method=match_method,
                imported_at=imported_at,
            )
            if os.path.isdir(raw_dir):
                self._record_run(
                    entry,
                    metadata,
                    input_part,
                    source_dir=source_dir,
                    raw_dir=raw_dir,
                    signature=signature,
                    match_method=match_method,
                    imported_at=imported_at,
                    manifest=manifest,
                )
                return MofaMineruImportResult(
                    source_dir,
                    "skipped",
                    native_id=entry.native_id,
                    title=entry.title,
                    raw_dir=raw_dir,
                    match_method=match_method,
                    page_count=metadata.page_count,
                    chunk_index=input_part.index,
                    chunk_count=input_part.count,
                    chunk_start=input_part.start_page,
                    chunk_end=input_part.end_page,
                    total_pages=input_part.total_pages,
                    message="相同签名的归档目录已存在",
                )

            incoming = os.path.join(raw_root, f".incoming-{run_dir_name}-{os.getpid()}")
            if os.path.exists(incoming):
                shutil.rmtree(incoming)
            try:
                shutil.copytree(source_dir, incoming, ignore=self._copy_ignore)
                with open(
                    os.path.join(incoming, "import_manifest.json"),
                    "w",
                    encoding="utf-8",
                ) as stream:
                    json.dump(manifest, stream, ensure_ascii=False, indent=2)
                os.rename(incoming, raw_dir)
                created_archive = True
            except Exception:
                if os.path.isdir(incoming):
                    shutil.rmtree(incoming, ignore_errors=True)
                raise

            self._record_run(
                entry,
                metadata,
                input_part,
                source_dir=source_dir,
                raw_dir=raw_dir,
                signature=signature,
                match_method=match_method,
                imported_at=imported_at,
                manifest=manifest,
            )
            return MofaMineruImportResult(
                source_dir,
                "imported",
                native_id=entry.native_id,
                title=entry.title,
                raw_dir=raw_dir,
                match_method=match_method,
                page_count=metadata.page_count,
                chunk_index=input_part.index,
                chunk_count=input_part.count,
                chunk_start=input_part.start_page,
                chunk_end=input_part.end_page,
                total_pages=input_part.total_pages,
                message=(
                    f"MinerU 第 {input_part.index}/{input_part.count} 段"
                    f"（{input_part.start_page}-{input_part.end_page}）已复制归档，来源目录保留"
                ),
            )
        except Exception as exc:
            if created_archive and raw_dir and os.path.isdir(raw_dir):
                shutil.rmtree(raw_dir, ignore_errors=True)
            return MofaMineruImportResult(
                source_dir,
                "failed",
                native_id=entry.native_id,
                title=entry.title,
                match_method=match_method,
                page_count=metadata.page_count,
                chunk_index=input_part.index,
                chunk_count=input_part.count,
                chunk_start=input_part.start_page,
                chunk_end=input_part.end_page,
                total_pages=input_part.total_pages,
                message=str(exc),
            )

    def import_directory(self, root_dir: str) -> MofaMineruBatchResult:
        results = tuple(self.import_result_dir(path) for path in self.discover_result_dirs(root_dir))
        if results:
            return MofaMineruBatchResult(results)
        root = os.path.abspath(os.path.expanduser(root_dir or ""))
        message = "目录不存在" if not os.path.isdir(root) else "未发现完整的 MinerU 结果目录"
        return MofaMineruBatchResult((MofaMineruImportResult(root, "invalid", message=message),))

    def get_watch_dir(self) -> str:
        row = self.db.fetchone(
            "SELECT value FROM mofa_mineru_settings WHERE key = ?",
            (self.WATCH_DIR_SETTING,),
        )
        return str(row["value"]) if row else ""

    def set_watch_dir(self, path: str) -> str:
        value = os.path.abspath(os.path.expanduser(path)) if path else ""
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_mineru_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (self.WATCH_DIR_SETTING, value, self.db.utc_now_iso()),
            )
        return value


class MofaMineruDirectoryWatcher:
    """Poll a MinerU export folder and import only directories stable for two scans."""

    def __init__(self, importer: MofaMineruImportService, *, interval_seconds: float = 5.0):
        self.importer = importer
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._root_dir = ""

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def start(
        self,
        root_dir: str,
        callback: Callable[[MofaMineruBatchResult], None] | None = None,
    ) -> None:
        root = os.path.abspath(os.path.expanduser(root_dir))
        if not os.path.isdir(root):
            raise NotADirectoryError(root)
        self.stop()
        with self._lock:
            self._root_dir = root
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._loop,
                args=(root, callback, self._stop_event),
                daemon=True,
                name="mofa-mineru-watcher",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(1.0, self.interval_seconds + 0.1))

    def _loop(
        self,
        root: str,
        callback: Callable[[MofaMineruBatchResult], None] | None,
        stop_event: threading.Event,
    ) -> None:
        previous: dict[str, tuple[int, int, int]] = {}
        processed: dict[str, tuple[int, int, int]] = {}
        while not stop_event.is_set():
            current_paths = self.importer.discover_result_dirs(root)
            current_snapshots: dict[str, tuple[int, int, int]] = {}
            results: list[MofaMineruImportResult] = []
            for path in current_paths:
                snapshot = self.importer.directory_snapshot(path)
                current_snapshots[path] = snapshot
                if previous.get(path) != snapshot or processed.get(path) == snapshot:
                    continue
                result = self.importer.import_result_dir(path)
                processed[path] = snapshot
                results.append(result)
            previous = current_snapshots
            processed = {path: value for path, value in processed.items() if path in current_snapshots}
            if results and callback is not None:
                try:
                    callback(MofaMineruBatchResult(tuple(results)))
                except Exception:
                    pass
            stop_event.wait(self.interval_seconds)
