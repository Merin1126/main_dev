"""Read-only corpus validation with persisted MOFA audit reports."""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable

import fitz

from services.db_service import DbService
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService
from services.mofa_mineru_normalization_service import NORMALIZED_PAGES_FILENAME


@dataclass(frozen=True)
class MofaCorpusIssue:
    native_id: str
    title: str
    severity: str
    stage: str
    code: str
    repair_action: str
    message: str
    details: dict


@dataclass(frozen=True)
class MofaCorpusAuditReport:
    audit_id: str
    scope_label: str
    entry_count: int
    healthy_count: int
    issues: tuple[MofaCorpusIssue, ...]
    duration_ms: int
    database_size_bytes: int
    indexed_document_count: int
    indexed_page_count: int
    indexed_block_count: int
    search_probe_ms: float
    search_probe_hits: int
    stage_counts: dict[str, int]
    repair_counts: dict[str, int]
    started_at: str
    finished_at: str

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def problem_entry_count(self) -> int:
        return len({item.native_id for item in self.issues})

    def native_ids_for_action(self, action: str) -> tuple[str, ...]:
        return tuple(
            sorted({item.native_id for item in self.issues if item.repair_action == action})
        )


class MofaCorpusAuditService:
    """Validate PDF -> MinerU -> Generation -> search JSON -> FTS consistency."""

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

    @staticmethod
    def _pdf_pages(path: str) -> int:
        with fitz.open(path) as document:
            return len(document)

    @staticmethod
    def _resolve_bundle_path(bundle_dir: str, value: str) -> str:
        path = str(value or "")
        return path if os.path.isabs(path) else os.path.join(bundle_dir, path)

    @staticmethod
    def _database_size(path: str) -> int:
        return sum(
            os.path.getsize(candidate)
            for candidate in (path, f"{path}-wal", f"{path}-shm")
            if os.path.isfile(candidate)
        )

    def _active_generations(self) -> dict[str, dict]:
        return {
            str(row["document_id"]): dict(row)
            for row in self.db.fetchall(
                """
                SELECT a.document_id, g.*
                FROM mofa_ocr_active_generations a
                JOIN mofa_ocr_generations g ON g.generation_id = a.generation_id
                """
            )
        }

    def _index_states(self) -> dict[str, dict]:
        return {
            str(row["document_id"]): dict(row)
            for row in self.db.fetchall("SELECT * FROM mofa_fts_index_state")
        }

    def _indexed_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        pages = {
            str(row["document_id"]): int(row["value"])
            for row in self.db.fetchall(
                "SELECT document_id, COUNT(*) AS value FROM mofa_search_pages GROUP BY document_id"
            )
        }
        blocks = {
            str(row["document_id"]): int(row["value"])
            for row in self.db.fetchall(
                "SELECT document_id, COUNT(*) AS value FROM mofa_search_blocks GROUP BY document_id"
            )
        }
        return pages, blocks

    @staticmethod
    def _issue(
        entry: MofaLibraryEntry,
        severity: str,
        stage: str,
        code: str,
        action: str,
        message: str,
        **details,
    ) -> MofaCorpusIssue:
        return MofaCorpusIssue(
            native_id=entry.native_id,
            title=entry.title,
            severity=severity,
            stage=stage,
            code=code,
            repair_action=action,
            message=message,
            details=details,
        )

    def _validate_normalized_pages(
        self,
        entry: MofaLibraryEntry,
        generation: dict,
        expected_pages: int,
    ) -> tuple[list[MofaCorpusIssue], int, int]:
        issues: list[MofaCorpusIssue] = []
        manifest_path = self._resolve_bundle_path(
            entry.bundle_dir,
            str(generation.get("artifact_path") or ""),
        )
        pages_path = os.path.join(os.path.dirname(manifest_path), NORMALIZED_PAGES_FILENAME)
        if not os.path.isfile(manifest_path) or not os.path.isfile(pages_path):
            return [
                self._issue(
                    entry,
                    "error",
                    "standardize",
                    "missing_generation_artifact",
                    "standardize",
                    "active Generation 的标准化文件缺失",
                    manifest_path=manifest_path,
                    pages_path=pages_path,
                )
            ], 0, 0
        page_count = block_count = 0
        bad_bbox = 0
        try:
            with open(pages_path, "r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    page = json.loads(line)
                    if int(page.get("page_index", -1)) != page_count:
                        raise ValueError(f"page_index 在 {page_count} 处不连续")
                    if str(page.get("generation_id") or "") != str(generation["generation_id"]):
                        raise ValueError("页文件 generation_id 与 active Generation 不一致")
                    for block in page.get("blocks") or []:
                        block_count += 1
                        bbox = block.get("bbox_norm")
                        if bbox is not None and (
                            not isinstance(bbox, list)
                            or len(bbox) != 4
                            or any(float(value) < 0 or float(value) > 1 for value in bbox)
                            or float(bbox[2]) <= float(bbox[0])
                            or float(bbox[3]) <= float(bbox[1])
                        ):
                            bad_bbox += 1
                    page_count += 1
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                self._issue(
                    entry,
                    "error",
                    "standardize",
                    "invalid_generation_pages",
                    "standardize",
                    f"标准化页文件无效：{exc}",
                    pages_path=pages_path,
                )
            )
            return issues, page_count, block_count
        if page_count != expected_pages or page_count != int(generation.get("page_count") or 0):
            issues.append(
                self._issue(
                    entry,
                    "error",
                    "standardize",
                    "generation_page_count_mismatch",
                    "standardize",
                    f"Generation 页数不一致：标准化 {page_count} / single-pages {expected_pages}",
                    generation_pages=int(generation.get("page_count") or 0),
                )
            )
        if block_count != int(generation.get("block_count") or 0):
            issues.append(
                self._issue(
                    entry,
                    "error",
                    "standardize",
                    "generation_block_count_mismatch",
                    "standardize",
                    f"Generation block 数不一致：{block_count}/{generation.get('block_count')}",
                )
            )
        if bad_bbox:
            issues.append(
                self._issue(
                    entry,
                    "error",
                    "viewer",
                    "invalid_bbox",
                    "standardize",
                    f"发现 {bad_bbox} 个越界或无效 bbox",
                )
            )
        return issues, page_count, block_count

    def _validate_search_json(
        self,
        entry: MofaLibraryEntry,
        generation: dict,
        expected_pages: int,
    ) -> list[MofaCorpusIssue]:
        path = self._resolve_bundle_path(
            entry.bundle_dir,
            str(generation.get("search_text_path") or ""),
        )
        if not os.path.isfile(path):
            return [
                self._issue(
                    entry,
                    "error",
                    "standardize",
                    "missing_search_json",
                    "standardize",
                    "当前 Generation 的分页检索 JSON 缺失",
                    path=path,
                )
            ]
        try:
            with open(path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if str(payload.get("generation_id") or "") != str(generation["generation_id"]):
                raise ValueError("generation_id 不一致")
            if len(payload.get("pages") or []) != expected_pages:
                raise ValueError("pages 数量不一致")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return [
                self._issue(
                    entry,
                    "error",
                    "standardize",
                    "invalid_search_json",
                    "standardize",
                    f"分页检索 JSON 无效：{exc}",
                    path=path,
                )
            ]
        return []

    def audit_entries(
        self,
        entries: Iterable[MofaLibraryEntry],
        *,
        scope_label: str = "当前范围",
        persist: bool = True,
        on_progress: Callable[[int, int, MofaLibraryEntry], None] | None = None,
    ) -> MofaCorpusAuditReport:
        values = list(entries)
        audit_id = f"mofa-audit-{uuid.uuid4().hex[:20]}"
        started_at = self.db.utc_now_iso()
        started = time.perf_counter()
        generations = self._active_generations()
        index_states = self._index_states()
        indexed_pages, indexed_blocks = self._indexed_counts()
        issues: list[MofaCorpusIssue] = []

        for position, entry in enumerate(values, start=1):
            document_id = self.db.make_document_id("mofa", entry.native_id)
            if not entry.pdf_exists:
                issues.append(self._issue(entry, "info", "download", "missing_pdf", "download", "PDF 尚未下载"))
                if on_progress:
                    on_progress(position, len(values), entry)
                continue
            if not entry.split_pdf_exists:
                issues.append(self._issue(entry, "warning", "split", "missing_split_pdf", "split", "尚未生成 single-pages PDF"))
                if on_progress:
                    on_progress(position, len(values), entry)
                continue
            try:
                split_pages = self._pdf_pages(entry.split_pdf_path)
                if split_pages <= 0:
                    raise ValueError("PDF 页数为 0")
            except Exception as exc:
                issues.append(self._issue(entry, "error", "split", "invalid_split_pdf", "split", f"single-pages PDF 无法读取：{exc}"))
                if on_progress:
                    on_progress(position, len(values), entry)
                continue
            if entry.mineru_expected_parts <= 0:
                issues.append(self._issue(entry, "error", "split", "missing_input_parts", "split", "无法读取 MinerU 输入分段"))
            elif entry.mineru_archived_parts < entry.mineru_expected_parts:
                issues.append(
                    self._issue(
                        entry,
                        "warning",
                        "import",
                        "incomplete_ocr_parts",
                        "import",
                        f"MinerU 分段仅归档 {entry.mineru_archived_parts}/{entry.mineru_expected_parts}",
                    )
                )

            generation = generations.get(document_id)
            generation_valid = True
            if generation is None:
                generation_valid = False
                if entry.mineru_archived_parts >= max(1, entry.mineru_expected_parts):
                    issues.append(self._issue(entry, "warning", "standardize", "missing_active_generation", "standardize", "MinerU 结果完整但尚未生成 active Generation"))
            else:
                generation_issues, _pages, _blocks = self._validate_normalized_pages(
                    entry,
                    generation,
                    split_pages,
                )
                search_issues = self._validate_search_json(entry, generation, split_pages)
                issues.extend(generation_issues)
                issues.extend(search_issues)
                generation_valid = not any(
                    item.native_id == entry.native_id and item.repair_action == "standardize"
                    for item in (*generation_issues, *search_issues)
                )

            state = index_states.get(document_id)
            if generation is not None and generation_valid:
                expected_index_blocks = int(generation.get("searchable_block_count") or 0)
                if state is None:
                    issues.append(self._issue(entry, "warning", "index", "missing_fts_index", "reindex", "active Generation 尚未进入全文索引"))
                elif (
                    str(state.get("generation_id") or "") != str(generation["generation_id"])
                    or int(state.get("page_count") or 0) != split_pages
                    or int(state.get("block_count") or 0) != expected_index_blocks
                    or indexed_pages.get(document_id, 0) != split_pages
                    or indexed_blocks.get(document_id, 0) != expected_index_blocks
                ):
                    issues.append(
                        self._issue(
                            entry,
                            "error",
                            "index",
                            "stale_or_incomplete_fts_index",
                            "reindex",
                            "全文索引与 active Generation 不一致",
                            state_generation=state.get("generation_id"),
                            active_generation=generation["generation_id"],
                            indexed_pages=indexed_pages.get(document_id, 0),
                            expected_pages=split_pages,
                            indexed_blocks=indexed_blocks.get(document_id, 0),
                            expected_blocks=expected_index_blocks,
                        )
                    )
            if on_progress:
                on_progress(position, len(values), entry)

        problem_ids = {item.native_id for item in issues}
        stage_counts = dict(Counter(item.stage for item in issues))
        repair_counts = dict(Counter(item.repair_action for item in issues if item.repair_action))
        duration_ms = round((time.perf_counter() - started) * 1000)
        index_summary = self.db.fetchone(
            """
            SELECT COUNT(*) AS documents,
                   COALESCE(SUM(page_count), 0) AS pages,
                   COALESCE(SUM(block_count), 0) AS blocks
            FROM mofa_fts_index_state
            """
        )
        probe_started = time.perf_counter()
        probe = self.db.fetchone(
            """
            SELECT COUNT(*) AS value
            FROM mofa_search_pages_fts
            WHERE mofa_search_pages_fts MATCH 'search_text : "共産党"'
            """
        )
        search_probe_ms = round((time.perf_counter() - probe_started) * 1000, 3)
        finished_at = self.db.utc_now_iso()
        report = MofaCorpusAuditReport(
            audit_id=audit_id,
            scope_label=scope_label,
            entry_count=len(values),
            healthy_count=len(values) - len(problem_ids),
            issues=tuple(issues),
            duration_ms=duration_ms,
            database_size_bytes=self._database_size(self.db.db_path),
            indexed_document_count=int(index_summary["documents"] or 0) if index_summary else 0,
            indexed_page_count=int(index_summary["pages"] or 0) if index_summary else 0,
            indexed_block_count=int(index_summary["blocks"] or 0) if index_summary else 0,
            search_probe_ms=search_probe_ms,
            search_probe_hits=int(probe["value"] or 0) if probe else 0,
            stage_counts=stage_counts,
            repair_counts=repair_counts,
            started_at=started_at,
            finished_at=finished_at,
        )
        if persist:
            self._persist(report)
        return report

    def _persist(self, report: MofaCorpusAuditReport) -> None:
        summary = {
            "problem_entries": report.problem_entry_count,
            "stage_counts": report.stage_counts,
            "repair_counts": report.repair_counts,
            "index": {
                "documents": report.indexed_document_count,
                "pages": report.indexed_page_count,
                "blocks": report.indexed_block_count,
            },
            "search_probe": {
                "query": "共産党",
                "hits": report.search_probe_hits,
                "duration_ms": report.search_probe_ms,
            },
        }
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_corpus_audit_runs(
                    audit_id, scope_label, status, entry_count, healthy_count,
                    issue_count, duration_ms, database_size_bytes, summary_json,
                    started_at, finished_at
                ) VALUES (?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.audit_id,
                    report.scope_label,
                    report.entry_count,
                    report.healthy_count,
                    report.issue_count,
                    report.duration_ms,
                    report.database_size_bytes,
                    json.dumps(summary, ensure_ascii=False),
                    report.started_at,
                    report.finished_at,
                ),
            )
            for issue in report.issues:
                document_id = self.db.make_document_id("mofa", issue.native_id)
                exists = conn.execute(
                    "SELECT 1 FROM documents WHERE document_id = ?",
                    (document_id,),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO mofa_corpus_audit_issues(
                        audit_id, document_id, native_id, title, severity, stage,
                        code, repair_action, message, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.audit_id,
                        document_id if exists else None,
                        issue.native_id,
                        issue.title,
                        issue.severity,
                        issue.stage,
                        issue.code,
                        issue.repair_action,
                        issue.message,
                        json.dumps(issue.details, ensure_ascii=False),
                    ),
                )
