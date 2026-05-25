from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import CacheService, PdfService
from utils.reporting import (
    build_report_entry,
    discover_pdf_files,
    now_iso,
    write_json,
)


def _default_output_path(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "report_index.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build report index for OCR+Analysis-complete PDFs.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root path.")
    parser.add_argument(
        "--pdf-root",
        default=None,
        help="PDF root directory (default: <project-root>/JACAR_Downloads).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Index output path (default: <project-root>/Reports/report_index.json).",
    )
    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root)
    pdf_root = os.path.abspath(args.pdf_root or os.path.join(project_root, "JACAR_Downloads"))
    output_path = os.path.abspath(args.output or _default_output_path(project_root))

    cache_service = CacheService()
    pdf_service = PdfService()
    pdf_paths = discover_pdf_files(pdf_root)

    entries = []
    ready_count = 0
    for pdf_path in pdf_paths:
        entry = build_report_entry(
            project_root=project_root,
            pdf_root=pdf_root,
            pdf_path=pdf_path,
            cache_service=cache_service,
            pdf_service=pdf_service,
        )
        if entry.ready:
            ready_count += 1
        entries.append(entry.to_dict())

    payload = {
        "generated_at": now_iso(),
        "project_root": project_root,
        "pdf_root": pdf_root,
        "total_documents": len(entries),
        "ready_documents": ready_count,
        "entries": entries,
    }
    write_json(output_path, payload)
    print(f"Index written: {output_path}")
    print(f"Documents: {len(entries)} | Ready(OCR+Analysis): {ready_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
