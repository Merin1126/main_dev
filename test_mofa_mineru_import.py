from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading

import fitz

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_filename_service import build_mineru_input_filename, build_mofa_pdf_filename
from services.mofa_library_service import MofaLibraryService
from services.mofa_mineru_import_service import (
    MofaMineruDirectoryWatcher,
    MofaMineruImportService,
)
from services.mofa_pdf_split_service import MofaPdfSplitService


def _make_pdf(path: str, label: str, pages: int = 3) -> None:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=420, height=594)
        page.insert_text((50, 80), f"{label} PAGE {index + 1}", fontsize=24)
        page.draw_rect(
            fitz.Rect(30 + index, 120, 390, 160 + index),
            color=(index / max(1, pages), 0.2, 0.5),
            fill=(index / max(1, pages), 0.2, 0.5),
        )
    document.save(path)
    document.close()


def _prepare_documents(root: str, service: MofaLibraryService):
    entries = service.list_entries(item_kind="")
    for entry in entries:
        os.makedirs(os.path.join(entry.bundle_dir, "mineru", "input"), exist_ok=True)
        _make_pdf(entry.pdf_path, entry.native_id)
        _make_pdf(entry.split_pdf_path, entry.native_id)
    return service.list_entries(item_kind="")


def _make_result(path: str, origin_pdf: str) -> None:
    os.makedirs(path, exist_ok=True)
    document = fitz.open(origin_pdf)
    try:
        page_count = len(document)
    finally:
        document.close()
    with open(os.path.join(path, "full.md"), "w", encoding="utf-8") as stream:
        stream.write("# MinerU OCR\n\n共産主義\n")
    with open(os.path.join(path, "layout.json"), "w", encoding="utf-8") as stream:
        json.dump(
            {
                "pdf_info": [{"page_idx": index} for index in range(page_count)],
                "_version_name": "3.4.0",
                "_backend": "hybrid",
                "_effort": "medium",
                "_ocr_enable": True,
            },
            stream,
        )
    with open(
        os.path.join(path, "12345678-1234-1234-1234-123456789012_content_list.json"),
        "w",
        encoding="utf-8",
    ) as stream:
        json.dump([{"page_idx": index, "text": "共産主義"} for index in range(page_count)], stream)
    shutil.copy2(
        origin_pdf,
        os.path.join(path, "12345678-1234-1234-1234-123456789012_origin.pdf"),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "mineru.sqlite3"))
        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/1921")
        items = [
            MofaCatalogItem(volume, "支那政局一", "https://mofa/one.pdf"),
            MofaCatalogItem(volume, "支那政局二", "https://mofa/two.pdf"),
            MofaCatalogItem(volume, "支那政局三", "https://mofa/three.pdf"),
            MofaCatalogItem(volume, "支那政局四", "https://mofa/four.pdf"),
        ]
        library = MofaLibraryService(project_root=root, db_service=db)
        assert library.cache_items(items) == 4
        entries = _prepare_documents(root, library)
        first, second, third, fourth = entries
        exports = os.path.join(root, "MinerU")

        named = os.path.join(
            exports,
            build_mineru_input_filename(first.title, first.native_id) + "-named-export",
        )
        _make_result(named, first.split_pdf_path)
        anonymous = os.path.join(exports, "document.single-pages.pdf-anonymous-export")
        _make_result(anonymous, second.split_pdf_path)

        importer = MofaMineruImportService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        result = importer.import_directory(exports)
        assert result.total == 2 and result.imported == 2, result
        by_id = {item.native_id: item for item in result.results}
        assert by_id[first.native_id].match_method == "native_id"
        assert by_id[second.native_id].match_method == "page_fingerprint"
        assert os.path.isfile(os.path.join(by_id[first.native_id].raw_dir, "import_manifest.json"))
        assert os.path.isdir(named), "Importer must preserve the MinerU source directory"
        assert db.fetchone("SELECT COUNT(*) AS count FROM mofa_mineru_runs")["count"] == 2

        duplicate = importer.import_directory(named)
        assert duplicate.skipped == 1 and duplicate.imported == 0
        assert importer.set_watch_dir(exports) == os.path.abspath(exports)
        assert importer.get_watch_dir() == os.path.abspath(exports)

        chunker = MofaPdfSplitService(max_mineru_pages=2)
        chunk_plan = chunker.ensure_chunks(fourth.split_pdf_path, fourth.bundle_dir)
        assert len(chunk_plan.parts) == 2
        first_chunk_export = os.path.join(
            exports,
            os.path.basename(chunk_plan.parts[0].path) + "-chunk-export",
        )
        _make_result(first_chunk_export, chunk_plan.parts[0].path)
        first_chunk_result = importer.import_directory(first_chunk_export)
        assert first_chunk_result.imported == 1
        assert first_chunk_result.results[0].chunk_start == 1
        partial = next(
            entry for entry in library.list_entries(item_kind="") if entry.native_id == fourth.native_id
        )
        assert partial.mineru_archived_parts == 1 and partial.mineru_expected_parts == 2
        assert partial.readiness == "OCR分段未完成"

        second_chunk_export = os.path.join(
            exports,
            os.path.basename(chunk_plan.parts[1].path) + "-chunk-export",
        )
        _make_result(second_chunk_export, chunk_plan.parts[1].path)
        second_chunk_result = importer.import_directory(second_chunk_export)
        assert second_chunk_result.imported == 1
        complete = next(
            entry for entry in library.list_entries(item_kind="") if entry.native_id == fourth.native_id
        )
        assert complete.mineru_archived_parts == complete.mineru_expected_parts == 2
        assert complete.readiness == "待整理"
        chunk_rows = db.fetchall(
            """
            SELECT chunk_start, chunk_end, total_pages
            FROM mofa_mineru_runs WHERE native_id = ? ORDER BY chunk_start
            """,
            (fourth.native_id,),
        )
        assert [tuple(row) for row in chunk_rows] == [(1, 2, 3), (3, 3, 3)]

        callbacks = []
        callback_event = threading.Event()

        def on_watch(batch) -> None:
            callbacks.append(batch)
            if any(item.native_id == third.native_id for item in batch.results):
                callback_event.set()

        watcher = MofaMineruDirectoryWatcher(importer, interval_seconds=0.1)
        watcher.start(exports, on_watch)
        watched = os.path.join(
            exports,
            build_mineru_input_filename(third.title, third.native_id) + "-watched-export",
        )
        _make_result(watched, third.split_pdf_path)
        assert callback_event.wait(3.0), "Watcher did not import a stable result directory"
        watcher.stop()
        assert not watcher.active
        assert any(batch.imported for batch in callbacks)
        assert db.fetchone("SELECT COUNT(*) AS count FROM mofa_mineru_runs")["count"] == 5
        db.close()
    print("MOFA MinerU importer checks passed: chunks, matching, dedupe, and watcher.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
