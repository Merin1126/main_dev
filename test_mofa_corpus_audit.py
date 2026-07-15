from __future__ import annotations

import json
import os
import tempfile

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_corpus_audit_service import MofaCorpusAuditService
from services.mofa_fulltext_search_service import MofaFullTextSearchService
from services.mofa_library_service import MofaLibraryService
from services.mofa_mineru_import_service import MofaMineruImportService
from services.mofa_mineru_normalization_service import MofaMineruNormalizationService
from services.mofa_pdf_split_service import MofaPdfSplitService
from test_mofa_mineru_normalization import _make_pdf, _make_result


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "audit.sqlite3"))
        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/1921")
        complete_item = MofaCatalogItem(volume, "完成史料", "https://mofa/complete.pdf")
        pending_item = MofaCatalogItem(volume, "待拆页史料", "https://mofa/pending.pdf")
        library = MofaLibraryService(project_root=root, db_service=db)
        library.cache_items([complete_item, pending_item])
        entries = library.list_entries(item_kind="")
        complete = next(item for item in entries if item.title == "完成史料")
        pending = next(item for item in entries if item.title == "待拆页史料")

        _make_pdf(complete.pdf_path, "COMPLETE", 2)
        _make_pdf(complete.split_pdf_path, "SINGLE", 2)
        _make_pdf(pending.pdf_path, "PENDING", 1)
        plan = MofaPdfSplitService().ensure_chunks(complete.split_pdf_path, complete.bundle_dir)
        export = os.path.join(root, "MinerU", os.path.basename(plan.parts[0].path) + "-result")
        _make_result(export, plan.parts[0].path, "監査")

        importer = MofaMineruImportService(project_root=root, db_service=db, library_service=library)
        assert importer.import_directory(export).imported == 1
        normalizer = MofaMineruNormalizationService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        complete = next(item for item in library.list_entries(item_kind="") if item.title == "完成史料")
        assert normalizer.normalize_document(complete).status == "standardized"
        search = MofaFullTextSearchService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        assert search.index_entry(complete).status == "indexed"

        audit = MofaCorpusAuditService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        refreshed = library.list_entries(item_kind="")
        report = audit.audit_entries(refreshed, scope_label="test")
        assert report.entry_count == 2
        assert report.healthy_count == 1
        assert report.native_ids_for_action("split") == (pending.native_id,)
        assert report.duration_ms >= 0 and report.database_size_bytes > 0
        stored = db.fetchone(
            "SELECT issue_count FROM mofa_corpus_audit_runs WHERE audit_id = ?",
            (report.audit_id,),
        )
        assert stored and int(stored["issue_count"]) == 1

        block = db.fetchone(
            "SELECT block_row_id FROM mofa_search_blocks WHERE document_id = ? LIMIT 1",
            (db.make_document_id("mofa", complete.native_id),),
        )
        assert block
        db.execute("DELETE FROM mofa_search_blocks WHERE block_row_id = ?", (block["block_row_id"],))
        index_report = audit.audit_entries([complete], persist=False)
        assert index_report.native_ids_for_action("reindex") == (complete.native_id,)
        assert search.index_entry(complete, force=True).status == "indexed"

        active = db.fetchone(
            """
            SELECT g.search_text_path FROM mofa_ocr_active_generations a
            JOIN mofa_ocr_generations g ON g.generation_id = a.generation_id
            WHERE a.document_id = ?
            """,
            (db.make_document_id("mofa", complete.native_id),),
        )
        search_path = os.path.join(complete.bundle_dir, str(active["search_text_path"]))
        with open(search_path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        payload["generation_id"] = "stale-generation"
        with open(search_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        stale_report = audit.audit_entries([complete], persist=False)
        assert stale_report.native_ids_for_action("standardize") == (complete.native_id,)
        db.close()

    print("MOFA corpus audit checks passed: healthy, repair routing, persistence, and stale data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
