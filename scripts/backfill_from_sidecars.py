"""从既有 Sidecar JSON / Hoover pending 文本回填 HRS SQLite。

用法：
    python scripts/backfill_from_sidecars.py
    python scripts/backfill_from_sidecars.py --dry-run
    python scripts/backfill_from_sidecars.py --limit 20

设计目标：
- 不修改任何既有 PDF/JSON 文件；
- 可重复运行（documents/files/hoover_pending 全部 upsert）；
- 用 sidecar 的逻辑 ID 建立 documents 主记录，用磁盘状态补齐 files 表；
- 将 Hoover_Pending_Tasks.txt 迁移到 hoover_pending 表，后续可替代文本去重。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

# 允许从仓库根目录直接执行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.db_service import DbService  # noqa: E402


@dataclass
class BackfillStats:
    sidecars_seen: int = 0
    sidecars_imported: int = 0
    sidecars_failed: int = 0
    pdf_files_seen: int = 0
    resume_dirs_seen: int = 0
    hoover_lines_seen: int = 0
    hoover_imported: int = 0


def _relpath(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except Exception:
        return path


def _sha1_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value)
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else None


def _detect_source(metadata: dict[str, Any], sidecar_path: str) -> str:
    url = str(metadata.get("Source_URL") or metadata.get("viewer_url") or metadata.get("url") or "")
    repo = str(metadata.get("Repo_Name") or metadata.get("所蔵館") or "")
    blob = f"{url} {repo} {sidecar_path}"
    if "hojishinbun.hoover.org" in blob or "Hoover" in blob or "胡佛" in blob:
        return "hoover"
    if "toyobunko" in blob or "tbopac" in blob or "東洋文庫" in blob:
        return "toyo"
    return "jacar"


def _native_id_for(metadata: dict[str, Any], source: str, sidecar_path: str) -> str:
    if source == "jacar":
        value = metadata.get("Ref_Code") or metadata.get("レファレンスコード")
        if value:
            return str(value).strip()
    if source == "toyo":
        value = (
            metadata.get("Bibliography ID")
            or metadata.get("bibId")
            or metadata.get("bbid")
            or metadata.get("Document_ID")
        )
        if value:
            return str(value).strip()
    if source == "hoover":
        url = metadata.get("Source_URL") or metadata.get("viewer_url") or metadata.get("url")
        if url:
            return _sha1_text(str(url))[:16]
    # 最后兜底：路径哈希，保证可入库但后续可人工修正
    return f"path_{_sha1_text(sidecar_path)[:16]}"


def _document_id(source: str, native_id: str) -> str:
    return f"{source}:{native_id}"


def _title_for(metadata: dict[str, Any], sidecar_path: str) -> str:
    return str(metadata.get("Title") or metadata.get("title") or os.path.splitext(os.path.basename(sidecar_path))[0])


def _upsert_document(
    conn,
    *,
    document_id: str,
    source: str,
    native_id: str,
    metadata: dict[str, Any],
    keyword: str | None,
    status: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO documents(
            document_id, source, native_id, title, repo_name, level2_name, parent_name,
            scale, viewer_url, search_keyword, metadata_json, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            title=excluded.title,
            repo_name=excluded.repo_name,
            level2_name=excluded.level2_name,
            parent_name=excluded.parent_name,
            scale=excluded.scale,
            viewer_url=COALESCE(excluded.viewer_url, documents.viewer_url),
            search_keyword=COALESCE(excluded.search_keyword, documents.search_keyword),
            metadata_json=excluded.metadata_json,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            document_id,
            source,
            native_id,
            _title_for(metadata, document_id),
            metadata.get("Repo_Name"),
            metadata.get("Level2_Name"),
            metadata.get("Parent_Name"),
            _safe_int(metadata.get("規模") or metadata.get("Scale") or metadata.get("scale")),
            metadata.get("Source_URL") or metadata.get("viewer_url") or metadata.get("url"),
            keyword,
            json.dumps(metadata, ensure_ascii=False),
            status,
            now,
            now,
        ),
    )


def _upsert_file(conn, *, document_id: str, kind: str, path: str, project_root: str, now: str) -> None:
    if not os.path.exists(path):
        return
    stat = os.stat(path)
    conn.execute(
        """
        INSERT INTO files(document_id, kind, path, size, mtime, sha256, verified_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(document_id, kind) DO UPDATE SET
            path=excluded.path,
            size=excluded.size,
            mtime=excluded.mtime,
            verified_at=excluded.verified_at
        """,
        (
            document_id,
            kind,
            _relpath(path, project_root),
            stat.st_size,
            int(stat.st_mtime),
            now,
        ),
    )


def _status_for(source: str, pdf_path: str, sidecar_path: str, resume_dir: str) -> str:
    if source == "hoover":
        return "pending_hoover"
    if os.path.exists(pdf_path) and os.path.exists(sidecar_path):
        return "downloaded"
    if os.path.isdir(resume_dir):
        return "downloading"
    return "discovered"


def _keyword_from_sidecar(sidecar_path: str, downloads_root: str) -> str | None:
    rel = _relpath(sidecar_path, downloads_root)
    parts = rel.split(os.sep)
    return parts[0] if len(parts) >= 2 else None


def iter_sidecars(downloads_root: str):
    for root, _, files in os.walk(downloads_root):
        for name in files:
            if not name.endswith(".json"):
                continue
            yield os.path.join(root, name)


def backfill_sidecars(db: DbService, downloads_root: str, project_root: str, *, dry_run: bool, limit: int | None) -> BackfillStats:
    stats = BackfillStats()
    now = db.utc_now_iso()

    with db.transaction() as conn:
        for sidecar_path in iter_sidecars(downloads_root):
            if limit is not None and stats.sidecars_seen >= limit:
                break
            stats.sidecars_seen += 1
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                if not isinstance(metadata, dict):
                    raise ValueError("sidecar root is not an object")

                pdf_path = os.path.splitext(sidecar_path)[0] + ".pdf"
                resume_dir = os.path.splitext(pdf_path)[0] + ".iiif_resume"
                source = _detect_source(metadata, sidecar_path)
                native_id = _native_id_for(metadata, source, sidecar_path)
                document_id = _document_id(source, native_id)
                keyword = _keyword_from_sidecar(sidecar_path, downloads_root)
                status = _status_for(source, pdf_path, sidecar_path, resume_dir)

                if dry_run:
                    stats.sidecars_imported += 1
                    if os.path.exists(pdf_path):
                        stats.pdf_files_seen += 1
                    if os.path.isdir(resume_dir):
                        stats.resume_dirs_seen += 1
                    continue

                _upsert_document(
                    conn,
                    document_id=document_id,
                    source=source,
                    native_id=native_id,
                    metadata=metadata,
                    keyword=keyword,
                    status=status,
                    now=now,
                )
                _upsert_file(conn, document_id=document_id, kind="sidecar", path=sidecar_path, project_root=project_root, now=now)
                if os.path.exists(pdf_path):
                    stats.pdf_files_seen += 1
                    _upsert_file(conn, document_id=document_id, kind="pdf", path=pdf_path, project_root=project_root, now=now)
                if os.path.isdir(resume_dir):
                    stats.resume_dirs_seen += 1
                    _upsert_file(conn, document_id=document_id, kind="iiif_resume", path=resume_dir, project_root=project_root, now=now)
                if source == "hoover":
                    viewer_url = metadata.get("Source_URL") or metadata.get("viewer_url") or metadata.get("url")
                    if viewer_url:
                        conn.execute(
                            """
                            INSERT INTO hoover_pending(document_id, viewer_url, last_seen_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT(document_id) DO UPDATE SET
                                viewer_url=excluded.viewer_url,
                                last_seen_at=excluded.last_seen_at
                            """,
                            (document_id, str(viewer_url), now),
                        )

                stats.sidecars_imported += 1
            except Exception as e:
                stats.sidecars_failed += 1
                print(f"⚠️  Sidecar 回填失败: {sidecar_path} | {e}")

    return stats


def iter_hoover_pending_files(downloads_root: str):
    for root, _, files in os.walk(downloads_root):
        if "Hoover_Pending_Tasks.txt" in files:
            yield os.path.join(root, "Hoover_Pending_Tasks.txt")


def backfill_hoover_pending(db: DbService, downloads_root: str, *, dry_run: bool) -> BackfillStats:
    stats = BackfillStats()
    now = db.utc_now_iso()

    with db.transaction() as conn:
        for pending_path in iter_hoover_pending_files(downloads_root):
            keyword = os.path.basename(os.path.dirname(pending_path))
            try:
                with open(pending_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = list(f)
            except Exception as e:
                print(f"⚠️  Hoover pending 读取失败，已跳过: {pending_path} | {e}")
                continue

            for line in lines:
                raw = line.strip()
                if not raw:
                    continue
                stats.hoover_lines_seen += 1
                if "|" in raw:
                    title, url = [x.strip() for x in raw.split("|", 1)]
                else:
                    title, url = raw, raw
                if not url:
                    continue
                native_id = _sha1_text(url)[:16]
                document_id = _document_id("hoover", native_id)
                metadata = {
                    "Title": title or "Hoover Pending Task",
                    "Source_URL": url,
                    "Download_Status": "pending_hoover_unavailable",
                    "Download_Note": "Imported from Hoover_Pending_Tasks.txt",
                }
                if dry_run:
                    stats.hoover_imported += 1
                    continue
                _upsert_document(
                    conn,
                    document_id=document_id,
                    source="hoover",
                    native_id=native_id,
                    metadata=metadata,
                    keyword=keyword,
                    status="pending_hoover",
                    now=now,
                )
                conn.execute(
                    """
                    INSERT INTO hoover_pending(document_id, viewer_url, last_seen_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        viewer_url=excluded.viewer_url,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (document_id, url, now),
                )
                stats.hoover_imported += 1

    return stats


def _merge_stats(a: BackfillStats, b: BackfillStats) -> BackfillStats:
    return BackfillStats(
        sidecars_seen=a.sidecars_seen + b.sidecars_seen,
        sidecars_imported=a.sidecars_imported + b.sidecars_imported,
        sidecars_failed=a.sidecars_failed + b.sidecars_failed,
        pdf_files_seen=a.pdf_files_seen + b.pdf_files_seen,
        resume_dirs_seen=a.resume_dirs_seen + b.resume_dirs_seen,
        hoover_lines_seen=a.hoover_lines_seen + b.hoover_lines_seen,
        hoover_imported=a.hoover_imported + b.hoover_imported,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="从 JACAR_Downloads 回填 HRS SQLite")
    parser.add_argument("--downloads-root", default=os.path.join(_PROJECT_ROOT, "JACAR_Downloads"))
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只扫描和统计，不写入数据库")
    parser.add_argument("--limit", type=int, default=None, help="限制 sidecar 扫描数量，便于烟雾测试")
    args = parser.parse_args()

    if not os.path.isdir(args.downloads_root):
        print(f"❌ 下载目录不存在: {args.downloads_root}")
        return 1

    db = DbService(db_path=args.db_path)
    sidecar_stats = backfill_sidecars(
        db,
        args.downloads_root,
        _PROJECT_ROOT,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    hoover_stats = backfill_hoover_pending(db, args.downloads_root, dry_run=args.dry_run)
    stats = _merge_stats(sidecar_stats, hoover_stats)

    print("====== HRS SQLite 回填完成 ======")
    print(f"模式: {'dry-run（未写入）' if args.dry_run else '写入数据库'}")
    print(f"Sidecar 扫描: {stats.sidecars_seen}")
    print(f"Sidecar 成功: {stats.sidecars_imported}")
    print(f"Sidecar 失败: {stats.sidecars_failed}")
    print(f"PDF 文件记录: {stats.pdf_files_seen}")
    print(f"IIIF 断点目录记录: {stats.resume_dirs_seen}")
    print(f"Hoover pending 行: {stats.hoover_lines_seen}")
    print(f"Hoover pending 入库: {stats.hoover_imported}")
    if not args.dry_run:
        doc_count = db.fetchone("SELECT COUNT(*) AS n FROM documents")
        file_count = db.fetchone("SELECT COUNT(*) AS n FROM files")
        hoover_count = db.fetchone("SELECT COUNT(*) AS n FROM hoover_pending")
        print(f"documents 总数: {doc_count['n'] if doc_count else 0}")
        print(f"files 总数: {file_count['n'] if file_count else 0}")
        print(f"hoover_pending 总数: {hoover_count['n'] if hoover_count else 0}")
        print(f"integrity_check: {db.integrity_check()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
