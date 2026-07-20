"""Build and query the local MOFA OCR full-text index."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from services.db_service import DbService
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService
from services.mofa_mineru_normalization_service import (
    NORMALIZED_PAGES_FILENAME,
    MofaMineruNormalizationService,
)
from services.mofa_search_lexicon_service import (
    EXPANSION_EXACT,
    MofaExpandedTerm,
    MofaSearchLexiconService,
    MofaSearchPlan,
)


SEARCH_MODE_PHRASE = "phrase"
SEARCH_MODE_ALL = "all"
SEARCH_MODE_ANY = "any"
SEARCH_MODES = {SEARCH_MODE_PHRASE, SEARCH_MODE_ALL, SEARCH_MODE_ANY}
_SPACE_RE = re.compile(r"\s+", re.UNICODE)


@dataclass(frozen=True)
class MofaIndexResult:
    native_id: str
    title: str
    status: str
    generation_id: str = ""
    page_count: int = 0
    block_count: int = 0
    message: str = ""


@dataclass(frozen=True)
class MofaIndexBatchResult:
    results: tuple[MofaIndexResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.results)

    @property
    def indexed(self) -> int:
        return self.count("indexed")

    @property
    def skipped(self) -> int:
        return self.count("skipped")

    @property
    def unavailable(self) -> int:
        return self.count("unavailable")

    @property
    def failed(self) -> int:
        return self.count("failed")


@dataclass(frozen=True)
class MofaSearchBlock:
    block_key: str
    block_order: int
    block_type: str
    bbox: tuple[float, float, float, float] | None
    raw_text: str
    search_text: str


@dataclass(frozen=True)
class MofaIndexedPage:
    document_id: str
    generation_id: str
    native_id: str
    year: int
    volume_code: str
    title: str
    page_index: int
    display_page: int
    source_pdf_page: int | None
    source_region: str
    printed_page_label: str
    raw_text: str
    search_text: str


@dataclass(frozen=True)
class MofaSearchHit:
    document_id: str
    generation_id: str
    native_id: str
    year: int
    volume_code: str
    title: str
    page_index: int
    display_page: int
    source_pdf_page: int | None
    source_region: str
    printed_page_label: str
    raw_text: str
    search_text: str
    score: float
    matching_blocks: tuple[MofaSearchBlock, ...]
    matched_terms: tuple[MofaExpandedTerm, ...] = ()
    expansion_level: str = EXPANSION_EXACT
    lexicon_revision: int = 0

    @property
    def snippet(self) -> str:
        texts = [item.raw_text.strip() for item in self.matching_blocks if item.raw_text.strip()]
        value = "　".join(texts) if texts else self.raw_text
        value = _SPACE_RE.sub(" ", value).strip()
        return value if len(value) <= 180 else f"{value[:177]}…"

    @property
    def reason_label(self) -> str:
        return "、".join(dict.fromkeys(item.label for item in self.matched_terms)) or "精确命中"

    @property
    def match_weight(self) -> float:
        return max((item.weight for item in self.matched_terms), default=1.0)


@dataclass(frozen=True)
class MofaSearchExecution:
    plan: MofaSearchPlan
    hits: tuple[MofaSearchHit, ...]


class MofaFullTextSearchService:
    """Maintain active-generation page/block indexes and execute local searches."""

    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
        library_service: MofaLibraryService | None = None,
        lexicon_service: MofaSearchLexiconService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.db = db_service or DbService()
        self.library = library_service or MofaLibraryService(
            project_root=self.project_root,
            db_service=self.db,
        )
        self.lexicon = lexicon_service or MofaSearchLexiconService(db_service=self.db)

    @staticmethod
    def _document_id(native_id: str) -> str:
        return DbService.make_document_id("mofa", native_id)

    def _active_generation(self, entry: MofaLibraryEntry) -> dict | None:
        row = self.db.fetchone(
            """
            SELECT g.*
            FROM mofa_ocr_active_generations a
            JOIN mofa_ocr_generations g ON g.generation_id = a.generation_id
            WHERE a.document_id = ? AND g.status = 'complete'
            """,
            (self._document_id(entry.native_id),),
        )
        return dict(row) if row else None

    @staticmethod
    def _artifact_path(entry: MofaLibraryEntry, generation: dict) -> str:
        artifact = str(generation.get("artifact_path") or "")
        if not os.path.isabs(artifact):
            artifact = os.path.join(entry.bundle_dir, artifact)
        return os.path.join(os.path.dirname(artifact), NORMALIZED_PAGES_FILENAME)

    @staticmethod
    def _read_pages(path: str, document_id: str, generation_id: str) -> list[dict]:
        pages: list[dict] = []
        with open(path, "r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    page = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"标准化页文件第 {line_number} 行不是有效 JSON") from exc
                if page.get("document_id") != document_id:
                    raise ValueError("标准化页文件 document_id 与当前史料不一致")
                if page.get("generation_id") != generation_id:
                    raise ValueError("标准化页文件 generation_id 与当前版本不一致")
                pages.append(page)
        pages.sort(key=lambda item: int(item.get("page_index", -1)))
        indexes = [int(item.get("page_index", -1)) for item in pages]
        if indexes != list(range(len(pages))):
            raise ValueError("标准化页文件的 page_index 不连续")
        return pages

    def index_entry(self, entry: MofaLibraryEntry, *, force: bool = False) -> MofaIndexResult:
        try:
            generation = self._active_generation(entry)
            if generation is None:
                return MofaIndexResult(
                    entry.native_id,
                    entry.title,
                    "unavailable",
                    message="没有可用的 active OCR Generation",
                )
            document_id = self._document_id(entry.native_id)
            generation_id = str(generation["generation_id"])
            expected_pages = int(generation.get("page_count") or 0)
            source_blocks = int(generation.get("block_count") or 0)
            expected_blocks = int(generation.get("searchable_block_count") or 0)
            state = self.db.fetchone(
                "SELECT * FROM mofa_fts_index_state WHERE document_id = ?",
                (document_id,),
            )
            if (
                not force
                and state
                and str(state["generation_id"]) == generation_id
                and int(state["page_count"]) == expected_pages
                and int(state["block_count"]) == expected_blocks
            ):
                return MofaIndexResult(
                    entry.native_id,
                    entry.title,
                    "skipped",
                    generation_id=generation_id,
                    page_count=expected_pages,
                    block_count=expected_blocks,
                    message="当前 Generation 已在全文索引中",
                )

            pages_path = self._artifact_path(entry, generation)
            if not os.path.isfile(pages_path):
                raise ValueError(f"缺少标准化页文件：{pages_path}")
            pages = self._read_pages(pages_path, document_id, generation_id)
            if len(pages) != expected_pages:
                raise ValueError(
                    f"标准化页数与 Generation 不一致：{len(pages)}/{expected_pages}"
                )
            block_total = sum(len(page.get("blocks") or []) for page in pages)
            if block_total != source_blocks:
                raise ValueError(
                    f"标准化 block 数与 Generation 不一致：{block_total}/{source_blocks}"
                )
            searchable_total = sum(
                int(bool(block.get("searchable")))
                for page in pages
                for block in (page.get("blocks") or [])
            )
            if searchable_total != expected_blocks:
                raise ValueError(
                    f"可检索 block 数与 Generation 不一致：{searchable_total}/{expected_blocks}"
                )

            now = self.db.utc_now_iso()
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM mofa_search_pages WHERE document_id = ?", (document_id,))
                for page in pages:
                    cursor = conn.execute(
                        """
                        INSERT INTO mofa_search_pages(
                            document_id, generation_id, native_id, gregorian_year,
                            volume_code, title, page_index, display_page,
                            source_pdf_page, source_region, printed_page_label,
                            raw_text, search_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            generation_id,
                            entry.native_id,
                            entry.year,
                            entry.volume_code,
                            entry.title,
                            int(page["page_index"]),
                            int(page.get("display_page") or int(page["page_index"]) + 1),
                            page.get("source_pdf_page"),
                            str(page.get("source_region") or ""),
                            str(page.get("printed_page_label") or ""),
                            str(page.get("raw_text") or ""),
                            str(page.get("search_text") or ""),
                        ),
                    )
                    page_row_id = int(cursor.lastrowid)
                    for block in page.get("blocks") or []:
                        if not block.get("searchable"):
                            continue
                        bbox = block.get("bbox_norm")
                        conn.execute(
                            """
                            INSERT INTO mofa_search_blocks(
                                block_key, page_row_id, document_id, generation_id,
                                page_index, block_order, block_type, bbox_json,
                                raw_text, search_text
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(block["block_key"]),
                                page_row_id,
                                document_id,
                                generation_id,
                                int(page["page_index"]),
                                int(block.get("block_order") or 0),
                                str(block.get("block_type") or "text"),
                                json.dumps(bbox) if bbox is not None else None,
                                str(block.get("raw_text") or ""),
                                str(block.get("search_text") or ""),
                            ),
                        )
                conn.execute(
                    """
                    INSERT INTO mofa_fts_index_state(
                        document_id, generation_id, page_count, block_count, indexed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        generation_id=excluded.generation_id,
                        page_count=excluded.page_count,
                        block_count=excluded.block_count,
                        indexed_at=excluded.indexed_at
                    """,
                    (document_id, generation_id, expected_pages, expected_blocks, now),
                )
            return MofaIndexResult(
                entry.native_id,
                entry.title,
                "indexed",
                generation_id=generation_id,
                page_count=expected_pages,
                block_count=expected_blocks,
                message=f"已索引 {expected_pages} 页、{expected_blocks} 个 block",
            )
        except ValueError as exc:
            return MofaIndexResult(entry.native_id, entry.title, "unavailable", message=str(exc))
        except Exception as exc:
            return MofaIndexResult(entry.native_id, entry.title, "failed", message=str(exc))

    def index_entries(
        self,
        entries: Iterable[MofaLibraryEntry],
        *,
        force: bool = False,
        on_progress: Callable[[int, int, MofaIndexResult], None] | None = None,
    ) -> MofaIndexBatchResult:
        values = list(entries)
        results: list[MofaIndexResult] = []
        for index, entry in enumerate(values, start=1):
            result = self.index_entry(entry, force=force)
            results.append(result)
            if on_progress:
                on_progress(index, len(values), result)
        return MofaIndexBatchResult(tuple(results))

    @staticmethod
    def _query_terms(query: str, mode: str) -> tuple[str, ...]:
        if mode not in SEARCH_MODES:
            raise ValueError(f"未知检索模式：{mode}")
        raw_terms = [item for item in _SPACE_RE.split((query or "").strip()) if item]
        if mode == SEARCH_MODE_PHRASE:
            raw_terms = [query]
        terms = tuple(
            value
            for value in (
                MofaMineruNormalizationService.normalize_search_text(item)
                for item in raw_terms
            )
            if value
        )
        if not terms:
            raise ValueError("请输入检索词")
        return terms

    @staticmethod
    def normalized_match_ranges(
        text: str,
        query_terms: Iterable[str],
    ) -> tuple[tuple[int, int], ...]:
        """Locate normalized queries in raw OCR text while preserving raw offsets."""
        normalized_parts: list[str] = []
        raw_offsets: list[int] = []
        for raw_index, character in enumerate(text or ""):
            value = MofaMineruNormalizationService.normalize_search_text(character)
            normalized_parts.append(value)
            raw_offsets.extend([raw_index] * len(value))
        normalized_text = "".join(normalized_parts)
        ranges: list[tuple[int, int]] = []
        for raw_term in query_terms:
            term = MofaMineruNormalizationService.normalize_search_text(raw_term)
            if not term:
                continue
            start = 0
            while True:
                match = normalized_text.find(term, start)
                if match < 0:
                    break
                end_index = match + len(term) - 1
                if match < len(raw_offsets) and end_index < len(raw_offsets):
                    ranges.append((raw_offsets[match], raw_offsets[end_index] + 1))
                start = match + max(1, len(term))
        ranges.sort()
        merged: list[tuple[int, int]] = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return tuple(merged)

    @staticmethod
    def _indexed_page_from_row(row) -> MofaIndexedPage:
        return MofaIndexedPage(
            document_id=str(row["document_id"]),
            generation_id=str(row["generation_id"]),
            native_id=str(row["native_id"]),
            year=int(row["gregorian_year"]),
            volume_code=str(row["volume_code"]),
            title=str(row["title"]),
            page_index=int(row["page_index"]),
            display_page=int(row["display_page"]),
            source_pdf_page=(
                int(row["source_pdf_page"])
                if row["source_pdf_page"] is not None
                else None
            ),
            source_region=str(row["source_region"]),
            printed_page_label=str(row["printed_page_label"]),
            raw_text=str(row["raw_text"]),
            search_text=str(row["search_text"]),
        )

    def get_indexed_page(
        self,
        document_id: str,
        generation_id: str,
        page_index: int,
    ) -> MofaIndexedPage | None:
        row = self.db.fetchone(
            """
            SELECT * FROM mofa_search_pages
            WHERE document_id = ? AND generation_id = ? AND page_index = ?
            """,
            (document_id, generation_id, int(page_index)),
        )
        return self._indexed_page_from_row(row) if row else None

    def indexed_pages_for_source_pdf(
        self,
        document_id: str,
        generation_id: str,
        source_pdf_page: int,
    ) -> tuple[MofaIndexedPage, ...]:
        rows = self.db.fetchall(
            """
            SELECT * FROM mofa_search_pages
            WHERE document_id = ? AND generation_id = ? AND source_pdf_page = ?
            ORDER BY page_index
            """,
            (document_id, generation_id, int(source_pdf_page)),
        )
        return tuple(self._indexed_page_from_row(row) for row in rows)

    def indexed_document_page_count(self, document_id: str, generation_id: str) -> int:
        row = self.db.fetchone(
            """
            SELECT COUNT(*) AS value FROM mofa_search_pages
            WHERE document_id = ? AND generation_id = ?
            """,
            (document_id, generation_id),
        )
        return int(row["value"] or 0) if row else 0

    @staticmethod
    def _fts_expression(terms: tuple[str, ...], mode: str) -> str:
        quoted = [f'search_text : "{term.replace(chr(34), chr(34) * 2)}"' for term in terms]
        return (" OR " if mode == SEARCH_MODE_ANY else " AND ").join(quoted)

    @staticmethod
    def _fts_expression_for_plan(plan: MofaSearchPlan) -> str:
        groups: list[str] = []
        for group in plan.groups:
            quoted = [
                f'search_text : "{item.term.replace(chr(34), chr(34) * 2)}"'
                for item in group
            ]
            groups.append("(" + " OR ".join(quoted) + ")")
        joiner = " OR " if plan.mode == SEARCH_MODE_ANY else " AND "
        return joiner.join(groups)

    @staticmethod
    def _short_where(column: str, terms: tuple[str, ...], mode: str) -> tuple[str, list[str]]:
        clauses = [f"instr({column}, ?) > 0" for _ in terms]
        return (f" {'OR' if mode == SEARCH_MODE_ANY else 'AND'} ".join(clauses), list(terms))

    @staticmethod
    def _short_where_for_plan(
        column: str,
        plan: MofaSearchPlan,
    ) -> tuple[str, list[str]]:
        groups: list[str] = []
        params: list[str] = []
        for group in plan.groups:
            groups.append("(" + " OR ".join(f"instr({column}, ?) > 0" for _ in group) + ")")
            params.extend(item.term for item in group)
        joiner = " OR " if plan.mode == SEARCH_MODE_ANY else " AND "
        return joiner.join(groups), params

    def _matching_blocks(
        self,
        document_id: str,
        page_index: int,
        plan: MofaSearchPlan,
    ) -> tuple[MofaSearchBlock, ...]:
        terms = plan.terms
        use_fts = all(len(term) >= 3 for term in terms)
        if use_fts:
            rows = self.db.fetchall(
                """
                SELECT b.*
                FROM mofa_search_blocks_fts f
                JOIN mofa_search_blocks b ON b.block_row_id = f.rowid
                WHERE mofa_search_blocks_fts MATCH ?
                  AND b.document_id = ? AND b.page_index = ?
                ORDER BY b.block_order
                """,
                (self._fts_expression(terms, SEARCH_MODE_ANY), document_id, page_index),
            )
        else:
            where, params = self._short_where("b.search_text", terms, SEARCH_MODE_ANY)
            rows = self.db.fetchall(
                f"""
                SELECT b.* FROM mofa_search_blocks b
                WHERE b.document_id = ? AND b.page_index = ? AND ({where})
                ORDER BY b.block_order
                """,
                (document_id, page_index, *params),
            )
        values: list[MofaSearchBlock] = []
        for row in rows:
            bbox = None
            if row["bbox_json"]:
                try:
                    parsed = json.loads(row["bbox_json"])
                    if isinstance(parsed, list) and len(parsed) == 4:
                        bbox = tuple(float(item) for item in parsed)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            values.append(
                MofaSearchBlock(
                    block_key=str(row["block_key"]),
                    block_order=int(row["block_order"]),
                    block_type=str(row["block_type"]),
                    bbox=bbox,
                    raw_text=str(row["raw_text"]),
                    search_text=str(row["search_text"]),
                )
            )
        return tuple(values)

    def execute_search(
        self,
        query: str,
        *,
        mode: str = SEARCH_MODE_PHRASE,
        expansion_level: str = EXPANSION_EXACT,
        year: int | None = None,
        volume_code: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> MofaSearchExecution:
        if mode not in SEARCH_MODES:
            raise ValueError(f"未知检索模式：{mode}")
        plan = self.lexicon.build_plan(query, mode, expansion_level)
        use_fts = all(len(term) >= 3 for term in plan.terms)
        expanded_query = any(item.category != "exact" for item in plan.expanded_terms)
        requested_end = max(1, int(offset) + int(limit))
        fetch_limit = min(5000, requested_end * 4) if expanded_query else int(limit)
        fetch_offset = 0 if expanded_query else int(offset)
        filters: list[str] = []
        filter_params: list[object] = []
        if year is not None:
            filters.append("p.gregorian_year = ?")
            filter_params.append(int(year))
        if volume_code:
            filters.append("p.volume_code = ?")
            filter_params.append(volume_code)
        suffix = "" if not filters else " AND " + " AND ".join(filters)
        if use_fts:
            rows = self.db.fetchall(
                f"""
                SELECT p.*, bm25(mofa_search_pages_fts) AS rank
                FROM mofa_search_pages_fts f
                JOIN mofa_search_pages p ON p.page_row_id = f.rowid
                WHERE mofa_search_pages_fts MATCH ? {suffix}
                ORDER BY rank, p.gregorian_year, p.volume_code, p.page_index
                LIMIT ? OFFSET ?
                """,
                (
                    self._fts_expression_for_plan(plan),
                    *filter_params,
                    fetch_limit,
                    fetch_offset,
                ),
            )
        else:
            where, term_params = self._short_where_for_plan("p.search_text", plan)
            rows = self.db.fetchall(
                f"""
                SELECT p.*, 0.0 AS rank FROM mofa_search_pages p
                WHERE ({where}) {suffix}
                ORDER BY p.gregorian_year, p.volume_code, p.page_index
                LIMIT ? OFFSET ?
                """,
                (*term_params, *filter_params, fetch_limit, fetch_offset),
            )
        hits: list[MofaSearchHit] = []
        for row in rows:
            document_id = str(row["document_id"])
            page_index = int(row["page_index"])
            page_search_text = str(row["search_text"])
            matched_terms = tuple(
                item for item in plan.expanded_terms if item.term in page_search_text
            )
            hits.append(
                MofaSearchHit(
                    document_id=document_id,
                    generation_id=str(row["generation_id"]),
                    native_id=str(row["native_id"]),
                    year=int(row["gregorian_year"]),
                    volume_code=str(row["volume_code"]),
                    title=str(row["title"]),
                    page_index=page_index,
                    display_page=int(row["display_page"]),
                    source_pdf_page=(
                        int(row["source_pdf_page"])
                        if row["source_pdf_page"] is not None
                        else None
                    ),
                    source_region=str(row["source_region"]),
                    printed_page_label=str(row["printed_page_label"]),
                    raw_text=str(row["raw_text"]),
                    search_text=str(row["search_text"]),
                    score=float(row["rank"] or 0.0),
                    matching_blocks=self._matching_blocks(document_id, page_index, plan),
                    matched_terms=matched_terms,
                    expansion_level=plan.expansion_level,
                    lexicon_revision=plan.lexicon_revision,
                )
            )
        if expanded_query:
            hits.sort(
                key=lambda item: (
                    -item.match_weight,
                    item.score,
                    item.year,
                    item.volume_code,
                    item.page_index,
                )
            )
            hits = hits[int(offset):int(offset) + int(limit)]
        return MofaSearchExecution(plan=plan, hits=tuple(hits))

    def search(
        self,
        query: str,
        *,
        mode: str = SEARCH_MODE_PHRASE,
        expansion_level: str = EXPANSION_EXACT,
        year: int | None = None,
        volume_code: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[MofaSearchHit]:
        return list(
            self.execute_search(
                query,
                mode=mode,
                expansion_level=expansion_level,
                year=year,
                volume_code=volume_code,
                limit=limit,
                offset=offset,
            ).hits
        )

    def index_summary(self) -> tuple[int, int, int]:
        row = self.db.fetchone(
            """
            SELECT COUNT(*) AS documents,
                   COALESCE(SUM(page_count), 0) AS pages,
                   COALESCE(SUM(block_count), 0) AS blocks
            FROM mofa_fts_index_state
            """
        )
        return (
            int(row["documents"] or 0),
            int(row["pages"] or 0),
            int(row["blocks"] or 0),
        ) if row else (0, 0, 0)
