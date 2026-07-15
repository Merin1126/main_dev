from __future__ import annotations

import json
import os
import shutil
import tempfile

import fitz

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_library_service import MofaLibraryService
from services.mofa_mineru_import_service import MofaMineruImportService
from services.mofa_mineru_normalization_service import MofaMineruNormalizationService
from services.mofa_pdf_split_service import MofaPdfSplitService


def _make_pdf(path: str, label: str, pages: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=420, height=594)
        page.insert_text((40, 70), f"{label} {index + 1}", fontsize=20)
    document.save(path)
    document.close()


def _make_result(path: str, origin_pdf: str, marker: str) -> None:
    os.makedirs(path, exist_ok=True)
    with fitz.open(origin_pdf) as document:
        page_count = len(document)
    blocks = []
    for index in range(page_count):
        blocks.append(
            {
                "type": "text",
                "text": f"中國共產黨{marker}{index + 1}",
                "bbox": [100, 100, 900, 250],
                "page_idx": index,
            }
        )
        blocks.append(
            {
                "type": "page_number",
                "text": str(index + 100),
                "bbox": [900, 900, 950, 980],
                "page_idx": index,
            }
        )
    blocks.append(
        {
            "type": "table",
            "table_body": "<table><tr><td>反帝國主義</td><td>上海</td></tr></table>",
            "bbox": [50, 300, 800, 800],
            "page_idx": page_count - 1,
        }
    )
    with open(os.path.join(path, "full.md"), "w", encoding="utf-8") as stream:
        stream.write(f"# {marker}\n")
    with open(os.path.join(path, "layout.json"), "w", encoding="utf-8") as stream:
        json.dump(
            {
                "pdf_info": [
                    {"page_idx": index, "page_size": [420, 594]}
                    for index in range(page_count)
                ],
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
        json.dump(blocks, stream, ensure_ascii=False)
    shutil.copy2(
        origin_pdf,
        os.path.join(path, "12345678-1234-1234-1234-123456789012_origin.pdf"),
    )


def _entry(library: MofaLibraryService, native_id: str):
    return next(
        item for item in library.list_entries(item_kind="") if item.native_id == native_id
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "normalization.sqlite3"))
        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/1921")
        chunked_item = MofaCatalogItem(volume, "支那政局", "https://mofa/chunked.pdf")
        legacy_item = MofaCatalogItem(volume, "露国関係", "https://mofa/legacy.pdf")
        library = MofaLibraryService(project_root=root, db_service=db)
        library.cache_items([chunked_item, legacy_item])
        entries = library.list_entries(item_kind="")
        chunked = next(item for item in entries if item.title == chunked_item.title)
        legacy = next(item for item in entries if item.title == legacy_item.title)

        _make_pdf(chunked.pdf_path, "SOURCE", 3)
        _make_pdf(chunked.split_pdf_path, "SINGLE", 3)
        _make_pdf(legacy.pdf_path, "LEGACY-SOURCE", 2)
        _make_pdf(legacy.split_pdf_path, "LEGACY-SINGLE", 2)

        chunker = MofaPdfSplitService(max_mineru_pages=2)
        chunk_plan = chunker.ensure_chunks(chunked.split_pdf_path, chunked.bundle_dir)
        legacy_plan = MofaPdfSplitService().ensure_chunks(legacy.split_pdf_path, legacy.bundle_dir)
        exports = os.path.join(root, "MinerU")
        importer = MofaMineruImportService(
            project_root=root,
            db_service=db,
            library_service=library,
        )

        first_export = os.path.join(exports, os.path.basename(chunk_plan.parts[0].path) + "-old")
        _make_result(first_export, chunk_plan.parts[0].path, "旧版")
        first_result = importer.import_directory(first_export)
        assert first_result.imported == 1, first_result

        second_export = os.path.join(exports, os.path.basename(chunk_plan.parts[1].path) + "-part2")
        _make_result(second_export, chunk_plan.parts[1].path, "第二段")
        second_result = importer.import_directory(second_export)
        assert second_result.imported == 1, second_result

        replacement_export = os.path.join(
            exports,
            os.path.basename(chunk_plan.parts[0].path) + "-replacement",
        )
        _make_result(replacement_export, chunk_plan.parts[0].path, "新版")
        replacement_result = importer.import_directory(replacement_export)
        assert replacement_result.imported == 1, replacement_result
        replacement_raw = replacement_result.results[0].raw_dir
        db.execute(
            "UPDATE mofa_mineru_runs SET imported_at = ? WHERE raw_dir = ?",
            ("9999-12-31T23:59:59+00:00", replacement_raw),
        )

        legacy_export = os.path.join(
            exports,
            os.path.basename(legacy_plan.parts[0].path) + "-legacy",
        )
        _make_result(legacy_export, legacy_plan.parts[0].path, "舊資料")
        legacy_import = importer.import_directory(legacy_export)
        assert legacy_import.imported == 1, legacy_import
        db.execute(
            """
            UPDATE mofa_mineru_runs
            SET input_sha256 = NULL, chunk_index = NULL, chunk_count = NULL,
                chunk_start = NULL, chunk_end = NULL, total_pages = NULL
            WHERE raw_dir = ?
            """,
            (legacy_import.results[0].raw_dir,),
        )

        normalizer = MofaMineruNormalizationService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        chunked = _entry(library, chunked.native_id)
        normalized = normalizer.normalize_document(chunked)
        assert normalized.status == "standardized", normalized
        assert normalized.page_count == 3
        assert normalized.block_count == 8
        assert normalized.searchable_block_count == 5
        assert os.path.isfile(normalized.artifact_path)
        assert os.path.isfile(normalized.search_text_path)

        with open(normalized.artifact_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        assert manifest["coverage"]["complete"] is True
        assert len(manifest["source_runs"]) == 2
        assert len(manifest["superseded_runs"]) == 1

        pages_path = os.path.join(os.path.dirname(normalized.artifact_path), "ocr_pages.v1.jsonl")
        with open(pages_path, "r", encoding="utf-8") as stream:
            pages = [json.loads(line) for line in stream if line.strip()]
        assert [page["page_index"] for page in pages] == [0, 1, 2]
        assert "中國共產黨新版1" in pages[0]["raw_text"]
        assert "中国共産党新版1" in pages[0]["search_text"]
        assert pages[0]["blocks"][0]["bbox_norm"] == [0.1, 0.1, 0.9, 0.25]
        assert pages[0]["printed_page_label"] == "100"
        assert pages[0]["blocks"][1]["searchable"] is False
        assert "反帝国主義" in pages[2]["search_text"]

        active = db.fetchone(
            "SELECT generation_id FROM mofa_ocr_active_generations WHERE document_id = ?",
            (db.make_document_id("mofa", chunked.native_id),),
        )
        assert active and active["generation_id"] == normalized.generation_id
        repeated = normalizer.normalize_document(_entry(library, chunked.native_id))
        assert repeated.status == "skipped", repeated

        legacy_normalized = normalizer.normalize_document(_entry(library, legacy.native_id))
        assert legacy_normalized.status == "standardized", legacy_normalized
        with open(legacy_normalized.artifact_path, "r", encoding="utf-8") as stream:
            legacy_manifest = json.load(stream)
        assert any("旧版单段兼容映射" in warning for warning in legacy_manifest["warnings"])
        assert _entry(library, legacy.native_id).readiness == "可检索"
        db.close()

    print("MOFA MinerU normalization checks passed: generations, chunks, legacy runs, and artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
