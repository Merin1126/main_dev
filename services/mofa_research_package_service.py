"""Create MOFA-specific research work packages from page-level candidates."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from typing import Iterable

from services.db_service import DbService
from services.mofa_candidate_service import EXCLUDED_STATUS, MofaCandidate, MofaCandidateService
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService


MOFA_PACKAGE_SOURCE = "mofa"
MOFA_PACKAGE_TYPE = "mofa_research_package"
MOFA_PACKAGE_ID_PREFIX = "MOFA-RP-"
MOFA_PACKAGE_MANIFEST = "mofa_research_package.json"
MOFA_PACKAGE_STATUSES = {"draft", "ready", "processing", "completed", "archived"}
MOFA_PACKAGE_SCOPE_CONTEXT = "candidate_context"
MOFA_PACKAGE_SCOPE_FULL_DOCUMENT = "full_document"
MOFA_PACKAGE_SCOPES = {MOFA_PACKAGE_SCOPE_CONTEXT, MOFA_PACKAGE_SCOPE_FULL_DOCUMENT}


@dataclass(frozen=True)
class MofaResearchRangePlan:
    document_id: str
    native_id: str
    generation_id: str
    year: int
    volume_code: str
    title: str
    start_page_index: int
    end_page_index: int
    selected_page_indexes: tuple[int, ...]
    candidate_ids: tuple[str, ...]
    page_count: int
    split_pdf_path: str
    bundle_dir: str

    @property
    def included_page_count(self) -> int:
        return self.end_page_index - self.start_page_index + 1

    @property
    def display_range(self) -> str:
        return f"{self.start_page_index + 1}—{self.end_page_index + 1}"


@dataclass(frozen=True)
class MofaResearchPackagePlan:
    package_id: str
    package_signature: str
    default_display_name: str
    selection_scope: str
    context_before: int
    context_after: int
    candidate_ids: tuple[str, ...]
    ranges: tuple[MofaResearchRangePlan, ...]

    @property
    def document_count(self) -> int:
        return len({item.document_id for item in self.ranges})

    @property
    def range_count(self) -> int:
        return len(self.ranges)

    @property
    def selected_page_count(self) -> int:
        return len(self.candidate_ids)

    @property
    def included_page_count(self) -> int:
        return sum(item.included_page_count for item in self.ranges)


@dataclass(frozen=True)
class MofaResearchPackage:
    package_id: str
    package_signature: str
    source: str
    package_type: str
    display_name: str
    status: str
    relative_dir: str
    package_dir: str
    manifest_path: str
    selection_scope: str
    context_before: int
    context_after: int
    document_count: int
    range_count: int
    selected_page_count: int
    included_page_count: int
    notes: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MofaResearchPackageCreateResult:
    package: MofaResearchPackage
    created: bool


@dataclass(frozen=True)
class MofaResearchPackageDeleteResult:
    package_id: str
    candidate_count: int
    cancelled_candidate_count: int
    retained_candidate_count: int
    retaining_package_ids: tuple[str, ...]
    directory_removed: bool
    directory_error: str


class MofaResearchPackageService:
    """Persist source-explicit MOFA research packages without copying source PDFs."""

    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
        library_service: MofaLibraryService | None = None,
        candidate_service: MofaCandidateService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.db = db_service or DbService()
        self.library = library_service or MofaLibraryService(
            project_root=self.project_root,
            db_service=self.db,
        )
        self.candidates = candidate_service or MofaCandidateService(db_service=self.db)
        self.research_root = os.path.join(
            self.project_root,
            "Historical_Documents",
            "research",
            MOFA_PACKAGE_SOURCE,
        )

    @staticmethod
    def _validate_context(value: int, label: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if result < 0 or result > 50:
            raise ValueError(f"{label}必须在 0—50 页之间")
        return result

    def _candidate_map(self, candidate_ids: Iterable[str]) -> dict[str, MofaCandidate]:
        requested = tuple(dict.fromkeys(str(value) for value in candidate_ids if value))
        if not requested:
            raise ValueError("请至少选择一条 MOFA 候选史料")
        values = {item.candidate_id: item for item in self.candidates.list_candidates()}
        missing = [candidate_id for candidate_id in requested if candidate_id not in values]
        if missing:
            raise ValueError(f"有 {len(missing)} 条候选记录不存在或已删除")
        selected = {candidate_id: values[candidate_id] for candidate_id in requested}
        excluded = [item for item in selected.values() if item.research_status == EXCLUDED_STATUS]
        if excluded:
            raise ValueError("已排除的候选页不能加入研究工作包；请先调整研究状态")
        return selected

    def _entry_map(self) -> dict[str, MofaLibraryEntry]:
        return {
            entry.native_id: entry
            for entry in self.library.list_entries(item_kind="")
        }

    def _generation_page_count(self, candidate: MofaCandidate) -> int:
        active = self.db.fetchone(
            """
            SELECT a.generation_id, g.page_count
            FROM mofa_ocr_active_generations a
            JOIN mofa_ocr_generations g ON g.generation_id = a.generation_id
            WHERE a.document_id = ? AND g.status = 'complete'
            """,
            (candidate.document_id,),
        )
        if not active:
            raise ValueError(f"{candidate.title} 没有可用的 active OCR Generation")
        if str(active["generation_id"]) != candidate.generation_id:
            raise ValueError(
                f"{candidate.title} 的候选页来自旧 OCR Generation；请重新检索并更新候选页"
            )
        page_count = int(active["page_count"] or 0)
        if page_count <= 0:
            raise ValueError(f"{candidate.title} 的 OCR 页数无效")
        return page_count

    @staticmethod
    def _signature(
        candidate_ids: Iterable[str],
        selection_scope: str,
        context_before: int,
        context_after: int,
    ) -> str:
        payload = json.dumps(
            {
                "source": MOFA_PACKAGE_SOURCE,
                "candidate_ids": sorted(candidate_ids),
                "selection_scope": selection_scope,
                "context_before": context_before,
                "context_after": context_after,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _default_name(
        candidates: Iterable[MofaCandidate],
        ranges: Iterable[MofaResearchRangePlan],
        selection_scope: str,
    ) -> str:
        values = tuple(candidates)
        range_values = tuple(ranges)
        years = sorted({item.year for item in values})
        if len({item.document_id for item in values}) == 1:
            title = values[0].title.strip() or values[0].native_id
            if len(title) > 42:
                title = f"{title[:39]}…"
            if selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT:
                return (
                    f"MOFA整份PDF研究工作包｜{values[0].year}｜{title}｜"
                    f"全{range_values[0].included_page_count}页"
                )
            selected = sorted({item.page_index + 1 for item in values})
            page_label = (
                f"p{selected[0]:04d}"
                if len(selected) == 1
                else f"p{selected[0]:04d}-p{selected[-1]:04d}"
            )
            return f"MOFA研究工作包｜{values[0].year}｜{title}｜{page_label}"
        year_label = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
        if selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT:
            return (
                f"MOFA整份PDF研究工作包｜{year_label}｜"
                f"{len({item.document_id for item in values})}份史料｜"
                f"全{sum(item.included_page_count for item in range_values)}页"
            )
        return (
            f"MOFA研究工作包｜{year_label}｜"
            f"{len({item.document_id for item in values})}份史料｜"
            f"{len(values)}个候选页｜{len(range_values)}个页段"
        )

    def preview(
        self,
        candidate_ids: Iterable[str],
        *,
        context_before: int = 0,
        context_after: int = 0,
        selection_scope: str = MOFA_PACKAGE_SCOPE_CONTEXT,
    ) -> MofaResearchPackagePlan:
        if selection_scope not in MOFA_PACKAGE_SCOPES:
            raise ValueError(f"未知工作包范围：{selection_scope}")
        if selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT:
            before = 0
            after = 0
        else:
            before = self._validate_context(context_before, "前置上下文")
            after = self._validate_context(context_after, "后置上下文")
        candidate_map = self._candidate_map(candidate_ids)
        entries = self._entry_map()
        grouped: dict[str, list[MofaCandidate]] = {}
        for candidate in candidate_map.values():
            if not candidate.document_id.startswith("mofa:"):
                raise ValueError("研究工作包只接受 source=mofa 的候选页")
            grouped.setdefault(candidate.document_id, []).append(candidate)

        ranges: list[MofaResearchRangePlan] = []
        for document_id, document_candidates in sorted(
            grouped.items(),
            key=lambda item: (
                min(value.year for value in item[1]),
                item[1][0].volume_code,
                item[1][0].title,
            ),
        ):
            document_candidates.sort(key=lambda item: item.page_index)
            first = document_candidates[0]
            if any(item.generation_id != first.generation_id for item in document_candidates):
                raise ValueError(
                    f"{first.title} 的候选页混用了不同 OCR Generation；请重新检索并更新候选页"
                )
            page_count = self._generation_page_count(first)
            if any(item.page_index < 0 or item.page_index >= page_count for item in document_candidates):
                raise ValueError(f"{first.title} 存在超出当前 OCR 页数的候选页")
            entry = entries.get(first.native_id)
            if entry is None or not os.path.isfile(entry.split_pdf_path):
                raise ValueError(f"{first.title} 的 single-pages PDF 不存在")
            if selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT:
                intervals = [
                    {
                        "start": 0,
                        "end": page_count - 1,
                        "selected": [item.page_index for item in document_candidates],
                        "candidate_ids": [item.candidate_id for item in document_candidates],
                    }
                ]
            else:
                intervals = [
                    {
                        "start": max(0, item.page_index - before),
                        "end": min(page_count - 1, item.page_index + after),
                        "selected": [item.page_index],
                        "candidate_ids": [item.candidate_id],
                    }
                    for item in document_candidates
                ]
            merged: list[dict] = []
            for interval in intervals:
                if merged and interval["start"] <= merged[-1]["end"] + 1:
                    merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
                    merged[-1]["selected"].extend(interval["selected"])
                    merged[-1]["candidate_ids"].extend(interval["candidate_ids"])
                else:
                    merged.append(interval)
            for interval in merged:
                indexed = self.db.fetchone(
                    """
                    SELECT COUNT(*) AS value FROM mofa_search_pages
                    WHERE document_id = ? AND generation_id = ?
                      AND page_index BETWEEN ? AND ?
                    """,
                    (
                        document_id,
                        first.generation_id,
                        int(interval["start"]),
                        int(interval["end"]),
                    ),
                )
                expected = int(interval["end"]) - int(interval["start"]) + 1
                if not indexed or int(indexed["value"] or 0) != expected:
                    raise ValueError(
                        f"{first.title} 的全文索引缺少页段 "
                        f"{int(interval['start']) + 1}—{int(interval['end']) + 1}"
                    )
                ranges.append(
                    MofaResearchRangePlan(
                        document_id=document_id,
                        native_id=first.native_id,
                        generation_id=first.generation_id,
                        year=first.year,
                        volume_code=first.volume_code,
                        title=first.title,
                        start_page_index=int(interval["start"]),
                        end_page_index=int(interval["end"]),
                        selected_page_indexes=tuple(sorted(set(interval["selected"]))),
                        candidate_ids=tuple(dict.fromkeys(interval["candidate_ids"])),
                        page_count=page_count,
                        split_pdf_path=entry.split_pdf_path,
                        bundle_dir=entry.bundle_dir,
                    )
                )

        signature = self._signature(candidate_map, selection_scope, before, after)
        package_id = f"{MOFA_PACKAGE_ID_PREFIX}{signature[:20].upper()}"
        return MofaResearchPackagePlan(
            package_id=package_id,
            package_signature=signature,
            default_display_name=self._default_name(
                candidate_map.values(),
                ranges,
                selection_scope,
            ),
            selection_scope=selection_scope,
            context_before=before,
            context_after=after,
            candidate_ids=tuple(sorted(candidate_map)),
            ranges=tuple(ranges),
        )

    @staticmethod
    def _normalize_display_name(value: str, fallback: str) -> str:
        name = (value or "").strip() or fallback
        if not name.upper().startswith("MOFA"):
            name = f"MOFA研究工作包｜{name}"
        return name

    def _relative_dir(self, package_id: str) -> str:
        return os.path.join(
            "Historical_Documents",
            "research",
            MOFA_PACKAGE_SOURCE,
            package_id,
        )

    def create_package(
        self,
        candidate_ids: Iterable[str],
        *,
        context_before: int = 0,
        context_after: int = 0,
        selection_scope: str = MOFA_PACKAGE_SCOPE_CONTEXT,
        display_name: str = "",
        notes: str = "",
    ) -> MofaResearchPackageCreateResult:
        plan = self.preview(
            candidate_ids,
            context_before=context_before,
            context_after=context_after,
            selection_scope=selection_scope,
        )
        existing = self.db.fetchone(
            "SELECT package_id FROM mofa_research_packages WHERE package_signature = ?",
            (plan.package_signature,),
        )
        if existing:
            package = self.get_package(str(existing["package_id"]))
            self._ensure_package_layout(package)
            self.rebuild_manifest(package.package_id)
            return MofaResearchPackageCreateResult(package=package, created=False)

        now = self.db.utc_now_iso()
        relative_dir = self._relative_dir(plan.package_id)
        name = self._normalize_display_name(display_name, plan.default_display_name)
        candidate_lookup = self._candidate_map(plan.candidate_ids)
        range_rows: list[tuple] = []
        candidate_rows: list[tuple] = []
        for order, item in enumerate(plan.ranges):
            digest = hashlib.sha256(
                f"{plan.package_id}\0{item.document_id}\0{item.start_page_index}\0{item.end_page_index}".encode(
                    "utf-8"
                )
            ).hexdigest()
            range_id = f"MOFA-RANGE-{digest[:20].upper()}"
            range_rows.append(
                (
                    range_id,
                    plan.package_id,
                    item.document_id,
                    item.native_id,
                    item.generation_id,
                    item.year,
                    item.volume_code,
                    item.title,
                    item.start_page_index,
                    item.end_page_index,
                    json.dumps(item.selected_page_indexes),
                    item.included_page_count,
                    order,
                )
            )
            candidate_by_page = {
                candidate_lookup[candidate_id].page_index: candidate_id
                for candidate_id in item.candidate_ids
            }
            candidate_rows.extend(
                (
                    plan.package_id,
                    range_id,
                    candidate_by_page[page_index],
                    page_index,
                )
                for page_index in item.selected_page_indexes
            )

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO mofa_research_packages(
                    package_id, package_signature, source, package_type,
                    display_name, package_status, relative_dir, manifest_filename, selection_scope,
                    context_before, context_after, document_count, range_count,
                    selected_page_count, included_page_count, notes, created_at, updated_at
                ) VALUES (?, ?, 'mofa', 'mofa_research_package', ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.package_id,
                    plan.package_signature,
                    name,
                    relative_dir,
                    MOFA_PACKAGE_MANIFEST,
                    plan.selection_scope,
                    plan.context_before,
                    plan.context_after,
                    plan.document_count,
                    plan.range_count,
                    plan.selected_page_count,
                    plan.included_page_count,
                    notes or "",
                    now,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO mofa_research_package_ranges(
                    range_id, package_id, document_id, native_id, generation_id,
                    gregorian_year, volume_code, title, start_page_index,
                    end_page_index, selected_pages_json, included_page_count, range_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                range_rows,
            )
            conn.executemany(
                """
                INSERT INTO mofa_research_package_candidates(
                    package_id, range_id, candidate_id, page_index
                ) VALUES (?, ?, ?, ?)
                """,
                candidate_rows,
            )

        package = self.get_package(plan.package_id)
        self._ensure_package_layout(package)
        self.rebuild_manifest(package.package_id)
        return MofaResearchPackageCreateResult(package=package, created=True)

    def _package_from_row(self, row) -> MofaResearchPackage:
        relative_dir = str(row["relative_dir"])
        package_dir = os.path.join(self.project_root, relative_dir)
        return MofaResearchPackage(
            package_id=str(row["package_id"]),
            package_signature=str(row["package_signature"]),
            source=str(row["source"]),
            package_type=str(row["package_type"]),
            display_name=str(row["display_name"]),
            status=str(row["package_status"]),
            relative_dir=relative_dir,
            package_dir=package_dir,
            manifest_path=os.path.join(package_dir, str(row["manifest_filename"])),
            selection_scope=str(row["selection_scope"]),
            context_before=int(row["context_before"]),
            context_after=int(row["context_after"]),
            document_count=int(row["document_count"]),
            range_count=int(row["range_count"]),
            selected_page_count=int(row["selected_page_count"]),
            included_page_count=int(row["included_page_count"]),
            notes=str(row["notes"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_package(self, package_id: str) -> MofaResearchPackage:
        row = self.db.fetchone(
            "SELECT * FROM mofa_research_packages WHERE package_id = ?",
            (package_id,),
        )
        if not row:
            raise ValueError("MOFA 研究工作包不存在")
        return self._package_from_row(row)

    def list_packages(self, *, status: str = "") -> list[MofaResearchPackage]:
        if status and status not in MOFA_PACKAGE_STATUSES:
            raise ValueError(f"未知工作包状态：{status}")
        if status:
            rows = self.db.fetchall(
                """
                SELECT * FROM mofa_research_packages
                WHERE package_status = ? ORDER BY created_at DESC, display_name
                """,
                (status,),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM mofa_research_packages ORDER BY created_at DESC, display_name"
            )
        return [self._package_from_row(row) for row in rows]

    def list_ranges(self, package_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM mofa_research_package_ranges
                WHERE package_id = ? ORDER BY range_order
                """,
                (package_id,),
            )
        ]

    def update_status(self, package_id: str, status: str) -> MofaResearchPackage:
        if status not in MOFA_PACKAGE_STATUSES:
            raise ValueError(f"未知工作包状态：{status}")
        self.db.execute(
            """
            UPDATE mofa_research_packages
            SET package_status = ?, updated_at = ? WHERE package_id = ?
            """,
            (status, self.db.utc_now_iso(), package_id),
        )
        package = self.get_package(package_id)
        self.rebuild_manifest(package_id)
        return package

    def _remove_package_directory(self, package: MofaResearchPackage) -> tuple[bool, str]:
        root = os.path.realpath(self.research_root)
        target = os.path.realpath(package.package_dir)
        try:
            is_child = os.path.commonpath((root, target)) == root
        except ValueError:
            is_child = False
        if (
            not is_child
            or target == root
            or os.path.dirname(target) != root
            or os.path.basename(target) != package.package_id
            or not package.package_id.startswith(MOFA_PACKAGE_ID_PREFIX)
        ):
            return False, "工作包目录不在受管 MOFA research 根目录中，未删除本地文件"
        if not os.path.exists(target):
            return False, ""
        if not os.path.isdir(target):
            return False, "工作包路径不是目录，未删除本地文件"
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return False, str(exc)
        return True, ""

    def delete_package(
        self,
        package_id: str,
        *,
        cancel_candidates: bool = False,
    ) -> MofaResearchPackageDeleteResult:
        """Delete a draft/ready package and optionally withdraw exclusive candidates.

        Candidates referenced by another package are retained. Source PDFs, OCR,
        normalized artifacts, FTS rows, and saved searches are never removed.
        """
        package = self.get_package(package_id)
        if package.status not in {"draft", "ready"}:
            raise ValueError(
                "只能删除“草稿”或“待处理”工作包；"
                "处理中、已完成或已归档的工作包受溯源保护"
            )
        candidate_rows = self.db.fetchall(
            """
            SELECT candidate_id FROM mofa_research_package_candidates
            WHERE package_id = ? ORDER BY candidate_id
            """,
            (package_id,),
        )
        candidate_ids = tuple(str(row["candidate_id"]) for row in candidate_rows)
        retaining_rows = []
        cancellable = candidate_ids
        if cancel_candidates and candidate_ids:
            placeholders = ", ".join("?" for _ in candidate_ids)
            retaining_rows = self.db.fetchall(
                f"""
                SELECT DISTINCT candidate_id, package_id
                FROM mofa_research_package_candidates
                WHERE candidate_id IN ({placeholders}) AND package_id <> ?
                ORDER BY package_id, candidate_id
                """,
                (*candidate_ids, package_id),
            )
            retained_ids = {str(row["candidate_id"]) for row in retaining_rows}
            cancellable = tuple(value for value in candidate_ids if value not in retained_ids)

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM mofa_research_packages WHERE package_id = ?", (package_id,))
            if cancel_candidates and cancellable:
                conn.executemany(
                    "DELETE FROM mofa_research_candidates WHERE candidate_id = ?",
                    ((candidate_id,) for candidate_id in cancellable),
                )

        directory_removed, directory_error = self._remove_package_directory(package)
        cancelled = len(cancellable) if cancel_candidates else 0
        retained = len(candidate_ids) - cancelled
        retaining_packages = tuple(
            dict.fromkeys(str(row["package_id"]) for row in retaining_rows)
        )
        return MofaResearchPackageDeleteResult(
            package_id=package_id,
            candidate_count=len(candidate_ids),
            cancelled_candidate_count=cancelled,
            retained_candidate_count=retained,
            retaining_package_ids=retaining_packages,
            directory_removed=directory_removed,
            directory_error=directory_error,
        )

    @staticmethod
    def _atomic_json(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_path, path)

    def _ensure_package_layout(self, package: MofaResearchPackage) -> None:
        os.makedirs(package.package_dir, exist_ok=True)
        for child in ("ocr", "analysis", "translation", "export"):
            os.makedirs(os.path.join(package.package_dir, child), exist_ok=True)

    def _path_reference(self, path: str) -> str:
        absolute = os.path.abspath(path)
        try:
            if os.path.commonpath((self.project_root, absolute)) == self.project_root:
                return os.path.relpath(absolute, self.project_root)
        except ValueError:
            pass
        return absolute

    def _saved_searches_for_package(self, package_id: str) -> list[dict]:
        rows = self.db.fetchall(
            """
            SELECT DISTINCT s.*
            FROM mofa_research_package_candidates pc
            JOIN mofa_candidate_search_sources cs ON cs.candidate_id = pc.candidate_id
            JOIN mofa_saved_searches s ON s.search_id = cs.search_id
            WHERE pc.package_id = ?
            ORDER BY s.saved_at, s.query_text
            """,
            (package_id,),
        )
        result: list[dict] = []
        for row in rows:
            try:
                snapshot = json.loads(row["expansion_snapshot_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            result.append(
                {
                    "search_id": row["search_id"],
                    "query_text": row["query_text"],
                    "normalized_query": row["normalized_query"],
                    "search_mode": row["search_mode"],
                    "year_filter": row["year_filter"],
                    "volume_filter": row["volume_filter"],
                    "expansion_level": row["expansion_level"],
                    "lexicon_revision": row["lexicon_revision"],
                    "expansion_snapshot": snapshot,
                    "saved_at": row["saved_at"],
                }
            )
        return result

    def rebuild_manifest(self, package_id: str) -> str:
        package = self.get_package(package_id)
        self._ensure_package_layout(package)
        entries = self._entry_map()
        ranges = self.list_ranges(package_id)
        documents: dict[str, dict] = {}
        for range_row in ranges:
            native_id = str(range_row["native_id"])
            entry = entries.get(native_id)
            if entry is None:
                raise ValueError(f"工作包来源史料 {native_id} 不在 MOFA 史料库中")
            selected_pages = tuple(json.loads(range_row["selected_pages_json"] or "[]"))
            page_rows = self.db.fetchall(
                """
                SELECT page_index, display_page, source_pdf_page, source_region,
                       printed_page_label
                FROM mofa_search_pages
                WHERE document_id = ? AND generation_id = ?
                  AND page_index BETWEEN ? AND ?
                ORDER BY page_index
                """,
                (
                    range_row["document_id"],
                    range_row["generation_id"],
                    range_row["start_page_index"],
                    range_row["end_page_index"],
                ),
            )
            expected = int(range_row["included_page_count"])
            if len(page_rows) != expected:
                raise ValueError(
                    f"{range_row['title']} 页段 {int(range_row['start_page_index']) + 1}—"
                    f"{int(range_row['end_page_index']) + 1} 与当前全文索引不一致"
                )
            candidate_rows = self.db.fetchall(
                """
                SELECT c.candidate_id, c.page_index, c.research_status, c.notes,
                       c.raw_text
                FROM mofa_research_package_candidates pc
                JOIN mofa_research_candidates c ON c.candidate_id = pc.candidate_id
                WHERE pc.range_id = ? ORDER BY c.page_index
                """,
                (range_row["range_id"],),
            )
            candidate_ids_by_page = {
                int(row["page_index"]): str(row["candidate_id"])
                for row in candidate_rows
            }
            range_payload = {
                "range_id": range_row["range_id"],
                "start_page_index": range_row["start_page_index"],
                "end_page_index": range_row["end_page_index"],
                "display_start_page": int(range_row["start_page_index"]) + 1,
                "display_end_page": int(range_row["end_page_index"]) + 1,
                "selected_page_indexes": list(selected_pages),
                "included_page_count": expected,
                "pages": [
                    {
                        "page_index": int(row["page_index"]),
                        "display_page": int(row["display_page"]),
                        "role": (
                            "selected"
                            if int(row["page_index"]) in selected_pages
                            else (
                                "document_scope"
                                if package.selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
                                else "context"
                            )
                        ),
                        "candidate_id": candidate_ids_by_page.get(int(row["page_index"]), ""),
                        "source_pdf_page": row["source_pdf_page"],
                        "source_region": row["source_region"],
                        "printed_page_label": row["printed_page_label"],
                    }
                    for row in page_rows
                ],
                "selected_candidates": [
                    {
                        "candidate_id": row["candidate_id"],
                        "page_index": row["page_index"],
                        "research_status": row["research_status"],
                        "notes": row["notes"],
                    }
                    for row in candidate_rows
                ],
            }
            document = documents.setdefault(
                str(range_row["document_id"]),
                {
                    "source": MOFA_PACKAGE_SOURCE,
                    "document_id": range_row["document_id"],
                    "native_id": native_id,
                    "generation_id": range_row["generation_id"],
                    "gregorian_year": range_row["gregorian_year"],
                    "volume_code": range_row["volume_code"],
                    "title": range_row["title"],
                    "source_bundle": self._path_reference(entry.bundle_dir),
                    "source_pdf": self._path_reference(entry.pdf_path),
                    "single_pages_pdf": self._path_reference(entry.split_pdf_path),
                    "ranges": [],
                },
            )
            document["ranges"].append(range_payload)

        payload = {
            "schema_version": 2,
            "package_id": package.package_id,
            "source": MOFA_PACKAGE_SOURCE,
            "package_type": MOFA_PACKAGE_TYPE,
            "display_name": package.display_name,
            "status": package.status,
            "selection_scope": package.selection_scope,
            "created_at": package.created_at,
            "updated_at": package.updated_at,
            "storage": {
                "relative_dir": package.relative_dir,
                "manifest_filename": MOFA_PACKAGE_MANIFEST,
                "source_pdfs_copied": False,
            },
            "context": {
                "before_pages": package.context_before,
                "after_pages": package.context_after,
            },
            "counts": {
                "documents": package.document_count,
                "ranges": package.range_count,
                "selected_pages": package.selected_page_count,
                "included_pages": package.included_page_count,
            },
            "notes": package.notes,
            "documents": list(documents.values()),
            "search_provenance": self._saved_searches_for_package(package_id),
        }
        self._atomic_json(package.manifest_path, payload)
        return package.manifest_path

    def summary(self) -> dict[str, int]:
        values = {status: 0 for status in MOFA_PACKAGE_STATUSES}
        for row in self.db.fetchall(
            """
            SELECT package_status, COUNT(*) AS value
            FROM mofa_research_packages GROUP BY package_status
            """
        ):
            values[str(row["package_status"])] = int(row["value"])
        values["total"] = sum(values.values())
        return values
