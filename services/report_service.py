from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import fitz
from docx import Document
from docx.shared import Inches

from config.api_key_store import load_google_api_key
from services import CacheService, LlmService, PdfService, TemplateService
from utils.reporting import (
    build_report_entry,
    discover_pdf_files,
    extract_transcription_text,
    load_index,
    now_iso,
    parse_analysis_page_json,
    safe_filename,
    write_json,
)

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ExportResult:
    success: int
    failed: int
    manifest_path: str
    rows: list[dict[str, Any]]


class ReportService:
    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.abspath(project_root)
        self.cache_service = CacheService()
        self.pdf_service = PdfService()

    def default_index_path(self) -> str:
        return os.path.join(self.project_root, "Reports", "report_index.json")

    def default_comparison_dir(self) -> str:
        return os.path.join(self.project_root, "Reports", "comparison_docx")

    def default_summary_dir(self) -> str:
        return os.path.join(self.project_root, "Reports", "summary")

    @staticmethod
    def _manifest_name(prefix: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"run_manifest_{prefix}_{ts}.json"

    def build_index(self, *, pdf_root: str | None = None, output_path: str | None = None) -> dict[str, Any]:
        pdf_root = os.path.abspath(pdf_root or os.path.join(self.project_root, "JACAR_Downloads"))
        output_path = os.path.abspath(output_path or self.default_index_path())
        pdf_paths = discover_pdf_files(pdf_root)
        entries: list[dict[str, Any]] = []
        ready_count = 0
        for pdf_path in pdf_paths:
            entry = build_report_entry(
                project_root=self.project_root,
                pdf_root=pdf_root,
                pdf_path=pdf_path,
                cache_service=self.cache_service,
                pdf_service=self.pdf_service,
            )
            if entry.ready:
                ready_count += 1
            entries.append(entry.to_dict())
        payload = {
            "generated_at": now_iso(),
            "project_root": self.project_root,
            "pdf_root": pdf_root,
            "total_documents": len(entries),
            "ready_documents": ready_count,
            "entries": entries,
        }
        write_json(output_path, payload)
        payload["index_file"] = output_path
        return payload

    def load_index(self, index_path: str | None = None) -> dict[str, Any]:
        return load_index(os.path.abspath(index_path or self.default_index_path()))

    def delete_index(self, index_path: str | None = None) -> bool:
        target = os.path.abspath(index_path or self.default_index_path())
        if not os.path.exists(target):
            return False
        os.remove(target)
        return True

    @staticmethod
    def _analysis_lines(parsed: dict[str, Any]) -> list[str]:
        ctx = parsed.get("Historical_Context", {}) or {}
        entities = parsed.get("Entities_and_Concepts", {}) or {}
        disc = parsed.get("Discourse_Analysis", {}) or {}
        lines = [
            f"- Date_Written: {ctx.get('Date_Written', '') or '未知'}",
            f"- Author_Sender: {ctx.get('Author_Sender', '') or '未知'}",
            f"- Recipient: {ctx.get('Recipient', '') or '未知'}",
            f"- Document_Type: {ctx.get('Document_Type', '') or '未知'}",
            f"- Observation_Info: {disc.get('Observation_Info', '') or '未提及'}",
            f"- Core_Judgment: {disc.get('Core_Judgment', '') or '未提及'}",
            f"- Response_Action: {disc.get('Response_Action', '') or '未提及'}",
            f"- Relevance_Score: {disc.get('Relevance_Score', '') or '未设定'}",
        ]
        orgs = entities.get("Organizations", []) or []
        keys = entities.get("Key_Figures", []) or []
        if isinstance(orgs, list) and orgs:
            lines.append(f"- Organizations: {', '.join(str(x) for x in orgs[:12])}")
        if isinstance(keys, list) and keys:
            lines.append(f"- Key_Figures: {', '.join(str(x) for x in keys[:12])}")
        return lines

    @staticmethod
    def _write_page_block(
        doc: Document,
        *,
        pdf_page,
        page_index: int,
        total_pages: int,
        ocr_text: str,
        analysis_raw: str,
        include_analysis_raw: bool,
        image_zoom: float,
    ) -> None:
        doc.add_heading(f"第 {page_index + 1} / {total_pages} 页", level=2)
        table = doc.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        table.autofit = False
        table.columns[0].width = Inches(3.0)
        table.columns[1].width = Inches(3.5)

        left = table.cell(0, 0)
        right = table.cell(0, 1)

        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(image_zoom, image_zoom), alpha=False)
        img_buf = io.BytesIO(pix.tobytes("png"))
        p_img = left.paragraphs[0]
        p_img.add_run().add_picture(img_buf, width=Inches(2.9))

        right.paragraphs[0].add_run("OCR 提取：")
        right.add_paragraph(ocr_text or "（空）")

        doc.add_paragraph("Analysis：")
        parsed, cleaned = parse_analysis_page_json(analysis_raw)
        if parsed is not None:
            for line in ReportService._analysis_lines(parsed):
                doc.add_paragraph(line)
        else:
            doc.add_paragraph(cleaned or "（空）")

        if include_analysis_raw and cleaned:
            doc.add_paragraph("Analysis 原文：")
            doc.add_paragraph(cleaned)

    def export_comparison_docx(
        self,
        *,
        index_data: dict[str, Any],
        selected_pdf_paths: set[str] | None,
        output_dir: str | None = None,
        include_incomplete: bool = False,
        include_analysis_raw: bool = False,
        image_zoom: float = 1.5,
        progress_cb: ProgressCallback | None = None,
    ) -> ExportResult:
        output_dir = os.path.abspath(output_dir or self.default_comparison_dir())
        os.makedirs(output_dir, exist_ok=True)
        entries = index_data.get("entries", []) or []
        selected = []
        for e in entries:
            pdf_path = str(e.get("pdf_path") or "")
            if selected_pdf_paths and pdf_path not in selected_pdf_paths:
                continue
            if not include_incomplete and not bool(e.get("ready", False)):
                continue
            selected.append(e)

        rows: list[dict[str, Any]] = []
        success = 0
        failed = 0
        total = len(selected)
        for i, entry in enumerate(selected, start=1):
            if progress_cb:
                progress_cb(i, total, f"导出内容1：{entry.get('pdf_rel_path')}")
            row: dict[str, Any] = {
                "pdf_rel_path": entry.get("pdf_rel_path"),
                "ready": bool(entry.get("ready")),
                "issues": entry.get("issues", []),
            }
            try:
                out = self._render_single_docx(
                    entry=entry,
                    output_dir=output_dir,
                    include_analysis_raw=include_analysis_raw,
                    image_zoom=max(0.8, float(image_zoom)),
                )
                row["status"] = "ok"
                row["output_docx"] = out
                success += 1
            except Exception as e:
                row["status"] = "failed"
                row["error"] = str(e)
                failed += 1
            rows.append(row)

        manifest = {
            "generated_at": now_iso(),
            "project_root": self.project_root,
            "output_dir": output_dir,
            "selected_documents": total,
            "success_documents": success,
            "failed_documents": failed,
            "rows": rows,
        }
        manifest_path = os.path.join(output_dir, self._manifest_name("comparison"))
        write_json(manifest_path, manifest)
        return ExportResult(success=success, failed=failed, manifest_path=manifest_path, rows=rows)

    def _render_single_docx(
        self,
        *,
        entry: dict[str, Any],
        output_dir: str,
        include_analysis_raw: bool,
        image_zoom: float,
    ) -> str:
        pdf_path = entry["pdf_path"]
        page_count = int(entry.get("page_count", 0))
        ocr_pages = self.cache_service.read_paged_cache(entry["ocr_cache_path"])
        analysis_pages = self.cache_service.read_paged_cache(entry["analysis_cache_path"])

        doc = Document()
        doc.add_heading(f"史料按页对照报告：{os.path.basename(pdf_path)}", level=1)
        doc.add_paragraph(f"来源文件：{entry.get('pdf_rel_path', pdf_path)}")
        doc.add_paragraph(f"总页数：{page_count}")

        with fitz.open(pdf_path) as pdf:
            total = min(page_count, len(pdf))
            for i in range(total):
                ocr_text = extract_transcription_text(ocr_pages[i] if i < len(ocr_pages) else "")
                analysis_raw = analysis_pages[i] if i < len(analysis_pages) else ""
                self._write_page_block(
                    doc,
                    pdf_page=pdf[i],
                    page_index=i,
                    total_pages=total,
                    ocr_text=ocr_text,
                    analysis_raw=analysis_raw,
                    include_analysis_raw=include_analysis_raw,
                    image_zoom=image_zoom,
                )
                if i < total - 1:
                    doc.add_page_break()

        name = safe_filename(os.path.splitext(os.path.basename(pdf_path))[0]) + "_按页对照报告.docx"
        out_path = os.path.join(output_dir, name)
        doc.save(out_path)
        return out_path

    @staticmethod
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

    @staticmethod
    def _build_analysis_bundle(analysis_pages: list[str], *, max_chars: int) -> tuple[str, bool]:
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

    def generate_summaries(
        self,
        *,
        index_data: dict[str, Any],
        selected_pdf_paths: set[str] | None,
        output_dir: str | None = None,
        api_key: str | None = None,
        model_name: str = "gemini-3.1-pro-preview",
        include_incomplete: bool = False,
        max_source_chars: int = 120000,
        progress_cb: ProgressCallback | None = None,
    ) -> ExportResult:
        output_dir = os.path.abspath(output_dir or self.default_summary_dir())
        os.makedirs(output_dir, exist_ok=True)
        entries = index_data.get("entries", []) or []
        selected = []
        for e in entries:
            pdf_path = str(e.get("pdf_path") or "")
            if selected_pdf_paths and pdf_path not in selected_pdf_paths:
                continue
            if not include_incomplete and not bool(e.get("ready", False)):
                continue
            selected.append(e)

        key = (
            (api_key or "").strip()
            or os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
            or load_google_api_key()
        )
        if not key:
            raise RuntimeError("未找到 Gemini API Key。请先在设置页配置。")

        llm = LlmService(api_key=key, project_root=self.project_root, timeout_sec=180)
        rows: list[dict[str, Any]] = []
        success = 0
        failed = 0
        total = len(selected)
        for i, entry in enumerate(selected, start=1):
            if progress_cb:
                progress_cb(i, total, f"导出内容2：{entry.get('pdf_rel_path')}")
            row: dict[str, Any] = {
                "pdf_rel_path": entry.get("pdf_rel_path"),
                "ready": bool(entry.get("ready")),
                "issues": entry.get("issues", []),
                "model": model_name,
            }
            try:
                analysis_pages = self.cache_service.read_paged_cache(entry["analysis_cache_path"])
                page_count = int(entry.get("page_count", 0))
                bundle, truncated = self._build_analysis_bundle(
                    analysis_pages,
                    max_chars=max(5000, int(max_source_chars)),
                )
                if not bundle.strip():
                    raise RuntimeError("Analysis 缓存为空，无法生成总结。")
                doc_name = os.path.basename(entry["pdf_path"])
                prompt = self._render_summary_prompt(doc_name=doc_name, page_count=page_count)
                summary_text, usage_summary = llm.detect_text(
                    screen_name="ReportSummaryUI",
                    task_name="进度汇报总结",
                    selected_pdf_path=entry["pdf_path"],
                    file_name=f"{doc_name}_summary",
                    model_name=model_name,
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
            except Exception as e:
                row["status"] = "failed"
                row["error"] = str(e)
                failed += 1
            rows.append(row)

        manifest = {
            "generated_at": now_iso(),
            "project_root": self.project_root,
            "output_dir": output_dir,
            "selected_documents": total,
            "success_documents": success,
            "failed_documents": failed,
            "rows": rows,
        }
        manifest_path = os.path.join(output_dir, self._manifest_name("summary"))
        write_json(manifest_path, manifest)
        return ExportResult(success=success, failed=failed, manifest_path=manifest_path, rows=rows)
