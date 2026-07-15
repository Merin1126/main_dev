"""Cached MOFA catalog and local corpus readiness index."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaCatalogScraper, MofaVolume
from services.db_service import DbService
from services.document_source_service import build_mofa_native_id
from services.document_storage_service import DocumentBundle, DocumentStorageService
from services.mofa_filename_service import build_mofa_pdf_filename
from services.mofa_pdf_split_service import MofaPdfSplitService


@dataclass(frozen=True)
class MofaLibraryEntry:
    native_id: str
    year: int
    volume_code: str
    volume_label: str
    title: str
    item_kind: str
    catalog_url: str
    pdf_url: str
    bundle_dir: str
    pdf_path: str
    pdf_exists: bool
    split_pdf_path: str
    split_pdf_exists: bool
    mineru_expected_parts: int
    mineru_archived_parts: int
    mineru_raw_exists: bool
    mineru_imported_exists: bool
    search_text_exists: bool

    @property
    def readiness(self) -> str:
        if self.search_text_exists:
            return "可检索"
        if self.mineru_imported_exists:
            return "待生成检索文本"
        if (
            self.mineru_expected_parts > 1
            and self.mineru_archived_parts < self.mineru_expected_parts
            and self.mineru_raw_exists
        ):
            return "OCR分段未完成"
        if self.mineru_raw_exists:
            return "待整理"
        if self.pdf_exists:
            return "待OCR"
        return "未下载"


@dataclass(frozen=True)
class MofaLibrarySummary:
    total: int
    pdf_ready: int
    split_pdf_ready: int
    mineru_raw_ready: int
    imported_ready: int
    searchable: int


@dataclass(frozen=True)
class MofaFilenameNormalizationResult:
    total: int
    renamed_pdfs: int
    renamed_split_pdfs: int
    unchanged: int
    failed: int
    errors: tuple[str, ...]


class MofaLibraryService:
    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
        catalog_scraper: MofaCatalogScraper | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.db = db_service or DbService()
        self.catalog = catalog_scraper or MofaCatalogScraper()
        self.storage = DocumentStorageService(project_root=self.project_root)
        self.splitter = MofaPdfSplitService()

    @staticmethod
    def native_id_for_item(item: MofaCatalogItem) -> str:
        return build_mofa_native_id(
            era_code=item.volume.era_code,
            era_year=item.volume.era_year,
            volume_code=item.volume.volume_code,
            fallback_key=item.pdf_url,
        )

    def cache_items(self, items: Iterable[MofaCatalogItem]) -> int:
        now = self.db.utc_now_iso()
        rows = []
        for order, item in enumerate(items, start=1):
            rows.append(
                (
                    self.native_id_for_item(item),
                    item.volume.gregorian_year,
                    item.volume.era_code,
                    item.volume.era_year,
                    item.volume.volume_code,
                    item.volume.volume_label,
                    item.title,
                    item.item_kind,
                    item.volume.catalog_url,
                    item.pdf_url,
                    order,
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        with self.db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO mofa_catalog_items(
                    native_id, gregorian_year, era_code, era_year, volume_code,
                    volume_label, title, item_kind, catalog_url, pdf_url,
                    item_order, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(native_id) DO UPDATE SET
                    gregorian_year=excluded.gregorian_year,
                    era_code=excluded.era_code,
                    era_year=excluded.era_year,
                    volume_code=excluded.volume_code,
                    volume_label=excluded.volume_label,
                    title=excluded.title,
                    item_kind=excluded.item_kind,
                    catalog_url=excluded.catalog_url,
                    pdf_url=excluded.pdf_url,
                    item_order=excluded.item_order,
                    last_seen_at=excluded.last_seen_at
                """,
                rows,
            )
        return len(rows)

    def sync_official_catalog(self, *, year_from: int = 1921, year_to: int = 1927) -> int:
        volumes = [
            volume
            for volume in self.catalog.discover_volumes()
            if int(year_from) <= volume.gregorian_year <= int(year_to)
        ]
        items: list[MofaCatalogItem] = []
        for volume in volumes:
            items.extend(self.catalog.fetch_volume_items(volume))
        return self.cache_items(items)

    @staticmethod
    def _dir_has_files(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        for _, _, files in os.walk(path):
            if any(not name.startswith(".") for name in files):
                return True
        return False

    def _bundle_status(
        self,
        row,
        mineru_runs: list,
    ) -> tuple[str, str, bool, str, bool, int, int, bool, bool, bool]:
        native_id = str(row["native_id"])
        year = str(row["gregorian_year"])
        volume_code = str(row["volume_code"])
        registered_path = str(row["registered_pdf_path"] or "")
        if registered_path and not os.path.isabs(registered_path):
            registered_path = os.path.join(self.project_root, registered_path)
        if registered_path and os.path.isfile(registered_path):
            pdf_path = os.path.abspath(registered_path)
            bundle_dir = os.path.dirname(pdf_path)
        else:
            identity = self.storage.build_identity(
                source="mofa",
                native_id=native_id,
                search_keyword="MOFA全量",
                collection="日本外交文書",
            )
            bundle_dir = self.storage.planned_bundle_dir(
                identity,
                hierarchy=(year, volume_code),
            )
            pdf_path = self.storage.find_bundle_pdf_path(bundle_dir) or os.path.join(
                bundle_dir,
                build_mofa_pdf_filename(str(row["title"]), native_id),
            )
        pdf_exists = os.path.isfile(pdf_path)
        split_pdf_path = MofaPdfSplitService.output_path_for_bundle(
            bundle_dir,
            pdf_path,
        )
        split_pdf_exists = os.path.isfile(split_pdf_path)
        raw_exists = self._dir_has_files(os.path.join(bundle_dir, "mineru", "raw"))
        input_parts = (
            self.splitter.load_input_parts(bundle_dir, split_pdf_path)
            if split_pdf_exists
            else ()
        )
        expected_parts = len(input_parts)
        archived_parts = 0
        for part in input_parts:
            matched = False
            for run in mineru_runs:
                raw_dir = str(run["raw_dir"] or "")
                if not raw_dir or not os.path.isdir(raw_dir):
                    continue
                run_sha = str(run["input_sha256"] or "")
                run_start = int(run["chunk_start"] or 0)
                run_end = int(run["chunk_end"] or 0)
                if part.sha256:
                    matched = bool(
                        run_sha == part.sha256
                        and run_start == part.start_page
                        and run_end == part.end_page
                    )
                    if expected_parts == 1 and not run_sha:
                        matched = True
                elif expected_parts == 1:
                    matched = True
                if matched:
                    break
            archived_parts += int(matched)
        if expected_parts == 1 and archived_parts == 0 and raw_exists and not mineru_runs:
            # Compatibility for pre-database raw folders.
            archived_parts = 1
        imported_exists = self._dir_has_files(os.path.join(bundle_dir, "mineru", "imported"))
        search_exists = os.path.isfile(os.path.join(bundle_dir, "search", "search_text.paged.json"))
        return (
            bundle_dir,
            pdf_path,
            pdf_exists,
            split_pdf_path,
            split_pdf_exists,
            expected_parts,
            archived_parts,
            raw_exists,
            imported_exists,
            search_exists,
        )

    def list_entries(
        self,
        *,
        year: int | None = None,
        volume_code: str = "",
        item_kind: str = "content",
        search_text: str = "",
        readiness: str = "",
    ) -> list[MofaLibraryEntry]:
        rows = self.db.fetchall(
            """
            SELECT c.*, fp.path AS registered_pdf_path
            FROM mofa_catalog_items c
            LEFT JOIN documents d
              ON d.source = 'mofa' AND d.native_id = c.native_id
            LEFT JOIN files fp
              ON fp.document_id = d.document_id AND fp.kind = 'pdf'
            ORDER BY c.gregorian_year, c.volume_code, c.item_order, c.title
            """
        )
        run_rows = self.db.fetchall(
            """
            SELECT native_id, raw_dir, input_sha256, chunk_start, chunk_end
            FROM mofa_mineru_runs
            """
        )
        runs_by_native_id: dict[str, list] = {}
        for run in run_rows:
            runs_by_native_id.setdefault(str(run["native_id"]), []).append(run)
        needle = (search_text or "").strip().lower()
        entries: list[MofaLibraryEntry] = []
        for row in rows:
            if year is not None and int(row["gregorian_year"]) != int(year):
                continue
            if volume_code and str(row["volume_code"]) != volume_code:
                continue
            if item_kind and str(row["item_kind"]) != item_kind:
                continue
            haystack = f'{row["title"]} {row["volume_label"]} {row["native_id"]}'.lower()
            if needle and needle not in haystack:
                continue
            (
                bundle_dir,
                pdf_path,
                pdf_ok,
                split_pdf_path,
                split_pdf_ok,
                expected_parts,
                archived_parts,
                raw_ok,
                imported_ok,
                search_ok,
            ) = self._bundle_status(
                row,
                runs_by_native_id.get(str(row["native_id"]), []),
            )
            entry = MofaLibraryEntry(
                native_id=str(row["native_id"]),
                year=int(row["gregorian_year"]),
                volume_code=str(row["volume_code"]),
                volume_label=str(row["volume_label"]),
                title=str(row["title"]),
                item_kind=str(row["item_kind"]),
                catalog_url=str(row["catalog_url"]),
                pdf_url=str(row["pdf_url"]),
                bundle_dir=bundle_dir,
                pdf_path=pdf_path,
                pdf_exists=pdf_ok,
                split_pdf_path=split_pdf_path,
                split_pdf_exists=split_pdf_ok,
                mineru_expected_parts=expected_parts,
                mineru_archived_parts=archived_parts,
                mineru_raw_exists=raw_ok,
                mineru_imported_exists=imported_ok,
                search_text_exists=search_ok,
            )
            if readiness and entry.readiness != readiness:
                continue
            entries.append(entry)
        return entries

    @staticmethod
    def summarize(entries: Iterable[MofaLibraryEntry]) -> MofaLibrarySummary:
        values = list(entries)
        return MofaLibrarySummary(
            total=len(values),
            pdf_ready=sum(entry.pdf_exists for entry in values),
            split_pdf_ready=sum(entry.split_pdf_exists for entry in values),
            mineru_raw_ready=sum(
                bool(entry.mineru_expected_parts)
                and entry.mineru_archived_parts >= entry.mineru_expected_parts
                for entry in values
            ),
            imported_ready=sum(entry.mineru_imported_exists for entry in values),
            searchable=sum(entry.search_text_exists for entry in values),
        )

    def available_years(self) -> list[int]:
        rows = self.db.fetchall(
            "SELECT DISTINCT gregorian_year FROM mofa_catalog_items ORDER BY gregorian_year"
        )
        return [int(row["gregorian_year"]) for row in rows]

    def available_volumes(self, year: int | None = None) -> list[str]:
        if year is None:
            rows = self.db.fetchall(
                "SELECT DISTINCT volume_code FROM mofa_catalog_items ORDER BY volume_code"
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT DISTINCT volume_code FROM mofa_catalog_items
                WHERE gregorian_year = ? ORDER BY volume_code
                """,
                (int(year),),
            )
        return [str(row["volume_code"]) for row in rows]

    @staticmethod
    def filename_is_normalized(entry: MofaLibraryEntry) -> bool:
        if not entry.pdf_exists:
            return True
        return os.path.basename(entry.pdf_path) == build_mofa_pdf_filename(
            entry.title,
            entry.native_id,
        )

    @classmethod
    def filename_normalization_needed(cls, entry: MofaLibraryEntry) -> bool:
        if not entry.pdf_exists:
            return False
        legacy_split = os.path.join(
            entry.bundle_dir,
            "mineru",
            "input",
            "document.single-pages.pdf",
        )
        return not cls.filename_is_normalized(entry) or os.path.isfile(legacy_split)

    def normalize_local_filenames(
        self,
        entries: Iterable[MofaLibraryEntry] | None = None,
        *,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> MofaFilenameNormalizationResult:
        """Rename existing MOFA PDFs in place; no network access is performed."""
        values = list(entries) if entries is not None else self.list_entries(item_kind="")
        targets = [entry for entry in values if entry.pdf_exists]
        splitter = MofaPdfSplitService()
        renamed_pdfs = renamed_splits = unchanged = failed = 0
        errors: list[str] = []
        for index, entry in enumerate(targets, start=1):
            original_pdf = os.path.abspath(entry.pdf_path)
            desired_pdf = os.path.join(
                entry.bundle_dir,
                build_mofa_pdf_filename(entry.title, entry.native_id),
            )
            pdf_renamed = False
            split_renamed = False
            try:
                if original_pdf != os.path.abspath(desired_pdf):
                    if os.path.exists(desired_pdf):
                        raise FileExistsError(f"目标 PDF 已存在：{desired_pdf}")
                    os.rename(original_pdf, desired_pdf)
                    pdf_renamed = True
                else:
                    desired_pdf = original_pdf

                _split_path, split_renamed = splitter.normalize_existing_output_name(
                    desired_pdf,
                    entry.bundle_dir,
                )
                identity = self.storage.build_identity(
                    source="mofa",
                    native_id=entry.native_id,
                    search_keyword="MOFA史料库",
                    collection="日本外交文書",
                )
                bundle = DocumentBundle(
                    root_dir=entry.bundle_dir,
                    identity=identity,
                    layout=self.storage.layout,
                    pdf_path=desired_pdf,
                )
                self.storage.write_manifest(bundle)
                sidecar_path = os.path.join(entry.bundle_dir, "sidecar.json")
                self.db.upsert_document(
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
                self.db.mark_downloaded_with_files(
                    source="mofa",
                    native_id=entry.native_id,
                    pdf_path=desired_pdf,
                    sidecar_path=sidecar_path if os.path.isfile(sidecar_path) else None,
                )
                renamed_pdfs += int(pdf_renamed)
                renamed_splits += int(split_renamed)
                unchanged += int(not pdf_renamed and not split_renamed)
            except Exception as exc:
                failed += 1
                errors.append(f"{entry.native_id}: {exc}")
                if pdf_renamed and not os.path.exists(original_pdf) and os.path.exists(desired_pdf):
                    try:
                        os.rename(desired_pdf, original_pdf)
                    except OSError:
                        pass
            if on_progress is not None:
                on_progress(index, len(targets), entry.title)
        return MofaFilenameNormalizationResult(
            total=len(targets),
            renamed_pdfs=renamed_pdfs,
            renamed_split_pdfs=renamed_splits,
            unchanged=unchanged,
            failed=failed,
            errors=tuple(errors),
        )

    def catalog_items(self, native_ids: Iterable[str]) -> list[MofaCatalogItem]:
        """Rebuild downloader input objects from the cached offline catalog."""
        ordered_ids = [str(value) for value in native_ids if str(value).strip()]
        if not ordered_ids:
            return []
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self.db.fetchall(
            f"""
            SELECT * FROM mofa_catalog_items
            WHERE native_id IN ({placeholders})
            """,
            ordered_ids,
        )
        by_id = {}
        for row in rows:
            volume = MofaVolume(
                era_code=str(row["era_code"]),
                era_year=int(row["era_year"]),
                gregorian_year=int(row["gregorian_year"]),
                volume_code=str(row["volume_code"]),
                volume_label=str(row["volume_label"]),
                catalog_url=str(row["catalog_url"]),
            )
            by_id[str(row["native_id"])] = MofaCatalogItem(
                volume=volume,
                title=str(row["title"]),
                pdf_url=str(row["pdf_url"]),
                item_kind=str(row["item_kind"]),
            )
        return [by_id[native_id] for native_id in ordered_ids if native_id in by_id]
