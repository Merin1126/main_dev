from __future__ import annotations

import os
import tempfile

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_fulltext_search_service import (
    SEARCH_MODE_ALL,
    SEARCH_MODE_ANY,
    MofaFullTextSearchService,
)
from services.mofa_library_service import MofaLibraryService
from services.mofa_mineru_import_service import MofaMineruImportService
from services.mofa_mineru_normalization_service import MofaMineruNormalizationService
from services.mofa_pdf_split_service import MofaPdfSplitService
from services.mofa_search_lexicon_service import (
    CATEGORY_ALIAS,
    CATEGORY_OCR,
    CATEGORY_RELATED,
    EXPANSION_CONCEPT,
    EXPANSION_OCR,
)
from test_mofa_mineru_normalization import _make_pdf, _make_result


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "search.sqlite3"))
        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/1921")
        item = MofaCatalogItem(volume, "中国共産党関係", "https://mofa/search.pdf")
        library = MofaLibraryService(project_root=root, db_service=db)
        library.cache_items([item])
        entry = library.list_entries(item_kind="")[0]

        _make_pdf(entry.pdf_path, "SOURCE", 2)
        _make_pdf(entry.split_pdf_path, "SINGLE", 2)
        plan = MofaPdfSplitService().ensure_chunks(entry.split_pdf_path, entry.bundle_dir)
        export = os.path.join(root, "MinerU", os.path.basename(plan.parts[0].path) + "-result")
        _make_result(export, plan.parts[0].path, "検索")

        importer = MofaMineruImportService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        imported = importer.import_directory(export)
        assert imported.imported == 1, imported
        normalizer = MofaMineruNormalizationService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        normalized = normalizer.normalize_document(library.list_entries(item_kind="")[0])
        assert normalized.status == "standardized", normalized

        service = MofaFullTextSearchService(
            project_root=root,
            db_service=db,
            library_service=library,
        )
        indexed = service.index_entry(library.list_entries(item_kind="")[0])
        assert indexed.status == "indexed", indexed
        assert indexed.page_count == 2
        assert indexed.block_count == 3
        assert service.index_summary() == (1, 2, 3)
        repeated = service.index_entry(library.list_entries(item_kind="")[0])
        assert repeated.status == "skipped", repeated

        phrase_hits = service.search("中国共産党")
        assert len(phrase_hits) == 2
        assert phrase_hits[0].matching_blocks
        assert phrase_hits[0].matching_blocks[0].bbox == (0.1, 0.1, 0.9, 0.25)
        linked_page = service.get_indexed_page(
            phrase_hits[0].document_id,
            phrase_hits[0].generation_id,
            phrase_hits[0].page_index,
        )
        assert linked_page and linked_page.raw_text == phrase_hits[0].raw_text
        assert service.indexed_document_page_count(
            phrase_hits[0].document_id,
            phrase_hits[0].generation_id,
        ) == 2
        if phrase_hits[0].source_pdf_page is not None:
            original_pages = service.indexed_pages_for_source_pdf(
                phrase_hits[0].document_id,
                phrase_hits[0].generation_id,
                phrase_hits[0].source_pdf_page,
            )
            assert phrase_hits[0].page_index in {page.page_index for page in original_pages}

        old_style_hits = service.search("反帝國主義")
        assert len(old_style_hits) == 1
        assert old_style_hits[0].display_page == 2
        assert "反帝國主義" in old_style_hits[0].snippet

        short_hits = service.search("上海")
        assert len(short_hits) == 1
        assert short_hits[0].matching_blocks
        all_hits = service.search("中国 上海", mode=SEARCH_MODE_ALL)
        assert len(all_hits) == 1 and all_hits[0].display_page == 2
        any_hits = service.search("不存在 上海", mode=SEARCH_MODE_ANY)
        assert len(any_hits) == 1

        assert service.search("中共") == []
        service.lexicon.add_rule(CATEGORY_ALIAS, "中共", "中国共産党", weight=0.9)
        concept_execution = service.execute_search(
            "中共", expansion_level=EXPANSION_CONCEPT
        )
        assert len(concept_execution.hits) == 2
        assert "中国共産党" in concept_execution.plan.terms
        assert concept_execution.hits[0].reason_label == "历史术语"
        assert concept_execution.hits[0].lexicon_revision == service.lexicon.current_revision()
        expanded_all = service.search(
            "中共 上海",
            mode=SEARCH_MODE_ALL,
            expansion_level=EXPANSION_CONCEPT,
        )
        assert len(expanded_all) == 1 and expanded_all[0].display_page == 2

        assert service.search("共産営") == []
        service.lexicon.add_rule(CATEGORY_OCR, "共産営", "共産党", weight=0.7)
        ocr_hits = service.search("共産営", expansion_level=EXPANSION_OCR)
        assert len(ocr_hits) == 2
        assert ocr_hits[0].reason_label == "OCR混淆"
        service.lexicon.add_rule(
            CATEGORY_RELATED, "概念候補", "中国共産党", weight=0.5
        )
        service.lexicon.add_rule(
            CATEGORY_RELATED, "概念候補", "上海", weight=0.9
        )
        weighted_hits = service.search(
            "概念候補", expansion_level=EXPANSION_CONCEPT
        )
        assert len(weighted_hits) == 2
        assert weighted_hits[0].display_page == 2
        assert weighted_hits[0].match_weight == 0.9
        assert weighted_hits[1].match_weight == 0.5
        assert service.normalized_match_ranges(
            "前文 反帝國主義 後文",
            ("反帝国主義",),
        ) == ((3, 8),)
        assert service.normalized_match_ranges(
            "中國 共產黨",
            ("中国共産党",),
        ) == ((0, 6),)

        forced = service.index_entry(library.list_entries(item_kind="")[0], force=True)
        assert forced.status == "indexed", forced
        assert len(service.search("中国共産党")) == 2
        page_rows = db.fetchone("SELECT COUNT(*) AS value FROM mofa_search_pages")
        assert page_rows and int(page_rows["value"]) == 2
        db.close()

    print(
        "MOFA FTS checks passed: generation indexing, page linkage, trigram, "
        "short terms, and bbox hits."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
