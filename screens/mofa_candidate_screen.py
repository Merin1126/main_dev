from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from config.settings import Color
from services.mofa_candidate_service import (
    CANDIDATE_STATUS,
    EXCLUDED_STATUS,
    RELEVANT_STATUS,
    MofaCandidate,
    MofaCandidateService,
)
from services.mofa_fulltext_search_service import MofaFullTextSearchService
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService
from services.mofa_research_package_service import MofaResearchPackageService
from screens.mofa_research_package_dialog import (
    MofaResearchPackageCreateDialog,
    MofaResearchPackageManagerDialog,
)
from utils.open_path import open_path_in_system, reveal_file_in_folder


_STATUS_FILTERS = {
    "全部状态": "",
    "候选": CANDIDATE_STATUS,
    "相关": RELEVANT_STATUS,
    "排除": EXCLUDED_STATUS,
}
_STATUS_LABELS = {
    CANDIDATE_STATUS: "候选",
    RELEVANT_STATUS: "相关",
    EXCLUDED_STATUS: "排除",
}


class MofaCandidateScreen(ctk.CTkFrame):
    """Persistent research basket for MOFA page-level search results."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, fg_color=Color.TRANSPARENT, corner_radius=0, **kwargs)
        self.library = MofaLibraryService()
        self.service = MofaCandidateService(db_service=self.library.db)
        self.search_service = MofaFullTextSearchService(
            project_root=self.library.project_root,
            db_service=self.library.db,
            library_service=self.library,
        )
        self.package_service = MofaResearchPackageService(
            project_root=self.library.project_root,
            db_service=self.library.db,
            library_service=self.library,
            candidate_service=self.service,
        )
        self.candidates: list[MofaCandidate] = []
        self.entries_by_native_id: dict[str, MofaLibraryEntry] = {}
        self._detail_candidate_id = ""
        self._detail_page_index = 0
        self._detail_page_count = 0
        self._detail_highlight_terms: tuple[str, ...] = ()
        self._build_ui()

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=22, pady=(18, 8))
        ctk.CTkLabel(
            header,
            text="MOFA 候选史料清单",
            font=("PingFang SC", 24, "bold"),
        ).pack(side="left")
        self.refresh_btn = ctk.CTkButton(
            header,
            text="刷新清单",
            width=96,
            command=self.refresh_candidates,
        )
        self.refresh_btn.pack(side="right")
        self.saved_searches_btn = ctk.CTkButton(
            header,
            text="已保存检索",
            width=104,
            command=self.show_saved_searches,
        )
        self.saved_searches_btn.pack(side="right", padx=(0, 8))
        self.packages_btn = ctk.CTkButton(
            header,
            text="MOFA工作包",
            width=104,
            command=self.show_research_packages,
        )
        self.packages_btn.pack(side="right", padx=(0, 8))

        self.summary_label = ctk.CTkLabel(
            self,
            text="正在读取候选史料…",
            anchor="w",
            text_color=Color.TEXT_GRAY,
        )
        self.summary_label.pack(fill="x", padx=24, pady=(0, 10))

        filters = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        filters.pack(fill="x", padx=22, pady=(0, 10))
        self.status_var = tk.StringVar(value="全部状态")
        self.status_menu = ctk.CTkOptionMenu(
            filters,
            values=list(_STATUS_FILTERS),
            variable=self.status_var,
            width=112,
            command=lambda _value: self.refresh_candidates(),
        )
        self.status_menu.pack(side="left", padx=(12, 6), pady=10)
        self.year_var = tk.StringVar(value="全部年份")
        self.year_menu = ctk.CTkOptionMenu(
            filters,
            values=["全部年份"],
            variable=self.year_var,
            width=104,
            command=lambda _value: self.refresh_candidates(),
        )
        self.year_menu.pack(side="left", padx=6, pady=10)
        self.tag_var = tk.StringVar(value="全部标签")
        self.tag_menu = ctk.CTkOptionMenu(
            filters,
            values=["全部标签"],
            variable=self.tag_var,
            width=120,
            command=lambda _value: self.refresh_candidates(),
        )
        self.tag_menu.pack(side="left", padx=6, pady=10)
        self.search_var = tk.StringVar()
        self.search_entry = ctk.CTkEntry(
            filters,
            textvariable=self.search_var,
            placeholder_text="搜索标题、MOFA ID、检索词、标签或备注",
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(8, 12), pady=10)
        self.search_entry.bind("<Return>", lambda _event: self.refresh_candidates())

        actions = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        actions.pack(fill="x", padx=22, pady=(0, 10))
        ctk.CTkButton(
            actions,
            text="标为候选",
            width=94,
            command=lambda: self._set_selected_status(CANDIDATE_STATUS),
        ).pack(side="left", padx=(12, 6), pady=10)
        ctk.CTkButton(
            actions,
            text="标为相关",
            width=94,
            command=lambda: self._set_selected_status(RELEVANT_STATUS),
        ).pack(side="left", padx=6, pady=10)
        ctk.CTkButton(
            actions,
            text="标为排除",
            width=94,
            fg_color=Color.RED,
            command=lambda: self._set_selected_status(EXCLUDED_STATUS),
        ).pack(side="left", padx=6, pady=10)
        self.remove_candidate_btn = ctk.CTkButton(
            actions,
            text="取消候选",
            width=94,
            fg_color=Color.TEXT_GRAY,
            command=self.remove_selected_candidates,
        )
        self.remove_candidate_btn.pack(side="left", padx=(12, 6), pady=10)
        self.create_package_btn = ctk.CTkButton(
            actions,
            text="创建MOFA研究工作包",
            width=150,
            command=self.create_research_package,
        )
        self.create_package_btn.pack(side="left", padx=(12, 6), pady=10)
        self.open_btn = ctk.CTkButton(
            actions,
            text="打开单页PDF",
            width=110,
            state="disabled",
            command=self.open_selected_pdf,
        )
        self.open_btn.pack(side="right", padx=(6, 12), pady=10)
        self.reveal_btn = ctk.CTkButton(
            actions,
            text="定位单页PDF",
            width=110,
            state="disabled",
            command=self.reveal_selected_pdf,
        )
        self.reveal_btn.pack(side="right", padx=6, pady=10)

        pane = tk.PanedWindow(
            self,
            orient="vertical",
            sashwidth=7,
            sashrelief="flat",
            borderwidth=0,
            bg=Color.BG_MAIN_DARK,
        )
        pane.pack(fill="both", expand=True, padx=22, pady=(0, 18))
        table_frame = ctk.CTkFrame(pane, fg_color=Color.BG_CARD, corner_radius=10)
        detail_frame = ctk.CTkFrame(pane, fg_color=Color.BG_CARD, corner_radius=10)
        pane.add(table_frame, minsize=230, stretch="always")
        pane.add(detail_frame, minsize=210, stretch="always")

        columns = (
            "status",
            "year",
            "volume",
            "title",
            "page",
            "printed",
            "queries",
            "tags",
            "blocks",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        labels = {
            "status": "状态",
            "year": "年份",
            "volume": "卷册",
            "title": "史料",
            "page": "PDF页",
            "printed": "印刷页码",
            "queries": "召回关键词",
            "tags": "标签",
            "blocks": "命中块",
        }
        widths = {
            "status": 65,
            "year": 60,
            "volume": 60,
            "title": 260,
            "page": 65,
            "printed": 85,
            "queries": 220,
            "tags": 150,
            "blocks": 66,
        }
        for key in columns:
            self.tree.heading(key, text=labels[key])
            self.tree.column(key, width=widths[key], stretch=key in {"title", "queries", "tags"})
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)
        self.tree.bind("<Double-Button-1>", lambda _event: self.open_selected_pdf())
        self.tree.bind("<Button-2>", self._show_context_menu)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Control-Button-1>", self._show_context_menu)
        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="取消候选", command=self.remove_selected_candidates)

        info = ctk.CTkFrame(detail_frame, fg_color=Color.TRANSPARENT)
        info.pack(fill="x", padx=10, pady=(10, 4))
        self.detail_title = ctk.CTkLabel(
            info,
            text="选择一条候选史料查看详情",
            anchor="w",
            font=("PingFang SC", 12, "bold"),
        )
        self.detail_title.pack(side="left", fill="x", expand=True)
        self.next_ocr_page_btn = ctk.CTkButton(
            info,
            text="›",
            width=38,
            state="disabled",
            command=lambda: self._change_detail_page(1),
        )
        self.next_ocr_page_btn.pack(side="right", padx=(4, 0))
        self.prev_ocr_page_btn = ctk.CTkButton(
            info,
            text="‹",
            width=38,
            state="disabled",
            command=lambda: self._change_detail_page(-1),
        )
        self.prev_ocr_page_btn.pack(side="right", padx=(4, 0))
        self.ocr_page_label = ctk.CTkLabel(info, text="— / —", width=72)
        self.ocr_page_label.pack(side="right", padx=(8, 0))
        self.jump_candidate_page_btn = ctk.CTkButton(
            info,
            text="返回命中页",
            width=88,
            state="disabled",
            command=self._jump_to_candidate_page,
        )
        self.jump_candidate_page_btn.pack(side="right", padx=(8, 0))

        editor = ctk.CTkFrame(detail_frame, fg_color=Color.TRANSPARENT)
        editor.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = ctk.CTkFrame(editor, fg_color=Color.TRANSPARENT)
        right = ctk.CTkFrame(editor, fg_color=Color.TRANSPARENT, width=330)
        left.pack(side="left", fill="both", expand=True)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        self.ocr_text = ctk.CTkTextbox(left, wrap="word", font=("PingFang SC", 12))
        self.ocr_text.pack(fill="both", expand=True)
        self.ocr_text.tag_config(
            "query_match",
            background="#ffcc4d",
            foreground="#251a00",
            underline=True,
        )
        self.ocr_text.tag_config(
            "section_heading",
            foreground="#6c6f78",
            underline=True,
        )
        self.ocr_text.configure(state="disabled")
        ctk.CTkLabel(right, text="研究备注（候选命中页）", anchor="w").pack(fill="x")
        self.notes_text = ctk.CTkTextbox(right, height=120, wrap="word")
        self.notes_text.pack(fill="x", pady=(4, 10))
        ctk.CTkLabel(right, text="标签（逗号分隔）", anchor="w").pack(fill="x")
        self.tags_var = tk.StringVar()
        self.tags_entry = ctk.CTkEntry(right, textvariable=self.tags_var)
        self.tags_entry.pack(fill="x", pady=(4, 10))
        self.save_meta_btn = ctk.CTkButton(
            right,
            text="保存备注和标签",
            state="disabled",
            command=self.save_selected_metadata,
        )
        self.save_meta_btn.pack(fill="x")

    def on_show(self) -> None:
        self.refresh_candidates()

    def refresh_candidates(self) -> None:
        years = ["全部年份", *(str(value) for value in self.library.available_years())]
        self.year_menu.configure(values=years)
        if self.year_var.get() not in years:
            self.year_var.set("全部年份")
        tags = ["全部标签", *self.service.available_tags()]
        self.tag_menu.configure(values=tags)
        if self.tag_var.get() not in tags:
            self.tag_var.set("全部标签")
        year_value = self.year_var.get()
        self.candidates = self.service.list_candidates(
            status=_STATUS_FILTERS[self.status_var.get()],
            year=int(year_value) if year_value.isdigit() else None,
            tag="" if self.tag_var.get() == "全部标签" else self.tag_var.get(),
            search_text=self.search_var.get(),
        )
        self.entries_by_native_id = {
            entry.native_id: entry for entry in self.library.list_entries(item_kind="")
        }
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for candidate in self.candidates:
            self.tree.insert(
                "",
                "end",
                iid=candidate.candidate_id,
                values=(
                    _STATUS_LABELS[candidate.research_status],
                    candidate.year,
                    candidate.volume_code,
                    candidate.title,
                    candidate.display_page,
                    candidate.printed_page_label or "—",
                    "、".join(candidate.search_queries),
                    "、".join(candidate.tags),
                    candidate.block_count,
                ),
            )
        summary = self.service.summary()
        package_summary = self.package_service.summary()
        self.summary_label.configure(
            text=(
                f"当前显示 {len(self.candidates)} · 全部 {summary['total']} · "
                f"候选 {summary[CANDIDATE_STATUS]} · 相关 {summary[RELEVANT_STATUS]} · "
                f"排除 {summary[EXCLUDED_STATUS]} · MOFA研究工作包 {package_summary['total']}"
            )
        )
        self._clear_detail()

    def _candidate_for_id(self, candidate_id: str) -> MofaCandidate | None:
        return next((item for item in self.candidates if item.candidate_id == candidate_id), None)

    def _selected_candidate(self) -> MofaCandidate | None:
        selection = self.tree.selection()
        return self._candidate_for_id(selection[0]) if selection else None

    def _show_selected(self, _event=None) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._clear_detail()
            return
        self._detail_candidate_id = candidate.candidate_id
        self._detail_page_index = candidate.page_index
        self._detail_page_count = self.search_service.indexed_document_page_count(
            candidate.document_id,
            candidate.generation_id,
        )
        self._detail_highlight_terms = self.service.highlight_terms_for_candidate(
            candidate.candidate_id
        )
        self._render_detail_page()
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", candidate.notes)
        self.tags_var.set("，".join(candidate.tags))
        self.save_meta_btn.configure(state="normal")
        entry = self.entries_by_native_id.get(candidate.native_id)
        ready = bool(entry and os.path.isfile(entry.split_pdf_path))
        self.open_btn.configure(state="normal" if ready else "disabled")
        self.reveal_btn.configure(state="normal" if ready else "disabled")

    def _clear_detail(self) -> None:
        self._detail_candidate_id = ""
        self._detail_page_index = 0
        self._detail_page_count = 0
        self._detail_highlight_terms = ()
        self.detail_title.configure(text="选择一条候选史料查看详情")
        self.ocr_text.configure(state="normal")
        self.ocr_text.delete("1.0", "end")
        self.ocr_text.configure(state="disabled")
        self.notes_text.delete("1.0", "end")
        self.tags_var.set("")
        self.save_meta_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.reveal_btn.configure(state="disabled")
        self.ocr_page_label.configure(text="— / —")
        self.prev_ocr_page_btn.configure(state="disabled")
        self.next_ocr_page_btn.configure(state="disabled")
        self.jump_candidate_page_btn.configure(state="disabled")

    def _detail_candidate(self) -> MofaCandidate | None:
        return self._candidate_for_id(self._detail_candidate_id)

    def _insert_highlighted_ocr(self, text: str) -> None:
        ranges = self.search_service.normalized_match_ranges(
            text,
            self._detail_highlight_terms,
        )
        cursor = 0
        for start, end in ranges:
            if start > cursor:
                self.ocr_text.insert("end", text[cursor:start])
            self.ocr_text.insert("end", text[start:end], ("query_match",))
            cursor = end
        if cursor < len(text):
            self.ocr_text.insert("end", text[cursor:])

    def _render_detail_page(self) -> None:
        candidate = self._detail_candidate()
        if candidate is None:
            return
        page = self.search_service.get_indexed_page(
            candidate.document_id,
            candidate.generation_id,
            self._detail_page_index,
        )
        self.ocr_text.configure(state="normal")
        self.ocr_text.delete("1.0", "end")
        if page is None:
            self.ocr_text.insert(
                "1.0",
                "当前页没有可用的 active Generation OCR 文本。",
                ("section_heading",),
            )
            self.detail_title.configure(text=f"{candidate.title} · OCR 页不存在")
        else:
            relation = "候选命中页" if page.page_index == candidate.page_index else "上下文页"
            offset = page.page_index - candidate.page_index
            offset_label = "" if offset == 0 else f" · 距命中页 {offset:+d}"
            region = {"right": "右半页", "left": "左半页", "full": "整页"}.get(
                page.source_region,
                page.source_region or "未知区域",
            )
            self.detail_title.configure(
                text=(
                    f"{candidate.title} · {relation}{offset_label} · "
                    f"single-pages 第 {page.display_page} 页 · "
                    f"原PDF第 {page.source_pdf_page or '—'}页/{region}"
                )
            )
            queries = "、".join(candidate.search_queries) or "—"
            self.ocr_text.insert(
                "end",
                f"—— {relation} · 召回关键词：{queries} ——\n",
                ("section_heading",),
            )
            self._insert_highlighted_ocr(page.raw_text)
        self.ocr_text.configure(state="disabled")
        self.ocr_text.see("1.0")
        self.ocr_page_label.configure(
            text=(
                f"{self._detail_page_index + 1} / {self._detail_page_count}"
                if self._detail_page_count
                else "— / —"
            )
        )
        self.prev_ocr_page_btn.configure(
            state="normal" if self._detail_page_index > 0 else "disabled"
        )
        self.next_ocr_page_btn.configure(
            state=(
                "normal"
                if self._detail_page_index + 1 < self._detail_page_count
                else "disabled"
            )
        )
        self.jump_candidate_page_btn.configure(
            state="normal" if self._detail_page_index != candidate.page_index else "disabled"
        )

    def _change_detail_page(self, delta: int) -> None:
        if not self._detail_candidate_id or not self._detail_page_count:
            return
        self._detail_page_index = max(
            0,
            min(
                self._detail_page_count - 1,
                self._detail_page_index + int(delta),
            ),
        )
        self._render_detail_page()

    def _jump_to_candidate_page(self) -> None:
        candidate = self._detail_candidate()
        if candidate is None:
            return
        self._detail_page_index = candidate.page_index
        self._render_detail_page()

    def _set_selected_status(self, status: str) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("请选择候选史料", "请先选择一条或多条候选记录。")
            return
        self.service.update_status(selected, status)
        self.refresh_candidates()

    def _show_context_menu(self, event) -> str:
        candidate_id = self.tree.identify_row(event.y)
        if not candidate_id:
            return "break"
        if candidate_id not in self.tree.selection():
            self.tree.selection_set(candidate_id)
        self.tree.focus(candidate_id)
        self._show_selected()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def remove_selected_candidates(self) -> None:
        selected = tuple(self.tree.selection())
        if not selected:
            messagebox.showwarning("请选择候选史料", "请先选择一条或多条候选记录。")
            return
        if not messagebox.askyesno(
            "确认取消候选",
            f"将选中的 {len(selected)} 条记录从 MOFA 候选清单撤回？\n\n"
            "候选备注、标签和候选召回关系将被删除；OCR、全文索引和原 PDF 不受影响。\n"
            "已加入研究工作包的记录会自动保留。",
            parent=self,
        ):
            return
        result = self.service.remove_candidates(selected)
        self.refresh_candidates()
        if result.protected:
            package_label = "、".join(result.package_ids)
            messagebox.showwarning(
                "候选撤回部分完成" if result.removed else "无法取消候选",
                f"已撤回 {result.removed} 条，保留 {result.protected} 条。\n"
                "保留记录已写入研究工作包，不能破坏溯源证据链。\n"
                f"工作包：{package_label}",
                parent=self,
            )
            return
        messagebox.showinfo(
            "已取消候选",
            f"已从 MOFA 候选清单撤回 {result.removed} 条记录。",
            parent=self,
        )

    def save_selected_metadata(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            return
        notes = self.notes_text.get("1.0", "end-1c")
        tags = [
            value.strip()
            for value in self.tags_var.get().replace("，", ",").split(",")
            if value.strip()
        ]
        self.service.update_notes(candidate.candidate_id, notes)
        self.service.set_tags(candidate.candidate_id, tags)
        self.refresh_candidates()
        if self.tree.exists(candidate.candidate_id):
            self.tree.selection_set(candidate.candidate_id)
            self.tree.focus(candidate.candidate_id)
            self._show_selected()
        messagebox.showinfo("候选史料已保存", "研究备注和标签已更新。")

    def open_selected_pdf(self) -> None:
        candidate = self._selected_candidate()
        entry = self.entries_by_native_id.get(candidate.native_id) if candidate else None
        if entry and os.path.isfile(entry.split_pdf_path):
            try:
                open_path_in_system(entry.split_pdf_path)
            except Exception as exc:
                messagebox.showerror("无法打开单页PDF", str(exc))

    def reveal_selected_pdf(self) -> None:
        candidate = self._selected_candidate()
        entry = self.entries_by_native_id.get(candidate.native_id) if candidate else None
        if entry and os.path.isfile(entry.split_pdf_path):
            try:
                reveal_file_in_folder(entry.split_pdf_path)
            except Exception as exc:
                messagebox.showerror("无法定位单页PDF", str(exc))

    def create_research_package(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning(
                "请选择 MOFA 候选页",
                "请先选择一条或多条候选记录，再创建 MOFA 研究工作包。",
            )
            return
        candidates = [
            candidate
            for candidate_id in selection
            if (candidate := self._candidate_for_id(candidate_id)) is not None
        ]
        if not candidates:
            return
        MofaResearchPackageCreateDialog(
            self,
            service=self.package_service,
            candidates=candidates,
            on_created=lambda _result: self.refresh_candidates(),
        )

    def show_research_packages(self) -> None:
        MofaResearchPackageManagerDialog(
            self,
            service=self.package_service,
            on_changed=self.refresh_candidates,
        )

    def show_saved_searches(self) -> None:
        searches = self.service.list_saved_searches()
        window = ctk.CTkToplevel(self)
        window.title("MOFA 已保存检索")
        window.geometry("860x480")
        window.minsize(680, 360)
        window.transient(self.winfo_toplevel())
        ctk.CTkLabel(
            window,
            text=f"共 {len(searches)} 组可复现检索条件",
            anchor="w",
            font=("PingFang SC", 14, "bold"),
        ).pack(fill="x", padx=16, pady=(16, 8))
        frame = ctk.CTkFrame(window, fg_color=Color.BG_CARD, corner_radius=10)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        columns = ("query", "mode", "expansion", "revision", "year", "volume", "results", "updated")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        labels = {
            "query": "检索词",
            "mode": "模式",
            "expansion": "扩展层",
            "revision": "词库版本",
            "year": "年份",
            "volume": "卷册",
            "results": "结果页",
            "updated": "最近使用",
        }
        widths = {
            "query": 210,
            "mode": 80,
            "expansion": 85,
            "revision": 75,
            "year": 65,
            "volume": 65,
            "results": 65,
            "updated": 170,
        }
        for key in columns:
            tree.heading(key, text=labels[key])
            tree.column(key, width=widths[key], stretch=key in {"query", "updated"})
        for search in searches:
            tree.insert(
                "",
                "end",
                values=(
                    search.query_text,
                    search.search_mode,
                    search.expansion_level,
                    f"r{search.lexicon_revision}" if search.lexicon_revision else "—",
                    search.year_filter or "全部",
                    search.volume_filter or "全部",
                    search.result_count,
                    search.last_used_at,
                ),
            )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
