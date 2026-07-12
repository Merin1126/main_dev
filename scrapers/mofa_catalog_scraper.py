"""Read the MOFA Japanese Diplomatic Documents catalog (1921--1927).

The MOFA collection is a volume catalog, not a JACAR-style keyword search
endpoint.  This adapter therefore discovers target volume pages first and
then extracts their item-level PDF links.  Downloading PDFs is Phase 3 and is
intentionally outside this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests


MOFA_INDEX_URL = "https://www.mofa.go.jp/mofaj/annai/honsho/shiryo/archives/mokuji.html"
_TAISHO_TARGET_RE = re.compile(r"^t(?P<era_year>1[0-5])-(?P<volume_code>[a-z0-9-]+)\.html$", re.I)
_SHOWA_1927_PAGE = "s21-1.html"
_WHITESPACE_RE = re.compile(r"\s+")
_PDF_SUFFIX_RE = re.compile(r"\s*[\(（]PDF[\)）]\s*$", re.I)


@dataclass(frozen=True)
class MofaVolume:
    era_code: str
    era_year: int
    gregorian_year: int
    volume_code: str
    volume_label: str
    catalog_url: str


@dataclass(frozen=True)
class MofaCatalogItem:
    volume: MofaVolume
    title: str
    pdf_url: str
    item_kind: str = "content"


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        values = dict(attrs)
        href = str(values.get("href") or "").strip()
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = _clean_text("".join(self._text))
        self.links.append((self._href, text))
        self._href = None
        self._text = []


def _clean_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value or "").strip()


def _page_basename(url_or_path: str) -> str:
    return urlparse(url_or_path).path.rsplit("/", 1)[-1].lower()


def _volume_from_link(href: str, label: str, *, index_url: str) -> MofaVolume | None:
    basename = _page_basename(href)
    match = _TAISHO_TARGET_RE.match(basename)
    if match:
        era_year = int(match.group("era_year"))
        volume_code = match.group("volume_code").upper()
        link_label = _clean_text(label) or f"第{volume_code}冊"
        return MofaVolume(
            era_code="T",
            era_year=era_year,
            gregorian_year=1911 + era_year,
            volume_code=volume_code,
            volume_label=f"大正{era_year}年（{1911 + era_year}年） {link_label}",
            catalog_url=urljoin(index_url, href),
        )

    if basename == _SHOWA_1927_PAGE:
        return MofaVolume(
            era_code="S",
            era_year=2,
            gregorian_year=1927,
            volume_code="1-1",
            volume_label="昭和2年（1927年） 第1部第1巻（対中国関係）",
            catalog_url=urljoin(index_url, href),
        )
    return None


def discover_target_volumes(
    index_html: str,
    *,
    index_url: str = MOFA_INDEX_URL,
) -> list[MofaVolume]:
    """Extract the official 1921--1927 target volume pages from the index."""
    parser = _AnchorParser()
    parser.feed(index_html or "")
    volumes: list[MofaVolume] = []
    seen: set[str] = set()
    for href, label in parser.links:
        volume = _volume_from_link(href, label, index_url=index_url)
        if volume is None or volume.catalog_url in seen:
            continue
        seen.add(volume.catalog_url)
        volumes.append(volume)
    volumes.sort(key=lambda item: (item.gregorian_year, item.volume_code))
    return volumes


def _item_kind(title: str) -> str:
    normalized = title.replace(" ", "")
    if "とびら・目次" in normalized or normalized in {"とびら", "目次"}:
        return "front_matter"
    if "索引" in normalized:
        return "index"
    if "奥付" in normalized:
        return "colophon"
    return "content"


def parse_volume_items(volume_html: str, *, volume: MofaVolume) -> list[MofaCatalogItem]:
    """Extract and classify all PDF links from one MOFA volume page."""
    parser = _AnchorParser()
    parser.feed(volume_html or "")
    items: list[MofaCatalogItem] = []
    seen: set[str] = set()
    for href, label in parser.links:
        absolute_url = urljoin(volume.catalog_url, href)
        if not urlparse(absolute_url).path.lower().endswith(".pdf") or absolute_url in seen:
            continue
        title = _PDF_SUFFIX_RE.sub("", _clean_text(label)).strip()
        if not title:
            title = _page_basename(absolute_url).rsplit(".", 1)[0]
        seen.add(absolute_url)
        items.append(
            MofaCatalogItem(
                volume=volume,
                title=title,
                pdf_url=absolute_url,
                item_kind=_item_kind(title),
            )
        )
    return items


class MofaCatalogScraper:
    """Network wrapper around the deterministic catalog parsing functions."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        user_agent: str = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
        }

    def _get_html(self, url: str) -> str:
        response = self.session.get(url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return response.text

    def discover_volumes(self, *, index_url: str = MOFA_INDEX_URL) -> list[MofaVolume]:
        return discover_target_volumes(self._get_html(index_url), index_url=index_url)

    def fetch_volume_items(self, volume: MofaVolume) -> list[MofaCatalogItem]:
        return parse_volume_items(self._get_html(volume.catalog_url), volume=volume)

    def collect_items(self, volumes: Iterable[MofaVolume]) -> list[MofaCatalogItem]:
        items: list[MofaCatalogItem] = []
        for volume in volumes:
            items.extend(self.fetch_volume_items(volume))
        return items
