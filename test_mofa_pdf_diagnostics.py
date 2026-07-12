from __future__ import annotations

import io
import os
import tempfile

import fitz
from PIL import Image

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.mofa_pdf_diagnostic_service import MofaPdfDiagnosticService


def _build_fixture_pdf(path: str) -> None:
    document = fitz.open()
    text_page = document.new_page()
    text_page.insert_text((72, 72), "searchable MOFA diplomatic document text " * 3)

    image = Image.new("RGB", (400, 600), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_page = document.new_page(width=400, height=600)
    image_page.insert_image(image_page.rect, stream=buffer.getvalue())
    document.save(path)
    document.close()


def main() -> int:
    service = MofaPdfDiagnosticService(min_searchable_chars=20)
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "mixed.pdf")
        _build_fixture_pdf(path)
        diagnostics = service.inspect_pdf(path)
        assert diagnostics.pages == 2
        assert diagnostics.searchable_pages == 1
        assert diagnostics.image_pages == 1
        assert diagnostics.ocr_required_pages == (2,)
        assert diagnostics.classification == "mixed"

        volume = MofaVolume("T", 10, 1921, "2", "大正10年 第2冊", "https://mofa/v")
        items = [
            MofaCatalogItem(volume, "正文1", "https://mofa/1.pdf"),
            MofaCatalogItem(volume, "正文2", "https://mofa/2.pdf"),
            MofaCatalogItem(volume, "奥付", "https://mofa/3.pdf", "colophon"),
        ]
        estimate = service.estimate_corpus(items=items, samples=[diagnostics])
        assert estimate.volumes == 1
        assert estimate.all_pdfs == 3
        assert estimate.content_pdfs == 2
        assert estimate.projected_content_pages == 4
        assert estimate.content_pdfs_by_year == {1921: 2}

    print("Phase 5A checks passed: text-layer classification, OCR pages, corpus estimate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
