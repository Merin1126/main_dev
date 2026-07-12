from __future__ import annotations

import json
import os
import tempfile

from services.document_source_service import (
    DocumentIdentity,
    MofaCitationMetadata,
    build_mofa_citation,
    build_mofa_native_id,
    build_sidecar_v2,
    identity_from_metadata,
)
from services.document_storage_service import DocumentStorageService


def _check_mofa_native_id() -> None:
    assert build_mofa_native_id(
        era_code="T",
        era_year=10,
        volume_code="2",
        printed_page_from=526,
    ) == "MOFA_T10_2_00526"
    assert build_mofa_native_id(
        era_code="S",
        era_year=2,
        volume_code="1-1",
        fallback_key="https://www.mofa.go.jp/example.pdf",
    ) == build_mofa_native_id(
        era_code="S",
        era_year=2,
        volume_code="1-1",
        fallback_key="https://www.mofa.go.jp/example.pdf",
    )


def _check_mofa_citation() -> str:
    citation = build_mofa_citation(
        MofaCitationMetadata(
            document_title="支那政局ニ関スル件",
            volume_label="大正十年第二冊",
            publication_year=1975,
            printed_page_from=526,
            printed_page_to=529,
        )
    )
    assert citation == (
        "日本外交文書：「支那政局ニ関スル件」（第526—529頁）、"
        "『日本外交文書』大正十年第二冊"
        "（日本外務省編、日本外務省発行、1975年）"
    )
    return citation


def _check_sidecar_and_storage_resolution(citation: str) -> None:
    identity = DocumentIdentity.build(
        source="MOFA",
        native_id="mofa_t10_2_00526",
        search_keyword="中国共産党",
        collection="日本外交文書",
        citation_text=citation,
    )
    payload = build_sidecar_v2(
        identity=identity,
        title="支那政局ニ関スル件",
        source_metadata={"volume": "大正十年第二冊"},
    )
    restored = identity_from_metadata(payload)
    assert restored == identity

    with tempfile.TemporaryDirectory() as project_root:
        bundle_dir = os.path.join(
            project_root,
            "JACAR_Downloads",
            "中国共産党",
            identity.native_id,
        )
        os.makedirs(bundle_dir)
        pdf_path = os.path.join(bundle_dir, "document.pdf")
        sidecar_path = os.path.join(bundle_dir, "sidecar.json")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4\n")
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        storage = DocumentStorageService(project_root=project_root, layout="bundle_v1")
        bundle = storage.resolve_bundle_from_pdf(pdf_path)
        assert bundle.identity == identity
        assert bundle.root_dir == os.path.abspath(bundle_dir)

        manifest_path = storage.write_manifest(bundle)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest["schema_version"] == 2
        assert manifest["identity"]["source"] == "mofa"
        assert manifest["identity"]["citation_text"] == citation


def _check_legacy_jacar_compatibility() -> None:
    restored = identity_from_metadata(
        {
            "Ref_Code": "B03030289700",
            "Title": "当地反帝国主義運動状況報告ノ件",
        },
        default_search_keyword="反帝國主義",
    )
    assert restored is not None
    assert restored.source == "jacar"
    assert restored.native_id == "B03030289700"
    assert restored.document_id == "jacar:B03030289700"


def main() -> int:
    _check_mofa_native_id()
    citation = _check_mofa_citation()
    _check_sidecar_and_storage_resolution(citation)
    _check_legacy_jacar_compatibility()
    print("Phase 1 checks passed: identity, MOFA citation, sidecar v2, storage, JACAR compatibility.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
