from __future__ import annotations

import json
import os
import tempfile

from scrapers.mofa_catalog_scraper import MofaCatalogItem, MofaVolume
from services.db_service import DbService
from services.document_storage_service import DocumentStorageService
from services.mofa_download_service import MofaDownloadService


class _FakeResponse:
    def __init__(self, payload: bytes = b"%PDF-1.4\nfixture\n", status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
        return None

    def close(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.payload


class _FakeSession:
    def __init__(self, payload: bytes = b"%PDF-1.4\nfixture\n") -> None:
        self.calls = 0
        self.pdf_calls = 0
        self.payload = payload

    def get(self, *args, **kwargs):
        self.calls += 1
        if not kwargs.get("stream"):
            return _FakeResponse(b"<html>catalog</html>")
        self.pdf_calls += 1
        return _FakeResponse(self.payload)


class _RetrySession(_FakeSession):
    def get(self, *args, **kwargs):
        self.calls += 1
        if not kwargs.get("stream"):
            return _FakeResponse(b"<html>catalog</html>")
        self.pdf_calls += 1
        if self.pdf_calls == 1:
            return _FakeResponse(b"<html>denied</html>", status_code=403)
        return _FakeResponse(self.payload)


def main() -> int:
    volume = MofaVolume(
        era_code="T",
        era_year=10,
        gregorian_year=1921,
        volume_code="2",
        volume_label="大正10年（1921年） 第2冊",
        catalog_url="https://www.mofa.go.jp/archives/t10-2.html",
    )
    item = MofaCatalogItem(
        volume=volume,
        title="支那政局ニ関スル件",
        pdf_url="https://www.mofa.go.jp/archives/pdfs/taisho10_2_01.pdf",
    )

    with tempfile.TemporaryDirectory() as root:
        db = DbService(db_path=os.path.join(root, "database", "test.sqlite3"))
        storage = DocumentStorageService(project_root=root, layout="bundle_v1")
        session = _FakeSession()
        service = MofaDownloadService(
            project_root=root,
            db_service=db,
            storage_service=storage,
            session=session,
        )
        progress: list[tuple[int, int | None]] = []
        result = service.download_item(
            item,
            search_keyword="中国共産党",
            on_progress=lambda current, total: progress.append((current, total)),
        )
        assert result.status == "downloaded"
        assert result.native_id.startswith("MOFA_T10_2_U")
        assert os.path.isfile(result.pdf_path)
        assert os.path.isfile(result.sidecar_path)
        assert progress

        with open(result.sidecar_path, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        assert sidecar["identity"]["source"] == "mofa"
        assert sidecar["source_metadata"]["citation_status"] == "pending_bibliography"
        assert sidecar["source_metadata"]["publication_year"] is None
        assert sidecar["Citation_Text"] == ""

        row = db.fetchone(
            "SELECT source, native_id, status FROM documents WHERE document_id = ?",
            (result.document_id,),
        )
        assert row is not None and dict(row)["status"] == "downloaded"
        files = db.fetchall(
            "SELECT kind FROM files WHERE document_id = ? ORDER BY kind",
            (result.document_id,),
        )
        assert [r["kind"] for r in files] == ["pdf", "sidecar"]

        second = service.download_item(item, search_keyword="中国共産党")
        assert second.status == "already_downloaded"
        assert second.native_id == result.native_id
        assert session.pdf_calls == 1

        invalid_item = MofaCatalogItem(
            volume=volume,
            title="無効応答テスト",
            pdf_url="https://www.mofa.go.jp/archives/pdfs/taisho10_2_invalid.pdf",
        )
        invalid_service = MofaDownloadService(
            project_root=root,
            db_service=db,
            storage_service=storage,
            session=_FakeSession(b"<html>blocked</html>"),
        )
        try:
            invalid_service.download_item(invalid_item, search_keyword="中国共産党")
            raise AssertionError("non-PDF response must fail")
        except ValueError as exc:
            assert "not a PDF" in str(exc)
        invalid_id = invalid_service.native_id_for_item(invalid_item)
        assert db.get_document_status("mofa", invalid_id) == "failed"
        invalid_bundle = storage.ensure_bundle_dir(
            storage.build_identity(
                source="mofa",
                native_id=invalid_id,
                search_keyword="中国共産党",
                collection="日本外交文書",
            )
        )
        assert not os.path.exists(invalid_bundle.pdf_path + ".part")

        retry_item = MofaCatalogItem(
            volume=volume,
            title="CDN 403 重试测试",
            pdf_url="https://www.mofa.go.jp/archives/pdfs/taisho10_2_retry.pdf",
        )
        retry_session = _RetrySession()
        retry_service = MofaDownloadService(
            project_root=root,
            db_service=db,
            storage_service=storage,
            session=retry_session,
        )
        retry_result = retry_service.download_item(retry_item, search_keyword="Phase5A")
        assert retry_result.status == "downloaded"
        assert retry_session.pdf_calls == 2
        db.close()

    print("Phase 3 checks passed: download, PDF validation, bundle, sidecar, DB, deduplication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
