from __future__ import annotations

import os
import tempfile
import threading

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_download_service import MofaDownloadResult
from services.mofa_workflow_service import (
    MOFA_MODE_MATCHED,
    MOFA_MODE_SCAN,
    MofaWorkflowService,
    title_matches_keyword,
)


class _FakeCatalog:
    def __init__(self, volumes, items_by_url) -> None:
        self.volumes = volumes
        self.items_by_url = items_by_url

    def discover_volumes(self):
        return list(self.volumes)

    def fetch_volume_items(self, volume):
        return list(self.items_by_url[volume.catalog_url])


class _FakeDownloader:
    def __init__(self) -> None:
        self.registered = []
        self.downloaded = []

    def register_item(self, item, *, search_keyword, run_id=None):
        self.registered.append((item, search_keyword, run_id))
        return "mofa:fixture"

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
        if on_progress is not None:
            on_progress(10, 10)
        self.downloaded.append((item, search_keyword, run_id))
        return MofaDownloadResult(
            status="downloaded",
            native_id="MOFA_FIXTURE",
            document_id="mofa:MOFA_FIXTURE",
            pdf_path="/tmp/document.pdf",
            sidecar_path="/tmp/sidecar.json",
            bytes_downloaded=10,
        )


def main() -> int:
    v1921 = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/v1921")
    v1927 = MofaVolume("S", 2, 1927, "1-1", "昭和2年 第1巻", "https://mofa/v1927")
    relevant = MofaCatalogItem(v1921, "中国共産黨関係一件", "https://mofa/relevant.pdf")
    unrelated = MofaCatalogItem(v1921, "通商条約関係", "https://mofa/unrelated.pdf")
    index_item = MofaCatalogItem(v1921, "日付索引", "https://mofa/index.pdf", "index")
    later = MofaCatalogItem(v1927, "北伐と租界問題", "https://mofa/later.pdf")
    catalog = _FakeCatalog(
        [v1921, v1927],
        {
            v1921.catalog_url: [relevant, unrelated, index_item],
            v1927.catalog_url: [later],
        },
    )
    assert title_matches_keyword(relevant.title, "中国共産党")

    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "workflow.sqlite3"))
        downloader = _FakeDownloader()
        service = MofaWorkflowService(
            catalog_scraper=catalog,
            download_service=downloader,
            db_service=db,
        )
        previews = []
        progress = []
        scan = service.run(
            keyword="中国共産党",
            year_from=1921,
            year_to=1921,
            mode=MOFA_MODE_SCAN,
            stop_event=threading.Event(),
            on_progress=lambda current, total, message: progress.append((current, total, message)),
            on_catalog_ready=previews.append,
        )
        assert len(scan.volumes) == 1
        assert len(scan.all_items) == 3
        assert scan.matched_items == (relevant,)
        assert not scan.selected_items
        assert not downloader.downloaded
        assert previews and progress

        matched = service.run(
            keyword="中国共産党",
            year_from=1921,
            year_to=1927,
            mode=MOFA_MODE_MATCHED,
            stop_event=threading.Event(),
        )
        assert matched.selected_items == (relevant,)
        assert matched.downloaded == 1
        assert len(downloader.registered) == 1
        assert len(downloader.downloaded) == 1

        stopped = threading.Event()
        stopped.set()
        aborted = service.run(
            keyword="",
            year_from=1921,
            year_to=1927,
            mode=MOFA_MODE_SCAN,
            stop_event=stopped,
        )
        assert aborted.aborted is True
        assert not aborted.volumes
        assert not aborted.all_items

        summaries = db.fetchall("SELECT dispatched, completed, succeeded, failed FROM download_runs ORDER BY id")
        assert [tuple(row) for row in summaries] == [
            (0, 0, 0, 0),
            (1, 1, 1, 0),
            (0, 0, 0, 0),
        ]
        db.close()

    print("Phase 4 workflow checks passed: scan default, year filter, title match, selection, run summary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
