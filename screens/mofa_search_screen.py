from __future__ import annotations

import os
import json
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk
from PIL import ImageDraw, ImageTk

from config.settings import Color
from services.mofa_candidate_service import MofaCandidateService
from services.mofa_fulltext_search_service import (
    SEARCH_MODE_ALL,
    SEARCH_MODE_ANY,
    SEARCH_MODE_PHRASE,
    MofaFullTextSearchService,
    MofaIndexBatchResult,
    MofaIndexResult,
    MofaSearchExecution,
    MofaSearchHit,
)
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService
from services.mofa_pdf_split_service import MofaPdfSplitService
from services.mofa_pdf_viewer_service import MofaPdfViewerService
from services.mofa_search_lexicon_service import (
    EXPANSION_CONCEPT,
    EXPANSION_EXACT,
    EXPANSION_GLYPH,
    EXPANSION_LABELS,
    EXPANSION_OCR,
)
from screens.mofa_lexicon_dialog import MofaLexiconDialog
from utils.open_path import open_path_in_system, reveal_file_in_folder


_MODE_LABELS = {
    "精确短语": SEARCH_MODE_PHRASE,
    "包含全部词": SEARCH_MODE_ALL,
    "包含任一词": SEARCH_MODE_ANY,
}
_EXPANSION_BY_LABEL = {
    "仅精确": EXPANSION_EXACT,
    "新旧字体": EXPANSION_GLYPH,
    "OCR容错": EXPANSION_OCR,
    "历史关联": EXPANSION_CONCEPT,
}


class MofaSearchScreen(ctk.CTkFrame):
    """Visual page-level MOFA search with an internal highlighted PDF viewer."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, fg_color=Color.TRANSPARENT, corner_radius=0, **kwargs)
        self.library = MofaLibraryService()
        self.search_service = MofaFullTextSearchService(
            project_root=self.library.project_root,
            db_service=self.library.db,
            library_service=self.library,
        )
        self.candidate_service = MofaCandidateService(db_service=self.library.db)
        self.entries_by_native_id: dict[str, MofaLibraryEntry] = {}
        self.hits: list[MofaSearchHit] = []
        self._last_execution: MofaSearchExecution | None = None
        self._pending_search_context: tuple[str, str, int | None, str] | None = None
        self._last_search_context: tuple[str, str, int | None, str] | None = None
        self._indexing = False
        self._searching = False
        self.viewer = MofaPdfViewerService()
        self._viewer_hit: MofaSearchHit | None = None
        self._viewer_page_index = 0
        self._viewer_page_count = 0
        self._viewer_zoom = 1.0
        self._viewer_photo: ImageTk.PhotoImage | None = None
        self._viewer_render_after: str | None = None
        self._viewer_split_ratio = 0.5
        self._zoom_anchor: tuple[float, float, float, float] | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=22, pady=(18, 10))
        ctk.CTkLabel(
            header,
            text="MOFA 全文检索",
            font=("PingFang SC", 24, "bold"),
            anchor="w",
        ).pack(side="left")
        self.index_btn = ctk.CTkButton(
            header,
            text="更新全文索引",
            width=120,
            command=self.update_index,
        )
        self.index_btn.pack(side="right")
        self.lexicon_btn = ctk.CTkButton(
            header,
            text="检索词库",
            width=96,
            command=self.open_lexicon_manager,
        )
        self.lexicon_btn.pack(side="right", padx=(0, 8))
        self.save_search_btn = ctk.CTkButton(
            header,
            text="保存当前检索",
            width=112,
            state="disabled",
            command=self.save_current_search,
        )
        self.save_search_btn.pack(side="right", padx=(0, 8))

        self.summary_label = ctk.CTkLabel(
            self,
            text="正在读取本地索引…",
            anchor="w",
            text_color=Color.TEXT_GRAY,
        )
        self.summary_label.pack(fill="x", padx=24, pady=(0, 10))

        search_bar = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        search_bar.pack(fill="x", padx=22, pady=(0, 10))
        self.query_var = tk.StringVar()
        self.query_entry = ctk.CTkEntry(
            search_bar,
            textvariable=self.query_var,
            placeholder_text="输入日文关键词；多个词可用空格分隔",
            height=36,
        )
        self.query_entry.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=10)
        self.query_entry.bind("<Return>", lambda _event: self.run_search())
        self.mode_var = tk.StringVar(value="精确短语")
        self.mode_menu = ctk.CTkOptionMenu(
            search_bar,
            values=list(_MODE_LABELS),
            variable=self.mode_var,
            width=116,
        )
        self.mode_menu.pack(side="left", padx=6, pady=10)
        self.expansion_var = tk.StringVar(value="仅精确")
        self.expansion_menu = ctk.CTkOptionMenu(
            search_bar,
            values=list(_EXPANSION_BY_LABEL),
            variable=self.expansion_var,
            width=108,
        )
        self.expansion_menu.pack(side="left", padx=6, pady=10)
        self.year_var = tk.StringVar(value="全部年份")
        self.year_menu = ctk.CTkOptionMenu(
            search_bar,
            values=["全部年份"],
            variable=self.year_var,
            width=100,
            command=self._year_changed,
        )
        self.year_menu.pack(side="left", padx=6, pady=10)
        self.volume_var = tk.StringVar(value="全部卷册")
        self.volume_menu = ctk.CTkOptionMenu(
            search_bar,
            values=["全部卷册"],
            variable=self.volume_var,
            width=100,
        )
        self.volume_menu.pack(side="left", padx=6, pady=10)
        self.search_btn = ctk.CTkButton(
            search_bar,
            text="检索",
            width=82,
            command=self.run_search,
        )
        self.search_btn.pack(side="left", padx=(6, 12), pady=10)

        body = tk.PanedWindow(
            self,
            orient="vertical",
            sashwidth=7,
            sashrelief="flat",
            borderwidth=0,
            bg=Color.BG_MAIN_DARK,
        )
        self.main_result_pane = body
        body.pack(fill="both", expand=True, padx=22, pady=(0, 18))

        result_frame = ctk.CTkFrame(body, fg_color=Color.BG_CARD, corner_radius=10)
        detail_frame = ctk.CTkFrame(body, fg_color=Color.BG_CARD, corner_radius=10)
        body.add(result_frame, minsize=150, height=230, stretch="never")
        body.add(detail_frame, minsize=380, stretch="always")

        style = ttk.Style()
        style.configure("MofaSearch.Treeview", rowheight=29, font=("PingFang SC", 11))
        style.configure("MofaSearch.Treeview.Heading", font=("PingFang SC", 11, "bold"))
        columns = ("year", "volume", "title", "page", "printed", "reason", "blocks", "snippet")
        self.tree = ttk.Treeview(
            result_frame,
            columns=columns,
            show="headings",
            style="MofaSearch.Treeview",
            selectmode="browse",
        )
        headings = {
            "year": "年份",
            "volume": "卷册",
            "title": "史料",
            "page": "PDF页",
            "printed": "印刷页码",
            "reason": "召回方式",
            "blocks": "命中块",
            "snippet": "命中内容",
        }
        widths = {
            "year": 62,
            "volume": 66,
            "title": 240,
            "page": 68,
            "printed": 86,
            "reason": 100,
            "blocks": 66,
            "snippet": 500,
        }
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=widths[key], minwidth=50, stretch=key in {"title", "snippet"})
        scrollbar = ttk.Scrollbar(result_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_hit)

        detail_actions = ctk.CTkFrame(detail_frame, fg_color=Color.TRANSPARENT)
        detail_actions.pack(fill="x", padx=10, pady=(8, 0))
        self.detail_title = ctk.CTkLabel(
            detail_actions,
            text="选择一条检索结果查看页级 OCR 文本",
            anchor="w",
            font=("PingFang SC", 12, "bold"),
        )
        self.detail_title.pack(side="left", fill="x", expand=True)
        self.reveal_btn = ctk.CTkButton(
            detail_actions,
            text="定位单页PDF",
            width=102,
            state="disabled",
            command=self.reveal_selected_pdf,
        )
        self.reveal_btn.pack(side="right", padx=(6, 0))
        self.open_btn = ctk.CTkButton(
            detail_actions,
            text="打开单页PDF",
            width=102,
            state="disabled",
            command=self.open_selected_pdf,
        )
        self.open_btn.pack(side="right", padx=(6, 0))
        self.add_all_candidates_btn = ctk.CTkButton(
            detail_actions,
            text="全部加入候选",
            width=108,
            state="disabled",
            command=self.add_all_candidates,
        )
        self.add_all_candidates_btn.pack(side="right", padx=(6, 0))
        self.add_candidate_btn = ctk.CTkButton(
            detail_actions,
            text="加入候选",
            width=88,
            state="disabled",
            command=self.add_selected_candidate,
        )
        self.add_candidate_btn.pack(side="right", padx=(6, 0))

        detail_body = tk.PanedWindow(
            detail_frame,
            orient="horizontal",
            sashwidth=7,
            sashrelief="flat",
            borderwidth=0,
            bg=Color.BG_MAIN_DARK,
        )
        self.reader_pane = detail_body
        detail_body.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        viewer_frame = ctk.CTkFrame(detail_body, fg_color=Color.BG_MAIN_DARK, corner_radius=8)
        text_frame = ctk.CTkFrame(detail_body, fg_color=Color.TRANSPARENT, corner_radius=0)
        detail_body.add(viewer_frame, minsize=440, stretch="always")
        detail_body.add(text_frame, minsize=280, width=360, stretch="never")

        viewer_toolbar = ctk.CTkFrame(viewer_frame, fg_color=Color.BG_CARD, corner_radius=0)
        viewer_toolbar.pack(fill="x")
        self.view_mode_var = tk.StringVar(value="单页视图")
        self.view_mode_menu = ctk.CTkOptionMenu(
            viewer_toolbar,
            values=["单页视图", "原始双页"],
            variable=self.view_mode_var,
            width=104,
            command=self._change_view_mode,
            state="disabled",
        )
        self.view_mode_menu.pack(side="left", padx=(8, 4), pady=6)
        self.prev_page_btn = ctk.CTkButton(
            viewer_toolbar,
            text="‹",
            width=34,
            state="disabled",
            command=lambda: self._change_viewer_page(-1),
        )
        self.prev_page_btn.pack(side="left", padx=3, pady=6)
        self.next_page_btn = ctk.CTkButton(
            viewer_toolbar,
            text="›",
            width=34,
            state="disabled",
            command=lambda: self._change_viewer_page(1),
        )
        self.next_page_btn.pack(side="left", padx=3, pady=6)
        self.page_label = ctk.CTkLabel(viewer_toolbar, text="— / —", width=74)
        self.page_label.pack(side="left", padx=4)
        ctk.CTkLabel(
            viewer_toolbar,
            text="左键拖动 · 滚轮缩放",
            text_color=Color.TEXT_GRAY,
            font=("PingFang SC", 10),
        ).pack(side="left", padx=8)
        self.zoom_out_btn = ctk.CTkButton(
            viewer_toolbar,
            text="−",
            width=34,
            state="disabled",
            command=lambda: self._change_zoom(-0.25),
        )
        self.zoom_out_btn.pack(side="right", padx=(3, 8), pady=6)
        self.zoom_label = ctk.CTkLabel(viewer_toolbar, text="适宽 100%", width=72)
        self.zoom_label.pack(side="right", padx=3)
        self.zoom_in_btn = ctk.CTkButton(
            viewer_toolbar,
            text="+",
            width=34,
            state="disabled",
            command=lambda: self._change_zoom(0.25),
        )
        self.zoom_in_btn.pack(side="right", padx=3, pady=6)
        self.jump_hit_btn = ctk.CTkButton(
            viewer_toolbar,
            text="返回命中页",
            width=88,
            state="disabled",
            command=self._jump_to_hit,
        )
        self.jump_hit_btn.pack(side="right", padx=3, pady=6)

        canvas_host = tk.Frame(viewer_frame, bg="#25272b", borderwidth=0)
        canvas_host.pack(fill="both", expand=True)
        self.pdf_canvas = tk.Canvas(
            canvas_host,
            bg="#25272b",
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        canvas_y = ttk.Scrollbar(canvas_host, orient="vertical", command=self.pdf_canvas.yview)
        canvas_x = ttk.Scrollbar(canvas_host, orient="horizontal", command=self.pdf_canvas.xview)
        self.pdf_canvas.configure(yscrollcommand=canvas_y.set, xscrollcommand=canvas_x.set)
        self.pdf_canvas.grid(row=0, column=0, sticky="nsew")
        canvas_y.grid(row=0, column=1, sticky="ns")
        canvas_x.grid(row=1, column=0, sticky="ew")
        canvas_host.grid_rowconfigure(0, weight=1)
        canvas_host.grid_columnconfigure(0, weight=1)
        self.pdf_canvas.bind("<Configure>", self._schedule_viewer_render)
        self.pdf_canvas.bind("<ButtonPress-1>", self._pan_start)
        self.pdf_canvas.bind("<B1-Motion>", self._pan_move)
        self.pdf_canvas.bind("<ButtonRelease-1>", self._pan_end)
        self.pdf_canvas.bind("<MouseWheel>", self._wheel_zoom)
        self.pdf_canvas.bind("<Button-4>", lambda event: self._wheel_zoom(event, direction=1))
        self.pdf_canvas.bind("<Button-5>", lambda event: self._wheel_zoom(event, direction=-1))

        self.detail_text = ctk.CTkTextbox(
            text_frame,
            wrap="word",
            font=("PingFang SC", 12),
        )
        self.detail_text.pack(fill="both", expand=True)
        self.detail_text.tag_config(
            "match_heading",
            foreground="#b35a00",
            underline=True,
        )
        self.detail_text.tag_config("match_block", background="#fff6cc")
        self.detail_text.tag_config(
            "query_match",
            background="#ffcc4d",
            foreground="#251a00",
            underline=True,
        )
        self.detail_text.tag_config(
            "section_heading",
            foreground="#6c6f78",
            underline=True,
        )
        text_widget = getattr(self.detail_text, "_textbox", self.detail_text)
        text_widget.bind("<Button-2>", self._show_ocr_context_menu)
        text_widget.bind("<Control-Button-1>", self._show_ocr_context_menu)
        self.detail_text.configure(state="disabled")
        self.after(180, self._place_reader_sashes)

    def on_show(self) -> None:
        self.refresh_index_summary()
        self._refresh_filters()
        self.after(80, self._place_reader_sashes)

    def _place_reader_sashes(self) -> None:
        try:
            outer_height = self.main_result_pane.winfo_height()
            if outer_height > 500:
                self.main_result_pane.sash_place(0, 0, min(250, round(outer_height * 0.30)))
            inner_width = self.reader_pane.winfo_width()
            if inner_width > 800:
                self.reader_pane.sash_place(0, round(inner_width * 0.72), 0)
        except tk.TclError:
            pass

    def _refresh_filters(self) -> None:
        years = ["全部年份", *(str(value) for value in self.library.available_years())]
        self.year_menu.configure(values=years)
        if self.year_var.get() not in years:
            self.year_var.set("全部年份")
        self._year_changed(self.year_var.get())

    def _year_changed(self, value: str) -> None:
        year = int(value) if value.isdigit() else None
        volumes = ["全部卷册", *self.library.available_volumes(year)]
        self.volume_menu.configure(values=volumes)
        if self.volume_var.get() not in volumes:
            self.volume_var.set("全部卷册")

    def refresh_index_summary(self) -> None:
        documents, pages, blocks = self.search_service.index_summary()
        revision = self.search_service.lexicon.current_revision()
        custom = sum(
            not rule.built_in for rule in self.search_service.lexicon.list_rules()
        )
        self.summary_label.configure(
            text=(
                f"当前索引：{documents} 份史料 · {pages} 页 · {blocks} 个可定位文本块。"
                f"检索词库 r{revision} · {custom} 条自定义规则。"
            )
        )

    def open_lexicon_manager(
        self,
        initial_source: str = "",
        category: str = "",
    ) -> None:
        MofaLexiconDialog(
            self,
            service=self.search_service.lexicon,
            on_changed=self.refresh_index_summary,
            initial_source=initial_source,
            initial_category=category,
        )

    def _show_ocr_context_menu(self, event):
        try:
            selected = self.detail_text.get("sel.first", "sel.last").strip()
        except (tk.TclError, ValueError):
            selected = ""
        if not selected:
            return
        if len(selected) > 80:
            selected = selected[:80]
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(
            label="添加为新旧字体规则…",
            command=lambda value=selected: self.open_lexicon_manager(value, "glyph"),
        )
        menu.add_command(
            label="添加为 OCR 混淆规则…",
            command=lambda value=selected: self.open_lexicon_manager(value, "ocr"),
        )
        menu.add_command(
            label="添加为历史术语…",
            command=lambda value=selected: self.open_lexicon_manager(value, "alias"),
        )
        menu.add_command(
            label="添加为关联概念…",
            command=lambda value=selected: self.open_lexicon_manager(value, "related"),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def update_index(self) -> None:
        if self._indexing or self._searching:
            return
        entries = [
            item for item in self.library.list_entries(item_kind="")
            if item.search_text_exists
        ]
        if not entries:
            messagebox.showwarning(
                "没有可索引资料",
                "请先在 MOFA史料库完成 MinerU 标准化。",
            )
            return
        self._indexing = True
        self._set_busy(True, "正在更新…")
        self.summary_label.configure(text=f"正在检查 {len(entries)} 份标准化史料…")
        threading.Thread(target=self._index_worker, args=(entries,), daemon=True).start()

    def _index_worker(self, entries: list[MofaLibraryEntry]) -> None:
        try:
            result = self.search_service.index_entries(
                entries,
                on_progress=lambda done, total, item: self.after(
                    0,
                    lambda d=done, t=total, value=item: self._index_progress(d, t, value),
                ),
            )
            self.after(0, lambda value=result: self._finish_index(value))
        except Exception as exc:
            self.after(0, lambda message=str(exc): self._finish_index(None, message))

    def _index_progress(self, done: int, total: int, result: MofaIndexResult) -> None:
        self.summary_label.configure(
            text=f"索引 {done}/{total} · {result.title} · {result.status}"
        )

    def _finish_index(
        self,
        result: MofaIndexBatchResult | None,
        error: str = "",
    ) -> None:
        self._indexing = False
        self._set_busy(False)
        self.refresh_index_summary()
        if error or result is None:
            messagebox.showerror("全文索引失败", error or "未知错误")
            return
        summary = (
            f"新建/更新 {result.indexed} · 已是最新版 {result.skipped} · "
            f"不可用 {result.unavailable} · 失败 {result.failed}"
        )
        problems = [item for item in result.results if item.status in {"unavailable", "failed"}]
        if problems:
            details = "\n".join(f"{item.native_id}：{item.message}" for item in problems[:10])
            messagebox.showwarning("全文索引完成（存在问题）", f"{summary}\n\n{details}")
        else:
            messagebox.showinfo("全文索引完成", summary)

    def run_search(self) -> None:
        if self._indexing or self._searching:
            return
        query = self.query_var.get().strip()
        if not query:
            messagebox.showwarning("请输入检索词", "请输入需要查找的日文关键词。")
            return
        self._searching = True
        self._set_busy(True, "检索中…")
        year_value = self.year_var.get()
        year = int(year_value) if year_value.isdigit() else None
        volume = self.volume_var.get()
        mode = _MODE_LABELS[self.mode_var.get()]
        volume_code = "" if volume == "全部卷册" else volume
        self._pending_search_context = (query, mode, year, volume_code)
        threading.Thread(
            target=self._search_worker,
            args=(
                query,
                mode,
                _EXPANSION_BY_LABEL[self.expansion_var.get()],
                year,
                volume_code,
            ),
            daemon=True,
        ).start()

    def _search_worker(
        self,
        query: str,
        mode: str,
        expansion_level: str,
        year: int | None,
        volume: str,
    ) -> None:
        try:
            execution = self.search_service.execute_search(
                query,
                mode=mode,
                expansion_level=expansion_level,
                year=year,
                volume_code=volume,
                limit=500,
            )
            self.after(0, lambda value=execution: self._finish_search(value))
        except Exception as exc:
            self.after(0, lambda message=str(exc): self._finish_search(None, message))

    def _finish_search(
        self,
        execution: MofaSearchExecution | None,
        error: str = "",
    ) -> None:
        self._searching = False
        self._set_busy(False)
        if error:
            self._pending_search_context = None
            messagebox.showerror("全文检索失败", error)
            return
        if execution is None:
            return
        self._last_execution = execution
        self._last_search_context = self._pending_search_context
        self._pending_search_context = None
        hits = list(execution.hits)
        self.hits = hits
        self.save_search_btn.configure(state="normal")
        self.add_all_candidates_btn.configure(state="normal" if hits else "disabled")
        self.entries_by_native_id = {
            item.native_id: item for item in self.library.list_entries(item_kind="")
        }
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for index, hit in enumerate(hits):
            self.tree.insert(
                "",
                "end",
                iid=f"hit-{index}",
                values=(
                    hit.year,
                    hit.volume_code,
                    hit.title,
                    hit.display_page,
                    hit.printed_page_label or "—",
                    hit.reason_label,
                    len(hit.matching_blocks),
                    hit.snippet,
                ),
            )
        expanded = [
            item.term
            for item in execution.plan.expanded_terms
            if item.category != "exact"
        ]
        expansion_note = (
            f" · 扩展 {len(expanded)} 个词：{'、'.join(expanded[:8])}"
            if expanded
            else " · 未产生额外扩展词"
        )
        self.summary_label.configure(
            text=(
                f"检索“{self.query_var.get().strip()}”得到 {len(hits)} 个命中页"
                f"（最多显示 500 条）· 词库 r{execution.plan.lexicon_revision}"
                f"{expansion_note}"
            )
        )
        if hits:
            self.tree.selection_set("hit-0")
            self.tree.focus("hit-0")
            self.tree.see("hit-0")
            self._show_selected_hit()
        else:
            self._clear_detail("没有找到匹配内容。可尝试“包含任一词”或缩短关键词。")

    def _set_busy(self, busy: bool, search_text: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.index_btn.configure(state=state)
        self.search_btn.configure(state=state, text=search_text or "检索")
        self.query_entry.configure(state=state)
        self.mode_menu.configure(state=state)
        self.expansion_menu.configure(state=state)
        self.year_menu.configure(state=state)
        self.volume_menu.configure(state=state)
        if busy:
            self.save_search_btn.configure(state="disabled")
            self.add_candidate_btn.configure(state="disabled")
            self.add_all_candidates_btn.configure(state="disabled")

    def _selected_hit(self) -> MofaSearchHit | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            index = int(selection[0].split("-", 1)[1])
            return self.hits[index]
        except (IndexError, ValueError):
            return None

    def _show_selected_hit(self, _event=None) -> None:
        hit = self._selected_hit()
        if hit is None:
            self._clear_detail("选择一条检索结果查看页级 OCR 文本。")
            return
        region = {"right": "右半页", "left": "左半页", "full": "整页"}.get(
            hit.source_region,
            hit.source_region or "未知",
        )
        self.detail_title.configure(
            text=(
                f"{hit.title} · single-pages 第 {hit.display_page} 页 · "
                f"原PDF第 {hit.source_pdf_page or '—'} 页/{region}"
            )
        )
        self._populate_detail_text(hit)
        entry = self.entries_by_native_id.get(hit.native_id)
        ready = bool(entry and os.path.isfile(entry.split_pdf_path))
        self.open_btn.configure(state="normal" if ready else "disabled")
        self.reveal_btn.configure(state="normal" if ready else "disabled")
        status = self.candidate_service.status_for_page(hit.document_id, hit.page_index)
        status_labels = {
            "candidate": "已在候选",
            "relevant": "已标相关",
            "excluded": "已排除",
        }
        self.add_candidate_btn.configure(
            state="normal",
            text=status_labels.get(status, "加入候选"),
        )
        self._load_viewer_hit(hit, entry)

    def _active_query_terms(self) -> tuple[str, ...]:
        hit = self._selected_hit()
        if hit and hit.matched_terms:
            return tuple(item.term for item in hit.matched_terms)
        query = self.query_var.get().strip()
        if not query:
            return ()
        if _MODE_LABELS.get(self.mode_var.get()) == SEARCH_MODE_PHRASE:
            return (query,)
        return tuple(value for value in query.split() if value)

    def _insert_highlighted_text(self, text: str, *, base_tag: str = "") -> None:
        ranges = self.search_service.normalized_match_ranges(
            text,
            self._active_query_terms(),
        )
        cursor = 0
        for start, end in ranges:
            if start > cursor:
                self.detail_text.insert(
                    "end",
                    text[cursor:start],
                    (base_tag,) if base_tag else (),
                )
            tags = tuple(tag for tag in (base_tag, "query_match") if tag)
            self.detail_text.insert("end", text[start:end], tags)
            cursor = end
        if cursor < len(text):
            self.detail_text.insert(
                "end",
                text[cursor:],
                (base_tag,) if base_tag else (),
            )

    def _populate_detail_text(self, hit: MofaSearchHit) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        if hit.matched_terms:
            explanations = "；".join(
                f"{item.label}：{item.term}（权重 {item.weight:g}）"
                for item in hit.matched_terms
            )
            self.detail_text.insert(
                "end",
                f"召回说明 · 词库 r{hit.lexicon_revision} · {explanations}\n\n",
                ("section_heading",),
            )
        for block in hit.matching_blocks:
            self.detail_text.insert(
                "end",
                f"[命中块 {block.block_order + 1} · {block.block_type} · bbox={block.bbox}]\n",
                ("match_heading",),
            )
            self._insert_highlighted_text(block.raw_text, base_tag="match_block")
            self.detail_text.insert("end", "\n\n")
        self.detail_text.insert("end", "—— 本页完整 OCR ——\n", ("section_heading",))
        self._insert_highlighted_text(hit.raw_text)
        self.detail_text.configure(state="disabled")
        self.detail_text.see("1.0")

    def _clear_detail(self, text: str) -> None:
        self.detail_title.configure(text=text)
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.reveal_btn.configure(state="disabled")
        self.add_candidate_btn.configure(state="disabled", text="加入候选")
        self.add_all_candidates_btn.configure(state="disabled")
        self._clear_viewer()

    def _current_search_context(self) -> tuple[str, str, int | None, str]:
        if self._last_execution is not None and self._last_search_context is not None:
            return self._last_search_context
        query = self.query_var.get().strip()
        mode = _MODE_LABELS[self.mode_var.get()]
        year_value = self.year_var.get()
        year = int(year_value) if year_value.isdigit() else None
        volume_value = self.volume_var.get()
        volume = "" if volume_value == "全部卷册" else volume_value
        return query, mode, year, volume

    def save_current_search(self) -> None:
        query, mode, year, volume = self._current_search_context()
        if not query:
            return
        level, revision, snapshot = self._current_expansion_context()
        saved = self.candidate_service.save_search(
            query,
            mode,
            year=year,
            volume_code=volume,
            result_count=len(self.hits),
            expansion_level=level,
            lexicon_revision=revision,
            expansion_snapshot=snapshot,
        )
        messagebox.showinfo(
            "检索条件已保存",
            f"检索词：{saved.query_text}\n模式：{saved.search_mode} / {EXPANSION_LABELS.get(saved.expansion_level, saved.expansion_level)}"
            f"\n词库版本：r{saved.lexicon_revision}\n结果页：{saved.result_count}",
        )

    def _current_expansion_context(self) -> tuple[str, int, dict]:
        execution = self._last_execution
        if execution is None:
            return _EXPANSION_BY_LABEL[self.expansion_var.get()], 0, {}
        return (
            execution.plan.expansion_level,
            execution.plan.lexicon_revision,
            execution.plan.snapshot(),
        )

    def add_selected_candidate(self) -> None:
        hit = self._selected_hit()
        if hit is None:
            return
        query, mode, year, volume = self._current_search_context()
        level, revision, snapshot = self._current_expansion_context()
        result = self.candidate_service.add_hits(
            [hit],
            query=query,
            mode=mode,
            year=year,
            volume_code=volume,
            result_count=len(self.hits),
            expansion_level=level,
            lexicon_revision=revision,
            expansion_snapshot=snapshot,
        )
        self.add_candidate_btn.configure(text="已在候选")
        action = "新增" if result.created else "合并检索来源"
        messagebox.showinfo(
            "候选史料已更新",
            f"{action}：{hit.title}\nPDF 第 {hit.display_page} 页",
        )

    def add_all_candidates(self) -> None:
        if not self.hits:
            return
        if not messagebox.askyesno(
            "确认批量加入候选",
            f"将当前显示的 {len(self.hits)} 个命中页加入候选清单。\n"
            "同一史料同一页面会自动合并，不会重复创建。是否继续？",
        ):
            return
        query, mode, year, volume = self._current_search_context()
        level, revision, snapshot = self._current_expansion_context()
        result = self.candidate_service.add_hits(
            self.hits,
            query=query,
            mode=mode,
            year=year,
            volume_code=volume,
            result_count=len(self.hits),
            expansion_level=level,
            lexicon_revision=revision,
            expansion_snapshot=snapshot,
        )
        self._show_selected_hit()
        messagebox.showinfo(
            "批量加入完成",
            f"处理 {result.total} 页 · 新增 {result.created} · 合并已有 {result.merged}",
        )

    @staticmethod
    def _read_split_ratio(entry: MofaLibraryEntry) -> float:
        path = MofaPdfSplitService.manifest_path_for_bundle(entry.bundle_dir)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = float(json.load(stream).get("settings", {}).get("split_ratio", 0.5))
            return max(0.05, min(0.95, value))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return 0.5

    def _load_viewer_hit(
        self,
        hit: MofaSearchHit,
        entry: MofaLibraryEntry | None,
    ) -> None:
        if entry is None or not os.path.isfile(entry.split_pdf_path):
            self._clear_viewer("对应 single-pages PDF 不存在")
            return
        self._viewer_hit = hit
        self._viewer_zoom = 1.0
        self._viewer_split_ratio = self._read_split_ratio(entry)
        self._jump_to_hit()
        for widget in (
            self.zoom_in_btn,
            self.zoom_out_btn,
            self.jump_hit_btn,
            self.view_mode_menu,
        ):
            widget.configure(state="normal")

    def _viewer_entry(self) -> MofaLibraryEntry | None:
        hit = self._viewer_hit
        return self.entries_by_native_id.get(hit.native_id) if hit else None

    def _is_original_view(self) -> bool:
        return self.view_mode_var.get() == "原始双页"

    def _viewer_path(self) -> str:
        entry = self._viewer_entry()
        if entry is None:
            return ""
        return entry.pdf_path if self._is_original_view() else entry.split_pdf_path

    def _hit_page_for_current_view(self) -> int:
        hit = self._viewer_hit
        if hit is None:
            return 0
        if self._is_original_view() and hit.source_pdf_page:
            return hit.source_pdf_page - 1
        return hit.page_index

    def _change_view_mode(self, _value: str) -> None:
        if self._viewer_hit is None:
            return
        path = self._viewer_path()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("PDF不存在", "当前视图对应的 PDF 文件不存在。")
            self.view_mode_var.set("单页视图")
        self._viewer_zoom = 1.0
        self._jump_to_hit()

    def _jump_to_hit(self) -> None:
        if self._viewer_hit is None:
            return
        self._viewer_page_index = self._hit_page_for_current_view()
        self._zoom_anchor = None
        self._schedule_viewer_render()

    def _change_viewer_page(self, delta: int) -> None:
        if self._viewer_hit is None:
            return
        self._viewer_page_index = max(
            0,
            min(max(0, self._viewer_page_count - 1), self._viewer_page_index + int(delta)),
        )
        self._zoom_anchor = None
        self._schedule_viewer_render()

    def _change_zoom(self, delta: float, *, anchor_x: float | None = None, anchor_y: float | None = None) -> None:
        if self._viewer_hit is None:
            return
        if anchor_x is None:
            anchor_x = self.pdf_canvas.winfo_width() / 2
        if anchor_y is None:
            anchor_y = self.pdf_canvas.winfo_height() / 2
        self._capture_zoom_anchor(anchor_x, anchor_y)
        self._viewer_zoom = self.viewer.clamp_zoom(self._viewer_zoom + float(delta))
        self._schedule_viewer_render()

    def _capture_zoom_anchor(self, canvas_x: float, canvas_y: float) -> None:
        try:
            values = [float(value) for value in str(self.pdf_canvas.cget("scrollregion")).split()]
        except (TypeError, ValueError):
            values = []
        if len(values) != 4:
            self._zoom_anchor = None
            return
        content_width = max(1.0, values[2] - values[0])
        content_height = max(1.0, values[3] - values[1])
        fraction_x = self.pdf_canvas.canvasx(canvas_x) / content_width
        fraction_y = self.pdf_canvas.canvasy(canvas_y) / content_height
        self._zoom_anchor = (canvas_x, canvas_y, fraction_x, fraction_y)

    def _wheel_zoom(self, event, *, direction: int | None = None):
        if self._viewer_hit is None:
            return "break"
        if direction is None:
            delta = float(getattr(event, "delta", 0) or 0)
            direction = 1 if delta > 0 else -1 if delta < 0 else 0
        if direction:
            self._change_zoom(
                0.15 * direction,
                anchor_x=float(event.x),
                anchor_y=float(event.y),
            )
        return "break"

    def _pan_start(self, event) -> None:
        self.pdf_canvas.scan_mark(event.x, event.y)
        self.pdf_canvas.configure(cursor="fleur")

    def _pan_move(self, event) -> None:
        self.pdf_canvas.scan_dragto(event.x, event.y, gain=1)

    def _pan_end(self, _event) -> None:
        self.pdf_canvas.configure(cursor="hand2")

    def _schedule_viewer_render(self, _event=None) -> None:
        if self._viewer_render_after:
            try:
                self.after_cancel(self._viewer_render_after)
            except (tk.TclError, ValueError):
                pass
        self._viewer_render_after = self.after(90, self._render_viewer)

    def _render_viewer(self) -> None:
        self._viewer_render_after = None
        hit = self._viewer_hit
        path = self._viewer_path()
        if hit is None or not path or not os.path.isfile(path):
            self._clear_viewer("PDF 文件不存在")
            return
        width = max(120, self.pdf_canvas.winfo_width())
        height = max(120, self.pdf_canvas.winfo_height())
        try:
            rendered = self.viewer.render_page(
                path,
                self._viewer_page_index,
                width,
                height,
                zoom=self._viewer_zoom,
                fit_mode="width",
                supersample=2.0,
            )
        except Exception as exc:
            self._clear_viewer(f"PDF 页面渲染失败：{exc}")
            return
        self._viewer_page_count = rendered.page_count
        self._viewer_page_index = max(
            0,
            min(self._viewer_page_index, max(0, rendered.page_count - 1)),
        )
        display_image = rendered.image.copy()
        image_width, image_height = display_image.size
        target_page = self._hit_page_for_current_view()
        if self._viewer_page_index == target_page:
            overlay = ImageDraw.Draw(display_image, "RGBA")
            outline_width = max(2, round(image_width / 700))
            for block in hit.matching_blocks:
                if block.bbox is None:
                    continue
                x0, y0, x1, y1 = self.viewer.bbox_on_canvas(
                    block.bbox,
                    image_width=image_width,
                    image_height=image_height,
                    offset_x=0,
                    offset_y=0,
                    source_region=hit.source_region,
                    original_view=self._is_original_view(),
                    split_ratio=self._viewer_split_ratio,
                )
                overlay.rectangle(
                    (x0, y0, x1, y1),
                    fill=(255, 211, 48, 46),
                    outline=(255, 132, 0, 220),
                    width=outline_width,
                )
        self._viewer_photo = ImageTk.PhotoImage(display_image)
        offset_x = max(12.0, (width - image_width) / 2.0)
        offset_y = max(12.0, (height - image_height) / 2.0)
        content_width = max(float(width), offset_x + image_width + 12.0)
        content_height = max(float(height), offset_y + image_height + 12.0)
        self.pdf_canvas.delete("all")
        self.pdf_canvas.create_image(
            offset_x,
            offset_y,
            image=self._viewer_photo,
            anchor="nw",
            tags=("pdf-page",),
        )
        self.pdf_canvas.configure(scrollregion=(0, 0, content_width, content_height))
        if self._zoom_anchor is None:
            self.pdf_canvas.xview_moveto(0)
            self.pdf_canvas.yview_moveto(0)
        else:
            anchor_x, anchor_y, fraction_x, fraction_y = self._zoom_anchor
            max_left = max(0.0, content_width - width)
            max_top = max(0.0, content_height - height)
            desired_left = max(0.0, min(max_left, fraction_x * content_width - anchor_x))
            desired_top = max(0.0, min(max_top, fraction_y * content_height - anchor_y))
            self.pdf_canvas.xview_moveto(desired_left / max(1.0, content_width))
            self.pdf_canvas.yview_moveto(desired_top / max(1.0, content_height))
            self._zoom_anchor = None
        self.page_label.configure(
            text=f"{self._viewer_page_index + 1} / {rendered.page_count}"
        )
        self.zoom_label.configure(text=f"适宽 {round(self._viewer_zoom * 100):d}%")
        self.prev_page_btn.configure(
            state="normal" if self._viewer_page_index > 0 else "disabled"
        )
        self.next_page_btn.configure(
            state=(
                "normal"
                if self._viewer_page_index + 1 < rendered.page_count
                else "disabled"
            )
        )

    def _clear_viewer(self, message: str = "选择检索结果后在此显示 PDF 命中页") -> None:
        self._viewer_hit = None
        self._viewer_photo = None
        self._viewer_page_count = 0
        self._zoom_anchor = None
        self.pdf_canvas.delete("all")
        width = max(120, self.pdf_canvas.winfo_width())
        height = max(120, self.pdf_canvas.winfo_height())
        self.pdf_canvas.create_text(
            width / 2,
            height / 2,
            text=message,
            fill="#a5a9b2",
            font=("PingFang SC", 12),
            width=max(100, width - 40),
            justify="center",
        )
        self.page_label.configure(text="— / —")
        self.zoom_label.configure(text="适宽 100%")
        for widget in (
            self.prev_page_btn,
            self.next_page_btn,
            self.zoom_in_btn,
            self.zoom_out_btn,
            self.jump_hit_btn,
            self.view_mode_menu,
        ):
            widget.configure(state="disabled")

    def open_selected_pdf(self) -> None:
        hit = self._selected_hit()
        entry = self.entries_by_native_id.get(hit.native_id) if hit else None
        if entry and os.path.isfile(entry.split_pdf_path):
            try:
                open_path_in_system(entry.split_pdf_path)
            except Exception as exc:
                messagebox.showerror("无法打开单页PDF", str(exc))

    def reveal_selected_pdf(self) -> None:
        hit = self._selected_hit()
        entry = self.entries_by_native_id.get(hit.native_id) if hit else None
        if entry and os.path.isfile(entry.split_pdf_path):
            try:
                reveal_file_in_folder(entry.split_pdf_path)
            except Exception as exc:
                messagebox.showerror("无法定位单页PDF", str(exc))
