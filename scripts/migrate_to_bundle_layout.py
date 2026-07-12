"""Migrate legacy HRS document artifacts into per-document bundle folders.

Default mode is dry-run. Use --execute to move files and update SQLite files
rows. The script is intentionally conservative: it never overwrites existing
bundle artifacts and it leaves legacy cache_index rows untouched because the
current schema keys by cache basename, which is not compatible with fixed names
like ocr.paged.json across multiple documents.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.cache_service import CacheService  # noqa: E402
from services.db_service import DbService  # noqa: E402
from services.document_storage_service import DocumentBundle, DocumentIdentity, DocumentStorageService  # noqa: E402
from services.report_service import ReportService  # noqa: E402
from utils.jacar_filename import extract_jacar_ref_from_path, parse_jacar_pdf_filename  # noqa: E402
from utils.jacar_sidecar import sidecar_path_for_pdf  # noqa: E402
from utils.reporting import safe_filename  # noqa: E402


@dataclass
class ArtifactMove:
    kind: str
    source: str
    target: str
    exists: bool
    target_exists: bool
    action: str


@dataclass
class MigrationPlan:
    ref: str
    source: str
    document_id: str
    search_keyword: str
    old_pdf: str
    bundle_dir: str
    new_pdf: str
    moves: list[ArtifactMove] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class MigrationStats:
    mode: str
    started_at: str
    scanned_pdfs: int = 0
    planned_documents: int = 0
    migrated_documents: int = 0
    skipped_documents: int = 0
    failed_documents: int = 0
    planned_moves: int = 0
    moved_files: int = 0
    skipped_existing_targets: int = 0
    missing_sources: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except Exception:
        return path


def _safe_keyword_from_pdf(pdf_path: str, downloads_root: str) -> str:
    try:
        rel = os.path.relpath(pdf_path, downloads_root)
        parts = rel.split(os.sep)
        if len(parts) > 1 and parts[0] not in {"", ".", ".."}:
            return parts[0]
    except ValueError:
        pass
    return "未分类"


def _discover_pdf_files(downloads_root: str) -> list[str]:
    pdfs: list[str] = []
    if not os.path.isdir(downloads_root):
        return pdfs
    for base, dirs, names in os.walk(downloads_root):
        dirs[:] = [d for d in dirs if d not in {"_scratch", "__pycache__"} and not d.endswith(".iiif_resume")]
        for name in names:
            if name.lower().endswith(".pdf"):
                pdfs.append(os.path.abspath(os.path.join(base, name)))
    return sorted(pdfs)


def _document_row(db: DbService, ref: str) -> dict[str, Any] | None:
    row = db.fetchone(
        """
        SELECT document_id, source, native_id, search_keyword
        FROM documents
        WHERE UPPER(native_id) = UPPER(?)
        ORDER BY CASE source WHEN 'jacar' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (ref,),
    )
    return dict(row) if row else None


def _page_number_from_database_json(path: str) -> int | None:
    match = re.search(r"_p(\d{4,})\.json$", os.path.basename(path), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _add_move(plan: MigrationPlan, *, kind: str, source: str, target: str) -> None:
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    exists = os.path.exists(source)
    target_exists = os.path.exists(target)
    if not exists:
        action = "missing"
    elif target_exists:
        action = "skip_existing_target"
    else:
        action = "move"
    plan.moves.append(
        ArtifactMove(
            kind=kind,
            source=source,
            target=target,
            exists=exists,
            target_exists=target_exists,
            action=action,
        )
    )


def build_plan(
    *,
    pdf_path: str,
    project_root: str,
    downloads_root: str,
    db: DbService,
    storage: DocumentStorageService,
    cache_service: CacheService,
    report_service: ReportService,
) -> MigrationPlan:
    ref = extract_jacar_ref_from_path(pdf_path)
    if not ref:
        return MigrationPlan(
            ref="",
            source="",
            document_id="",
            search_keyword=_safe_keyword_from_pdf(pdf_path, downloads_root),
            old_pdf=pdf_path,
            bundle_dir="",
            new_pdf="",
            skipped=True,
            skip_reason="no_ref",
        )

    row = _document_row(db, ref)
    source = str(row.get("source") if row else "jacar") or "jacar"
    search_keyword = str(row.get("search_keyword") if row else "") or _safe_keyword_from_pdf(pdf_path, downloads_root)
    document_id = str(row.get("document_id") if row else f"{source}:{ref}")
    identity = DocumentIdentity(
        source=source,
        native_id=ref.upper(),
        search_keyword=search_keyword or "未分类",
        document_id=document_id,
    )
    bundle_dir = storage.planned_bundle_dir(identity)
    new_pdf = storage.planned_pdf_path(
        source=source,
        native_id=ref,
        search_keyword=search_keyword or "未分类",
        legacy_fallback_path=pdf_path,
    )
    plan = MigrationPlan(
        ref=ref.upper(),
        source=source,
        document_id=document_id,
        search_keyword=search_keyword or "未分类",
        old_pdf=os.path.abspath(pdf_path),
        bundle_dir=bundle_dir,
        new_pdf=new_pdf,
    )

    if os.path.dirname(pdf_path) == bundle_dir:
        plan.skipped = True
        plan.skip_reason = "already_bundle"
        return plan
    if os.path.exists(new_pdf):
        plan.skipped = True
        plan.skip_reason = "target_pdf_exists"
        return plan

    _add_move(plan, kind="pdf", source=pdf_path, target=new_pdf)
    _add_move(plan, kind="sidecar", source=sidecar_path_for_pdf(pdf_path), target=os.path.join(bundle_dir, "sidecar.json"))

    for kind in ("ocr", "analysis", "translation"):
        legacy_cache = storage.legacy_cache_path(pdf_path, kind)  # type: ignore[arg-type]
        _add_move(plan, kind=kind, source=legacy_cache, target=storage._bundle_artifact_path(bundle_dir, kind))  # noqa: SLF001

    analysis_cache = storage.legacy_cache_path(pdf_path, "analysis")
    _add_move(
        plan,
        kind="analysis_context",
        source=cache_service.build_context_sidecar_path(analysis_cache),
        target=os.path.join(bundle_dir, "analysis.context.json"),
    )

    legacy_resume = os.path.splitext(pdf_path)[0] + ".iiif_resume"
    _add_move(
        plan,
        kind="iiif_resume_dir",
        source=legacy_resume,
        target=os.path.join(bundle_dir, "_scratch", "iiif_resume"),
    )

    json_dir = os.path.join(project_root, "Database_JSON")
    if os.path.isdir(json_dir):
        prefix = f"JACAR_{ref}_p"
        for name in sorted(os.listdir(json_dir)):
            if not name.lower().endswith(".json"):
                continue
            if not name.upper().startswith(prefix.upper()):
                continue
            src = os.path.join(json_dir, name)
            page_num = _page_number_from_database_json(src)
            dst_name = f"p{page_num:04d}.json" if page_num is not None else name
            _add_move(plan, kind="structured_json", source=src, target=os.path.join(bundle_dir, "structured", dst_name))

    basename = os.path.splitext(os.path.basename(pdf_path))[0]
    docx_root = os.path.join(project_root, "Docx_Output", safe_filename(search_keyword or "未分类"))
    _add_move(
        plan,
        kind="export_ocr_docx",
        source=os.path.join(docx_root, safe_filename(basename) + ".docx"),
        target=os.path.join(bundle_dir, "export", safe_filename(os.path.splitext(os.path.basename(new_pdf))[0]) + ".docx"),
    )
    _add_move(
        plan,
        kind="export_translation_docx",
        source=os.path.join(docx_root, safe_filename(basename) + "_译文.docx"),
        target=os.path.join(
            bundle_dir,
            "export",
            safe_filename(os.path.splitext(os.path.basename(new_pdf))[0]) + "_译文.docx",
        ),
    )

    _add_move(
        plan,
        kind="export_comparison_docx",
        source=report_service.expected_comparison_docx_path(pdf_path, report_service.default_comparison_dir()),
        target=os.path.join(bundle_dir, "export", "comparison.docx"),
    )
    _add_move(
        plan,
        kind="summary",
        source=report_service.expected_summary_md_path(pdf_path, report_service.default_summary_dir()),
        target=os.path.join(bundle_dir, "summary.md"),
    )

    return plan


def _move_artifact(move: ArtifactMove) -> str:
    if move.action != "move":
        return move.action
    os.makedirs(os.path.dirname(move.target), exist_ok=True)
    if os.path.isdir(move.source):
        shutil.move(move.source, move.target)
    else:
        shutil.move(move.source, move.target)
    return "moved"


def _write_manifest(plan: MigrationPlan, storage: DocumentStorageService) -> str:
    identity = DocumentIdentity(
        source=plan.source,
        native_id=plan.ref,
        search_keyword=plan.search_keyword,
        document_id=plan.document_id,
    )
    bundle = DocumentBundle(
        root_dir=plan.bundle_dir,
        layout="bundle_v1",
        pdf_path=plan.new_pdf,
        identity=identity,
    )
    return storage.write_manifest(bundle)


def _update_sqlite_files(db: DbService, plan: MigrationPlan) -> None:
    now = db.utc_now_iso()
    with db.transaction() as conn:
        for kind, path in (("pdf", plan.new_pdf), ("sidecar", os.path.join(plan.bundle_dir, "sidecar.json"))):
            if not os.path.exists(path):
                continue
            st = os.stat(path)
            conn.execute(
                """
                INSERT INTO files(document_id, kind, path, size, mtime, sha256, verified_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(document_id, kind) DO UPDATE SET
                    path = excluded.path,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    verified_at = excluded.verified_at
                """,
                (plan.document_id, kind, path, int(st.st_size), int(st.st_mtime), now),
            )


def execute_plan(plan: MigrationPlan, *, db: DbService, storage: DocumentStorageService) -> dict[str, Any]:
    row: dict[str, Any] = asdict(plan)
    row["executed_moves"] = []
    if plan.skipped:
        row["status"] = "skipped"
        return row

    try:
        for move in plan.moves:
            result = _move_artifact(move)
            row["executed_moves"].append({"kind": move.kind, "result": result, "source": move.source, "target": move.target})
        _write_manifest(plan, storage)
        _update_sqlite_files(db, plan)
        row["status"] = "ok"
    except Exception as exc:
        row["status"] = "failed"
        row["error"] = str(exc)
    return row


def write_report(project_root: str, stats: MigrationStats) -> str:
    out_dir = os.path.join(project_root, "Scraper_Logs")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"migration_bundle_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(stats), f, ensure_ascii=False, indent=2)
    return path


def run(args: argparse.Namespace) -> int:
    project_root = os.path.abspath(args.project_root or _PROJECT_ROOT)
    downloads_root = os.path.abspath(args.downloads_root or os.path.join(project_root, "JACAR_Downloads"))
    cache_service = CacheService()
    db = DbService()
    storage = DocumentStorageService(project_root=project_root, layout="bundle_v1", cache_service=cache_service)
    report_service = ReportService(project_root=project_root)
    mode = "execute" if args.execute else ("verify-only" if args.verify_only else "dry-run")
    stats = MigrationStats(mode=mode, started_at=datetime.now().isoformat(timespec="seconds"))

    pdfs = _discover_pdf_files(downloads_root)
    if args.ref:
        ref_filter = args.ref.strip().upper()
        pdfs = [p for p in pdfs if ref_filter in p.upper()]
    if args.keyword:
        keyword = args.keyword.strip()
        pdfs = [p for p in pdfs if f"{os.sep}{keyword}{os.sep}" in p]
    if args.limit is not None:
        pdfs = pdfs[: max(0, int(args.limit))]

    stats.scanned_pdfs = len(pdfs)
    for pdf_path in pdfs:
        plan = build_plan(
            pdf_path=pdf_path,
            project_root=project_root,
            downloads_root=downloads_root,
            db=db,
            storage=storage,
            cache_service=cache_service,
            report_service=report_service,
        )
        stats.planned_documents += 0 if plan.skipped else 1
        stats.planned_moves += sum(1 for m in plan.moves if m.action == "move")
        stats.skipped_existing_targets += sum(1 for m in plan.moves if m.action == "skip_existing_target")
        stats.missing_sources += sum(1 for m in plan.moves if m.action == "missing")

        if args.execute:
            row = execute_plan(plan, db=db, storage=storage)
            if row.get("status") == "ok":
                stats.migrated_documents += 1
                stats.moved_files += sum(1 for m in row.get("executed_moves", []) if m.get("result") == "moved")
            elif row.get("status") == "skipped":
                stats.skipped_documents += 1
            else:
                stats.failed_documents += 1
            stats.rows.append(row)
        else:
            row = asdict(plan)
            row["status"] = "skipped" if plan.skipped else "planned"
            stats.skipped_documents += 1 if plan.skipped else 0
            stats.rows.append(row)

    report_path = write_report(project_root, stats)
    print(f"Mode: {mode}")
    print(f"Scanned PDFs: {stats.scanned_pdfs}")
    print(f"Planned documents: {stats.planned_documents}")
    print(f"Planned moves: {stats.planned_moves}")
    print(f"Missing sources: {stats.missing_sources}")
    print(f"Existing targets skipped: {stats.skipped_existing_targets}")
    if args.execute:
        print(f"Migrated documents: {stats.migrated_documents}")
        print(f"Moved files: {stats.moved_files}")
        print(f"Failed documents: {stats.failed_documents}")
    print(f"Report: {report_path}")
    return 1 if stats.failed_documents else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy HRS artifacts to per-document bundle layout.")
    parser.add_argument("--project-root", default=_PROJECT_ROOT)
    parser.add_argument("--downloads-root", default=None)
    parser.add_argument("--keyword", default="", help="Only process JACAR_Downloads/<keyword>/...")
    parser.add_argument("--ref", default="", help="Only process PDFs whose path contains this Ref.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Plan only. This is the default when --execute is absent.")
    parser.add_argument("--verify-only", action="store_true", help="Plan and report current migration status without moving files.")
    parser.add_argument("--execute", action="store_true", help="Move files and update SQLite. Without this, dry-run only.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
