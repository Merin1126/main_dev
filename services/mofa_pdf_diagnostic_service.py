"""Text-layer diagnostics and corpus-size estimates for MOFA PDFs."""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Iterable

import fitz

from scrapers.mofa_catalog_scraper import MofaCatalogItem


@dataclass(frozen=True)
class MofaPdfDiagnostics:
    path: str
    bytes: int
    pages: int
    text_chars: int
    searchable_pages: int
    image_pages: int
    ocr_required_pages: tuple[int, ...]
    classification: str

    @property
    def text_coverage(self) -> float:
        return self.searchable_pages / self.pages if self.pages else 0.0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["text_coverage"] = self.text_coverage
        return payload


@dataclass(frozen=True)
class MofaCorpusEstimate:
    volumes: int
    all_pdfs: int
    content_pdfs: int
    content_pdfs_by_year: dict[int, int]
    sample_count: int
    sample_pages: int
    sample_bytes: int
    average_pages_per_pdf: float
    average_bytes_per_pdf: float
    projected_content_pages: int
    projected_content_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


class MofaPdfDiagnosticService:
    def __init__(self, *, min_searchable_chars: int = 20) -> None:
        self.min_searchable_chars = max(1, int(min_searchable_chars))

    def inspect_pdf(self, path: str) -> MofaPdfDiagnostics:
        absolute_path = os.path.abspath(path)
        document = fitz.open(absolute_path)
        try:
            text_chars = 0
            searchable_pages = 0
            image_pages = 0
            ocr_required: list[int] = []
            for page_index, page in enumerate(document):
                text = page.get_text().strip()
                chars = len(text)
                text_chars += chars
                has_images = bool(page.get_images(full=True))
                image_pages += int(has_images)
                if chars >= self.min_searchable_chars:
                    searchable_pages += 1
                else:
                    ocr_required.append(page_index + 1)

            page_count = len(document)
            if page_count and searchable_pages == page_count:
                classification = "searchable_text"
            elif searchable_pages == 0 and image_pages == page_count and page_count:
                classification = "image_only"
            elif searchable_pages == 0:
                classification = "no_searchable_text"
            else:
                classification = "mixed"
            return MofaPdfDiagnostics(
                path=absolute_path,
                bytes=os.path.getsize(absolute_path),
                pages=page_count,
                text_chars=text_chars,
                searchable_pages=searchable_pages,
                image_pages=image_pages,
                ocr_required_pages=tuple(ocr_required),
                classification=classification,
            )
        finally:
            document.close()

    def estimate_corpus(
        self,
        *,
        items: Iterable[MofaCatalogItem],
        samples: Iterable[MofaPdfDiagnostics],
    ) -> MofaCorpusEstimate:
        item_list = list(items)
        sample_list = list(samples)
        if not sample_list:
            raise ValueError("at least one MOFA PDF sample is required")
        content_items = [item for item in item_list if item.item_kind == "content"]
        by_year = Counter(item.volume.gregorian_year for item in content_items)
        volume_urls = {item.volume.catalog_url for item in item_list}
        avg_pages = mean(sample.pages for sample in sample_list)
        avg_bytes = mean(sample.bytes for sample in sample_list)
        return MofaCorpusEstimate(
            volumes=len(volume_urls),
            all_pdfs=len(item_list),
            content_pdfs=len(content_items),
            content_pdfs_by_year=dict(sorted(by_year.items())),
            sample_count=len(sample_list),
            sample_pages=sum(sample.pages for sample in sample_list),
            sample_bytes=sum(sample.bytes for sample in sample_list),
            average_pages_per_pdf=avg_pages,
            average_bytes_per_pdf=avg_bytes,
            projected_content_pages=round(len(content_items) * avg_pages),
            projected_content_bytes=round(len(content_items) * avg_bytes),
        )
