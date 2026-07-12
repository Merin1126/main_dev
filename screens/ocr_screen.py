from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from components.editor_search import EditorSearchController
from components.ui.button import Button
from config.academic_prompts import render_ocr_prompt
from config.settings import Color
from screens.base_screen import BaseDocumentScreen
from utils.ocr_page_text import (
    compose_ocr_page_xml,
    extract_layout_analysis,
    extract_transcription_text,
    page_has_ocr_xml_structure,
)


class OCRScreen(BaseDocumentScreen):
    requires_image_input = True

    screen_title = "史料校对"
    cache_dir_name = "OCR_Cache"
    right_panel_title = "\U0000F14B 史料 OCR 校对区"
    primary_action_label = "开始 OCR 识别"
    task_short_name = "OCR"
    progress_verb = "识别"
    force_full_label = "强制重新识别"
    single_page_label = "仅识别当前页"
    re_single_label = "重新识别当前页"
    export_dialog_title = "保存提取的文字"
    empty_page_marker = "（本页未识别到文本）"
    idle_editor_hint = (
        "👈 请在左侧选择一份已下载的史料 PDF 文件。\n\n"
        "此处将显示本地缓存或新的 OCR 识别结果..."
    )

    def __init__(self, master, **kwargs):
        self._layout_panel_expanded = False
        super().__init__(master, **kwargs)
        self._editor_search = EditorSearchController(self.right_frame, self.text_editor)
        self._editor_search.attach()

    def _customize_text_action_frame(self) -> None:
        self.btn_save_edits.pack_forget()
        self.btn_save_edits.destroy()

        action_row = ctk.CTkFrame(self.text_action_frame, fg_color=Color.TRANSPARENT)
        action_row.pack(fill="x")

        self.btn_layout_toggle = Button(
            action_row,
            text="版面",
            width=52,
            height=38,
            fg_color=Color.BG_BUTTON_MUTED,
            hover_color=Color.BG_BUTTON_MUTED_HOVER,
            command=self._toggle_layout_panel,
        )
        self.btn_layout_toggle.pack(side="left", padx=(0, 6))

        # 在 action_row 内重建保存按钮，避免默认 width=300 与 pack/grid 混用导致无法显示
        self.btn_save_edits = Button(
            action_row,
            text="💾 保存",
            width=80,
            height=38,
            fg_color=Color.BTN_SUCCESS,
            hover_color=Color.BTN_SUCCESS_HOVER,
            command=self.save_edits_to_disk,
        )
        self.btn_save_edits.pack(side="left", fill="x", expand=True)

        self.layout_panel = ctk.CTkFrame(self.right_frame, fg_color=("#eef2f7", "#2a3038"), corner_radius=8)
        header = ctk.CTkFrame(self.layout_panel, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(
            header,
            text="版面分析  <layout_analysis>",
            font=("Arial", 12, "bold"),
            anchor="w",
        ).pack(side="left")
        Button(
            header,
            text="保存版面",
            width=88,
            height=28,
            command=self._save_layout_analysis_only,
        ).pack(side="right")

        self.layout_editor = ctk.CTkTextbox(
            self.layout_panel,
            wrap="word",
            font=("Arial", 12),
            corner_radius=6,
            height=140,
        )
        self.layout_editor.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _current_page_raw(self) -> str:
        if not self.ocr_pages:
            return ""
        idx = max(0, min(self.current_ocr_page_index, len(self.ocr_pages) - 1))
        return str(self.ocr_pages[idx] or "")

    def _page_uses_split_view(self, raw: str | None = None) -> bool:
        text = raw if raw is not None else self._current_page_raw()
        return page_has_ocr_xml_structure(text)

    def _toggle_layout_panel(self) -> None:
        if self._layout_panel_expanded:
            self._save_current_ocr_page()
            self.layout_panel.pack_forget()
            self._layout_panel_expanded = False
            self.btn_layout_toggle.configure(fg_color=Color.BG_BUTTON_MUTED)
            return
        if not self._page_uses_split_view():
            messagebox.showinfo("提示", "当前页无版面分析结构（非标准 OCR XML 输出）。")
            return
        self._sync_layout_editor_from_page()
        self.layout_panel.pack(fill="x", padx=10, pady=(0, 6), before=self.text_action_frame)
        self._layout_panel_expanded = True
        self.btn_layout_toggle.configure(fg_color=Color.BTN_PRIMARY_ALT)

    def _sync_layout_editor_from_page(self) -> None:
        raw = self._current_page_raw()
        self.layout_editor.delete("0.0", "end")
        self.layout_editor.insert("0.0", extract_layout_analysis(raw))

    def _save_layout_analysis_only(self) -> None:
        if not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return
        if not self._page_uses_split_view():
            messagebox.showwarning("提示", "当前页无可保存的版面分析内容。")
            return
        self._save_current_ocr_page()
        self.save_edits_to_disk()

    def _show_current_ocr_page(self) -> None:
        total = len(self.ocr_pages)
        if total == 0:
            self.ocr_pages = [""]
            total = 1
        self.current_ocr_page_index = max(0, min(self.current_ocr_page_index, total - 1))
        raw = str(self.ocr_pages[self.current_ocr_page_index] or "")
        display = (
            extract_transcription_text(raw)
            if self._page_uses_split_view(raw)
            else raw
        )
        self.text_editor.delete("0.0", "end")
        self.text_editor.insert("0.0", display)
        if self._layout_panel_expanded:
            if self._page_uses_split_view(raw):
                self._sync_layout_editor_from_page()
            else:
                self.layout_panel.pack_forget()
                self._layout_panel_expanded = False
                self.btn_layout_toggle.configure(fg_color=Color.BG_BUTTON_MUTED)
        self.ocr_page_label.configure(text=f"文字页码: {self.current_ocr_page_index + 1} / {total}")
        if hasattr(self, "ocr_page_entry"):
            self.ocr_page_entry.delete(0, "end")
            self.ocr_page_entry.insert(0, str(self.current_ocr_page_index + 1))
        self._sync_pdf_page_to_text_page()

    def _save_current_ocr_page(self) -> None:
        if not self.ocr_pages:
            self.ocr_pages = [""]
            self.current_ocr_page_index = 0
        idx = self.current_ocr_page_index
        raw = str(self.ocr_pages[idx] or "")
        editor_text = self.text_editor.get("0.0", "end").strip()
        if self._page_uses_split_view(raw) or (
            self._layout_panel_expanded and self.layout_editor.get("0.0", "end").strip()
        ):
            layout_text = self.layout_editor.get("0.0", "end").strip()
            if not self._layout_panel_expanded:
                layout_text = extract_layout_analysis(raw)
            self.ocr_pages[idx] = compose_ocr_page_xml(
                layout=layout_text,
                transcription=editor_text,
            )
        else:
            self.ocr_pages[idx] = editor_text

    def get_academic_prompt(self, page_index: int = None) -> str:
        return render_ocr_prompt()

    def export_document(self) -> None:
        self._export_vertical_historical_docx(kind="ocr", dialog_title=self.export_dialog_title)
