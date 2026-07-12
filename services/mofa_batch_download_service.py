"""Sequential, pausable MOFA corpus download queue."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from scrapers.mofa_catalog_scraper import MofaCatalogItem
from services.db_service import DbService
from services.mofa_download_service import MofaDownloadCancelled, MofaDownloadService


@dataclass(frozen=True)
class MofaBatchProgress:
    state: str
    current_index: int
    total: int
    downloaded: int
    skipped: int
    failed: int
    current_title: str = ""
    current_bytes: int = 0
    current_total_bytes: int | None = None
    message: str = ""


@dataclass(frozen=True)
class MofaBatchResult:
    total: int
    downloaded: int
    skipped: int
    failed: int
    cancelled: bool


ProgressCallback = Callable[[MofaBatchProgress], None]


class MofaBatchDownloadService:
    """Download one PDF at a time; pause safely between files."""

    def __init__(
        self,
        *,
        db_service: DbService | None = None,
        download_service: MofaDownloadService | None = None,
    ) -> None:
        self.db = db_service or DbService()
        self.downloader = download_service or MofaDownloadService(db_service=self.db)
        self._run_gate = threading.Event()
        self._run_gate.set()
        self._cancel = threading.Event()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def paused(self) -> bool:
        return self._active and not self._run_gate.is_set()

    def pause(self) -> None:
        if self._active:
            self._run_gate.clear()

    def resume(self) -> None:
        self._run_gate.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._run_gate.set()

    @staticmethod
    def _emit(callback: ProgressCallback | None, progress: MofaBatchProgress) -> None:
        if callback is not None:
            callback(progress)

    def run(
        self,
        items: Iterable[MofaCatalogItem],
        *,
        on_progress: ProgressCallback | None = None,
        search_keyword: str = "MOFA史料库",
    ) -> MofaBatchResult:
        values = list(items)
        if self._active:
            raise RuntimeError("MOFA batch download is already running")
        self._active = True
        self._cancel.clear()
        self._run_gate.set()
        downloaded = skipped = failed = 0
        run_id: int | None = None
        try:
            run_id = self.db.begin_download_run(
                keyword=search_keyword,
                year_from=str(min((item.volume.gregorian_year for item in values), default="")),
                year_to=str(max((item.volume.gregorian_year for item in values), default="")),
                notes="source=mofa; mode=library_batch",
            )
            for index, item in enumerate(values, start=1):
                if not self._run_gate.is_set():
                    self._emit(
                        on_progress,
                        MofaBatchProgress(
                            state="paused",
                            current_index=index,
                            total=len(values),
                            downloaded=downloaded,
                            skipped=skipped,
                            failed=failed,
                            current_title=item.title,
                            message="队列已暂停",
                        ),
                    )
                while not self._run_gate.wait(timeout=0.2):
                    if self._cancel.is_set():
                        break
                if self._cancel.is_set():
                    break

                def stream_progress(current: int, total: int | None) -> None:
                    self._emit(
                        on_progress,
                        MofaBatchProgress(
                            state="downloading",
                            current_index=index,
                            total=len(values),
                            downloaded=downloaded,
                            skipped=skipped,
                            failed=failed,
                            current_title=item.title,
                            current_bytes=current,
                            current_total_bytes=total,
                        ),
                    )

                try:
                    self.downloader.register_item(
                        item,
                        search_keyword=search_keyword,
                        run_id=run_id,
                    )
                    result = self.downloader.download_item(
                        item,
                        search_keyword=search_keyword,
                        run_id=run_id,
                        on_progress=stream_progress,
                        should_stop=self._cancel.is_set,
                    )
                    if result.status == "downloaded":
                        downloaded += 1
                    else:
                        skipped += 1
                except MofaDownloadCancelled:
                    self._cancel.set()
                    break
                except Exception:
                    failed += 1
                self._emit(
                    on_progress,
                    MofaBatchProgress(
                        state="running",
                        current_index=index,
                        total=len(values),
                        downloaded=downloaded,
                        skipped=skipped,
                        failed=failed,
                        current_title=item.title,
                    ),
                )
            cancelled = self._cancel.is_set()
            result = MofaBatchResult(
                total=len(values),
                downloaded=downloaded,
                skipped=skipped,
                failed=failed,
                cancelled=cancelled,
            )
            self.db.finish_download_run(
                run_id,
                dispatched=len(values),
                completed=downloaded + skipped + failed,
                succeeded=downloaded + skipped,
                failed=failed,
                sidecar_only=0,
                notes=f"source=mofa; mode=library_batch; cancelled={cancelled}",
            )
            self._emit(
                on_progress,
                MofaBatchProgress(
                    state="cancelled" if cancelled else "completed",
                    current_index=downloaded + skipped + failed,
                    total=len(values),
                    downloaded=downloaded,
                    skipped=skipped,
                    failed=failed,
                ),
            )
            return result
        except Exception:
            if run_id is not None:
                self.db.finish_download_run(
                    run_id,
                    dispatched=len(values),
                    completed=downloaded + skipped + failed,
                    succeeded=downloaded + skipped,
                    failed=failed + 1,
                    sidecar_only=0,
                    notes="source=mofa; mode=library_batch; workflow_error=true",
                )
            raise
        finally:
            self._active = False
            self._run_gate.set()
