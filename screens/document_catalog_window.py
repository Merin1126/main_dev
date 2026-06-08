"""史料目录管理：SQLite 可视化、筛选排序、重命名与目录导出。"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from tkinter import messagebox

import customtkinter as ctk

from components.ui.button import Button
from config.settings import Color
from services.document_catalog_service import CatalogEntry, DocumentCatalogService
from utils.app_state import AppState
from utils.jacar_filename import parse_jacar_pdf_filename
from utils.open_path import open_path_in_system


class DocumentCatalogWindow(ctk.CTkToplevel):
    def __init__(self, master, project_root: str, **kwargs):
        super().__init__(master, fg_color=("#f5f6f8", "#1f1f23"), **kwargs)
        self.project_root = os.path.abspath(project_root)
        self.service = DocumentCatalogService(project_root=self.project_root)
        self.entries: list[CatalogEntry] = []
        self.filtered_entries: list[CatalogEntry] = []
        self.selected: CatalogEntry | None = None
        self.row_frames: dict[str, ctk.CTkFrame] = {}
        self.row_vars: dict[str, ctk.BooleanVar] = {}
        self.selection_state: dict[str, bool] = {}
        self.running = False

        self.search_var = ctk.StringVar()
        self.keyword_var = ctk.StringVar(value="全部")
        self.health_filter_var = ctk.StringVar(value="全部")
        self.only_abnormal_var = ctk.BooleanVar(value=False)
        self.sort_key_var = ctk.StringVar(value="keyword")
        self.sort_dir_var = ctk.StringVar(value="asc")
        self.export_scope_var = ctk.StringVar(value="filtered")

        self.title("史料目录管理")
        self.geometry("1180x780")
        self.minsize(960, 640)
        self.attributes("-topmost", True)

        self._build_ui()
        self._reload_list_async()

    def _build_ui(self) -> None:
        root = ctk.CTkFrame(self, corner_radius=10)
        root.pack(fill="both", expand=True, padx=12, pady=12)

        header = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            header,
            text="史料目录管理",
            font=("Arial", 22, "bold"),
            text_color=Color.TEXT,
        ).pack(side="left")
        Button(header, text="刷新列表", width=110, command=self._reload_list_async).pack(
            side="right", padx=(8, 0)
        )

        self.stats_label = ctk.CTkLabel(
            root,
            text="加载中…",
            font=("Arial", 12),
            text_color=Color.TEXT_MUTED,
            anchor="w",
        )
        self.stats_label.pack(fill="x", padx=14, pady=(0, 8))

        filter_bar = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        filter_bar.pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkLabel(filter_bar, text="搜索", width=40).pack(side="left")
        search_entry = ctk.CTkEntry(filter_bar, textvariable=self.search_var, width=220)
        search_entry.pack(side="left", padx=(4, 12))
        search_entry.bind("<Return>", lambda _e: self._apply_filters())

        ctk.CTkLabel(filter_bar, text="专题").pack(side="left")
        self.keyword_menu = ctk.CTkOptionMenu(
            filter_bar,
            variable=self.keyword_var,
            values=["全部"],
            width=140,
            command=lambda _v: self._apply_filters(),
        )
        self.keyword_menu.pack(side="left", padx=(4, 12))

        ctk.CTkLabel(filter_bar, text="排序").pack(side="left")
        ctk.CTkOptionMenu(
            filter_bar,
            variable=self.sort_key_var,
            values=["keyword", "title", "ref", "updated_at"],
            width=120,
            command=lambda _v: self._apply_filters(),
        ).pack(side="left", padx=(4, 4))
        ctk.CTkOptionMenu(
            filter_bar,
            variable=self.sort_dir_var,
            values=["asc", "desc"],
            width=80,
            command=lambda _v: self._apply_filters(),
        ).pack(side="left", padx=(0, 8))

        filter_bar2 = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        filter_bar2.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(filter_bar2, text="健康").pack(side="left")
        ctk.CTkOptionMenu(
            filter_bar2,
            variable=self.health_filter_var,
            values=["全部", "正常", "警告", "严重"],
            width=100,
            command=lambda _v: self._apply_filters(),
        ).pack(side="left", padx=(4, 12))
        ctk.CTkCheckBox(
            filter_bar2,
            text="仅异常",
            variable=self.only_abnormal_var,
            command=self._apply_filters,
        ).pack(side="left", padx=(0, 12))
        Button(filter_bar2, text="应用筛选", width=90, command=self._apply_filters).pack(side="left")

        select_bar = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        select_bar.pack(fill="x", padx=12, pady=(0, 4))
        Button(select_bar, text="全选当前列表", width=120, command=self._select_all_filtered).pack(
            side="left", padx=(0, 6)
        )
        Button(select_bar, text="取消全选", width=90, command=self._select_none).pack(side="left", padx=(0, 10))
        Button(
            select_bar,
            text="刷新选中健康状态",
            width=150,
            command=self._reprobe_selected_async,
        ).pack(side="left")
        self.selection_count_label = ctk.CTkLabel(
            select_bar,
            text="已勾选 0 条",
            font=("Arial", 12),
            text_color=Color.TEXT_MUTED,
        )
        self.selection_count_label.pack(side="left", padx=(12, 0))

        body = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        list_wrap = ctk.CTkFrame(body, corner_radius=10)
        list_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        list_header = ctk.CTkFrame(list_wrap, fg_color=Color.TRANSPARENT)
        list_header.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(
            list_header,
            text="　　状态　专题 / Ref　　　　　　标题",
            font=("Arial", 11, "bold"),
            text_color=Color.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x")
        self.list_frame = ctk.CTkScrollableFrame(
            list_wrap,
            fg_color=("#f2f4f8", "#252932"),
            corner_radius=8,
        )
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=8)

        detail_wrap = ctk.CTkFrame(body, corner_radius=10)
        detail_wrap.grid(row=0, column=1, sticky="nsew")
        self._build_detail_panel(detail_wrap)

        export_bar = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        export_bar.pack(fill="x", padx=12, pady=(4, 8))
        ctk.CTkLabel(export_bar, text="导出范围").pack(side="left")
        ctk.CTkRadioButton(
            export_bar,
            text="当前筛选",
            variable=self.export_scope_var,
            value="filtered",
        ).pack(side="left", padx=(8, 4))
        ctk.CTkRadioButton(
            export_bar,
            text="全部条目",
            variable=self.export_scope_var,
            value="all",
        ).pack(side="left", padx=(4, 4))
        ctk.CTkRadioButton(
            export_bar,
            text="已勾选",
            variable=self.export_scope_var,
            value="selected",
        ).pack(side="left", padx=(4, 12))
        Button(export_bar, text="导出 DOCX 目录", width=140, command=lambda: self._export("docx")).pack(
            side="left", padx=4
        )
        Button(export_bar, text="导出 PDF 目录", width=140, command=lambda: self._export("pdf")).pack(
            side="left", padx=4
        )
        Button(
            export_bar,
            text="导出 DOCX+PDF",
            width=150,
            fg_color=Color.BTN_SUCCESS_ALT,
            hover_color=Color.BTN_SUCCESS_ALT_HOVER,
            command=lambda: self._export("both"),
        ).pack(side="left", padx=4)

        self.status_label = ctk.CTkLabel(
            root,
            text="就绪",
            font=("Arial", 12),
            text_color=Color.TEXT_MUTED,
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=14, pady=(0, 10))

    def _build_detail_panel(self, parent: ctk.CTkFrame) -> None:
        scroll = ctk.CTkScrollableFrame(parent, fg_color=Color.TRANSPARENT)
        scroll.pack(fill="both", expand=True, padx=4, pady=4)
        pad = {"padx": 14, "pady": 6}

        ctk.CTkLabel(scroll, text="条目详情", font=("Arial", 16, "bold")).pack(anchor="w", **pad)
        self.detail_hint = ctk.CTkLabel(
            scroll,
            text="请在左侧选择一条史料。",
            font=("Arial", 12),
            text_color=Color.TEXT_MUTED,
            wraplength=360,
            justify="left",
        )
        self.detail_hint.pack(anchor="w", **pad)

        self.ref_label = ctk.CTkLabel(scroll, text="", anchor="w")
        self.ref_label.pack(anchor="w", **pad)

        ctk.CTkLabel(scroll, text="二级分类（level2）", anchor="w").pack(anchor="w", padx=14, pady=(6, 2))
        self.level2_entry = ctk.CTkEntry(scroll, height=32)
        self.level2_entry.pack(fill="x", padx=14)
        self.level2_entry.bind("<KeyRelease>", lambda _e: self._update_rename_preview())

        ctk.CTkLabel(scroll, text="标题（「」内）", anchor="w").pack(anchor="w", padx=14, pady=(6, 2))
        self.title_entry = ctk.CTkEntry(scroll, height=32)
        self.title_entry.pack(fill="x", padx=14)
        self.title_entry.bind("<KeyRelease>", lambda _e: self._update_rename_preview())

        ctk.CTkLabel(scroll, text="卷名（『』内）", anchor="w").pack(anchor="w", padx=14, pady=(6, 2))
        self.parent_entry = ctk.CTkEntry(scroll, height=32)
        self.parent_entry.pack(fill="x", padx=14)
        self.parent_entry.bind("<KeyRelease>", lambda _e: self._update_rename_preview())

        ctk.CTkLabel(scroll, text="馆藏（括号内）", anchor="w").pack(anchor="w", padx=14, pady=(6, 2))
        self.repo_entry = ctk.CTkEntry(scroll, height=32)
        self.repo_entry.pack(fill="x", padx=14)
        self.repo_entry.bind("<KeyRelease>", lambda _e: self._update_rename_preview())

        self.image_range_label = ctk.CTkLabel(
            scroll,
            text="画像范围：（只读，来自文件名）",
            anchor="w",
            justify="left",
            wraplength=360,
            font=("Arial", 11),
            text_color=Color.TEXT_MUTED,
        )
        self.image_range_label.pack(anchor="w", padx=14, pady=(4, 2))

        self.preview_label = ctk.CTkLabel(
            scroll,
            text="",
            anchor="w",
            justify="left",
            wraplength=360,
            text_color=("#64748b", "#94a3b8"),
        )
        self.preview_label.pack(anchor="w", padx=14, pady=6)

        self.meta_label = ctk.CTkLabel(
            scroll,
            text="",
            anchor="w",
            justify="left",
            wraplength=360,
            font=("Arial", 11),
        )
        self.meta_label.pack(anchor="w", padx=14)

        actions = ctk.CTkFrame(scroll, fg_color=Color.TRANSPARENT)
        actions.pack(fill="x", padx=12, pady=8)
        self.btn_rename = Button(
            actions,
            text="应用元数据重命名",
            width=120,
            command=self._apply_rename,
            state="disabled",
        )
        self.btn_rename.pack(fill="x", pady=4)
        Button(actions, text="在侧栏打开史料", width=120, command=self._open_in_sidebar).pack(fill="x", pady=4)
        Button(actions, text="复制 Ref", width=120, command=self._copy_ref).pack(fill="x", pady=4)
        Button(actions, text="打开 PDF 所在文件夹", width=120, command=self._open_pdf_folder).pack(
            fill="x", pady=4
        )

        preview_sec = ctk.CTkFrame(scroll, fg_color=Color.TRANSPARENT)
        preview_sec.pack(fill="x", padx=12, pady=(4, 2))
        ctk.CTkLabel(preview_sec, text="发布页预览", font=("Arial", 13, "bold")).pack(anchor="w")
        Button(
            preview_sec,
            text="生成发布页（快速）",
            command=lambda: self._generate_html_preview(enrich=False),
        ).pack(fill="x", pady=3)
        Button(
            preview_sec,
            text="生成发布页（富化本专题）",
            command=lambda: self._generate_html_preview(enrich=True),
        ).pack(fill="x", pady=3)
        Button(
            preview_sec,
            text="生成抽屉版发布页",
            command=lambda: self._generate_html_preview(enrich=False, release=True),
        ).pack(fill="x", pady=3)

        Button(actions, text="查看本条目变更记录", width=120, command=self._show_audit_log).pack(
            fill="x", pady=4
        )

        ctk.CTkLabel(
            scroll,
            text="提示：可编辑二级分类/标题/卷名/馆藏；Ref 与画像范围不可改。双击列表行可快速打开史料。",
            font=("Arial", 11),
            text_color=Color.TEXT_HINT_TUPLE,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(4, 10))

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        self.btn_rename.configure(state=state if self.selected else "disabled")

    def _reload_list_async(self) -> None:
        if self.running:
            return
        self.status_label.configure(text="正在加载数据库条目…")
        self._set_running(True)

        def _job():
            err = None
            entries: list[CatalogEntry] = []
            keywords: list[str] = []
            try:
                entries = self.service.fetch_entries(probe_health=True)
                keywords = self.service.list_keywords()
            except Exception as exc:
                err = exc
                traceback.print_exc()

            def _done():
                self._set_running(False)
                if err:
                    self.status_label.configure(text="加载失败")
                    messagebox.showerror("加载失败", str(err), parent=self)
                    return
                self.entries = entries
                kw_values = ["全部"] + keywords
                self.keyword_menu.configure(values=kw_values)
                self._apply_filters()
                self.status_label.configure(text=f"已加载 {len(self.entries)} 条 JACAR 记录")

            self.after(0, _done)

        threading.Thread(target=_job, daemon=True).start()

    def _apply_filters(self) -> None:
        needle = (self.search_var.get() or "").strip().lower()
        keyword_filter = self.keyword_var.get() or "全部"
        sort_key = self.sort_key_var.get()
        if sort_key not in ("keyword", "title", "ref", "updated_at"):
            sort_key = "keyword"
        sort_dir = self.sort_dir_var.get()
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"

        health_filter = self.health_filter_var.get() or "全部"
        only_abnormal = bool(self.only_abnormal_var.get())

        filtered: list[CatalogEntry] = []
        for entry in self.entries:
            if keyword_filter != "全部" and entry.search_keyword != keyword_filter:
                continue
            if needle and not DocumentCatalogService._matches_search(entry, needle):
                continue
            if not DocumentCatalogService.matches_health_filter(entry, health_filter):
                continue
            if only_abnormal and not entry.is_abnormal:
                continue
            filtered.append(entry)

        reverse = sort_dir == "desc"
        if sort_key == "title":
            filtered.sort(key=lambda e: (e.title.lower(), e.native_id), reverse=reverse)
        elif sort_key == "ref":
            filtered.sort(key=lambda e: e.native_id.upper(), reverse=reverse)
        elif sort_key == "updated_at":
            filtered.sort(key=lambda e: e.updated_at, reverse=reverse)
        else:
            filtered.sort(
                key=lambda e: (e.search_keyword, e.title.lower(), e.native_id),
                reverse=reverse,
            )

        self.filtered_entries = filtered
        self._render_list()
        self._update_stats_label()

    def _update_stats_label(self) -> None:
        tiers = DocumentCatalogService.count_health_tiers(self.entries)
        shown_tiers = DocumentCatalogService.count_health_tiers(self.filtered_entries)
        ok = sum(1 for e in self.filtered_entries if e.pdf_ok)
        abnormal = sum(1 for e in self.filtered_entries if e.is_abnormal)
        self.stats_label.configure(
            text=(
                f"显示 {len(self.filtered_entries)} / 共 {len(self.entries)} 条｜"
                f"PDF 可用 {ok} 条｜当前异常 {abnormal} 条\n"
                f"全库：🟢 {tiers['ok']}　🟡 {tiers['warn']}　🔴 {tiers['error']}　｜"
                f"当前列表：🟢 {shown_tiers['ok']}　🟡 {shown_tiers['warn']}　🔴 {shown_tiers['error']}"
            )
        )
        self._update_selection_count_label()

    def _update_selection_count_label(self) -> None:
        n = sum(1 for v in self.selection_state.values() if v)
        self.selection_count_label.configure(text=f"已勾选 {n} 条")

    def _render_list(self) -> None:
        prev_selected_id = self.selected.document_id if self.selected else None
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.row_frames.clear()
        self.row_vars.clear()

        if not self.filtered_entries:
            ctk.CTkLabel(
                self.list_frame,
                text="无匹配条目，请调整筛选条件。",
                text_color=Color.TEXT_MUTED,
            ).pack(anchor="w", padx=10, pady=10)
            self.selected = None
            self._clear_detail()
            return

        for entry in self.filtered_entries:
            row = ctk.CTkFrame(
                self.list_frame,
                fg_color=("#eef2f7", "#2a3140"),
                corner_radius=8,
            )
            row.pack(fill="x", padx=4, pady=3)

            checked = self.selection_state.get(entry.document_id, False)
            var = ctk.BooleanVar(value=checked)
            var.trace_add(
                "write",
                lambda *_a, doc_id=entry.document_id, v=var: self._on_row_checked(doc_id, v),
            )
            self.row_vars[entry.document_id] = var

            top = ctk.CTkFrame(row, fg_color=Color.TRANSPARENT)
            top.pack(fill="x", padx=8, pady=(6, 2))
            ctk.CTkCheckBox(top, text="", variable=var, width=24).pack(side="left", padx=(0, 4))

            tier = entry.health_tier()
            status_color = (
                Color.TEXT_SUCCESS
                if tier == "ok"
                else (Color.TEXT_WARNING if tier == "warn" else Color.RED)
            )
            ctk.CTkLabel(
                top,
                text=f"{entry.status_icon()} {entry.health_status_label()}",
                width=72,
                text_color=status_color,
                font=("Arial", 12, "bold"),
            ).pack(side="left")

            title_short = entry.title[:36] + ("…" if len(entry.title) > 36 else "")
            line1 = f"[{entry.search_keyword}] {title_short}"
            line2 = f"{entry.native_id}  {entry.health_note}"
            info_btn = ctk.CTkButton(
                top,
                text=f"{line1}\n{line2}",
                anchor="w",
                fg_color=Color.TRANSPARENT,
                hover_color=("#dbe8fb", "#2f4b75"),
                text_color=Color.TEXT,
                font=("Arial", 12),
                height=48,
                command=lambda e=entry: self._select_entry(e),
            )
            info_btn.pack(side="left", fill="x", expand=True)
            info_btn.bind("<Double-Button-1>", lambda _e, ent=entry: self._open_in_sidebar(ent, quiet=True))
            row.bind("<Double-Button-1>", lambda _e, ent=entry: self._open_in_sidebar(ent, quiet=True))

            self.row_frames[entry.document_id] = row

        if prev_selected_id:
            for entry in self.filtered_entries:
                if entry.document_id == prev_selected_id:
                    self._select_entry(entry)
                    break
            else:
                self.selected = None
                self._clear_detail()

    def _on_row_checked(self, document_id: str, var: ctk.BooleanVar) -> None:
        self.selection_state[document_id] = bool(var.get())
        self._update_selection_count_label()

    def _select_all_filtered(self) -> None:
        for entry in self.filtered_entries:
            self.selection_state[entry.document_id] = True
            if entry.document_id in self.row_vars:
                self.row_vars[entry.document_id].set(True)
        self._update_selection_count_label()

    def _select_none(self) -> None:
        for entry in self.filtered_entries:
            self.selection_state[entry.document_id] = False
            if entry.document_id in self.row_vars:
                self.row_vars[entry.document_id].set(False)
        self._update_selection_count_label()

    def _get_selected_entries(self) -> list[CatalogEntry]:
        by_id = {e.document_id: e for e in self.entries}
        selected: list[CatalogEntry] = []
        for doc_id, checked in self.selection_state.items():
            if checked and doc_id in by_id:
                selected.append(by_id[doc_id])
        return selected

    def _reprobe_selected_async(self) -> None:
        targets = self._get_selected_entries()
        if not targets:
            messagebox.showwarning("提示", "请先勾选要刷新的条目。", parent=self)
            return
        if self.running:
            return
        self.status_label.configure(text=f"正在刷新 {len(targets)} 条健康状态…")
        self._set_running(True)

        def _job():
            err = None
            try:
                self.service.reprobe_entries(targets)
            except Exception as exc:
                err = exc
                traceback.print_exc()

            def _done():
                self._set_running(False)
                if err:
                    messagebox.showerror("刷新失败", str(err), parent=self)
                    self.status_label.configure(text="刷新失败")
                    return
                self._apply_filters()
                self.status_label.configure(text=f"已刷新 {len(targets)} 条健康状态")

            self.after(0, _done)

        threading.Thread(target=_job, daemon=True).start()

    def _select_entry(self, entry: CatalogEntry) -> None:
        self.selected = entry
        for doc_id, frame in self.row_frames.items():
            if doc_id == entry.document_id:
                frame.configure(fg_color=("#dbe8fb", "#3a5f96"))
            else:
                frame.configure(fg_color=Color.TRANSPARENT)

        self.ref_label.configure(
            text=f"Ref: {entry.native_id}　专题: {entry.search_keyword}\n"
            f"状态: {entry.status}　页数: {entry.scale or '—'}"
        )
        for widget in (self.level2_entry, self.title_entry, self.parent_entry, self.repo_entry):
            widget.delete(0, "end")
        parts = None
        if entry.pdf_path and os.path.isfile(entry.pdf_path):
            parts = parse_jacar_pdf_filename(entry.pdf_path)
        if parts:
            self.level2_entry.insert(0, parts.level2)
            self.title_entry.insert(0, parts.title)
            self.parent_entry.insert(0, parts.parent)
            self.repo_entry.insert(0, parts.repo)
            self.image_range_label.configure(text=f"画像范围：{parts.image_range}（只读）")
            self.btn_rename.configure(state="normal")
        else:
            self.level2_entry.insert(0, entry.level2_name)
            self.title_entry.insert(0, entry.title)
            self.parent_entry.insert(0, entry.parent_name)
            self.repo_entry.insert(0, entry.repo_name)
            self.image_range_label.configure(text="画像范围：—（非标准文件名）")
            self.btn_rename.configure(state="disabled")
            self.preview_label.configure(text="非标准 PDF 文件名，请先在 Finder 中改为标准格式。")

        cache = []
        if entry.ocr_ok:
            cache.append("OCR")
        if entry.analysis_ok:
            cache.append("Analysis")
        if entry.translation_ok:
            cache.append("Translation")
        self.meta_label.configure(
            text=(
                f"健康: {entry.status_icon()} {entry.health_status_label()}\n"
                f"PDF: {'✓' if entry.pdf_ok else '✗'}  JSON: {'✓' if entry.sidecar_ok else '✗'}  "
                f"标准名: {'✓' if entry.standard_filename else '✗'}\n"
                f"缓存: {', '.join(cache) if cache else '无'}\n"
                f"{os.path.basename(entry.pdf_path) if entry.pdf_path else '（无路径）'}"
            )
        )
        self._update_rename_preview()

    def _clear_detail(self) -> None:
        self.detail_hint.configure(text="请在左侧选择一条史料。")
        self.ref_label.configure(text="")
        for widget in (self.level2_entry, self.title_entry, self.parent_entry, self.repo_entry):
            widget.delete(0, "end")
        self.image_range_label.configure(text="画像范围：（只读）")
        self.preview_label.configure(text="")
        self.meta_label.configure(text="")
        self.btn_rename.configure(state="disabled")

    def _update_rename_preview(self) -> None:
        if not self.selected or not self.selected.pdf_ok:
            return
        try:
            new_path, parts = self.service.rename_service.build_new_path_from_fields(
                self.selected.pdf_path,
                title=self.title_entry.get().strip(),
                level2=self.level2_entry.get().strip(),
                parent=self.parent_entry.get().strip(),
                repo=self.repo_entry.get().strip(),
            )
            preview = parts.build_pdf_filename()
            if os.path.basename(new_path) == os.path.basename(self.selected.pdf_path):
                preview += "\n（文件名不变，仅更新 JSON/SQL 元数据）"
            self.preview_label.configure(text=f"预览文件名：\n{preview}")
        except ValueError as exc:
            self.preview_label.configure(text=str(exc))

    def _apply_rename(self) -> None:
        if not self.selected:
            return
        if not messagebox.askyesno(
            "确认重命名",
            "将按标准文件名重命名 PDF，并同步 sidecar / 缓存 / Database_JSON / SQLite。\n"
            "变更将写入审计日志。\n\n是否继续？",
            parent=self,
        ):
            return
        entry = self.selected

        def _job():
            result = self.service.rename_entry_metadata(
                entry,
                title=self.title_entry.get().strip(),
                level2=self.level2_entry.get().strip(),
                parent=self.parent_entry.get().strip(),
                repo=self.repo_entry.get().strip(),
            )
            self.after(0, lambda: self._on_rename_done(entry.document_id, result))

        self.status_label.configure(text="正在重命名…")
        self._set_running(True)
        threading.Thread(target=_job, daemon=True).start()

    def _on_rename_done(self, document_id: str, result) -> None:
        self._set_running(False)
        if not result.success:
            messagebox.showerror("重命名失败", result.message, parent=self)
            self.status_label.configure(text="重命名失败")
            return
        messagebox.showinfo("重命名完成", result.message, parent=self)
        AppState().set_selected_pdf(result.new_pdf_path)
        self._reload_list_async()

    def _open_in_sidebar(self, entry: CatalogEntry | None = None, *, quiet: bool = False) -> None:
        target = entry or self.selected
        if not target or not target.pdf_ok:
            if not quiet:
                messagebox.showwarning("提示", "当前条目无可用 PDF。", parent=self)
            return
        AppState().set_selected_pdf(target.pdf_path)
        top = self.winfo_toplevel()
        if hasattr(top, "screen_manager"):
            top.screen_manager.change_screen("ocr")
        if not quiet:
            messagebox.showinfo("提示", "已在史料文件库选中该 PDF，并切换到「史料校对」。", parent=self)

    def _copy_ref(self) -> None:
        if not self.selected or not self.selected.native_id:
            return
        self.clipboard_clear()
        self.clipboard_append(self.selected.native_id)
        self.status_label.configure(text=f"已复制 Ref：{self.selected.native_id}")

    def _open_pdf_folder(self) -> None:
        if not self.selected or not self.selected.pdf_path:
            return
        folder = os.path.dirname(self.selected.pdf_path)
        if not os.path.isdir(folder):
            messagebox.showwarning("提示", "文件夹不存在。", parent=self)
            return
        try:
            open_path_in_system(folder)
        except Exception as exc:
            messagebox.showerror("错误", str(exc), parent=self)

    def _export(self, fmt: str) -> None:
        if self.running:
            return
        scope = self.export_scope_var.get()
        if scope == "all":
            to_export = self.entries
            summary = "全部 JACAR 条目"
        elif scope == "selected":
            to_export = self._get_selected_entries()
            summary = f"已勾选 {len(to_export)} 条"
        else:
            to_export = self.filtered_entries
            summary = (
                f"筛选：{self.search_var.get() or '无'} / {self.keyword_var.get()} / "
                f"{self.health_filter_var.get()}"
            )

        if not to_export:
            hint = "请先勾选要导出的条目。" if scope == "selected" else "没有可导出的条目。"
            messagebox.showwarning("提示", hint, parent=self)
            return

        def _job():
            err = None
            paths: dict[str, str] = {}
            try:
                paths = self.service.export_catalog(
                    to_export,
                    fmt=fmt,  # type: ignore[arg-type]
                    filter_summary=summary,
                )
            except Exception as exc:
                err = exc
                traceback.print_exc()

            def _done():
                self._set_running(False)
                if err:
                    messagebox.showerror("导出失败", str(err), parent=self)
                    self.status_label.configure(text="导出失败")
                    return
                lines = [f"{k.upper()}: {v}" for k, v in paths.items()]
                messagebox.showinfo("导出完成", "\n".join(lines), parent=self)
                self.status_label.configure(text="导出完成")

            self.after(0, _done)

        self.status_label.configure(text="正在导出目录…")
        self._set_running(True)
        threading.Thread(target=_job, daemon=True).start()

    def _generate_html_preview(self, *, enrich: bool, release: bool = False) -> None:
        if self.running:
            return
        enrich_cat = None
        if enrich and self.selected:
            enrich_cat = self.selected.search_keyword
        label = "富化发布页" if enrich else ("抽屉版" if release else "快速发布页")

        def _job():
            err = None
            out_path = ""
            try:
                out_path = self.service.generate_html_preview(
                    enrich=enrich,
                    enrich_cat=enrich_cat,
                    release=release,
                )
            except Exception as exc:
                err = exc
                traceback.print_exc()

            def _done():
                self._set_running(False)
                if err:
                    messagebox.showerror("生成失败", str(err), parent=self)
                    self.status_label.configure(text="发布页生成失败")
                    return
                try:
                    open_path_in_system(out_path)
                except Exception as exc:
                    messagebox.showwarning(
                        "已生成但无法自动打开",
                        f"{out_path}\n\n{exc}",
                        parent=self,
                    )
                else:
                    messagebox.showinfo(
                        "发布页已生成",
                        f"已在浏览器中打开：\n{out_path}",
                        parent=self,
                    )
                self.status_label.configure(text=f"{label} 已生成")

            self.after(0, _done)

        if enrich:
            if not messagebox.askyesno(
                "富化发布页",
                "将渲染当前专题 PDF 预览并拼装 OCR/Analysis（可能耗时较长）。\n\n是否继续？",
                parent=self,
            ):
                return
        self.status_label.configure(text=f"正在生成{label}…")
        self._set_running(True)
        threading.Thread(target=_job, daemon=True).start()

    def _show_audit_log(self) -> None:
        if not self.selected:
            messagebox.showwarning("提示", "请先选择一条史料。", parent=self)
            return
        text = self.service.format_audit_logs(self.selected, limit=40)
        win = ctk.CTkToplevel(self)
        win.title(f"变更记录 — {self.selected.native_id}")
        win.geometry("520x420")
        win.transient(self)
        ctk.CTkLabel(
            win,
            text=f"document_id: {self.selected.document_id}",
            font=("Arial", 11),
            text_color=Color.TEXT_MUTED,
        ).pack(anchor="w", padx=12, pady=(10, 4))
        box = ctk.CTkTextbox(win, font=("Menlo", 12) if sys.platform == "darwin" else ("Courier", 12))
        box.pack(fill="both", expand=True, padx=12, pady=8)
        box.insert("1.0", text)
        box.configure(state="disabled")
        Button(win, text="关闭", width=80, command=win.destroy).pack(pady=(0, 12))
