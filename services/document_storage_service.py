from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from config.settings import (
    BUNDLE_REL_ANALYSIS,
    BUNDLE_REL_ANALYSIS_CONTEXT,
    BUNDLE_REL_EXPORT_COMPARISON_DOCX,
    BUNDLE_REL_EXPORT_OCR_DOCX,
    BUNDLE_REL_EXPORT_TRANSLATION_DOCX,
    BUNDLE_REL_MANIFEST,
    BUNDLE_REL_OCR,
    BUNDLE_REL_SCRATCH_DIR,
    BUNDLE_REL_SIDECAR,
    BUNDLE_REL_STRUCTURED_DIR,
    BUNDLE_REL_SUMMARY,
    BUNDLE_REL_TRANSLATION,
    DOCUMENT_BUNDLE_ROOT,
    DOCUMENT_STORAGE_LAYOUT,
    LEGACY_ANALYSIS_CACHE_DIR,
    LEGACY_OCR_CACHE_DIR,
    LEGACY_TRANSLATION_CACHE_DIR,
)
from services.cache_service import CacheService
from services.document_source_service import DocumentIdentity, identity_from_metadata
from utils.jacar_filename import extract_jacar_ref_from_path
from utils.jacar_sidecar import sidecar_path_for_pdf

LayoutMode = Literal["legacy", "bundle_v1"]
PagedArtifactKind = Literal["ocr", "analysis", "translation"]
ArtifactKind = Literal[
    "pdf",
    "sidecar",
    "manifest",
    "ocr",
    "analysis",
    "translation",
    "analysis_context",
    "summary",
    "export_ocr_docx",
    "export_translation_docx",
    "export_comparison_docx",
    "structured_dir",
    "structured_page",
    "scratch_dir",
    "iiif_resume_dir",
]


@dataclass(frozen=True)
class DocumentBundle:
    root_dir: str
    identity: DocumentIdentity
    layout: LayoutMode
    pdf_path: str


class DocumentStorageService:
    """Single entry point for document artifact paths.

    Phase 0/1 keeps ``legacy`` as the real storage backend while exposing the
    same API that the later per-document bundle layout will use.
    """

    _LEGACY_CACHE_DIRS: dict[PagedArtifactKind, str] = {
        "ocr": str(LEGACY_OCR_CACHE_DIR),
        "analysis": str(LEGACY_ANALYSIS_CACHE_DIR),
        "translation": str(LEGACY_TRANSLATION_CACHE_DIR),
    }

    _BUNDLE_REL_PATHS: dict[ArtifactKind, str] = {
        "sidecar": BUNDLE_REL_SIDECAR,
        "manifest": BUNDLE_REL_MANIFEST,
        "ocr": BUNDLE_REL_OCR,
        "analysis": BUNDLE_REL_ANALYSIS,
        "translation": BUNDLE_REL_TRANSLATION,
        "analysis_context": BUNDLE_REL_ANALYSIS_CONTEXT,
        "summary": BUNDLE_REL_SUMMARY,
        "export_ocr_docx": BUNDLE_REL_EXPORT_OCR_DOCX,
        "export_translation_docx": BUNDLE_REL_EXPORT_TRANSLATION_DOCX,
        "export_comparison_docx": BUNDLE_REL_EXPORT_COMPARISON_DOCX,
        "structured_dir": BUNDLE_REL_STRUCTURED_DIR,
        "scratch_dir": BUNDLE_REL_SCRATCH_DIR,
    }

    def __init__(
        self,
        *,
        project_root: str | None = None,
        layout: LayoutMode | None = None,
        cache_service: CacheService | None = None,
    ) -> None:
        self.project_root = os.path.abspath(
            project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.layout: LayoutMode = layout or self._configured_layout()
        self.cache_service = cache_service or CacheService()
        configured_root = os.path.abspath(str(DOCUMENT_BUNDLE_ROOT))
        default_root = os.path.abspath(str(DOCUMENT_BUNDLE_ROOT))
        self.bundle_root = (
            os.path.join(self.project_root, "Historical_Documents")
            if configured_root == default_root
            else configured_root
        )

    @staticmethod
    def _configured_layout() -> LayoutMode:
        return "bundle_v1" if str(DOCUMENT_STORAGE_LAYOUT).strip() == "bundle_v1" else "legacy"

    def is_bundle_layout(self) -> bool:
        return self.layout == "bundle_v1"

    def resolve_bundle_from_pdf(self, pdf_path: str) -> DocumentBundle:
        """Return a best-effort bundle object for UI actions and future writes."""
        abs_pdf = os.path.abspath(pdf_path)
        search_keyword = self._search_keyword_from_path(abs_pdf)
        identity = self._identity_from_bundle_files(abs_pdf, search_keyword=search_keyword)
        if identity is None:
            ref = (extract_jacar_ref_from_path(abs_pdf) or "").upper()
            identity = DocumentIdentity.build(
                source="jacar",
                native_id=ref,
                search_keyword=search_keyword,
            )

        parent_dir = os.path.dirname(abs_pdf)
        parent_name = os.path.basename(parent_dir).upper()
        if self.is_bundle_layout() and identity.native_id and parent_name == identity.native_id.upper():
            root_dir = parent_dir
        elif self.is_bundle_layout() and identity.native_id:
            root_dir = self.planned_bundle_dir(identity)
        else:
            root_dir = parent_dir
        return DocumentBundle(
            root_dir=os.path.abspath(root_dir),
            identity=identity,
            layout=self.layout,
            pdf_path=abs_pdf,
        )

    def build_identity(
        self,
        *,
        source: str,
        native_id: str,
        search_keyword: str,
        collection: str = "",
        citation_text: str = "",
    ) -> DocumentIdentity:
        return DocumentIdentity.build(
            source=source,
            native_id=native_id,
            search_keyword=search_keyword,
            collection=collection,
            citation_text=citation_text,
        )

    @staticmethod
    def _safe_path_part(value: str) -> str:
        clean = str(value or "").strip().replace("/", "_").replace("\\", "_")
        return clean or "unknown"

    def planned_bundle_dir(
        self,
        identity: DocumentIdentity,
        *,
        hierarchy: tuple[str, ...] = (),
    ) -> str:
        ref = identity.native_id or "unknown"
        source = self._safe_path_part(identity.source or "unknown")
        parts = [self.bundle_root, source]
        parts.extend(self._safe_path_part(part) for part in hierarchy if str(part or "").strip())
        parts.append(self._safe_path_part(ref))
        return os.path.abspath(
            os.path.join(*parts)
        )

    def ensure_bundle_dir(
        self,
        identity: DocumentIdentity,
        *,
        hierarchy: tuple[str, ...] = (),
    ) -> DocumentBundle:
        root_dir = self.planned_bundle_dir(identity, hierarchy=hierarchy)
        os.makedirs(root_dir, exist_ok=True)
        return DocumentBundle(
            root_dir=root_dir,
            identity=identity,
            layout=self.layout,
            pdf_path=self._find_bundle_pdf_path(root_dir) or os.path.join(root_dir, "document.pdf"),
        )

    def planned_pdf_path(
        self,
        *,
        source: str,
        native_id: str,
        search_keyword: str,
        legacy_fallback_path: str,
    ) -> str:
        if not self.is_bundle_layout() or not native_id or native_id == "Unknown_Ref":
            return legacy_fallback_path
        identity = self.build_identity(
            source=source,
            native_id=native_id,
            search_keyword=search_keyword,
        )
        filename = os.path.basename(legacy_fallback_path) or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = "document.pdf"
        return os.path.abspath(os.path.join(self.planned_bundle_dir(identity), filename))

    def artifact_path(
        self,
        bundle: DocumentBundle,
        kind: ArtifactKind,
        *,
        page_index: int | None = None,
        create_parent_dirs: bool = False,
    ) -> str:
        """Resolve an artifact path in the active layout."""
        if bundle.layout == "legacy":
            path = self._legacy_artifact_path(bundle.pdf_path, kind, page_index=page_index)
        elif kind == "pdf":
            path = bundle.pdf_path
        else:
            path = self._bundle_artifact_path(bundle.root_dir, kind, page_index=page_index)
        if create_parent_dirs:
            parent = path if os.path.splitext(path)[1] == "" else os.path.dirname(path)
            os.makedirs(parent, exist_ok=True)
        return path

    def legacy_cache_path(self, pdf_path: str, kind: PagedArtifactKind) -> str:
        return self.cache_service.build_cache_path(
            pdf_path,
            self._legacy_cache_dir(kind),
        )

    def resolve_read_path_with_fallback(
        self,
        bundle: DocumentBundle,
        kind: ArtifactKind,
        *,
        page_index: int | None = None,
    ) -> tuple[str, LayoutMode]:
        """Prefer bundle files when enabled, otherwise return the legacy path."""
        if bundle.layout == "bundle_v1":
            bundle_path = self._bundle_artifact_path(bundle.root_dir, kind, page_index=page_index)
            if os.path.exists(bundle_path):
                return bundle_path, "bundle_v1"
            if kind in {"ocr", "analysis", "translation"}:
                legacy_path = self._legacy_artifact_path(bundle.pdf_path, kind, page_index=page_index)
                if os.path.exists(legacy_path):
                    return legacy_path, "legacy"
            return bundle_path, "bundle_v1"
        return self.artifact_path(bundle, kind, page_index=page_index), "legacy"

    def resolve_write_path(
        self,
        bundle: DocumentBundle,
        kind: ArtifactKind,
        *,
        page_index: int | None = None,
        create_parent_dirs: bool = True,
    ) -> str:
        return self.artifact_path(
            bundle,
            kind,
            page_index=page_index,
            create_parent_dirs=create_parent_dirs,
        )

    def write_manifest(self, bundle: DocumentBundle, *, artifacts: dict[str, str] | None = None) -> str:
        """Write a small bundle manifest. Legacy mode intentionally does nothing."""
        if bundle.layout != "bundle_v1":
            return ""
        manifest_path = self.artifact_path(bundle, "manifest", create_parent_dirs=True)
        payload = {
            "schema_version": 2,
            "layout": "bundle_v1",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "identity": {
                "source": bundle.identity.source,
                "native_id": bundle.identity.native_id,
                "search_keyword": bundle.identity.search_keyword,
                "document_id": bundle.identity.document_id,
                "collection": bundle.identity.collection,
                "citation_text": bundle.identity.citation_text,
            },
            "pdf": {
                "path": os.path.relpath(bundle.pdf_path, bundle.root_dir),
                "exists": os.path.isfile(bundle.pdf_path),
                "size": os.path.getsize(bundle.pdf_path) if os.path.isfile(bundle.pdf_path) else 0,
                "mtime_ns": os.stat(bundle.pdf_path).st_mtime_ns if os.path.isfile(bundle.pdf_path) else 0,
            },
            "artifacts": artifacts or self._existing_bundle_artifacts(bundle),
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return manifest_path

    def touch_manifest_artifact(self, bundle: DocumentBundle) -> str:
        return self.write_manifest(bundle)

    def _legacy_artifact_path(
        self,
        pdf_path: str,
        kind: ArtifactKind,
        *,
        page_index: int | None = None,
    ) -> str:
        if kind in {"ocr", "analysis", "translation"}:
            return self.legacy_cache_path(pdf_path, kind)  # type: ignore[arg-type]
        if kind == "analysis_context":
            return self.cache_service.build_context_sidecar_path(
                self.legacy_cache_path(pdf_path, "analysis")
            )
        if kind == "pdf":
            return os.path.abspath(pdf_path)
        if kind == "sidecar":
            return sidecar_path_for_pdf(pdf_path)
        if kind == "iiif_resume_dir":
            return os.path.splitext(os.path.abspath(pdf_path))[0] + ".iiif_resume"
        if kind == "structured_page":
            ref = extract_jacar_ref_from_path(pdf_path) or "UNKNOWN"
            if page_index is None:
                raise ValueError("page_index is required for structured_page")
            return os.path.join(
                self.project_root,
                "Database_JSON",
                f"JACAR_{ref}_p{page_index + 1:04d}.json",
            )
        return self._bundle_artifact_path(os.path.dirname(os.path.abspath(pdf_path)), kind, page_index=page_index)

    def _bundle_artifact_path(
        self,
        root_dir: str,
        kind: ArtifactKind,
        *,
        page_index: int | None = None,
    ) -> str:
        if kind == "pdf":
            return self._find_bundle_pdf_path(root_dir) or os.path.join(root_dir, "document.pdf")
        if kind == "export_ocr_docx":
            return self._bundle_docx_export_path(root_dir, translation=False)
        if kind == "export_translation_docx":
            return self._bundle_docx_export_path(root_dir, translation=True)
        if kind == "structured_page":
            if page_index is None:
                raise ValueError("page_index is required for structured_page")
            rel_path = os.path.join(BUNDLE_REL_STRUCTURED_DIR, f"p{page_index + 1:04d}.json")
        elif kind == "iiif_resume_dir":
            rel_path = os.path.join(BUNDLE_REL_SCRATCH_DIR, "iiif_resume")
        else:
            rel_path = self._BUNDLE_REL_PATHS[kind]
        return os.path.abspath(os.path.join(root_dir, rel_path))

    def _legacy_cache_dir(self, kind: PagedArtifactKind) -> str:
        cache_dir = self._LEGACY_CACHE_DIRS[kind]
        if not os.path.isabs(cache_dir):
            return os.path.join(self.project_root, cache_dir)
        return cache_dir

    def _existing_bundle_artifacts(self, bundle: DocumentBundle) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for kind in (
            "pdf",
            "sidecar",
            "ocr",
            "analysis",
            "translation",
            "analysis_context",
            "summary",
            "export_ocr_docx",
            "export_translation_docx",
            "export_comparison_docx",
        ):
            path = self.artifact_path(bundle, kind)  # type: ignore[arg-type]
            if os.path.exists(path):
                artifacts[kind] = os.path.relpath(path, bundle.root_dir)
        structured_dir = self.artifact_path(bundle, "structured_dir")
        if os.path.isdir(structured_dir):
            artifacts["structured_dir"] = os.path.relpath(structured_dir, bundle.root_dir)
        return artifacts

    def _search_keyword_from_path(self, pdf_path: str) -> str:
        downloads_root = os.path.join(self.project_root, "JACAR_Downloads")
        try:
            rel = os.path.relpath(pdf_path, downloads_root)
        except ValueError:
            return "未分类"
        parts = rel.split(os.sep)
        if len(parts) > 1 and parts[0] not in {"", ".", ".."}:
            return parts[0]
        return "未分类"

    def _identity_from_bundle_files(
        self,
        pdf_path: str,
        *,
        search_keyword: str,
    ) -> DocumentIdentity | None:
        root_dir = os.path.dirname(os.path.abspath(pdf_path))
        candidates = (
            os.path.join(root_dir, BUNDLE_REL_MANIFEST),
            os.path.join(root_dir, BUNDLE_REL_SIDECAR),
            sidecar_path_for_pdf(pdf_path),
        )
        seen: set[str] = set()
        for candidate in candidates:
            candidate = os.path.abspath(candidate)
            if candidate in seen or not os.path.isfile(candidate):
                continue
            seen.add(candidate)
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError, TypeError):
                continue
            identity = identity_from_metadata(
                payload,
                default_search_keyword=search_keyword,
            )
            if identity is not None:
                return identity
        return None

    @staticmethod
    def _find_bundle_pdf_path(root_dir: str) -> str | None:
        if not os.path.isdir(root_dir):
            return None
        pdfs = [
            os.path.join(root_dir, name)
            for name in os.listdir(root_dir)
            if name.lower().endswith(".pdf") and os.path.isfile(os.path.join(root_dir, name))
        ]
        if not pdfs:
            return None
        pdfs.sort(key=lambda p: (os.path.basename(p) == "document.pdf", os.path.basename(p)))
        return os.path.abspath(pdfs[0])

    def _bundle_docx_export_path(self, root_dir: str, *, translation: bool) -> str:
        pdf_path = self._find_bundle_pdf_path(root_dir)
        if pdf_path:
            basename = os.path.splitext(os.path.basename(pdf_path))[0]
        else:
            basename = os.path.basename(os.path.abspath(root_dir)) or "document"
        suffix = "_译文.docx" if translation else ".docx"
        return os.path.abspath(os.path.join(root_dir, "export", f"{basename}{suffix}"))
