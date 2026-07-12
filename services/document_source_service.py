"""Archive-neutral document identity, metadata, and citation helpers.

This module deliberately has no database, GUI, or network dependencies.  It
defines the small contract shared by JACAR today and MOFA in later phases.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


_SOURCE_RE = re.compile(r"[^a-z0-9_-]+")
_ID_PART_RE = re.compile(r"[^A-Z0-9_-]+")


def normalize_source(source: str | None, *, default: str = "jacar") -> str:
    value = (source or default).strip().lower()
    value = _SOURCE_RE.sub("_", value).strip("_")
    return value or default


def normalize_native_id(native_id: str | None) -> str:
    return (native_id or "").strip().upper()


@dataclass(frozen=True)
class DocumentIdentity:
    source: str
    native_id: str
    search_keyword: str
    document_id: str
    collection: str = ""
    citation_text: str = ""

    @classmethod
    def build(
        cls,
        *,
        source: str,
        native_id: str,
        search_keyword: str = "未分类",
        collection: str = "",
        citation_text: str = "",
    ) -> "DocumentIdentity":
        source_clean = normalize_source(source)
        native_id_clean = normalize_native_id(native_id)
        keyword_clean = (search_keyword or "未分类").strip() or "未分类"
        return cls(
            source=source_clean,
            native_id=native_id_clean,
            search_keyword=keyword_clean,
            document_id=(f"{source_clean}:{native_id_clean}" if native_id_clean else ""),
            collection=(collection or "").strip(),
            citation_text=(citation_text or "").strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class MofaCitationMetadata:
    document_title: str
    volume_label: str
    publication_year: int | str
    printed_page_from: int | None = None
    printed_page_to: int | None = None
    pdf_page_from: int | None = None
    pdf_page_to: int | None = None
    editor: str = "日本外務省"
    publisher: str = "日本外務省"
    collection: str = "日本外交文書"


def _id_part(value: str | int) -> str:
    cleaned = _ID_PART_RE.sub("_", str(value).strip().upper()).strip("_")
    if not cleaned:
        raise ValueError("MOFA native-id component cannot be empty")
    return cleaned


def build_mofa_native_id(
    *,
    era_code: str,
    era_year: int,
    volume_code: str,
    printed_page_from: int | None = None,
    item_index: int | None = None,
    fallback_key: str = "",
) -> str:
    """Build a stable, path-safe MOFA identifier.

    Preferred form: ``MOFA_T10_2_00526``.  When the printed start page is not
    known yet, a deterministic URL/catalog-key digest is used instead.
    """
    if int(era_year) <= 0:
        raise ValueError("era_year must be positive")
    prefix = f"MOFA_{_id_part(era_code)}{int(era_year)}_{_id_part(volume_code)}"
    if printed_page_from is not None:
        if int(printed_page_from) <= 0:
            raise ValueError("printed_page_from must be positive")
        suffix = f"{int(printed_page_from):05d}"
    else:
        key = (fallback_key or "").strip()
        if not key:
            raise ValueError("fallback_key is required when printed_page_from is unknown")
        suffix = "U" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()
    if item_index is not None:
        if int(item_index) <= 0:
            raise ValueError("item_index must be positive")
        suffix += f"_{int(item_index):02d}"
    return f"{prefix}_{suffix}"


def _page_range_text(metadata: MofaCitationMetadata) -> str:
    start = metadata.printed_page_from
    end = metadata.printed_page_to
    if start is not None:
        end = end if end is not None else start
        return f"第{start}頁" if end == start else f"第{start}—{end}頁"

    pdf_start = metadata.pdf_page_from
    pdf_end = metadata.pdf_page_to
    if pdf_start is not None:
        pdf_end = pdf_end if pdf_end is not None else pdf_start
        return f"PDF第{pdf_start}頁" if pdf_end == pdf_start else f"PDF第{pdf_start}—{pdf_end}頁"
    return ""


def build_mofa_citation(metadata: MofaCitationMetadata) -> str:
    """Render the HRS canonical MOFA citation agreed for translated drafts."""
    title = (metadata.document_title or "").strip()
    volume = (metadata.volume_label or "").strip()
    if not title:
        raise ValueError("document_title is required")
    if not volume:
        raise ValueError("volume_label is required")
    if not str(metadata.publication_year).strip():
        raise ValueError("publication_year is required")

    pages = _page_range_text(metadata)
    page_part = f"（{pages}）" if pages else ""
    return (
        f"日本外交文書：「{title}」{page_part}、"
        f"『{metadata.collection}』{volume}（{metadata.editor}編、"
        f"{metadata.publisher}発行、{metadata.publication_year}年）"
    )


def identity_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    default_search_keyword: str = "未分类",
) -> DocumentIdentity | None:
    """Read identity from sidecar v2, manifest, or compatible flat fields."""
    if not isinstance(metadata, Mapping):
        return None
    nested = metadata.get("identity")
    identity_data = nested if isinstance(nested, Mapping) else metadata

    source = identity_data.get("source") or metadata.get("Source")
    native_id = (
        identity_data.get("native_id")
        or metadata.get("Native_ID")
        or metadata.get("native_id")
        or metadata.get("Ref_Code")
        or metadata.get("レファレンスコード")
    )
    if not native_id:
        return None
    if not source:
        source = "jacar" if (metadata.get("Ref_Code") or metadata.get("レファレンスコード")) else ""
    if not source:
        return None

    return DocumentIdentity.build(
        source=str(source),
        native_id=str(native_id),
        search_keyword=str(
            identity_data.get("search_keyword")
            or metadata.get("Search_Keyword")
            or default_search_keyword
        ),
        collection=str(identity_data.get("collection") or metadata.get("Collection") or ""),
        citation_text=str(identity_data.get("citation_text") or metadata.get("Citation_Text") or ""),
    )


def build_sidecar_v2(
    *,
    identity: DocumentIdentity,
    title: str,
    source_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the common envelope used by future MOFA and other sources."""
    return {
        "schema_version": 2,
        "identity": identity.to_dict(),
        "title": (title or "").strip(),
        "source_metadata": dict(source_metadata or {}),
    }


class DocumentSourceService:
    """Small facade for callers that prefer the existing service style."""

    normalize_source = staticmethod(normalize_source)
    normalize_native_id = staticmethod(normalize_native_id)
    build_mofa_native_id = staticmethod(build_mofa_native_id)
    build_mofa_citation = staticmethod(build_mofa_citation)
    identity_from_metadata = staticmethod(identity_from_metadata)
    build_sidecar_v2 = staticmethod(build_sidecar_v2)
