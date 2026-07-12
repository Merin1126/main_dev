from __future__ import annotations

import os
import tempfile
import threading
import time

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_batch_download_service import MofaBatchDownloadService
from services.mofa_download_service import MofaDownloadResult


class _FakeDownloader:
    def __init__(self) -> None:
        self.registered = []

    def register_item(self, item, *, search_keyword, run_id=None):
        self.registered.append(item.title)
        return f"mofa:{item.title}"

    def download_item(
        self,
        item,
        *,
        search_keyword,
        run_id=None,
        on_progress=None,
        should_stop=None,
    ):
        assert should_stop is not None and not should_stop()
        if item.title == "失敗":
            raise RuntimeError("fixture failure")
        if on_progress is not None:
            on_progress(5, 10)
            on_progress(10, 10)
        status = "already_downloaded" if item.title == "既存" else "downloaded"
        return MofaDownloadResult(
            status=status,
            native_id=f"MOFA_{item.title}",
            document_id=f"mofa:MOFA_{item.title}",
            pdf_path="/tmp/document.pdf",
            sidecar_path="/tmp/sidecar.json",
            bytes_downloaded=10 if status == "downloaded" else 0,
        )


class _PauseDownloader(_FakeDownloader):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.second_started = threading.Event()

    def download_item(self, item, **kwargs):
        if item.title == "新規":
            self.first_started.set()
            assert self.release_first.wait(timeout=2)
        else:
            self.second_started.set()
        return super().download_item(item, **kwargs)


def main() -> int:
    volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/v1921")
    items = [
        MofaCatalogItem(volume, "新規", "https://mofa/new.pdf"),
        MofaCatalogItem(volume, "既存", "https://mofa/existing.pdf"),
        MofaCatalogItem(volume, "失敗", "https://mofa/failure.pdf"),
    ]
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "batch.sqlite3"))
        downloader = _FakeDownloader()
        service = MofaBatchDownloadService(db_service=db, download_service=downloader)
        events = []
        result = service.run(items, on_progress=events.append)
        assert result.total == 3
        assert result.downloaded == 1
        assert result.skipped == 1
        assert result.failed == 1
        assert not result.cancelled
        assert downloader.registered == ["新規", "既存", "失敗"]
        assert events[-1].state == "completed"
        row = db.fetchone(
            "SELECT dispatched, completed, succeeded, failed, notes FROM download_runs ORDER BY id DESC"
        )
        assert tuple(row)[:4] == (3, 3, 2, 1)
        assert "mode=library_batch" in row["notes"]

        pause_downloader = _PauseDownloader()
        pause_service = MofaBatchDownloadService(db_service=db, download_service=pause_downloader)
        holder = []
        thread = threading.Thread(target=lambda: holder.append(pause_service.run(items[:2])))
        thread.start()
        assert pause_downloader.first_started.wait(timeout=2)
        pause_service.pause()
        pause_downloader.release_first.set()
        time.sleep(0.3)
        assert pause_service.paused
        assert not pause_downloader.second_started.is_set()
        pause_service.resume()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert pause_downloader.second_started.is_set()
        assert holder and holder[0].downloaded == 1 and holder[0].skipped == 1
        db.close()
    print("Phase 5B-2 checks passed: managed batch download accounting and recovery semantics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
