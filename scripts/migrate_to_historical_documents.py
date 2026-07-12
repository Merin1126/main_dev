#!/usr/bin/env python3
"""Copy legacy JACAR bundles into Historical_Documents without deleting originals.

Dry-run is the default.  ``--execute`` copies only non-conflicting files and
updates SQLite file paths after a complete bundle copy. Existing legacy files
are deliberately retained for rollback and compatibility.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.db_service import DbService  # noqa: E402
from utils.jacar_filename import extract_jacar_ref_from_path  # noqa: E402


@dataclass(frozen=True)
class BundlePlan:
    ref: str
    source_dir: str
    target_dir: str
    files: tuple[tuple[str, str], ...]
    conflicts: tuple[str, ...]


def _same_file(left: str, right: str) -> bool:
    if not (os.path.isfile(left) and os.path.isfile(right)):
        return False
    if os.path.getsize(left) != os.path.getsize(right):
        return False

    def digest(path: str) -> str:
        value = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        return value.hexdigest()
    return digest(left) == digest(right)


def build_plans(project_root: str) -> list[BundlePlan]:
    legacy_root = os.path.join(project_root, "JACAR_Downloads")
    target_root = os.path.join(project_root, "Historical_Documents", "jacar")
    source_pdfs: dict[str, list[str]] = {}
    for current, dirs, files in os.walk(legacy_root):
        dirs[:] = [name for name in dirs if name not in {"_scratch", "duplicates"}]
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.join(current, name)
            ref = extract_jacar_ref_from_path(pdf_path)
            if ref:
                source_pdfs.setdefault(ref.upper(), []).append(pdf_path)

    plans: list[BundlePlan] = []
    for ref, candidates in sorted(source_pdfs.items()):
        candidates.sort(key=lambda path: (os.path.basename(os.path.dirname(path)).upper() != ref, path))
        canonical_pdf = candidates[0]
        source_dir = os.path.dirname(canonical_pdf)
        target_dir = os.path.join(target_root, ref)
        conflicts: list[str] = []
        for duplicate_pdf in candidates[1:]:
            if not _same_file(canonical_pdf, duplicate_pdf):
                conflicts.append("non-identical legacy PDF: " + duplicate_pdf)

        planned_files: list[tuple[str, str]] = []
        if os.path.basename(source_dir).upper() == ref:
            for current, dirs, files in os.walk(source_dir):
                dirs[:] = [name for name in dirs if name not in {"_scratch", "duplicates"}]
                rel_dir = os.path.relpath(current, source_dir)
                for name in files:
                    source_file = os.path.join(current, name)
                    rel_path = name if rel_dir == "." else os.path.join(rel_dir, name)
                    planned_files.append((source_file, rel_path))
        else:
            planned_files.append((canonical_pdf, os.path.basename(canonical_pdf)))
            sidecar = os.path.splitext(canonical_pdf)[0] + ".json"
            if os.path.isfile(sidecar):
                planned_files.append((sidecar, os.path.basename(sidecar)))

        for source_file, rel_path in planned_files:
            target_file = os.path.join(target_dir, rel_path)
            if os.path.exists(target_file) and not _same_file(source_file, target_file):
                conflicts.append(os.path.relpath(target_file, target_dir))
        plans.append(BundlePlan(ref, source_dir, target_dir, tuple(planned_files), tuple(conflicts)))
    return plans


def _copy_bundle(plan: BundlePlan) -> None:
    for source_file, rel_path in plan.files:
        target_file = os.path.join(plan.target_dir, rel_path)
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        if not os.path.exists(target_file):
            shutil.copy2(source_file, target_file)


def _update_db_paths(db: DbService, plan: BundlePlan) -> None:
    rows = db.fetchall("SELECT kind, path FROM files WHERE document_id = ?", (f"jacar:{plan.ref}",))
    targets = [os.path.join(plan.target_dir, rel_path) for _, rel_path in plan.files]
    with db.transaction() as conn:
        for row in rows:
            kind = str(row["kind"] or "")
            suffix = ".pdf" if kind == "pdf" else ".json" if kind == "sidecar" else ""
            new_path = next((path for path in targets if suffix and path.lower().endswith(suffix)), "")
            if os.path.exists(new_path):
                conn.execute(
                    "UPDATE files SET path = ? WHERE document_id = ? AND kind = ?",
                    (new_path, f"jacar:{plan.ref}", kind),
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate JACAR bundles to Historical_Documents")
    parser.add_argument("--project-root", default=PROJECT_ROOT)
    parser.add_argument("--execute", action="store_true", help="copy files and update SQLite paths")
    args = parser.parse_args()
    root = os.path.abspath(args.project_root)
    plans = build_plans(root)
    conflicts = [plan for plan in plans if plan.conflicts]
    print(f"mode={'execute' if args.execute else 'dry-run'} bundles={len(plans)} conflicts={len(conflicts)}")
    for plan in conflicts:
        print(f"CONFLICT {plan.ref}: {', '.join(plan.conflicts)}")
    if args.execute:
        db = DbService(db_path=os.path.join(root, "database", "hrs.sqlite3"))
        try:
            for plan in plans:
                if plan.conflicts:
                    continue
                _copy_bundle(plan)
                _update_db_paths(db, plan)
        finally:
            db.close()
        print(f"copied={len(plans) - len(conflicts)} skipped_conflicts={len(conflicts)} legacy_retained=yes")
    return 0 if not conflicts else 2


if __name__ == "__main__":
    raise SystemExit(main())
