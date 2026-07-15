from __future__ import annotations

import json
import os
import tempfile

from services.db_service import DbService
from services.mofa_search_lexicon_service import (
    CATEGORY_ALIAS,
    CATEGORY_GLYPH,
    CATEGORY_OCR,
    CATEGORY_RELATED,
    EXPANSION_CONCEPT,
    EXPANSION_EXACT,
    EXPANSION_GLYPH,
    EXPANSION_OCR,
    MofaSearchLexiconService,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "lexicon.sqlite3"))
        service = MofaSearchLexiconService(db_service=db)
        assert service.current_revision() >= 1
        assert any(rule.built_in for rule in service.list_rules(category=CATEGORY_GLYPH))
        initial_revision = service.current_revision()

        glyph = service.add_rule(CATEGORY_GLYPH, "辨", "弁", provenance="研究者确认")
        ocr = service.add_rule(CATEGORY_OCR, "共産営", "共産党", weight=0.7)
        alias = service.add_rule(CATEGORY_ALIAS, "中共", "中国共産党", weight=0.9)
        related = service.add_rule(
            CATEGORY_RELATED,
            "赤化",
            "中国共産党",
            bidirectional=False,
            weight=0.55,
        )
        assert service.current_revision() == initial_revision + 4
        complete_revision = service.current_revision()

        exact = service.build_plan("中共", "phrase", EXPANSION_EXACT)
        assert exact.terms == ("中共",)
        glyph_only = service.build_plan("辨明", "phrase", EXPANSION_GLYPH)
        assert "弁明" in glyph_only.terms
        ocr_plan = service.build_plan("共産営", "phrase", EXPANSION_OCR)
        assert "共産党" in ocr_plan.terms
        concept = service.build_plan("中共", "phrase", EXPANSION_CONCEPT)
        assert "中国共産党" in concept.terms
        related_plan = service.build_plan("赤化", "phrase", EXPANSION_CONCEPT)
        assert "中国共産党" in related_plan.terms
        assert concept.snapshot()["lexicon_revision"] == service.current_revision()

        disabled = service.set_active(ocr.rule_id, False)
        assert not disabled.active
        assert "共産党" not in service.build_plan("共産営", "phrase", EXPANSION_OCR).terms
        service.update_rule(
            alias.rule_id,
            category=CATEGORY_ALIAS,
            source_term="中共",
            target_term="中国共産党",
            bidirectional=True,
            weight=0.85,
            notes="项目术语",
            provenance="研究者确认",
        )
        service.delete_rule(related.rule_id)
        assert len(service.revision_history()) >= 8

        export_path = os.path.join(root, "lexicon.json")
        service.export_file(export_path)
        with open(export_path, "r", encoding="utf-8") as stream:
            exported = json.load(stream)
        assert exported["schema_version"] == 1
        assert all(not item.get("built_in") for item in exported["rules"])
        assert {item["source_term"] for item in exported["rules"]} == {"辨", "共産営", "中共"}
        revision_before_reimport = service.current_revision()
        created, skipped = service.import_file(export_path)
        assert created == 0 and skipped == 3
        assert service.current_revision() == revision_before_reimport

        try:
            service.set_active(next(rule.rule_id for rule in service.list_rules() if rule.built_in), False)
        except ValueError:
            pass
        else:
            raise AssertionError("built-in rules must remain immutable")
        restored_revision = service.restore_revision(complete_revision)
        assert restored_revision == complete_revision
        assert service.current_revision() == complete_revision
        restored_custom = [rule for rule in service.list_rules() if not rule.built_in]
        assert len(restored_custom) == 4
        assert next(rule for rule in restored_custom if rule.rule_id == ocr.rule_id).active
        db.close()

    print("MOFA lexicon checks passed: revisions, layered expansion, lifecycle, and export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
