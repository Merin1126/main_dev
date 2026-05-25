from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import ReportService


def _default_index_path(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "report_index.json")


def _default_output_dir(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "summary")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate per-document summary via Gemini.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root path.")
    parser.add_argument("--index-file", default=None, help="Index file path.")
    parser.add_argument("--output-dir", default=None, help="Summary output directory.")
    parser.add_argument("--manifest", default=None, help="Optional manifest output path.")
    parser.add_argument("--model", default="gemini-3.1-pro-preview", help="Gemini model name.")
    parser.add_argument("--api-key", default=None, help="Gemini API key. Defaults to env/.secrets.")
    parser.add_argument("--include-incomplete", action="store_true", help="Include incomplete docs.")
    parser.add_argument("--max-docs", type=int, default=0, help="Process up to N documents (0 = all).")
    parser.add_argument(
        "--max-source-chars",
        type=int,
        default=120000,
        help="Max analysis chars per summary chunk.",
    )
    parser.add_argument("--chunk-pages", type=int, default=20, help="Pages per summary chunk.")
    parser.add_argument("--retry-attempts", type=int, default=3, help="Retry attempts for retryable API errors.")
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Regenerate summaries even if .summary.md already exists.",
    )
    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root)
    report_service = ReportService(project_root)
    index_file = os.path.abspath(args.index_file or _default_index_path(project_root))
    output_dir = os.path.abspath(args.output_dir or _default_output_dir(project_root))
    os.makedirs(output_dir, exist_ok=True)

    index_data = report_service.load_index(index_file)
    entries = index_data.get("entries", []) or []
    selected = [
        e
        for e in entries
        if bool(e.get("ready", False)) or args.include_incomplete
    ]
    if args.max_docs and args.max_docs > 0:
        selected = selected[: args.max_docs]
    selected_paths = {str(e.get("pdf_path") or "") for e in selected if str(e.get("pdf_path") or "")}

    result = report_service.generate_summaries(
        index_data=index_data,
        selected_pdf_paths=selected_paths,
        output_dir=output_dir,
        api_key=args.api_key,
        model_name=args.model,
        include_incomplete=args.include_incomplete,
        max_source_chars=max(5000, int(args.max_source_chars)),
        chunk_pages=max(1, int(args.chunk_pages)),
        retry_attempts=max(1, int(args.retry_attempts)),
        skip_existing=not bool(args.no_skip_existing),
    )

    if args.manifest:
        # 兼容显式 manifest 路径需求：复制服务结果到指定位置
        import shutil

        shutil.copyfile(result.manifest_path, os.path.abspath(args.manifest))
        print(f"Manifest copied to: {os.path.abspath(args.manifest)}")
    print(
        f"Done. success={result.success}, failed={result.failed}, skipped={result.skipped}\n"
        f"Manifest: {result.manifest_path}"
    )
    return 0 if result.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
