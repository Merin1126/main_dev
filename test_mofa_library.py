from __future__ import annotations

import json
import os
import tempfile

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_library_service import MofaLibraryService


class _FakeCatalog:
    def __init__(self, volume, items) -> None:
        self.volume = volume
        self.items = items

    def discover_volumes(self):
        return [self.volume]

    def fetch_volume_items(self, volume):
        assert volume == self.volume
        return list(self.items)


def main() -> int:
    volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/v1921")
    content = MofaCatalogItem(volume, "支那政局ニ関スル件", "https://mofa/content.pdf")
    index = MofaCatalogItem(volume, "日付索引", "https://mofa/index.pdf", "index")
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "library.sqlite3"))
        service = MofaLibraryService(
            project_root=root,
            db_service=db,
            catalog_scraper=_FakeCatalog(volume, [content, index]),
        )
        assert service.sync_official_catalog() == 2
        entries = service.list_entries()
        assert len(entries) == 1 and entries[0].title == content.title
        entry = entries[0]
        assert entry.bundle_dir == os.path.join(
            root,
            "Historical_Documents",
            "mofa",
            "1921",
            "2",
            entry.native_id,
        )
        assert entry.readiness == "未下载"

        os.makedirs(entry.bundle_dir)
        with open(entry.pdf_path, "wb") as stream:
            stream.write(b"%PDF-1.4\n")
        os.makedirs(os.path.join(entry.bundle_dir, "mineru", "raw", "run-1"))
        with open(os.path.join(entry.bundle_dir, "mineru", "raw", "run-1", "content_list.json"), "w") as stream:
            json.dump([], stream)
        refreshed = service.list_entries()[0]
        assert refreshed.pdf_exists and refreshed.mineru_raw_exists
        assert refreshed.readiness == "待导入"

        os.makedirs(os.path.join(entry.bundle_dir, "search"))
        with open(
            os.path.join(entry.bundle_dir, "search", "search_text.paged.json"),
            "w",
            encoding="utf-8",
        ) as stream:
            json.dump({"format": "paged_v1", "pages": []}, stream)
        searchable = service.list_entries(readiness="可检索")
        assert len(searchable) == 1 and searchable[0].search_text_exists
        assert service.summarize(searchable).searchable == 1
        assert len(service.list_entries(item_kind="")) == 2
        db.close()
    print("Phase 5B-1 checks passed: catalog cache and local corpus readiness index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
