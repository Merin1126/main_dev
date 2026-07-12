"""Source-specific discovery adapters for HRS."""

from .mofa_catalog_scraper import (
    MOFA_INDEX_URL,
    MofaCatalogItem,
    MofaCatalogScraper,
    MofaVolume,
    discover_target_volumes,
    parse_volume_items,
)

__all__ = [
    "MOFA_INDEX_URL",
    "MofaCatalogItem",
    "MofaCatalogScraper",
    "MofaVolume",
    "discover_target_volumes",
    "parse_volume_items",
]
