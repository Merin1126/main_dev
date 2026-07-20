from __future__ import annotations

import os
import tempfile

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.mofa_candidate_service import (
    EXCLUDED_STATUS,
    RELEVANT_STATUS,
    MofaCandidateService,
)
from services.mofa_fulltext_search_service import MofaFullTextSearchService
from services.mofa_library_service import MofaLibraryService
from services.mofa_mineru_import_service import MofaMineruImportService
from services.mofa_mineru_normalization_service import MofaMineruNormalizationService
from services.mofa_pdf_split_service import MofaPdfSplitService
from services.mofa_search_lexicon_service import CATEGORY_ALIAS, EXPANSION_CONCEPT
from test_mofa_mineru_normalization import _make_pdf, _make_result


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "candidates.sqlite3"))
        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/1921")
        item = MofaCatalogItem(volume, "候補史料", "https://mofa/candidate.pdf")
        library = MofaLibraryService(project_root=root, db_service=db)
        library.cache_items([item])
        entry = library.list_entries(item_kind="")[0]
        _make_pdf(entry.pdf_path, "SOURCE", 2)
        _make_pdf(entry.split_pdf_path, "SINGLE", 2)
        plan = MofaPdfSplitService().ensure_chunks(entry.split_pdf_path, entry.bundle_dir)
        export = os.path.join(root, "MinerU", os.path.basename(plan.parts[0].path) + "-result")
        _make_result(export, plan.parts[0].path, "候補")
        importer = MofaMineruImportService(project_root=root, db_service=db, library_service=library)
        assert importer.import_directory(export).imported == 1
        entry = library.list_entries(item_kind="")[0]
        normalizer = MofaMineruNormalizationService(project_root=root, db_service=db, library_service=library)
        assert normalizer.normalize_document(entry).status == "standardized"
        search = MofaFullTextSearchService(project_root=root, db_service=db, library_service=library)
        assert search.index_entry(entry).status == "indexed"

        hits = search.search("中国共産党")
        assert len(hits) == 2
        service = MofaCandidateService(db_service=db)
        first = service.add_hits(
            [hits[0]],
            query="中国共産党",
            mode="phrase",
            year=1921,
            volume_code="2",
            result_count=2,
        )
        assert first.created == 1 and first.merged == 0
        repeated = service.add_hits(
            [hits[0]],
            query="共産党",
            mode="phrase",
            year=1921,
            volume_code="2",
            result_count=2,
        )
        assert repeated.created == 0 and repeated.merged == 1
        candidate = service.list_candidates()[0]
        assert candidate.search_queries == ("中国共産党", "共産党")
        assert candidate.block_count == 1
        assert service.summary()["total"] == 1

        service.update_status([candidate.candidate_id], RELEVANT_STATUS)
        service.update_notes(candidate.candidate_id, "核心材料，需校对")
        assert service.set_tags(candidate.candidate_id, ["反共", "北京", "反共"]) == ("反共", "北京")
        updated = service.list_candidates(status=RELEVANT_STATUS, tag="反共")[0]
        assert updated.notes == "核心材料，需校对"
        assert updated.tags == ("北京", "反共")
        assert service.list_candidates(search_text="共産党")[0].candidate_id == candidate.candidate_id

        second = service.add_hits(
            [hits[1]],
            query="中国共産党",
            mode="phrase",
            result_count=2,
        )
        assert second.created == 1
        second_id = second.candidate_ids[0]
        service.update_status([second_id], EXCLUDED_STATUS)
        summary = service.summary()
        assert summary["total"] == 2
        assert summary[RELEVANT_STATUS] == 1
        assert summary[EXCLUDED_STATUS] == 1
        saved_count = db.fetchone("SELECT COUNT(*) AS value FROM mofa_saved_searches")
        assert saved_count and int(saved_count["value"]) == 3
        assert len(service.list_saved_searches()) == 3

        search.lexicon.add_rule(CATEGORY_ALIAS, "中共", "中国共産党", weight=0.9)
        expanded = search.execute_search("中共", expansion_level=EXPANSION_CONCEPT)
        saved_expanded = service.save_search(
            "中共",
            "phrase",
            result_count=len(expanded.hits),
            expansion_level=expanded.plan.expansion_level,
            lexicon_revision=expanded.plan.lexicon_revision,
            expansion_snapshot=expanded.plan.snapshot(),
        )
        assert saved_expanded.expansion_level == EXPANSION_CONCEPT
        assert saved_expanded.lexicon_revision == search.lexicon.current_revision()
        assert saved_expanded.expansion_snapshot["groups"][0][1]["term"] == "中国共産党"
        service.add_hit(expanded.hits[0], saved_expanded)
        highlight_terms = service.highlight_terms_for_candidate(candidate.candidate_id)
        assert "中共" in highlight_terms
        assert "中国共産党" in highlight_terms

        removal = service.remove_candidates((second_id, "missing-candidate"))
        assert removal.requested == 2
        assert removal.removed == 1
        assert removal.protected == 0
        assert removal.missing == 1
        assert service.status_for_page(hits[1].document_id, hits[1].page_index) == ""
        assert service.summary()["total"] == 1
        assert not db.fetchone(
            "SELECT 1 FROM mofa_candidate_search_sources WHERE candidate_id = ?",
            (second_id,),
        )
        db.close()

    print("MOFA candidate checks passed: dedupe, provenance, status, metadata, and removal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
