from __future__ import annotations

import json
import os
import tempfile

import fitz

from services.mofa_pdf_split_service import MofaPdfSplitService


def _make_sample(path: str) -> None:
    document = fitz.open()
    spread = document.new_page(width=840, height=594)
    spread.draw_rect(fitz.Rect(0, 0, 420, 594), color=(1, 0, 0), fill=(1, 0, 0))
    spread.draw_rect(fitz.Rect(420, 0, 840, 594), color=(0, 0, 1), fill=(0, 0, 1))
    spread.insert_text((80, 100), "LEFT-421", fontsize=24, color=(1, 1, 1))
    spread.insert_text((520, 100), "RIGHT-420", fontsize=24, color=(1, 1, 1))
    portrait = document.new_page(width=420, height=594)
    portrait.insert_text((80, 100), "PORTRAIT", fontsize=24)
    document.save(path)
    document.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        source_path = os.path.join(root, "document.pdf")
        _make_sample(source_path)
        service = MofaPdfSplitService()
        result = service.split(source_path, root)
        assert os.path.basename(result.output_path) == "document single-pages.pdf"
        assert result.source_pages == 2
        assert result.output_pages == 3
        assert result.split_pages == 1
        assert result.preserved_pages == 1
        assert result.mappings[0].order == ("right", "left")
        assert service.is_current(source_path, root)

        output = fitz.open(result.output_path)
        try:
            assert len(output) == 3
            assert "RIGHT-420" in output[0].get_text()
            assert "LEFT-421" not in output[0].get_text()
            assert "LEFT-421" in output[1].get_text()
            assert "PORTRAIT" in output[2].get_text()
            assert round(output[0].rect.width) == 420
        finally:
            output.close()

        with open(result.manifest_path, encoding="utf-8") as stream:
            manifest = json.load(stream)
        assert manifest["settings"]["reading_order"] == "right_to_left"
        assert manifest["page_mapping"][0]["output_pdf_pages"] == [1, 2]

        chunker = MofaPdfSplitService(max_mineru_pages=2)
        plan = chunker.ensure_chunks(result.output_path, root)
        assert plan.total_pages == 3 and len(plan.parts) == 2
        assert [(part.start_page, part.end_page) for part in plan.parts] == [(1, 2), (3, 3)]
        assert os.path.basename(plan.parts[0].path).endswith("p0001-p0002.pdf")
        assert chunker.chunks_are_current(result.output_path, root)
        with open(plan.manifest_path, encoding="utf-8") as stream:
            chunk_manifest = json.load(stream)
        assert chunk_manifest["settings"]["max_pages_per_part"] == 2
        assert chunk_manifest["parts"][1]["start_page"] == 3

        with open(source_path, "ab") as stream:
            stream.write(b"\n% changed")
        assert not service.is_current(source_path, root)
    print("MOFA PDF split checks passed: right-to-left mapping and source preservation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
