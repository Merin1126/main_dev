from __future__ import annotations

from pathlib import Path

from scrapers.mofa_catalog_scraper import discover_target_volumes, parse_volume_items


FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
INDEX_URL = "https://www.mofa.go.jp/mofaj/annai/honsho/shiryo/archives/mokuji.html"


def main() -> int:
    index_html = (FIXTURES / "mofa_index_sample.html").read_text(encoding="utf-8")
    volumes = discover_target_volumes(index_html, index_url=INDEX_URL)
    assert [(v.gregorian_year, v.volume_code) for v in volumes] == [
        (1921, "11"),
        (1921, "12"),
        (1926, "23"),
        (1927, "1-1"),
    ]
    assert all(v.catalog_url.startswith("https://www.mofa.go.jp/") for v in volumes)

    volume_html = (FIXTURES / "mofa_volume_sample.html").read_text(encoding="utf-8")
    items = parse_volume_items(volume_html, volume=volumes[0])
    assert len(items) == 5
    assert [item.item_kind for item in items] == [
        "front_matter",
        "content",
        "content",
        "index",
        "colophon",
    ]
    assert items[1].title == "支那政局ニ関スル件"
    assert items[1].pdf_url == "https://www.mofa.go.jp/mofaj/annai/honsho/pdf/t10/sample-001.pdf"
    print("Phase 2 parser checks passed: target volumes, PDF items, URL resolution, classification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
