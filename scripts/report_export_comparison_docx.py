from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.report_service import ReportService
from utils.reporting import load_index, now_iso, write_json


def _default_index_path(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "report_index.json")


def _default_output_dir(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "comparison_docx")


def _default_manifest_path(output_dir: str) -> str:
    return os.path.join(output_dir, f"run_manifest_comparison_{now_iso().replace(':', '-')}.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export per-page comparison Word report.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root path.")
    parser.add_argument("--index-file", default=None, help="Index file path.")
    parser.add_argument("--output-dir", default=None, help="Output directory for docx files.")
    parser.add_argument("--manifest", default=None, help="Optional manifest output path.")
    parser.add_argument("--include-incomplete", action="store_true", help="Include incomplete documents.")
    parser.add_argument("--include-analysis-raw", action="store_true", help="Append raw analysis text/json.")
    parser.add_argument("--max-docs", type=int, default=0, help="Process up to N documents (0 = all).")
    parser.add_argument(
        "--image-dpi",
        type=float,
        default=240.0,
        help="Raster DPI for embedded page images (96-600; higher = sharper, larger file).",
    )
    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root)
    index_file = os.path.abspath(args.index_file or _default_index_path(project_root))
    output_dir = os.path.abspath(args.output_dir or _default_output_dir(project_root))
    os.makedirs(output_dir, exist_ok=True)

    index_data = load_index(index_file)
    entries = index_data.get("entries", []) or []
    selected = [
        e
        for e in entries
        if bool(e.get("ready", False)) or args.include_incomplete
    ]
    if args.max_docs and args.max_docs > 0:
        selected = selected[: args.max_docs]

    report_service = ReportService(project_root)
    manifest_rows: list[dict[str, Any]] = []
    success = 0
    failed = 0
    for entry in selected:
        row: dict[str, Any] = {
            "pdf_rel_path": entry.get("pdf_rel_path"),
            "ready": bool(entry.get("ready")),
            "issues": entry.get("issues", []),
        }
        try:
            out = report_service._render_single_docx(
                entry=entry,
                output_dir=output_dir,
                include_analysis_raw=args.include_analysis_raw,
                image_dpi=float(args.image_dpi),
            )
            row["status"] = "ok"
            row["output_docx"] = out
            success += 1
            print(f"[OK] {entry.get('pdf_rel_path')} -> {out}")
        except Exception as e:
            row["status"] = "failed"
            row["error"] = str(e)
            failed += 1
            print(f"[FAILED] {entry.get('pdf_rel_path')}: {e}")
        manifest_rows.append(row)

    manifest = {
        "generated_at": now_iso(),
        "project_root": project_root,
        "index_file": index_file,
        "output_dir": output_dir,
        "selected_documents": len(selected),
        "success_documents": success,
        "failed_documents": failed,
        "rows": manifest_rows,
    }
    manifest_path = os.path.abspath(args.manifest or _default_manifest_path(output_dir))
    write_json(manifest_path, manifest)
    print(f"Manifest written: {manifest_path}")
    print(f"Done. success={success}, failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
