"""Standardize archived MinerU results into versioned, page-aware MOFA artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Iterable

import fitz

from services.db_service import DbService
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService
from services.mofa_pdf_split_service import MofaMineruInputPart, MofaPdfSplitService


NORMALIZED_SCHEMA_VERSION = 1
PARSER_VERSION = "mineru-content-list-v1"
NORMALIZER_VERSION = "mofa-search-text-v1"
NORMALIZED_MANIFEST_FILENAME = "normalized_manifest.json"
NORMALIZED_PAGES_FILENAME = "ocr_pages.v1.jsonl"
SEARCH_TEXT_FILENAME = "search_text.paged.json"

_NON_SEARCHABLE_TYPES = {
    "equation",
    "footer",
    "image",
    "interline_equation",
    "page_number",
}
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)

# This is intentionally a conservative, versioned map. OCR-confusion replacements do
# not belong here: those will be an explicitly labelled query-expansion layer.
OLD_STYLE_FOLD_PAIRS_V1 = (
        ("亞", "亜"),
        ("壓", "圧"),
        ("應", "応"),
        ("會", "会"),
        ("覺", "覚"),
        ("學", "学"),
        ("國", "国"),
        ("實", "実"),
        ("總", "総"),
        ("對", "対"),
        ("體", "体"),
        ("臺", "台"),
        ("團", "団"),
        ("黨", "党"),
        ("獨", "独"),
        ("發", "発"),
        ("變", "変"),
        ("滿", "満"),
        ("與", "与"),
        ("舊", "旧"),
        ("縣", "県"),
        ("勞", "労"),
        ("號", "号"),
        ("關", "関"),
        ("產", "産"),
        ("經", "経"),
        ("濟", "済"),
        ("戰", "戦"),
        ("處", "処"),
        ("條", "条"),
        ("權", "権"),
        ("轉", "転"),
        ("讀", "読"),
        ("從", "従"),
)

_OLD_STYLE_FOLD_V1 = str.maketrans(
    {
        source: target for source, target in OLD_STYLE_FOLD_PAIRS_V1
    }
)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag.lower() in {"br", "p", "td", "th", "tr", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "td", "th", "tr", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return _WHITESPACE_RE.sub(" ", "".join(self.parts)).strip()


@dataclass(frozen=True)
class MofaNormalizationResult:
    native_id: str
    title: str
    status: str
    generation_id: str = ""
    page_count: int = 0
    block_count: int = 0
    searchable_block_count: int = 0
    artifact_path: str = ""
    search_text_path: str = ""
    message: str = ""


@dataclass(frozen=True)
class MofaNormalizationBatchResult:
    results: tuple[MofaNormalizationResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.results)

    @property
    def standardized(self) -> int:
        return self.count("standardized")

    @property
    def skipped(self) -> int:
        return self.count("skipped")

    @property
    def incomplete(self) -> int:
        return self.count("incomplete")

    @property
    def failed(self) -> int:
        return self.count("failed")


@dataclass(frozen=True)
class _SelectedRun:
    part: MofaMineruInputPart
    row: dict
    legacy_mapping: bool


class MofaMineruNormalizationService:
    """Build immutable normalized generations without modifying PDF or MinerU raw files."""

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
    def _sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _json_file(path: str):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    @staticmethod
    def _content_list_path(raw_dir: str) -> str:
        names = sorted(
            name
            for name in os.listdir(raw_dir)
            if name.lower().endswith("_content_list.json")
            and not name.lower().endswith("_content_list_v2.json")
        )
        if not names:
            raise ValueError("MinerU raw 目录缺少扁平 *_content_list.json")
        return os.path.join(raw_dir, names[0])

    @staticmethod
    def _table_text(html: str) -> str:
        parser = _HtmlTextExtractor()
        parser.feed(html or "")
        parser.close()
        return parser.text()

    @classmethod
    def _block_text(cls, block: dict) -> str:
        value = block.get("text")
        if isinstance(value, str) and value.strip():
            return value.strip()
        table_body = block.get("table_body")
        if isinstance(table_body, str) and table_body.strip():
            return cls._table_text(table_body)
        parts: list[str] = []
        for key in ("table_caption", "table_footnote", "image_caption", "image_footnote"):
            value = block.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(item) for item in value if item)
        return "\n".join(item.strip() for item in parts if item.strip())

    @staticmethod
    def normalize_search_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text or "").translate(_OLD_STYLE_FOLD_V1)
        return _WHITESPACE_RE.sub("", normalized)

    @staticmethod
    def _normalized_bbox(value) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            coords = [float(item) for item in value]
        except (TypeError, ValueError):
            return None
        if any(item < -1 or item > 1001 for item in coords):
            return None
        x0, y0, x1, y1 = [max(0.0, min(1.0, item / 1000.0)) for item in coords]
        if x1 <= x0 or y1 <= y0:
            return None
        return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]

    @staticmethod
    def _page_size(layout_page: dict, fallback: tuple[float, float]) -> tuple[float, float]:
        value = layout_page.get("page_size") if isinstance(layout_page, dict) else None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                width, height = float(value[0]), float(value[1])
                if width > 0 and height > 0:
                    return width, height
            except (TypeError, ValueError):
                pass
        return fallback

    @staticmethod
    def _load_source_page_mapping(bundle_dir: str) -> dict[int, dict]:
        path = MofaPdfSplitService.manifest_path_for_bundle(bundle_dir)
        if not os.path.isfile(path):
            return {}
        try:
            manifest = MofaMineruNormalizationService._json_file(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        result: dict[int, dict] = {}
        for item in manifest.get("page_mapping", []):
            if not isinstance(item, dict):
                continue
            output_pages = item.get("output_pdf_pages")
            if not isinstance(output_pages, list):
                continue
            order = item.get("order") if isinstance(item.get("order"), list) else []
            for index, output_page in enumerate(output_pages):
                try:
                    page_number = int(output_page)
                    source_page = int(item.get("source_pdf_page"))
                except (TypeError, ValueError):
                    continue
                region = str(order[index]) if index < len(order) else "full"
                result[page_number - 1] = {
                    "source_pdf_page": source_page,
                    "source_region": region,
                    "split_action": str(item.get("action") or ""),
                }
        return result

    def _select_runs(
        self,
        entry: MofaLibraryEntry,
        parts: tuple[MofaMineruInputPart, ...],
    ) -> tuple[tuple[_SelectedRun, ...], tuple[str, ...], tuple[str, ...]]:
        rows = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM mofa_mineru_runs
                WHERE document_id = ?
                ORDER BY imported_at, run_id
                """,
                (self.db.make_document_id("mofa", entry.native_id),),
            )
            if os.path.isdir(str(row["raw_dir"] or ""))
        ]
        selected: list[_SelectedRun] = []
        superseded: list[str] = []
        warnings: list[str] = []
        for part in parts:
            exact = []
            for row in rows:
                run_start = int(row.get("chunk_start") or 0)
                run_end = int(row.get("chunk_end") or 0)
                run_sha = str(row.get("input_sha256") or "")
                range_matches = run_start == part.start_page and run_end == part.end_page
                sha_matches = not part.sha256 or run_sha == part.sha256
                if range_matches and sha_matches and int(row.get("page_count") or 0) == part.page_count:
                    exact.append(row)
            candidates = exact
            legacy = False
            if not candidates and len(parts) == 1:
                candidates = [
                    row
                    for row in rows
                    if not row.get("input_sha256")
                    and not row.get("chunk_start")
                    and not row.get("chunk_end")
                    and int(row.get("page_count") or 0) == part.page_count
                ]
                legacy = bool(candidates)
            if not candidates:
                raise ValueError(
                    f"缺少当前 MinerU 输入第 {part.index}/{part.count} 段"
                    f"（{part.start_page}-{part.end_page}）的可用 OCR run"
                )
            candidates.sort(key=lambda row: (str(row.get("imported_at") or ""), str(row["run_id"])))
            chosen = candidates[-1]
            selected.append(_SelectedRun(part=part, row=chosen, legacy_mapping=legacy))
            if legacy:
                warnings.append(
                    f"{chosen['run_id']} 使用旧版单段兼容映射：chunk_start=1"
                )
            if len(candidates) > 1:
                replaced = [str(row["run_id"]) for row in candidates[:-1]]
                superseded.extend(replaced)
                warnings.append(
                    f"页段 {part.start_page}-{part.end_page} 选择最新 run "
                    f"{chosen['run_id']}，替代 {', '.join(replaced)}"
                )
        return tuple(selected), tuple(superseded), tuple(warnings)

    @staticmethod
    def _validate_parts(parts: tuple[MofaMineruInputPart, ...], total_pages: int) -> None:
        if not parts:
            raise ValueError("没有可用的 MinerU 输入分段")
        expected = 1
        for part in sorted(parts, key=lambda item: item.start_page):
            if part.start_page != expected:
                raise ValueError(f"MinerU 输入分段不连续：期望从第 {expected} 页开始")
            if part.end_page < part.start_page or part.page_count != part.end_page - part.start_page + 1:
                raise ValueError("MinerU 输入分段页码范围无效")
            expected = part.end_page + 1
        if expected - 1 != total_pages:
            raise ValueError(f"MinerU 输入分段仅覆盖 {expected - 1}/{total_pages} 页")

    def _source_signature(
        self,
        entry: MofaLibraryEntry,
        selected: tuple[_SelectedRun, ...],
        single_pdf_sha256: str,
    ) -> str:
        digest = hashlib.sha256()
        for value in (
            entry.native_id,
            PARSER_VERSION,
            NORMALIZER_VERSION,
            single_pdf_sha256,
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        for selected_run in selected:
            digest.update(str(selected_run.row["result_signature"]).encode("ascii"))
            digest.update(
                f":{selected_run.part.start_page}:{selected_run.part.end_page}".encode("ascii")
            )
        for manifest_path in (
            self.splitter.manifest_path_for_bundle(entry.bundle_dir),
            self.splitter.chunk_manifest_path_for_bundle(entry.bundle_dir),
        ):
            if os.path.isfile(manifest_path):
                digest.update(self._sha256(manifest_path).encode("ascii"))
        return digest.hexdigest()

    @staticmethod
    def _generation_paths(entry: MofaLibraryEntry, generation_id: str) -> dict[str, str]:
        generation_dir = os.path.join(entry.bundle_dir, "mineru", "imported", generation_id)
        return {
            "generation_dir": generation_dir,
            "manifest": os.path.join(generation_dir, NORMALIZED_MANIFEST_FILENAME),
            "pages": os.path.join(generation_dir, NORMALIZED_PAGES_FILENAME),
            "search": os.path.join(entry.bundle_dir, "search", SEARCH_TEXT_FILENAME),
        }

    def _active_generation_id(self, document_id: str) -> str:
        row = self.db.fetchone(
            "SELECT generation_id FROM mofa_ocr_active_generations WHERE document_id = ?",
            (document_id,),
        )
        return str(row["generation_id"]) if row else ""

    @staticmethod
    def _write_json_atomic(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            with open(temp_path, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        finally:
            if os.path.isfile(temp_path):
                os.unlink(temp_path)

    @staticmethod
    def _replace_directory(incoming: str, target: str) -> None:
        if not os.path.exists(target):
            os.replace(incoming, target)
            return
        backup = f"{target}.old-{os.getpid()}-{threading.get_ident()}"
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(target, backup)
        try:
            os.replace(incoming, target)
        except Exception:
            os.replace(backup, target)
            raise
        shutil.rmtree(backup, ignore_errors=True)

    def _parse_selected_runs(
        self,
        entry: MofaLibraryEntry,
        generation_id: str,
        selected: tuple[_SelectedRun, ...],
        total_pages: int,
    ) -> tuple[list[dict], int, int, list[str]]:
        pages: dict[int, dict] = {}
        block_count = 0
        searchable_block_count = 0
        warnings: list[str] = []
        source_mapping = self._load_source_page_mapping(entry.bundle_dir)

        for selected_run in sorted(selected, key=lambda item: item.part.start_page):
            raw_dir = str(selected_run.row["raw_dir"])
            content = self._json_file(self._content_list_path(raw_dir))
            layout = self._json_file(os.path.join(raw_dir, "layout.json"))
            if not isinstance(content, list):
                raise ValueError(f"{selected_run.row['run_id']} content_list 不是数组")
            pdf_info = layout.get("pdf_info") if isinstance(layout, dict) else None
            if not isinstance(pdf_info, list) or len(pdf_info) != selected_run.part.page_count:
                raise ValueError(
                    f"{selected_run.row['run_id']} layout 页数与输入页段不一致"
                )
            with fitz.open(selected_run.part.path) as part_pdf:
                if len(part_pdf) != selected_run.part.page_count:
                    raise ValueError("MinerU 输入分段 PDF 页数与清单不一致")
                fallback_sizes = [
                    (float(part_pdf[index].rect.width), float(part_pdf[index].rect.height))
                    for index in range(len(part_pdf))
                ]

            blocks_by_local_page: dict[int, list[dict]] = {
                index: [] for index in range(selected_run.part.page_count)
            }
            for raw_block in content:
                if not isinstance(raw_block, dict):
                    continue
                try:
                    local_page = int(raw_block.get("page_idx"))
                except (TypeError, ValueError):
                    warnings.append(f"{selected_run.row['run_id']} 存在缺少 page_idx 的 block")
                    continue
                if local_page not in blocks_by_local_page:
                    warnings.append(
                        f"{selected_run.row['run_id']} 存在越界 block page_idx={local_page}"
                    )
                    continue
                blocks_by_local_page[local_page].append(raw_block)

            for local_page in range(selected_run.part.page_count):
                global_index = selected_run.part.start_page - 1 + local_page
                if global_index in pages:
                    raise ValueError(f"标准化页码重叠：single-pages 第 {global_index + 1} 页")
                width, height = self._page_size(pdf_info[local_page], fallback_sizes[local_page])
                normalized_blocks: list[dict] = []
                printed_labels: list[str] = []
                for block_order, raw_block in enumerate(blocks_by_local_page[local_page]):
                    block_type = str(raw_block.get("type") or "text").strip().lower()
                    raw_text = self._block_text(raw_block)
                    search_text = self.normalize_search_text(raw_text)
                    searchable = bool(search_text) and block_type not in _NON_SEARCHABLE_TYPES
                    if block_type == "page_number" and raw_text:
                        printed_labels.append(raw_text)
                    block_key = f"{generation_id}:p{global_index + 1}:b{block_order + 1}"
                    normalized_blocks.append(
                        {
                            "block_key": block_key,
                            "block_order": block_order,
                            "block_type": block_type,
                            "raw_text": raw_text,
                            "search_text": search_text,
                            "bbox_norm": self._normalized_bbox(raw_block.get("bbox")),
                            "searchable": searchable,
                            "source_run_id": str(selected_run.row["run_id"]),
                            "source_local_page_index": local_page,
                        }
                    )
                    block_count += 1
                    searchable_block_count += int(searchable)
                nonempty = [block["raw_text"] for block in normalized_blocks if block["raw_text"]]
                searchable_texts = [
                    block["search_text"]
                    for block in normalized_blocks
                    if block["searchable"] and block["search_text"]
                ]
                mapping = source_mapping.get(global_index, {})
                pages[global_index] = {
                    "schema_version": NORMALIZED_SCHEMA_VERSION,
                    "document_id": self.db.make_document_id("mofa", entry.native_id),
                    "native_id": entry.native_id,
                    "generation_id": generation_id,
                    "page_index": global_index,
                    "display_page": global_index + 1,
                    "source_pdf_page": mapping.get("source_pdf_page"),
                    "source_region": mapping.get("source_region", ""),
                    "split_action": mapping.get("split_action", ""),
                    "page_width": width,
                    "page_height": height,
                    "printed_page_label": " / ".join(printed_labels),
                    "raw_text": "\n".join(nonempty),
                    "search_text": "\n".join(searchable_texts),
                    "source_run_id": str(selected_run.row["run_id"]),
                    "source_local_page_index": local_page,
                    "blocks": normalized_blocks,
                }

        ordered = [pages[index] for index in range(total_pages) if index in pages]
        if len(ordered) != total_pages:
            missing = [str(index + 1) for index in range(total_pages) if index not in pages]
            raise ValueError(f"标准化页码不完整，缺少：{', '.join(missing[:20])}")
        return ordered, block_count, searchable_block_count, warnings

    def _write_generation(
        self,
        entry: MofaLibraryEntry,
        generation_id: str,
        manifest: dict,
        pages: list[dict],
        *,
        force: bool,
    ) -> dict[str, str]:
        paths = self._generation_paths(entry, generation_id)
        incoming = f"{paths['generation_dir']}.incoming-{os.getpid()}-{threading.get_ident()}"
        if os.path.isdir(incoming):
            shutil.rmtree(incoming, ignore_errors=True)
        os.makedirs(incoming, exist_ok=False)
        try:
            with open(
                os.path.join(incoming, NORMALIZED_MANIFEST_FILENAME),
                "w",
                encoding="utf-8",
            ) as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
            with open(
                os.path.join(incoming, NORMALIZED_PAGES_FILENAME),
                "w",
                encoding="utf-8",
            ) as stream:
                for page in pages:
                    stream.write(json.dumps(page, ensure_ascii=False, separators=(",", ":")))
                    stream.write("\n")
            if force or not os.path.isdir(paths["generation_dir"]):
                self._replace_directory(incoming, paths["generation_dir"])
            else:
                shutil.rmtree(incoming, ignore_errors=True)
        finally:
            if os.path.isdir(incoming):
                shutil.rmtree(incoming, ignore_errors=True)
        return paths

    def _record_generation(
        self,
        *,
        entry: MofaLibraryEntry,
        generation_id: str,
        source_signature: str,
        source_run_ids: list[str],
        superseded_run_ids: list[str],
        single_pdf_sha256: str,
        page_count: int,
        block_count: int,
        searchable_block_count: int,
        artifact_path: str,
        search_text_path: str,
        warnings: list[str],
    ) -> None:
        document_id = self.db.make_document_id("mofa", entry.native_id)
        now = self.db.utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_ocr_generations(
                    generation_id, document_id, native_id, source_signature,
                    source_run_ids_json, superseded_run_ids_json,
                    parser_version, normalizer_version, single_pdf_sha256,
                    page_count, block_count, searchable_block_count,
                    artifact_path, search_text_path, status, warnings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)
                ON CONFLICT(document_id, source_signature) DO UPDATE SET
                    source_run_ids_json=excluded.source_run_ids_json,
                    superseded_run_ids_json=excluded.superseded_run_ids_json,
                    single_pdf_sha256=excluded.single_pdf_sha256,
                    page_count=excluded.page_count,
                    block_count=excluded.block_count,
                    searchable_block_count=excluded.searchable_block_count,
                    artifact_path=excluded.artifact_path,
                    search_text_path=excluded.search_text_path,
                    status='complete',
                    warnings_json=excluded.warnings_json
                """,
                (
                    generation_id,
                    document_id,
                    entry.native_id,
                    source_signature,
                    json.dumps(source_run_ids, ensure_ascii=False),
                    json.dumps(superseded_run_ids, ensure_ascii=False),
                    PARSER_VERSION,
                    NORMALIZER_VERSION,
                    single_pdf_sha256,
                    page_count,
                    block_count,
                    searchable_block_count,
                    artifact_path,
                    search_text_path,
                    json.dumps(warnings, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO mofa_ocr_active_generations(document_id, generation_id, activated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    activated_at=excluded.activated_at
                """,
                (document_id, generation_id, now),
            )

    def normalize_document(
        self,
        entry: MofaLibraryEntry,
        *,
        force: bool = False,
    ) -> MofaNormalizationResult:
        try:
            if not os.path.isfile(entry.split_pdf_path):
                raise ValueError("完整 single-pages PDF 不存在")
            parts = self.splitter.load_input_parts(entry.bundle_dir, entry.split_pdf_path)
            with fitz.open(entry.split_pdf_path) as document:
                total_pages = len(document)
            self._validate_parts(parts, total_pages)
            selected, superseded, selection_warnings = self._select_runs(entry, parts)
            single_pdf_sha256 = self._sha256(entry.split_pdf_path)
            source_signature = self._source_signature(entry, selected, single_pdf_sha256)
            generation_id = f"mofaocr-{source_signature[:24]}"
            paths = self._generation_paths(entry, generation_id)
            document_id = self.db.make_document_id("mofa", entry.native_id)
            if (
                not force
                and self._active_generation_id(document_id) == generation_id
                and os.path.isfile(paths["manifest"])
                and os.path.isfile(paths["pages"])
                and os.path.isfile(paths["search"])
            ):
                row = self.db.fetchone(
                    "SELECT * FROM mofa_ocr_generations WHERE generation_id = ?",
                    (generation_id,),
                )
                return MofaNormalizationResult(
                    entry.native_id,
                    entry.title,
                    "skipped",
                    generation_id=generation_id,
                    page_count=int(row["page_count"] or 0) if row else total_pages,
                    block_count=int(row["block_count"] or 0) if row else 0,
                    searchable_block_count=(
                        int(row["searchable_block_count"] or 0) if row else 0
                    ),
                    artifact_path=paths["manifest"],
                    search_text_path=paths["search"],
                    message="当前 Generation 已是最新版",
                )

            pages, block_count, searchable_block_count, parse_warnings = self._parse_selected_runs(
                entry,
                generation_id,
                selected,
                total_pages,
            )
            warnings = [*selection_warnings, *parse_warnings]
            source_runs = [str(item.row["run_id"]) for item in selected]
            manifest = {
                "schema_version": NORMALIZED_SCHEMA_VERSION,
                "generation_id": generation_id,
                "document_id": document_id,
                "native_id": entry.native_id,
                "title": entry.title,
                "year": entry.year,
                "volume_code": entry.volume_code,
                "volume_label": entry.volume_label,
                "parser_version": PARSER_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
                "source_signature": source_signature,
                "source_runs": source_runs,
                "superseded_runs": list(superseded),
                "pdf": {
                    "path": os.path.relpath(entry.split_pdf_path, entry.bundle_dir),
                    "sha256": single_pdf_sha256,
                    "pages": total_pages,
                },
                "coverage": {
                    "first_page": 1,
                    "last_page": total_pages,
                    "page_count": total_pages,
                    "complete": True,
                },
                "counts": {
                    "blocks": block_count,
                    "searchable_blocks": searchable_block_count,
                },
                "warnings": warnings,
            }
            paths = self._write_generation(
                entry,
                generation_id,
                manifest,
                pages,
                force=True,
            )
            search_payload = {
                "format": "mofa_search_paged_v1",
                "schema_version": NORMALIZED_SCHEMA_VERSION,
                "generation_id": generation_id,
                "document_id": document_id,
                "native_id": entry.native_id,
                "title": entry.title,
                "year": entry.year,
                "volume_code": entry.volume_code,
                "volume_label": entry.volume_label,
                "pdf": manifest["pdf"],
                "pages": [
                    {
                        "page_index": page["page_index"],
                        "display_page": page["display_page"],
                        "source_pdf_page": page["source_pdf_page"],
                        "source_region": page["source_region"],
                        "printed_page_label": page["printed_page_label"],
                        "raw_text": page["raw_text"],
                        "search_text": page["search_text"],
                    }
                    for page in pages
                ],
            }
            self._write_json_atomic(paths["search"], search_payload)
            artifact_rel = os.path.relpath(paths["manifest"], entry.bundle_dir)
            search_rel = os.path.relpath(paths["search"], entry.bundle_dir)
            self._record_generation(
                entry=entry,
                generation_id=generation_id,
                source_signature=source_signature,
                source_run_ids=source_runs,
                superseded_run_ids=list(superseded),
                single_pdf_sha256=single_pdf_sha256,
                page_count=total_pages,
                block_count=block_count,
                searchable_block_count=searchable_block_count,
                artifact_path=artifact_rel,
                search_text_path=search_rel,
                warnings=warnings,
            )
            return MofaNormalizationResult(
                entry.native_id,
                entry.title,
                "standardized",
                generation_id=generation_id,
                page_count=total_pages,
                block_count=block_count,
                searchable_block_count=searchable_block_count,
                artifact_path=paths["manifest"],
                search_text_path=paths["search"],
                message=f"已标准化 {total_pages} 页、{searchable_block_count} 个可检索文本块",
            )
        except ValueError as exc:
            return MofaNormalizationResult(
                entry.native_id,
                entry.title,
                "incomplete",
                message=str(exc),
            )
        except Exception as exc:
            return MofaNormalizationResult(
                entry.native_id,
                entry.title,
                "failed",
                message=str(exc),
            )

    def normalize_entries(
        self,
        entries: Iterable[MofaLibraryEntry],
        *,
        force: bool = False,
        on_progress: Callable[[int, int, MofaNormalizationResult], None] | None = None,
    ) -> MofaNormalizationBatchResult:
        values = list(entries)
        results: list[MofaNormalizationResult] = []
        for index, entry in enumerate(values, start=1):
            result = self.normalize_document(entry, force=force)
            results.append(result)
            if on_progress is not None:
                on_progress(index, len(values), result)
        return MofaNormalizationBatchResult(tuple(results))
