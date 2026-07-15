"""Persistent MOFA research candidates and reproducible search provenance."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from services.db_service import DbService
from services.mofa_fulltext_search_service import MofaSearchBlock, MofaSearchHit
from services.mofa_mineru_normalization_service import MofaMineruNormalizationService


CANDIDATE_STATUS = "candidate"
RELEVANT_STATUS = "relevant"
EXCLUDED_STATUS = "excluded"
CANDIDATE_STATUSES = {CANDIDATE_STATUS, RELEVANT_STATUS, EXCLUDED_STATUS}


@dataclass(frozen=True)
class MofaSavedSearch:
    search_id: str
    query_text: str
    normalized_query: str
    search_mode: str
    year_filter: int | None
    volume_filter: str
    result_count: int
    expansion_level: str
    lexicon_revision: int
    expansion_snapshot: dict
    saved_at: str
    last_used_at: str


@dataclass(frozen=True)
class MofaCandidate:
    candidate_id: str
    document_id: str
    native_id: str
    generation_id: str
    year: int
    volume_code: str
    title: str
    page_index: int
    display_page: int
    source_pdf_page: int | None
    source_region: str
    printed_page_label: str
    raw_text: str
    research_status: str
    notes: str
    tags: tuple[str, ...]
    search_queries: tuple[str, ...]
    block_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MofaCandidateAddResult:
    total: int
    created: int
    merged: int
    candidate_ids: tuple[str, ...]
    search_id: str


class MofaCandidateService:
    def __init__(self, *, db_service: DbService | None = None) -> None:
        self.db = db_service or DbService()

    @staticmethod
    def _search_signature(
        query: str,
        mode: str,
        year: int | None,
        volume_code: str,
        expansion_level: str,
        lexicon_revision: int,
        expansion_snapshot: dict,
    ) -> tuple[str, str]:
        normalized = MofaMineruNormalizationService.normalize_search_text(query)
        payload = json.dumps(
            {
                "query": normalized,
                "mode": mode,
                "year": int(year) if year is not None else None,
                "volume": volume_code or "",
                "expansion_level": expansion_level or "exact",
                "lexicon_revision": max(0, int(lexicon_revision)),
                "expansion_snapshot": expansion_snapshot or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest(), normalized

    def save_search(
        self,
        query: str,
        mode: str,
        *,
        year: int | None = None,
        volume_code: str = "",
        result_count: int = 0,
        expansion_level: str = "exact",
        lexicon_revision: int = 0,
        expansion_snapshot: dict | None = None,
    ) -> MofaSavedSearch:
        query = (query or "").strip()
        if not query:
            raise ValueError("检索词不能为空")
        snapshot = expansion_snapshot or {}
        signature, normalized = self._search_signature(
            query,
            mode,
            year,
            volume_code,
            expansion_level,
            lexicon_revision,
            snapshot,
        )
        search_id = f"mofasearch-{signature[:24]}"
        now = self.db.utc_now_iso()
        self.db.execute(
            """
            INSERT INTO mofa_saved_searches(
                search_id, search_signature, query_text, normalized_query,
                search_mode, year_filter, volume_filter, result_count,
                expansion_level, lexicon_revision, expansion_snapshot_json,
                saved_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(search_signature) DO UPDATE SET
                query_text=excluded.query_text,
                result_count=excluded.result_count,
                last_used_at=excluded.last_used_at
            """,
            (
                search_id,
                signature,
                query,
                normalized,
                mode,
                int(year) if year is not None else None,
                volume_code or "",
                max(0, int(result_count)),
                expansion_level or "exact",
                max(0, int(lexicon_revision)),
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        row = self.db.fetchone("SELECT * FROM mofa_saved_searches WHERE search_id = ?", (search_id,))
        return self._saved_search_from_row(row)

    @staticmethod
    def _saved_search_from_row(row) -> MofaSavedSearch:
        try:
            snapshot = json.loads(row["expansion_snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        return MofaSavedSearch(
            search_id=str(row["search_id"]),
            query_text=str(row["query_text"]),
            normalized_query=str(row["normalized_query"]),
            search_mode=str(row["search_mode"]),
            year_filter=int(row["year_filter"]) if row["year_filter"] is not None else None,
            volume_filter=str(row["volume_filter"]),
            result_count=int(row["result_count"]),
            expansion_level=str(row["expansion_level"] or "exact"),
            lexicon_revision=int(row["lexicon_revision"] or 0),
            expansion_snapshot=snapshot if isinstance(snapshot, dict) else {},
            saved_at=str(row["saved_at"]),
            last_used_at=str(row["last_used_at"]),
        )

    @staticmethod
    def _candidate_id(hit: MofaSearchHit) -> str:
        digest = hashlib.sha256(f"{hit.document_id}\0{hit.page_index}".encode("utf-8")).hexdigest()
        return f"mofacandidate-{digest[:24]}"

    def add_hit(self, hit: MofaSearchHit, saved_search: MofaSavedSearch) -> tuple[str, bool]:
        candidate_id = self._candidate_id(hit)
        now = self.db.utc_now_iso()
        existing = self.db.fetchone(
            "SELECT generation_id FROM mofa_research_candidates WHERE candidate_id = ?",
            (candidate_id,),
        )
        created = existing is None
        generation_changed = bool(existing and str(existing["generation_id"]) != hit.generation_id)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_research_candidates(
                    candidate_id, document_id, native_id, generation_id,
                    gregorian_year, volume_code, title, page_index, display_page,
                    source_pdf_page, source_region, printed_page_label, raw_text,
                    research_status, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', '', ?, ?)
                ON CONFLICT(document_id, page_index) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    gregorian_year=excluded.gregorian_year,
                    volume_code=excluded.volume_code,
                    title=excluded.title,
                    display_page=excluded.display_page,
                    source_pdf_page=excluded.source_pdf_page,
                    source_region=excluded.source_region,
                    printed_page_label=excluded.printed_page_label,
                    raw_text=excluded.raw_text,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate_id,
                    hit.document_id,
                    hit.native_id,
                    hit.generation_id,
                    hit.year,
                    hit.volume_code,
                    hit.title,
                    hit.page_index,
                    hit.display_page,
                    hit.source_pdf_page,
                    hit.source_region,
                    hit.printed_page_label,
                    hit.raw_text,
                    now,
                    now,
                ),
            )
            if generation_changed:
                conn.execute("DELETE FROM mofa_candidate_blocks WHERE candidate_id = ?", (candidate_id,))
            conn.execute(
                """
                INSERT INTO mofa_candidate_search_sources(candidate_id, search_id, score, recorded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(candidate_id, search_id) DO UPDATE SET
                    score=excluded.score,
                    recorded_at=excluded.recorded_at
                """,
                (candidate_id, saved_search.search_id, hit.score, now),
            )
            for block in hit.matching_blocks:
                self._upsert_block(conn, candidate_id, hit.generation_id, block)
        return candidate_id, created

    @staticmethod
    def _upsert_block(conn, candidate_id: str, generation_id: str, block: MofaSearchBlock) -> None:
        conn.execute(
            """
            INSERT INTO mofa_candidate_blocks(
                candidate_id, block_key, generation_id, block_order, block_type,
                bbox_json, raw_text, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, block_key) DO UPDATE SET
                generation_id=excluded.generation_id,
                block_order=excluded.block_order,
                block_type=excluded.block_type,
                bbox_json=excluded.bbox_json,
                raw_text=excluded.raw_text,
                search_text=excluded.search_text
            """,
            (
                candidate_id,
                block.block_key,
                generation_id,
                block.block_order,
                block.block_type,
                json.dumps(block.bbox) if block.bbox is not None else None,
                block.raw_text,
                block.search_text,
            ),
        )

    def add_hits(
        self,
        hits: Iterable[MofaSearchHit],
        *,
        query: str,
        mode: str,
        year: int | None = None,
        volume_code: str = "",
        result_count: int | None = None,
        expansion_level: str = "exact",
        lexicon_revision: int = 0,
        expansion_snapshot: dict | None = None,
    ) -> MofaCandidateAddResult:
        values = list(hits)
        saved = self.save_search(
            query,
            mode,
            year=year,
            volume_code=volume_code,
            result_count=len(values) if result_count is None else result_count,
            expansion_level=expansion_level,
            lexicon_revision=lexicon_revision,
            expansion_snapshot=expansion_snapshot,
        )
        created = 0
        ids: list[str] = []
        for hit in values:
            candidate_id, was_created = self.add_hit(hit, saved)
            ids.append(candidate_id)
            created += int(was_created)
        return MofaCandidateAddResult(
            total=len(values),
            created=created,
            merged=len(values) - created,
            candidate_ids=tuple(ids),
            search_id=saved.search_id,
        )

    def _candidate_maps(self) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], dict[str, int]]:
        tags: dict[str, list[str]] = {}
        for row in self.db.fetchall("SELECT candidate_id, tag FROM mofa_candidate_tags ORDER BY tag"):
            tags.setdefault(str(row["candidate_id"]), []).append(str(row["tag"]))
        queries: dict[str, list[str]] = {}
        for row in self.db.fetchall(
            """
            SELECT cs.candidate_id, s.query_text
            FROM mofa_candidate_search_sources cs
            JOIN mofa_saved_searches s ON s.search_id = cs.search_id
            ORDER BY s.saved_at, s.query_text
            """
        ):
            values = queries.setdefault(str(row["candidate_id"]), [])
            query = str(row["query_text"])
            if query not in values:
                values.append(query)
        blocks = {
            str(row["candidate_id"]): int(row["value"])
            for row in self.db.fetchall(
                "SELECT candidate_id, COUNT(*) AS value FROM mofa_candidate_blocks GROUP BY candidate_id"
            )
        }
        return (
            {key: tuple(value) for key, value in tags.items()},
            {key: tuple(value) for key, value in queries.items()},
            blocks,
        )

    def list_candidates(
        self,
        *,
        status: str = "",
        year: int | None = None,
        tag: str = "",
        search_text: str = "",
    ) -> list[MofaCandidate]:
        if status and status not in CANDIDATE_STATUSES:
            raise ValueError(f"未知候选状态：{status}")
        rows = self.db.fetchall(
            """
            SELECT * FROM mofa_research_candidates
            ORDER BY gregorian_year, volume_code, title, page_index
            """
        )
        tags, queries, blocks = self._candidate_maps()
        needle = (search_text or "").strip().lower()
        result: list[MofaCandidate] = []
        for row in rows:
            candidate_id = str(row["candidate_id"])
            row_tags = tags.get(candidate_id, ())
            row_queries = queries.get(candidate_id, ())
            if status and str(row["research_status"]) != status:
                continue
            if year is not None and int(row["gregorian_year"]) != int(year):
                continue
            if tag and tag not in row_tags:
                continue
            haystack = " ".join(
                (
                    str(row["title"]),
                    str(row["native_id"]),
                    str(row["notes"]),
                    *row_tags,
                    *row_queries,
                )
            ).lower()
            if needle and needle not in haystack:
                continue
            result.append(
                MofaCandidate(
                    candidate_id=candidate_id,
                    document_id=str(row["document_id"]),
                    native_id=str(row["native_id"]),
                    generation_id=str(row["generation_id"]),
                    year=int(row["gregorian_year"]),
                    volume_code=str(row["volume_code"]),
                    title=str(row["title"]),
                    page_index=int(row["page_index"]),
                    display_page=int(row["display_page"]),
                    source_pdf_page=(int(row["source_pdf_page"]) if row["source_pdf_page"] is not None else None),
                    source_region=str(row["source_region"]),
                    printed_page_label=str(row["printed_page_label"]),
                    raw_text=str(row["raw_text"]),
                    research_status=str(row["research_status"]),
                    notes=str(row["notes"]),
                    tags=row_tags,
                    search_queries=row_queries,
                    block_count=blocks.get(candidate_id, 0),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        return result

    def update_status(self, candidate_ids: Iterable[str], status: str) -> int:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"未知候选状态：{status}")
        values = tuple(dict.fromkeys(str(value) for value in candidate_ids if value))
        now = self.db.utc_now_iso()
        with self.db.transaction() as conn:
            for candidate_id in values:
                conn.execute(
                    "UPDATE mofa_research_candidates SET research_status = ?, updated_at = ? WHERE candidate_id = ?",
                    (status, now, candidate_id),
                )
        return len(values)

    def update_notes(self, candidate_id: str, notes: str) -> None:
        self.db.execute(
            "UPDATE mofa_research_candidates SET notes = ?, updated_at = ? WHERE candidate_id = ?",
            (notes or "", self.db.utc_now_iso(), candidate_id),
        )

    def set_tags(self, candidate_id: str, tags: Iterable[str]) -> tuple[str, ...]:
        values = tuple(
            dict.fromkeys(
                value.strip()
                for value in tags
                if value and value.strip()
            )
        )
        now = self.db.utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM mofa_candidate_tags WHERE candidate_id = ?", (candidate_id,))
            conn.executemany(
                "INSERT INTO mofa_candidate_tags(candidate_id, tag, created_at) VALUES (?, ?, ?)",
                ((candidate_id, tag, now) for tag in values),
            )
            conn.execute(
                "UPDATE mofa_research_candidates SET updated_at = ? WHERE candidate_id = ?",
                (now, candidate_id),
            )
        return values

    def available_tags(self) -> list[str]:
        return [
            str(row["tag"])
            for row in self.db.fetchall("SELECT DISTINCT tag FROM mofa_candidate_tags ORDER BY tag")
        ]

    def list_saved_searches(self) -> list[MofaSavedSearch]:
        return [
            self._saved_search_from_row(row)
            for row in self.db.fetchall(
                "SELECT * FROM mofa_saved_searches ORDER BY last_used_at DESC, query_text"
            )
        ]

    def summary(self) -> dict[str, int]:
        values = {status: 0 for status in CANDIDATE_STATUSES}
        for row in self.db.fetchall(
            "SELECT research_status, COUNT(*) AS value FROM mofa_research_candidates GROUP BY research_status"
        ):
            values[str(row["research_status"])] = int(row["value"])
        values["total"] = sum(values[status] for status in CANDIDATE_STATUSES)
        return values

    def status_for_page(self, document_id: str, page_index: int) -> str:
        row = self.db.fetchone(
            """
            SELECT research_status FROM mofa_research_candidates
            WHERE document_id = ? AND page_index = ?
            """,
            (document_id, int(page_index)),
        )
        return str(row["research_status"]) if row else ""
