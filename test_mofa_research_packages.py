from __future__ import annotations

import json
import os
import tempfile

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_candidate_service import EXCLUDED_STATUS, MofaCandidateService
from services.mofa_fulltext_search_service import MofaFullTextSearchService
from services.mofa_library_service import MofaLibraryService
from services.mofa_mineru_import_service import MofaMineruImportService
from services.mofa_mineru_normalization_service import MofaMineruNormalizationService
from services.mofa_pdf_split_service import MofaPdfSplitService
from services.mofa_research_package_service import (
    MOFA_PACKAGE_ID_PREFIX,
    MOFA_PACKAGE_MANIFEST,
    MOFA_PACKAGE_SCOPE_FULL_DOCUMENT,
    MofaResearchPackageService,
)
from test_mofa_mineru_normalization import _make_pdf, _make_result


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "research-packages.sqlite3"))
        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/1921")
        item = MofaCatalogItem(volume, "中国共産党関係", "https://mofa/research-package.pdf")
        library = MofaLibraryService(project_root=root, db_service=db)
        library.cache_items([item])
        entry = library.list_entries(item_kind="")[0]
        _make_pdf(entry.pdf_path, "SOURCE", 5)
        _make_pdf(entry.split_pdf_path, "SINGLE", 5)
        split = MofaPdfSplitService().ensure_chunks(entry.split_pdf_path, entry.bundle_dir)
        export = os.path.join(root, "MinerU", os.path.basename(split.parts[0].path) + "-result")
        _make_result(export, split.parts[0].path, "研究包")
        importer = MofaMineruImportService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        assert importer.import_directory(export).imported == 1
        normalizer = MofaMineruNormalizationService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        entry = library.list_entries(item_kind="")[0]
        assert normalizer.normalize_document(entry).status == "standardized"
        search = MofaFullTextSearchService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        assert search.index_entry(entry).status == "indexed"
        hits = search.search("中国共産党")
        assert len(hits) == 5

        candidates = MofaCandidateService(db_service=db)
        added = candidates.add_hits(
            (hits[0], hits[2]),
            query="中国共産党",
            mode="phrase",
            result_count=5,
        )
        assert added.created == 2

        packages = MofaResearchPackageService(
            project_root=root,
            db_service=db,
            library_service=library,
            candidate_service=candidates,
        )
        separate = packages.preview(added.candidate_ids, context_before=0, context_after=0)
        assert separate.range_count == 2
        merged = packages.preview(added.candidate_ids, context_before=0, context_after=1)
        assert merged.package_id.startswith(MOFA_PACKAGE_ID_PREFIX)
        assert merged.range_count == 1
        assert merged.ranges[0].start_page_index == 0
        assert merged.ranges[0].end_page_index == 3
        assert merged.selected_page_count == 2
        assert merged.included_page_count == 4

        created = packages.create_package(
            added.candidate_ids,
            context_before=0,
            context_after=1,
            display_name="核心资料",
            notes="Phase 7A 测试工作包",
        )
        assert created.created
        package = created.package
        assert package.source == "mofa"
        assert package.package_type == "mofa_research_package"
        assert package.display_name.startswith("MOFA研究工作包｜")
        assert package.package_id in package.package_dir
        assert os.path.join("research", "mofa") in package.package_dir
        assert os.path.basename(package.manifest_path) == MOFA_PACKAGE_MANIFEST
        assert os.path.isfile(package.manifest_path)
        for child in ("ocr", "analysis", "translation", "export"):
            assert os.path.isdir(os.path.join(package.package_dir, child))

        with open(package.manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        assert manifest["schema_version"] == 2
        assert manifest["source"] == "mofa"
        assert manifest["package_type"] == "mofa_research_package"
        assert manifest["selection_scope"] == "candidate_context"
        assert manifest["storage"]["source_pdfs_copied"] is False
        assert manifest["counts"] == {
            "documents": 1,
            "ranges": 1,
            "selected_pages": 2,
            "included_pages": 4,
        }
        assert manifest["documents"][0]["source"] == "mofa"
        assert manifest["documents"][0]["single_pages_pdf"].startswith("Historical_Documents/")
        roles = [page["role"] for page in manifest["documents"][0]["ranges"][0]["pages"]]
        assert roles == ["selected", "context", "selected", "context"]
        assert manifest["search_provenance"][0]["query_text"] == "中国共産党"

        full_preview = packages.preview(
            added.candidate_ids,
            selection_scope=MOFA_PACKAGE_SCOPE_FULL_DOCUMENT,
            context_before=12,
            context_after=12,
        )
        assert full_preview.package_id != package.package_id
        assert full_preview.selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
        assert full_preview.context_before == 0 and full_preview.context_after == 0
        assert full_preview.range_count == 1
        assert full_preview.ranges[0].start_page_index == 0
        assert full_preview.ranges[0].end_page_index == 4
        assert full_preview.included_page_count == 5
        assert full_preview.default_display_name.startswith("MOFA整份PDF研究工作包｜")
        full_created = packages.create_package(
            added.candidate_ids,
            selection_scope=MOFA_PACKAGE_SCOPE_FULL_DOCUMENT,
        )
        assert full_created.created
        assert full_created.package.selection_scope == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
        with open(full_created.package.manifest_path, "r", encoding="utf-8") as stream:
            full_manifest = json.load(stream)
        assert full_manifest["selection_scope"] == MOFA_PACKAGE_SCOPE_FULL_DOCUMENT
        full_roles = [
            page["role"]
            for page in full_manifest["documents"][0]["ranges"][0]["pages"]
        ]
        assert full_roles == [
            "selected",
            "document_scope",
            "selected",
            "document_scope",
            "document_scope",
        ]
        deleted_full = packages.delete_package(
            full_created.package.package_id,
            cancel_candidates=False,
        )
        assert deleted_full.retained_candidate_count == 2

        repeated = packages.create_package(
            reversed(added.candidate_ids),
            context_before=0,
            context_after=1,
            display_name="不会覆盖已有名称",
        )
        assert not repeated.created
        assert repeated.package.package_id == package.package_id
        assert repeated.package.display_name == package.display_name
        assert len(packages.list_packages()) == 1
        ready = packages.update_status(package.package_id, "ready")
        assert ready.status == "ready"
        with open(ready.manifest_path, "r", encoding="utf-8") as stream:
            assert json.load(stream)["status"] == "ready"

        protected = candidates.remove_candidates(added.candidate_ids)
        assert protected.removed == 0
        assert protected.protected == 2
        assert protected.package_ids == (package.package_id,)
        assert candidates.summary()["total"] == 2

        shared_added = candidates.add_hits(
            (hits[4],),
            query="共産党",
            mode="phrase",
            result_count=5,
        )
        shared_package = packages.create_package(
            (added.candidate_ids[0], shared_added.candidate_ids[0]),
            context_before=0,
            context_after=0,
            display_name="共享候选删除测试",
        ).package
        shared_dir = shared_package.package_dir
        deleted_shared = packages.delete_package(
            shared_package.package_id,
            cancel_candidates=True,
        )
        assert deleted_shared.cancelled_candidate_count == 1
        assert deleted_shared.retained_candidate_count == 1
        assert deleted_shared.retaining_package_ids == (package.package_id,)
        assert deleted_shared.directory_removed
        assert not os.path.exists(shared_dir)
        assert candidates.status_for_page(hits[4].document_id, hits[4].page_index) == ""
        assert candidates.status_for_page(hits[0].document_id, hits[0].page_index)

        retained_added = candidates.add_hits(
            (hits[1],),
            query="共産党",
            mode="phrase",
            result_count=5,
        )
        retained_package = packages.create_package(
            retained_added.candidate_ids,
            context_before=0,
            context_after=0,
            display_name="保留候选删除测试",
        ).package
        packages.update_status(retained_package.package_id, "processing")
        try:
            packages.delete_package(retained_package.package_id)
        except ValueError as exc:
            assert "溯源保护" in str(exc)
        else:
            raise AssertionError("processing packages must be protected from deletion")
        packages.update_status(retained_package.package_id, "draft")
        deleted_retained = packages.delete_package(
            retained_package.package_id,
            cancel_candidates=False,
        )
        assert deleted_retained.cancelled_candidate_count == 0
        assert deleted_retained.retained_candidate_count == 1
        assert candidates.status_for_page(hits[1].document_id, hits[1].page_index)
        assert len(packages.list_packages()) == 1

        candidates.update_status((added.candidate_ids[0],), EXCLUDED_STATUS)
        try:
            packages.preview(added.candidate_ids, context_before=0, context_after=0)
        except ValueError as exc:
            assert "已排除" in str(exc)
        else:
            raise AssertionError("excluded candidates must not enter a new package")
        db.close()

    print(
        "MOFA research package checks passed: naming, context, provenance, dedupe, "
        "full-document scope, safe deletion, and shared-candidate protection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
