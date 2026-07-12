"""Inspect real MOFA samples and optionally estimate the 1921--1927 corpus."""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scrapers.mofa_catalog_scraper import MofaCatalogScraper  # noqa: E402
from services.mofa_pdf_diagnostic_service import MofaPdfDiagnosticService  # noqa: E402


def _collect_live_items():
    scraper = MofaCatalogScraper(timeout=90)
    volumes = scraper.discover_volumes()

    def fetch(volume):
        return MofaCatalogScraper(timeout=90).fetch_volume_items(volume)

    with ThreadPoolExecutor(max_workers=4) as executor:
        groups = list(executor.map(fetch, volumes))
    return [item for group in groups for item in group]


def main() -> int:
    parser = argparse.ArgumentParser(description="MOFA Phase 5A PDF/text-layer diagnostics")
    parser.add_argument("samples", nargs="+", help="Local MOFA sample PDF paths")
    parser.add_argument(
        "--live-catalog",
        action="store_true",
        help="Read the official 1921-1927 catalog and add a corpus-size estimate",
    )
    parser.add_argument("--output", default="", help="Optional JSON report path")
    args = parser.parse_args()

    service = MofaPdfDiagnosticService()
    samples = [service.inspect_pdf(path) for path in args.samples]
    payload = {"samples": [sample.to_dict() for sample in samples]}
    if args.live_catalog:
        payload["corpus_estimate"] = service.estimate_corpus(
            items=_collect_live_items(),
            samples=samples,
        ).to_dict()

    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        output_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(encoded + "\n")
        print(output_path)
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
