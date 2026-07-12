"""MOFA PDF download, bundle persistence, and SQLite status closure."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable

import requests

from scrapers.mofa_catalog_scraper import MofaCatalogItem
from services.db_service import DbService
from services.document_source_service import build_mofa_native_id, build_sidecar_v2
from services.document_storage_service import DocumentBundle, DocumentStorageService


ProgressCallback = Callable[[int, int | None], None]
StopCallback = Callable[[], bool]


class MofaDownloadCancelled(RuntimeError):
    """Raised when the user stops an active MOFA stream download."""


@dataclass(frozen=True)
class MofaDownloadResult:
    status: str
    native_id: str
    document_id: str
    pdf_path: str
    sidecar_path: str
    bytes_downloaded: int = 0


class MofaDownloadService:
    """Persist one catalog item without coupling it to the GUI."""

    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
        storage_service: DocumentStorageService | None = None,
        session: requests.Session | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.db = db_service or DbService()
        self.storage = storage_service or DocumentStorageService(project_root=project_root)
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept": "application/pdf,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        }
        self._primed_catalog_urls: set[str] = set()

    def _prime_catalog_session(self, catalog_url: str, *, force: bool = False) -> None:
        """Warm the same HTTP connection on the volume page before PDF access.

        MOFA's CDN may deny a cold direct PDF request even with a browser user
        agent.  Visiting the referring catalog page in the same Session and
        then sending Referer has been verified against the live 1927 corpus.
        """
        url = (catalog_url or "").strip()
        if not url or (not force and url in self._primed_catalog_urls):
            return
        headers = dict(self.headers)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        try:
            response.raise_for_status()
        finally:
            response.close()
        self._primed_catalog_urls.add(url)

    def _open_pdf_response(self, item: MofaCatalogItem):
        self._prime_catalog_session(item.volume.catalog_url)
        headers = dict(self.headers)
        headers["Referer"] = item.volume.catalog_url
        response = self.session.get(
            item.pdf_url,
            headers=headers,
            timeout=self.timeout,
            stream=True,
        )
        if response.status_code == 403:
            response.close()
            self._prime_catalog_session(item.volume.catalog_url, force=True)
            response = self.session.get(
                item.pdf_url,
                headers=headers,
                timeout=self.timeout,
                stream=True,
            )
        return response

    @staticmethod
    def native_id_for_item(item: MofaCatalogItem) -> str:
        return build_mofa_native_id(
            era_code=item.volume.era_code,
            era_year=item.volume.era_year,
            volume_code=item.volume.volume_code,
            fallback_key=item.pdf_url,
        )

    def _identity_and_bundle(
        self,
        item: MofaCatalogItem,
        *,
        search_keyword: str,
    ) -> tuple[str, DocumentBundle]:
        native_id = self.native_id_for_item(item)
        identity = self.storage.build_identity(
            source="mofa",
            native_id=native_id,
            search_keyword=search_keyword,
            collection="日本外交文書",
        )
        existing = self.db.fetchone(
            """
            SELECT f.path
            FROM files f
            JOIN documents d ON d.document_id = f.document_id
            WHERE d.source = 'mofa' AND d.native_id = ? AND f.kind = 'pdf'
            LIMIT 1
            """,
            (native_id,),
        )
        if existing:
            existing_path = os.path.abspath(str(existing["path"] or ""))
            if os.path.isfile(existing_path):
                resolved = self.storage.resolve_bundle_from_pdf(existing_path)
                if resolved.identity.document_id == identity.document_id:
                    return native_id, resolved
                return native_id, DocumentBundle(
                    root_dir=os.path.dirname(existing_path),
                    identity=identity,
                    layout=self.storage.layout,
                    pdf_path=existing_path,
                )
        hierarchy = (
            str(item.volume.gregorian_year),
            str(item.volume.volume_code),
        )
        return native_id, self.storage.ensure_bundle_dir(identity, hierarchy=hierarchy)

    @staticmethod
    def _source_metadata(item: MofaCatalogItem) -> dict:
        return {
            "catalog_url": item.volume.catalog_url,
            "pdf_url": item.pdf_url,
            "era_code": item.volume.era_code,
            "era_year": item.volume.era_year,
            "gregorian_year": item.volume.gregorian_year,
            "volume_code": item.volume.volume_code,
            "volume_label": item.volume.volume_label,
            "item_kind": item.item_kind,
            "editor": "日本外務省",
            "publisher": "日本外務省",
            "publication_year": None,
            "printed_page_from": None,
            "printed_page_to": None,
            "pdf_page_from": None,
            "pdf_page_to": None,
            "citation_status": "pending_bibliography",
        }

    def _sidecar_payload(self, item: MofaCatalogItem, bundle: DocumentBundle) -> dict:
        payload = build_sidecar_v2(
            identity=bundle.identity,
            title=item.title,
            source_metadata=self._source_metadata(item),
        )
        # Flat compatibility fields keep existing catalog/report code readable
        # until Phase 6 finishes the archive-neutral migration.
        payload.update(
            {
                "Source": "mofa",
                "Native_ID": bundle.identity.native_id,
                "Title": item.title,
                "Collection": "日本外交文書",
                "Citation_Text": "",
            }
        )
        return payload

    @staticmethod
    def _write_json(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        temp_path = path + ".part"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)

    def _upsert_discovered(
        self,
        item: MofaCatalogItem,
        *,
        native_id: str,
        search_keyword: str,
        metadata: dict,
        status: str,
    ) -> str:
        return self.db.upsert_document(
            source="mofa",
            native_id=native_id,
            title=item.title,
            repo_name="日本外務省",
            level2_name="日本外交文書",
            parent_name=item.volume.volume_label,
            viewer_url=item.volume.catalog_url,
            search_keyword=search_keyword,
            metadata=metadata,
            status=status,
        )

    def register_item(
        self,
        item: MofaCatalogItem,
        *,
        search_keyword: str,
        run_id: int | None = None,
    ) -> str:
        """Register one selected item so the DB-driven monitor can show it."""
        native_id, bundle = self._identity_and_bundle(item, search_keyword=search_keyword)
        pdf_path = self.storage.resolve_write_path(bundle, "pdf", create_parent_dirs=True)
        metadata = self._sidecar_payload(item, bundle)
        already_downloaded = (
            self.db.get_document_status("mofa", native_id) == "downloaded"
            and os.path.isfile(pdf_path)
        )
        self._upsert_discovered(
            item,
            native_id=native_id,
            search_keyword=search_keyword,
            metadata=metadata,
            status="downloaded" if already_downloaded else "discovered",
        )
        self.db.add_download_event(
            native_id,
            "succeeded" if already_downloaded else "queued",
            message="already downloaded" if already_downloaded else "MOFA catalog item queued",
            run_id=run_id,
            source="mofa",
        )
        return bundle.identity.document_id

    def download_item(
        self,
        item: MofaCatalogItem,
        *,
        search_keyword: str,
        run_id: int | None = None,
        on_progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> MofaDownloadResult:
        native_id, bundle = self._identity_and_bundle(item, search_keyword=search_keyword)
        pdf_path = self.storage.resolve_write_path(bundle, "pdf", create_parent_dirs=True)
        sidecar_path = self.storage.resolve_write_path(bundle, "sidecar", create_parent_dirs=True)
        metadata = self._sidecar_payload(item, bundle)
        document_id = bundle.identity.document_id

        existing_status = self.db.get_document_status("mofa", native_id)
        if os.path.isfile(pdf_path):
            self._write_json(sidecar_path, metadata)
            self._upsert_discovered(
                item,
                native_id=native_id,
                search_keyword=search_keyword,
                metadata=metadata,
                status="downloaded",
            )
            self.db.mark_downloaded_with_files(
                source="mofa",
                native_id=native_id,
                pdf_path=pdf_path,
                sidecar_path=sidecar_path,
            )
            self.storage.write_manifest(bundle)
            self.db.add_download_event(
                native_id,
                "succeeded",
                message="physical PDF already exists; metadata and database repaired",
                run_id=run_id,
                source="mofa",
            )
            return MofaDownloadResult(
                status="already_downloaded" if existing_status == "downloaded" else "repaired",
                native_id=native_id,
                document_id=document_id,
                pdf_path=pdf_path,
                sidecar_path=sidecar_path,
                bytes_downloaded=0,
            )

        self._upsert_discovered(
            item,
            native_id=native_id,
            search_keyword=search_keyword,
            metadata=metadata,
            status="discovered",
        )
        self.db.mark_document_status("mofa", native_id, "downloading")
        self.db.add_download_event(native_id, "downloading", run_id=run_id, source="mofa")

        part_path = pdf_path + ".part"
        downloaded = 0
        try:
            with self._open_pdf_response(item) as response:
                response.raise_for_status()
                raw_total = response.headers.get("Content-Length")
                total = int(raw_total) if raw_total and raw_total.isdigit() else None
                with open(part_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if should_stop is not None and should_stop():
                            raise MofaDownloadCancelled("MOFA download stopped by user")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None:
                            on_progress(downloaded, total)

            with open(part_path, "rb") as f:
                if f.read(5) != b"%PDF-":
                    raise ValueError("MOFA response is not a PDF file")
            os.replace(part_path, pdf_path)
            self._write_json(sidecar_path, metadata)
            self.storage.write_manifest(bundle)
            self.db.mark_downloaded_with_files(
                source="mofa",
                native_id=native_id,
                pdf_path=pdf_path,
                sidecar_path=sidecar_path,
            )
            self.db.add_download_event(
                native_id,
                "succeeded",
                message=f"bytes={downloaded}",
                run_id=run_id,
                source="mofa",
            )
            return MofaDownloadResult(
                status="downloaded",
                native_id=native_id,
                document_id=document_id,
                pdf_path=pdf_path,
                sidecar_path=sidecar_path,
                bytes_downloaded=downloaded,
            )
        except MofaDownloadCancelled as exc:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            finally:
                self.db.mark_document_status("mofa", native_id, "discovered")
                self.db.add_download_event(
                    native_id,
                    "aborted",
                    message=str(exc),
                    run_id=run_id,
                    source="mofa",
                )
            raise
        except Exception as exc:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            finally:
                self.db.mark_document_status("mofa", native_id, "failed")
                self.db.add_download_event(
                    native_id,
                    "failed",
                    message=str(exc),
                    run_id=run_id,
                    source="mofa",
                )
            raise
