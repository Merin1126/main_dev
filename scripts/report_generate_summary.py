from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.api_key_store import load_google_api_key
from services import CacheService, LlmService, TemplateService
from utils.reporting import (
    load_index,
    now_iso,
    parse_analysis_page_json,
    safe_filename,
    write_json,
)


def _default_index_path(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "report_index.json")


def _default_output_dir(project_root: str) -> str:
    return os.path.join(project_root, "Reports", "summary")


def _default_manifest_path(output_dir: str) -> str:
    return os.path.join(output_dir, f"run_manifest_summary_{now_iso().replace(':', '-')}.json")


def _render_summary_prompt(*, doc_name: str, page_count: int) -> str:
    try:
        return TemplateService().render_prompt(
            "report_summary_prompt.jinja",
            {"doc_name": doc_name, "page_count": page_count},
        )
    except Exception:
        return (
            f"请基于以下 Analysis 数据，生成《{doc_name}》的研究汇报摘要。\n"
            "要求：概述背景时间线、观察判断因应主线、高频组织人物、研究价值与证据摘录。"
        )


def _build_analysis_bundle(
    analysis_pages: list[str],
    *,
    max_chars: int,
) -> tuple[str, bool]:
    blocks: list[str] = []
    total = 0
    truncated = False
    for idx, page_raw in enumerate(analysis_pages, start=1):
        parsed, cleaned = parse_analysis_page_json(page_raw)
        if parsed is not None:
            ctx = parsed.get("Historical_Context", {}) or {}
            entities = parsed.get("Entities_and_Concepts", {}) or {}
            disc = parsed.get("Discourse_Analysis", {}) or {}
            obj = {
                "page": idx,
                "Date_Written": ctx.get("Date_Written"),
                "Author_Sender": ctx.get("Author_Sender"),
                "Recipient": ctx.get("Recipient"),
                "Document_Type": ctx.get("Document_Type"),
                "Organizations": (entities.get("Organizations", []) or [])[:12],
                "Key_Figures": (entities.get("Key_Figures", []) or [])[:12],
                "Discourse_Keywords": (entities.get("Discourse_Keywords", []) or [])[:20],
                "Observation_Info": disc.get("Observation_Info"),
                "Core_Judgment": disc.get("Core_Judgment"),
                "Response_Action": disc.get("Response_Action"),
                "Relevance_Score": disc.get("Relevance_Score"),
            }
            chunk = f"[PAGE {idx}]\n{json.dumps(obj, ensure_ascii=False, indent=2)}"
        else:
            chunk = f"[PAGE {idx}]\n{cleaned[:2500]}"

        if total + len(chunk) > max_chars:
            truncated = True
            break
        blocks.append(chunk)
        total += len(chunk)
    return "\n\n".join(blocks), truncated


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
        help="Max analysis source chars sent to LLM per document.",
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

    api_key = (
        (args.api_key or "").strip()
        or os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
        or load_google_api_key()
    )
    if not api_key:
        raise RuntimeError("未找到 Gemini API Key。请设置 GOOGLE_GEMINI_API_KEY 或 --api-key。")

    llm = LlmService(api_key=api_key, project_root=project_root, timeout_sec=180)
    cache_service = CacheService()

    manifest_rows: list[dict[str, Any]] = []
    success = 0
    failed = 0
    for entry in selected:
        row: dict[str, Any] = {
            "pdf_rel_path": entry.get("pdf_rel_path"),
            "ready": bool(entry.get("ready")),
            "issues": entry.get("issues", []),
            "model": args.model,
        }
        try:
            analysis_pages = cache_service.read_paged_cache(entry["analysis_cache_path"])
            page_count = int(entry.get("page_count", 0))
            bundle, truncated = _build_analysis_bundle(
                analysis_pages,
                max_chars=max(5000, int(args.max_source_chars)),
            )
            if not bundle.strip():
                raise RuntimeError("Analysis 缓存为空，无法生成总结。")

            doc_name = os.path.basename(entry["pdf_path"])
            prompt = _render_summary_prompt(doc_name=doc_name, page_count=page_count)
            summary_text, usage_summary = llm.detect_text(
                screen_name="ReportSummaryScript",
                task_name="进度汇报总结",
                selected_pdf_path=entry["pdf_path"],
                file_name=f"{doc_name}_summary",
                model_name=args.model,
                academic_prompt=prompt,
                behavior_name="进度汇报总结",
                page_index=None,
                source_text=bundle,
            )

            out_name = safe_filename(os.path.splitext(doc_name)[0]) + ".summary.md"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(f"# {doc_name}｜分析总结\n\n")
                if truncated:
                    f.write("> 注：源数据超长，已按上限截断后生成本总结。\n\n")
                f.write(summary_text.strip() + "\n")

            row["status"] = "ok"
            row["output_summary"] = out_path
            row["analysis_pages"] = len(analysis_pages)
            row["source_truncated"] = truncated
            row["usage_summary"] = usage_summary
            success += 1
            print(f"[OK] {entry.get('pdf_rel_path')} -> {out_path}")
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
