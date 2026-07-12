"""Cached MOFA catalog and local corpus readiness index."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaCatalogScraper
from services.db_service import DbService
from services.document_source_service import build_mofa_native_id
from services.document_storage_service import DocumentStorageService


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
    mineru_raw_exists: bool
    mineru_imported_exists: bool
    search_text_exists: bool

    @property
    def readiness(self) -> str:
        if self.search_text_exists:
            return "可检索"
        if self.mineru_imported_exists:
            return "待生成检索文本"
        if self.mineru_raw_exists:
            return "待导入"
        if self.pdf_exists:
            return "待OCR"
        return "未下载"


@dataclass(frozen=True)
class MofaLibrarySummary:
    total: int
    pdf_ready: int
    mineru_raw_ready: int
    imported_ready: int
    searchable: int


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

    def _bundle_status(self, row) -> tuple[str, str, bool, bool, bool, bool]:
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
            pdf_path = os.path.join(bundle_dir, "document.pdf")
        pdf_exists = os.path.isfile(pdf_path)
        raw_exists = self._dir_has_files(os.path.join(bundle_dir, "mineru", "raw"))
        imported_exists = self._dir_has_files(os.path.join(bundle_dir, "mineru", "imported"))
        search_exists = os.path.isfile(os.path.join(bundle_dir, "search", "search_text.paged.json"))
        return bundle_dir, pdf_path, pdf_exists, raw_exists, imported_exists, search_exists

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
            bundle_dir, pdf_path, pdf_ok, raw_ok, imported_ok, search_ok = self._bundle_status(row)
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
            mineru_raw_ready=sum(entry.mineru_raw_exists for entry in values),
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
