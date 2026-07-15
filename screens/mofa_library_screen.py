from __future__ import annotations

import os
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from config.settings import Color
from services.mofa_corpus_audit_service import (
    MofaCorpusAuditReport,
    MofaCorpusAuditService,
)
from services.mofa_fulltext_search_service import (
    MofaFullTextSearchService,
    MofaIndexBatchResult,
    MofaIndexResult,
)
from services.mofa_batch_download_service import (
    MofaBatchDownloadService,
    MofaBatchProgress,
    MofaBatchResult,
)
from services.mofa_library_service import (
    MofaFilenameNormalizationResult,
    MofaLibraryEntry,
    MofaLibraryService,
)
from services.mofa_mineru_import_service import (
    MofaMineruBatchResult,
    MofaMineruDirectoryWatcher,
    MofaMineruImportService,
)
from services.mofa_mineru_normalization_service import (
    MofaMineruNormalizationService,
    MofaNormalizationBatchResult,
    MofaNormalizationResult,
)
from services.mofa_pdf_split_service import MofaPdfSplitService
from utils.open_path import open_path_in_system, reveal_file_in_folder


_KIND_LABELS = {
    "正文": "content",
    "全部类型": "",
    "扉页/目录": "front_matter",
    "索引": "index",
    "奥付": "colophon",
}
_READINESS_VALUES = [
    "全部状态",
    "未下载",
    "待OCR",
    "OCR分段未完成",
    "待整理",
    "待生成检索文本",
    "可检索",
]


class MofaLibraryScreen(ctk.CTkFrame):
    """MOFA catalog inventory with local PDF/OCR/search readiness."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.BG_CONTENT, corner_radius=0, **kwargs)
        self.service = MofaLibraryService()
        self.entries: list[MofaLibraryEntry] = []
        self._syncing = False
        self.batch = MofaBatchDownloadService(db_service=self.service.db)
        self.splitter = MofaPdfSplitService()
        self._downloading = False
        self._splitting = False
        self._normalizing = False
        self._importing = False
        self._standardizing = False
        self._auditing = False
        self.mineru_importer = MofaMineruImportService(
            project_root=self.service.project_root,
            db_service=self.service.db,
            library_service=self.service,
        )
        self.mineru_watcher = MofaMineruDirectoryWatcher(self.mineru_importer)
        self.mineru_normalizer = MofaMineruNormalizationService(
            project_root=self.service.project_root,
            db_service=self.service.db,
            library_service=self.service,
        )
        self.corpus_auditor = MofaCorpusAuditService(
            project_root=self.service.project_root,
            db_service=self.service.db,
            library_service=self.service,
        )
        self.fulltext_indexer = MofaFullTextSearchService(
            project_root=self.service.project_root,
            db_service=self.service.db,
            library_service=self.service,
        )
        self._mineru_watch_dir = self.mineru_importer.get_watch_dir()
        self._context_native_id = ""
        self._build_ui()
        self.after(50, self.refresh_local)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=22, pady=(18, 8))
        ctk.CTkLabel(
            header,
            text="MOFA 史料库",
            font=("PingFang SC", 24, "bold"),
            text_color=Color.TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="《日本外交文書》1921—1927 本地索引",
            font=("PingFang SC", 13),
            text_color=Color.TEXT_GRAY,
        ).pack(side="left", padx=(14, 0), pady=(7, 0))

        actions = ctk.CTkFrame(header, fg_color=Color.TRANSPARENT)
        actions.pack(side="right")
        self.local_refresh_btn = ctk.CTkButton(
            actions,
            text="刷新本地状态",
            width=112,
            command=self.refresh_local,
        )
        self.local_refresh_btn.pack(side="left", padx=(0, 8))
        self.audit_btn = ctk.CTkButton(
            actions,
            text="验收当前范围",
            width=112,
            command=self.audit_current_filter,
        )
        self.audit_btn.pack(side="left", padx=(0, 8))
        self.normalize_names_btn = ctk.CTkButton(
            actions,
            text="规范化文件名",
            width=112,
            command=self.normalize_local_filenames,
        )
        self.normalize_names_btn.pack(side="left", padx=(0, 8))
        self.sync_btn = ctk.CTkButton(
            actions,
            text="同步官网目录",
            width=112,
            command=self.sync_official_catalog,
        )
        self.sync_btn.pack(side="left")

        self.summary_label = ctk.CTkLabel(
            self,
            text="正在读取本地目录缓存…",
            anchor="w",
            font=("PingFang SC", 13),
            text_color=Color.TEXT_GRAY,
        )
        self.summary_label.pack(fill="x", padx=24, pady=(0, 10))

        filters = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        filters.pack(fill="x", padx=22, pady=(0, 10))
        self.year_var = ctk.StringVar(value="全部年份")
        self.year_menu = ctk.CTkOptionMenu(
            filters,
            variable=self.year_var,
            values=["全部年份"],
            width=112,
            command=self._on_year_changed,
        )
        self.year_menu.pack(side="left", padx=(12, 6), pady=10)
        self.volume_var = ctk.StringVar(value="全部卷册")
        self.volume_menu = ctk.CTkOptionMenu(
            filters,
            variable=self.volume_var,
            values=["全部卷册"],
            width=112,
            command=lambda _value: self.refresh_local(),
        )
        self.volume_menu.pack(side="left", padx=6, pady=10)
        self.kind_var = ctk.StringVar(value="正文")
        self.kind_menu = ctk.CTkOptionMenu(
            filters,
            variable=self.kind_var,
            values=list(_KIND_LABELS),
            width=112,
            command=lambda _value: self.refresh_local(),
        )
        self.kind_menu.pack(side="left", padx=6, pady=10)
        self.readiness_var = ctk.StringVar(value="全部状态")
        self.readiness_menu = ctk.CTkOptionMenu(
            filters,
            variable=self.readiness_var,
            values=_READINESS_VALUES,
            width=142,
            command=lambda _value: self.refresh_local(),
        )
        self.readiness_menu.pack(side="left", padx=6, pady=10)
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            filters,
            textvariable=self.search_var,
            placeholder_text="搜索标题、卷名或 MOFA ID",
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(8, 12), pady=10)
        self.search_entry.bind("<Return>", lambda _event: self.refresh_local())

        download_bar = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        download_bar.pack(fill="x", padx=22, pady=(0, 10))
        self.download_selected_btn = ctk.CTkButton(
            download_bar,
            text="下载选中缺失项",
            width=132,
            command=self.download_selected,
        )
        self.download_selected_btn.pack(side="left", padx=(12, 6), pady=10)
        self.download_filtered_btn = ctk.CTkButton(
            download_bar,
            text="下载当前筛选缺失项",
            width=156,
            command=self.download_filtered,
        )
        self.download_filtered_btn.pack(side="left", padx=6, pady=10)
        self.split_selected_btn = ctk.CTkButton(
            download_bar,
            text="生成MinerU输入",
            width=118,
            command=self.split_selected_pdfs,
        )
        self.split_selected_btn.pack(side="left", padx=(14, 6), pady=10)
        self.pause_btn = ctk.CTkButton(
            download_bar,
            text="暂停队列",
            width=94,
            state="disabled",
            command=self.toggle_pause,
        )
        self.pause_btn.pack(side="left", padx=(18, 6), pady=10)
        self.stop_btn = ctk.CTkButton(
            download_bar,
            text="停止",
            width=76,
            state="disabled",
            fg_color=Color.RED,
            command=self.stop_download,
        )
        self.stop_btn.pack(side="left", padx=6, pady=10)
        self.download_progress = ctk.CTkProgressBar(download_bar, width=180)
        self.download_progress.set(0)
        self.download_progress.pack(side="right", padx=(8, 12), pady=10)
        self.download_status_label = ctk.CTkLabel(
            download_bar,
            text="可按 Command/Ctrl 或 Shift 多选；暂停在当前文件完成后生效。",
            font=("PingFang SC", 11),
            text_color=Color.TEXT_GRAY,
        )
        self.download_status_label.pack(side="right", fill="x", expand=True, padx=8, pady=10)

        mineru_bar = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        mineru_bar.pack(fill="x", padx=22, pady=(0, 10))
        self.choose_mineru_dir_btn = ctk.CTkButton(
            mineru_bar,
            text="选择 MinerU 结果目录",
            width=154,
            command=self.choose_mineru_result_dir,
        )
        self.choose_mineru_dir_btn.pack(side="left", padx=(12, 6), pady=10)
        self.import_mineru_btn = ctk.CTkButton(
            mineru_bar,
            text="立即扫描导入",
            width=118,
            command=self.import_mineru_results,
        )
        self.import_mineru_btn.pack(side="left", padx=6, pady=10)
        self.watch_mineru_btn = ctk.CTkButton(
            mineru_bar,
            text="开始监控",
            width=96,
            command=self.toggle_mineru_watcher,
        )
        self.watch_mineru_btn.pack(side="left", padx=6, pady=10)
        self.standardize_mineru_btn = ctk.CTkButton(
            mineru_bar,
            text="标准化当前范围",
            width=128,
            command=self.standardize_current_filter,
        )
        self.standardize_mineru_btn.pack(side="left", padx=6, pady=10)
        self.mineru_status_label = ctk.CTkLabel(
            mineru_bar,
            text=self._mineru_directory_text(),
            anchor="w",
            font=("PingFang SC", 11),
            text_color=Color.TEXT_GRAY,
        )
        self.mineru_status_label.pack(side="left", fill="x", expand=True, padx=(10, 12), pady=10)

        table_frame = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        table_frame.pack(fill="both", expand=True, padx=22, pady=(0, 10))
        style = ttk.Style()
        style.configure("Mofa.Treeview", rowheight=29, font=("PingFang SC", 11))
        style.configure("Mofa.Treeview.Heading", font=("PingFang SC", 11, "bold"))
        columns = (
            "year",
            "volume",
            "title",
            "pdf",
            "split",
            "raw",
            "imported",
            "search",
            "status",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            style="Mofa.Treeview",
            selectmode="extended",
        )
        headings = {
            "year": "年份",
            "volume": "卷册",
            "title": "目录事项",
            "pdf": "PDF",
            "split": "单页PDF",
            "raw": "MinerU原始",
            "imported": "已导入",
            "search": "检索文本",
            "status": "当前阶段",
        }
        widths = {
            "year": 64,
            "volume": 68,
            "title": 390,
            "pdf": 52,
            "split": 68,
            "raw": 82,
            "imported": 68,
            "search": 76,
            "status": 110,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=50,
                stretch=column == "title",
                anchor="w" if column == "title" else "center",
            )
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-2>", self._show_context_menu)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Control-Button-1>", self._show_context_menu)
        self._build_context_menu()

        detail_frame = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        detail_frame.pack(fill="x", padx=22, pady=(0, 14))
        self.detail_label = ctk.CTkLabel(
            detail_frame,
            text="选择条目后显示本地路径与官网地址。",
            anchor="w",
            justify="left",
            font=("PingFang SC", 11),
            text_color=Color.TEXT_GRAY,
        )
        self.detail_label.pack(side="left", fill="x", expand=True, padx=(2, 12))
        self.open_pdf_btn = ctk.CTkButton(
            detail_frame,
            text="打开史料",
            width=96,
            state="disabled",
            command=self.open_selected_pdf,
        )
        self.open_pdf_btn.pack(side="right", padx=(6, 0))
        self.reveal_pdf_btn = ctk.CTkButton(
            detail_frame,
            text="在文件夹中显示",
            width=122,
            state="disabled",
            command=self.reveal_selected_pdf,
        )
        self.reveal_pdf_btn.pack(side="right", padx=(6, 0))

    def on_show(self) -> None:
        """Refresh disk readiness whenever the route becomes visible."""
        if not self._syncing and not self._auditing:
            self.refresh_local()

    def destroy(self) -> None:
        self.mineru_watcher.stop()
        super().destroy()

    def _mineru_directory_text(self) -> str:
        if not self._mineru_watch_dir:
            return "尚未选择 MinerU 结果目录；导入采用复制归档，原始结果会保留。"
        return f"结果目录：{self._mineru_watch_dir}"

    def choose_mineru_result_dir(self) -> None:
        if self._importing or self._standardizing or self._auditing:
            return
        initial = self._mineru_watch_dir if os.path.isdir(self._mineru_watch_dir) else ""
        if not initial:
            suggested = os.path.expanduser("~/MinerU")
            initial = suggested if os.path.isdir(suggested) else os.path.expanduser("~")
        selected = filedialog.askdirectory(
            parent=self,
            title="选择 MinerU 桌面端结果目录",
            initialdir=initial,
            mustexist=True,
        )
        if not selected:
            return
        if self.mineru_watcher.active:
            self.mineru_watcher.stop()
        self._mineru_watch_dir = self.mineru_importer.set_watch_dir(selected)
        self.mineru_status_label.configure(text=self._mineru_directory_text())
        self._refresh_mineru_controls()

    def _refresh_mineru_controls(self) -> None:
        watching = self.mineru_watcher.active
        self.choose_mineru_dir_btn.configure(
            state="disabled" if self._importing or self._standardizing or self._auditing or watching else "normal"
        )
        self.import_mineru_btn.configure(
            state="disabled" if self._importing or self._standardizing or self._auditing or watching else "normal",
            text="扫描中…" if self._importing else "立即扫描导入",
        )
        self.watch_mineru_btn.configure(
            state="disabled" if self._importing or self._standardizing or self._auditing else "normal",
            text="停止监控" if watching else "开始监控",
            fg_color=Color.RED if watching else Color.PRIMARY,
        )
        self.standardize_mineru_btn.configure(
            state=(
                "disabled"
                if self._importing or self._standardizing or self._auditing or watching
                else "normal"
            ),
            text="标准化中…" if self._standardizing else "标准化当前范围",
        )

    def _require_mineru_directory(self) -> str:
        path = self._mineru_watch_dir
        if path and os.path.isdir(path):
            return path
        messagebox.showwarning(
            "请选择 MinerU 结果目录",
            "请先选择 MinerU 桌面端保存结果的上级目录。",
        )
        return ""

    def import_mineru_results(self) -> None:
        if self._importing:
            return
        if (
            self._syncing
            or self._downloading
            or self._splitting
            or self._normalizing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            messagebox.showinfo("当前任务进行中", "请等待当前 MOFA 任务结束后再导入 MinerU 结果。")
            return
        root = self._require_mineru_directory()
        if not root:
            return
        self._importing = True
        self._refresh_mineru_controls()
        self._set_split_controls(True)
        self.mineru_status_label.configure(text=f"正在扫描并校验：{root}")
        threading.Thread(target=self._mineru_import_worker, args=(root,), daemon=True).start()

    def _mineru_import_worker(self, root: str) -> None:
        try:
            result = self.mineru_importer.import_directory(root)
        except Exception as exc:
            result = MofaMineruBatchResult(())
            self.after(0, lambda message=str(exc): self._finish_mineru_import(result, message))
            return
        self.after(0, lambda value=result: self._finish_mineru_import(value))

    @staticmethod
    def _mineru_batch_text(result: MofaMineruBatchResult) -> str:
        return (
            f"发现 {result.total} · 导入 {result.imported} · 已存在 {result.skipped} · "
            f"未匹配 {result.unmatched} · 歧义 {result.ambiguous} · "
            f"无效 {result.invalid} · 失败 {result.failed}"
        )

    @staticmethod
    def _mineru_problem_details(result: MofaMineruBatchResult) -> str:
        problems = [
            item for item in result.results if item.status not in {"imported", "skipped"}
        ]
        return "\n".join(
            f"{os.path.basename(item.source_dir)}：{item.message}" for item in problems[:10]
        )

    def _finish_mineru_import(
        self,
        result: MofaMineruBatchResult,
        error: str = "",
    ) -> None:
        self._importing = False
        self._set_split_controls(False)
        self._refresh_mineru_controls()
        self.refresh_local()
        if error:
            self.mineru_status_label.configure(text="MinerU 结果扫描异常结束。")
            messagebox.showerror("MinerU 导入失败", error)
            return
        summary = self._mineru_batch_text(result)
        self.mineru_status_label.configure(text=summary)
        details = self._mineru_problem_details(result)
        if details:
            messagebox.showwarning("MinerU 导入完成（存在待确认项）", f"{summary}\n\n{details}")
        else:
            messagebox.showinfo("MinerU 导入完成", summary)

    def toggle_mineru_watcher(self) -> None:
        if self.mineru_watcher.active:
            self.mineru_watcher.stop()
            self.mineru_status_label.configure(
                text=f"目录监控已停止。{self._mineru_directory_text()}"
            )
            self._refresh_mineru_controls()
            return
        if self._standardizing or self._auditing:
            messagebox.showinfo("当前任务进行中", "请等待 MinerU 标准化结束后再开始监控。")
            return
        root = self._require_mineru_directory()
        if not root:
            return
        try:
            self.mineru_watcher.start(root, self._on_mineru_watch_result)
        except Exception as exc:
            messagebox.showerror("无法启动目录监控", str(exc))
            return
        self.mineru_status_label.configure(
            text="正在监控；结果目录连续两次保持不变后才会自动导入。"
        )
        self._refresh_mineru_controls()

    def _on_mineru_watch_result(self, result: MofaMineruBatchResult) -> None:
        self.after(0, lambda value=result: self._apply_mineru_watch_result(value))

    def _apply_mineru_watch_result(self, result: MofaMineruBatchResult) -> None:
        if result.imported:
            self.refresh_local()
        self.mineru_status_label.configure(
            text=f"监控运行中 · 最近一次：{self._mineru_batch_text(result)}"
        )

    def standardize_current_filter(self) -> None:
        if (
            self._syncing
            or self._downloading
            or self._splitting
            or self._normalizing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            messagebox.showinfo("当前任务进行中", "请等待当前 MOFA 任务结束后再标准化。")
            return
        if self.mineru_watcher.active:
            messagebox.showinfo("请先停止监控", "标准化期间需要固定 OCR run 集合，请先停止目录监控。")
            return
        targets = [
            entry
            for entry in self.entries
            if entry.split_pdf_exists
            and entry.mineru_expected_parts > 0
            and entry.mineru_archived_parts >= entry.mineru_expected_parts
        ]
        if not targets:
            messagebox.showwarning(
                "没有可标准化的史料",
                "当前筛选范围内没有已完成全部 MinerU 分段归档的史料。",
            )
            return
        if not messagebox.askyesno(
            "确认标准化 MinerU 结果",
            f"将检查并标准化当前范围内 {len(targets)} 份史料。\n\n"
            "重复 OCR 会选择与当前分段清单匹配的最新完整版本；旧版单段结果会兼容映射。\n"
            "原始 PDF、single-pages PDF 和 MinerU raw 均不会被修改。是否继续？",
        ):
            return
        self._standardizing = True
        self._refresh_mineru_controls()
        self._set_split_controls(True)
        self.download_progress.set(0)
        self.mineru_status_label.configure(text=f"正在标准化 {len(targets)} 份 MinerU 结果…")
        threading.Thread(
            target=self._standardize_worker,
            args=(targets,),
            daemon=True,
        ).start()

    def _standardize_worker(self, targets: list[MofaLibraryEntry]) -> None:
        try:
            result = self.mineru_normalizer.normalize_entries(
                targets,
                on_progress=lambda done, total, item: self.after(
                    0,
                    lambda: self._apply_standardize_progress(done, total, item),
                ),
            )
            self.after(0, lambda value=result: self._finish_standardize(value))
        except Exception as exc:
            self.after(
                0,
                lambda message=str(exc): self._finish_standardize(
                    MofaNormalizationBatchResult(()),
                    error=message,
                ),
            )

    def _apply_standardize_progress(
        self,
        done: int,
        total: int,
        result: MofaNormalizationResult,
    ) -> None:
        self.download_progress.set(done / max(1, total))
        self.mineru_status_label.configure(
            text=f"标准化 {done}/{total} · {result.title} · {result.status}"
        )

    def _finish_standardize(
        self,
        result: MofaNormalizationBatchResult,
        *,
        error: str = "",
    ) -> None:
        self._standardizing = False
        self._set_split_controls(False)
        self._refresh_mineru_controls()
        self.refresh_local()
        if error:
            self.mineru_status_label.configure(text="MinerU 标准化异常结束。")
            messagebox.showerror("MinerU 标准化失败", error)
            return
        summary = (
            f"标准化 {result.standardized} · 已是最新版 {result.skipped} · "
            f"不完整 {result.incomplete} · 失败 {result.failed}"
        )
        self.mineru_status_label.configure(text=summary)
        problems = [
            item
            for item in result.results
            if item.status not in {"standardized", "skipped"}
        ]
        details = "\n".join(
            f"{item.native_id}：{item.message}" for item in problems[:10]
        )
        if details:
            messagebox.showwarning("MinerU 标准化完成（存在未完成项）", f"{summary}\n\n{details}")
        else:
            messagebox.showinfo(
                "MinerU 标准化完成",
                f"{summary}\n\n标准化数据已写入 mineru/imported，分页检索文本已写入 search。",
            )

    def audit_current_filter(self) -> None:
        if (
            self._syncing
            or self._downloading
            or self._splitting
            or self._normalizing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            messagebox.showinfo("当前任务进行中", "请等待当前 MOFA 任务结束后再验收。")
            return
        if self.mineru_watcher.active:
            messagebox.showinfo("请先停止监控", "验收期间需要固定文件状态，请先停止 MinerU 目录监控。")
            return
        targets = list(self.entries)
        if not targets:
            messagebox.showwarning("当前范围为空", "当前筛选范围内没有可验收的 MOFA 条目。")
            return
        self._auditing = True
        self._set_split_controls(True)
        self._refresh_mineru_controls()
        self.download_progress.set(0)
        self.download_status_label.configure(text=f"正在验收 {len(targets)} 份 MOFA 史料…")
        scope = (
            f"{self.year_var.get()} / {self.volume_var.get()} / "
            f"{self.kind_var.get()} / {self.readiness_var.get()}"
        )
        threading.Thread(
            target=self._audit_worker,
            args=(targets, scope),
            daemon=True,
        ).start()

    def _audit_worker(self, targets: list[MofaLibraryEntry], scope: str) -> None:
        try:
            report = self.corpus_auditor.audit_entries(
                targets,
                scope_label=scope,
                on_progress=lambda done, total, entry: self.after(
                    0,
                    lambda d=done, t=total, value=entry: self._audit_progress(d, t, value),
                ),
            )
            self.after(0, lambda value=report: self._finish_audit(value))
        except Exception as exc:
            self.after(0, lambda message=str(exc): self._finish_audit(None, message))

    def _audit_progress(self, done: int, total: int, entry: MofaLibraryEntry) -> None:
        self.download_progress.set(done / max(1, total))
        self.download_status_label.configure(text=f"验收 {done}/{total} · {entry.title}")

    def _finish_audit(
        self,
        report: MofaCorpusAuditReport | None,
        error: str = "",
    ) -> None:
        self._auditing = False
        self._set_split_controls(False)
        self._refresh_mineru_controls()
        if error or report is None:
            self.download_status_label.configure(text="MOFA 全链路验收异常结束。")
            messagebox.showerror("MOFA 验收失败", error or "未知错误")
            return
        self.download_progress.set(1)
        self.download_status_label.configure(
            text=(
                f"验收完成 · 健康 {report.healthy_count}/{report.entry_count} · "
                f"问题条目 {report.problem_entry_count} · {report.duration_ms / 1000:.2f}s"
            )
        )
        self._show_audit_report(report)

    def _show_audit_report(self, report: MofaCorpusAuditReport) -> None:
        window = ctk.CTkToplevel(self)
        window.title("MOFA 真实语料验收报告")
        window.geometry("1080x680")
        window.minsize(820, 520)
        window.transient(self.winfo_toplevel())

        summary = ctk.CTkFrame(window, fg_color=Color.BG_CARD, corner_radius=10)
        summary.pack(fill="x", padx=16, pady=(16, 10))
        database_mb = report.database_size_bytes / 1024 / 1024
        ctk.CTkLabel(
            summary,
            text=(
                f"范围：{report.scope_label}\n"
                f"健康 {report.healthy_count}/{report.entry_count} · "
                f"问题条目 {report.problem_entry_count} · 问题记录 {report.issue_count} · "
                f"耗时 {report.duration_ms / 1000:.2f}s · SQLite {database_mb:.1f} MB\n"
                f"全库索引 {report.indexed_document_count} 份 / {report.indexed_page_count} 页 / "
                f"{report.indexed_block_count} 块 · 探针“共産党” {report.search_probe_hits} 页 / "
                f"{report.search_probe_ms:.3f}ms"
            ),
            anchor="w",
            justify="left",
            font=("PingFang SC", 13),
        ).pack(fill="x", padx=14, pady=12)

        actions = ctk.CTkFrame(window, fg_color=Color.TRANSPARENT)
        actions.pack(fill="x", padx=16, pady=(0, 10))
        standardize_ids = report.native_ids_for_action("standardize")
        reindex_ids = report.native_ids_for_action("reindex")
        split_ids = report.native_ids_for_action("split")
        ctk.CTkButton(
            actions,
            text=f"重新标准化 {len(standardize_ids)}",
            state="normal" if standardize_ids else "disabled",
            command=lambda: self._repair_audit_standardize(report, window),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text=f"重建索引 {len(reindex_ids)}",
            state="normal" if reindex_ids else "disabled",
            command=lambda: self._repair_audit_index(report, window),
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            actions,
            text=f"生成分页 {len(split_ids)}",
            state="normal" if split_ids else "disabled",
            command=lambda: self._repair_audit_split(report, window),
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            actions,
            text="关闭",
            fg_color=Color.TEXT_GRAY,
            command=window.destroy,
        ).pack(side="right")

        frame = ctk.CTkFrame(window, fg_color=Color.BG_CARD, corner_radius=10)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        columns = ("severity", "stage", "year", "title", "code", "repair", "message")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        labels = {
            "severity": "级别",
            "stage": "阶段",
            "year": "年份",
            "title": "史料",
            "code": "问题代码",
            "repair": "建议操作",
            "message": "说明",
        }
        widths = {
            "severity": 62,
            "stage": 82,
            "year": 58,
            "title": 230,
            "code": 190,
            "repair": 90,
            "message": 330,
        }
        entries = {item.native_id: item for item in self.service.list_entries(item_kind="")}
        for key in columns:
            tree.heading(key, text=labels[key])
            tree.column(key, width=widths[key], stretch=key in {"title", "message"})
        for index, issue in enumerate(report.issues):
            entry = entries.get(issue.native_id)
            tree.insert(
                "",
                "end",
                iid=f"issue-{index}",
                values=(
                    issue.severity,
                    issue.stage,
                    entry.year if entry else "—",
                    issue.title,
                    issue.code,
                    issue.repair_action or "人工检查",
                    issue.message,
                ),
            )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
        if not report.issues:
            tree.insert("", "end", values=("OK", "all", "—", "当前范围", "healthy", "无需操作", "全链路状态一致"))

    def _entries_for_audit_action(
        self,
        report: MofaCorpusAuditReport,
        action: str,
    ) -> list[MofaLibraryEntry]:
        wanted = set(report.native_ids_for_action(action))
        return [
            entry
            for entry in self.service.list_entries(item_kind="")
            if entry.native_id in wanted
        ]

    def _repair_audit_standardize(
        self,
        report: MofaCorpusAuditReport,
        window: ctk.CTkToplevel,
    ) -> None:
        targets = self._entries_for_audit_action(report, "standardize")
        if not targets:
            return
        if not messagebox.askyesno(
            "确认重新标准化",
            f"仅重新标准化验收报告中的 {len(targets)} 份失败或过期史料。是否继续？",
            parent=window,
        ):
            return
        window.destroy()
        self._standardizing = True
        self._set_split_controls(True)
        self._refresh_mineru_controls()
        self.download_progress.set(0)
        threading.Thread(
            target=self._repair_standardize_worker,
            args=(targets,),
            daemon=True,
        ).start()

    def _repair_standardize_worker(self, targets: list[MofaLibraryEntry]) -> None:
        try:
            result = self.mineru_normalizer.normalize_entries(
                targets,
                force=True,
                on_progress=lambda done, total, item: self.after(
                    0,
                    lambda d=done, t=total, value=item: self._apply_standardize_progress(
                        d, t, value
                    ),
                ),
            )
            self.after(0, lambda value=result: self._finish_standardize(value))
        except Exception as exc:
            self.after(
                0,
                lambda message=str(exc): self._finish_standardize(
                    MofaNormalizationBatchResult(()),
                    error=message,
                ),
            )

    def _repair_audit_index(
        self,
        report: MofaCorpusAuditReport,
        window: ctk.CTkToplevel,
    ) -> None:
        targets = self._entries_for_audit_action(report, "reindex")
        if not targets:
            return
        if not messagebox.askyesno(
            "确认重建全文索引",
            f"仅重建验收报告中的 {len(targets)} 份缺失或过期索引。是否继续？",
            parent=window,
        ):
            return
        window.destroy()
        self._auditing = True
        self._set_split_controls(True)
        self._refresh_mineru_controls()
        self.download_progress.set(0)
        threading.Thread(target=self._repair_index_worker, args=(targets,), daemon=True).start()

    def _repair_index_worker(self, targets: list[MofaLibraryEntry]) -> None:
        try:
            result = self.fulltext_indexer.index_entries(
                targets,
                force=True,
                on_progress=lambda done, total, item: self.after(
                    0,
                    lambda d=done, t=total, value=item: self._repair_index_progress(d, t, value),
                ),
            )
            self.after(0, lambda value=result: self._finish_repair_index(value))
        except Exception as exc:
            self.after(0, lambda message=str(exc): self._finish_repair_index(None, message))

    def _repair_index_progress(self, done: int, total: int, item: MofaIndexResult) -> None:
        self.download_progress.set(done / max(1, total))
        self.download_status_label.configure(text=f"索引修复 {done}/{total} · {item.title}")

    def _finish_repair_index(
        self,
        result: MofaIndexBatchResult | None,
        error: str = "",
    ) -> None:
        self._auditing = False
        self._set_split_controls(False)
        self._refresh_mineru_controls()
        self.refresh_local()
        if error or result is None:
            messagebox.showerror("索引修复失败", error or "未知错误")
            return
        messagebox.showinfo(
            "索引修复完成",
            f"更新 {result.indexed} · 跳过 {result.skipped} · 不可用 {result.unavailable} · 失败 {result.failed}",
        )

    def _repair_audit_split(
        self,
        report: MofaCorpusAuditReport,
        window: ctk.CTkToplevel,
    ) -> None:
        targets = self._entries_for_audit_action(report, "split")
        window.destroy()
        self._start_split(targets)

    def _selected_year(self) -> int | None:
        value = self.year_var.get()
        return int(value) if value.isdigit() else None

    def _on_year_changed(self, _value: str) -> None:
        year = self._selected_year()
        volumes = ["全部卷册", *self.service.available_volumes(year)]
        self.volume_menu.configure(values=volumes)
        self.volume_var.set("全部卷册")
        self.refresh_local()

    def _refresh_filter_options(self) -> None:
        years = ["全部年份", *(str(year) for year in self.service.available_years())]
        current_year = self.year_var.get()
        self.year_menu.configure(values=years)
        if current_year not in years:
            self.year_var.set("全部年份")
        volumes = ["全部卷册", *self.service.available_volumes(self._selected_year())]
        current_volume = self.volume_var.get()
        self.volume_menu.configure(values=volumes)
        if current_volume not in volumes:
            self.volume_var.set("全部卷册")

    def refresh_local(self) -> None:
        self._refresh_filter_options()
        volume = self.volume_var.get()
        readiness = self.readiness_var.get()
        self.entries = self.service.list_entries(
            year=self._selected_year(),
            volume_code="" if volume == "全部卷册" else volume,
            item_kind=_KIND_LABELS.get(self.kind_var.get(), "content"),
            search_text=self.search_var.get(),
            readiness="" if readiness == "全部状态" else readiness,
        )
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self._context_native_id = ""
        for entry in self.entries:
            split_text = (
                f"{entry.mineru_expected_parts}段"
                if entry.mineru_expected_parts > 1
                else ("✓" if entry.split_pdf_exists else "—")
            )
            raw_text = (
                f"{entry.mineru_archived_parts}/{entry.mineru_expected_parts}"
                if entry.mineru_expected_parts > 1
                else ("✓" if entry.mineru_raw_exists else "—")
            )
            self.tree.insert(
                "",
                "end",
                iid=entry.native_id,
                values=(
                    entry.year,
                    entry.volume_code,
                    entry.title,
                    "✓" if entry.pdf_exists else "—",
                    split_text,
                    raw_text,
                    "✓" if entry.mineru_imported_exists else "—",
                    "✓" if entry.search_text_exists else "—",
                    entry.readiness,
                ),
            )
        summary = self.service.summarize(self.entries)
        if summary.total:
            self.summary_label.configure(
                text=(
                    f"当前显示 {summary.total} 项　PDF {summary.pdf_ready}　"
                    f"单页PDF {summary.split_pdf_ready}　"
                    f"MinerU完整 {summary.mineru_raw_ready}　已导入 {summary.imported_ready}　"
                    f"可检索 {summary.searchable}"
                )
            )
        else:
            self.summary_label.configure(
                text="本地尚无 MOFA 目录缓存，请点击“同步官网目录”。"
            )
        self.detail_label.configure(text="选择条目后显示本地路径与官网地址。")
        self._update_action_buttons(None)

    def sync_official_catalog(self) -> None:
        if (
            self._syncing
            or self._downloading
            or self._splitting
            or self._normalizing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            return
        self._syncing = True
        self.sync_btn.configure(state="disabled", text="同步中…")
        self.summary_label.configure(text="正在读取 MOFA 1921—1927 官网目录，请稍候…")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self) -> None:
        try:
            count = self.service.sync_official_catalog()
        except Exception as exc:
            self.after(0, lambda message=str(exc): self._finish_sync(error=message))
            return
        self.after(0, lambda: self._finish_sync(count=count))

    def _finish_sync(self, *, count: int = 0, error: str = "") -> None:
        self._syncing = False
        self.sync_btn.configure(state="normal", text="同步官网目录")
        if error:
            self.summary_label.configure(text="MOFA 官网目录同步失败，本地缓存未受影响。")
            messagebox.showerror("MOFA 目录同步失败", error)
            return
        self.refresh_local()
        messagebox.showinfo("MOFA 目录同步完成", f"已缓存 {count} 个目录 PDF 条目。")

    def _on_selection(self, _event=None) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._update_action_buttons(None)
            return
        local = entry.pdf_path if entry.pdf_exists else f"未下载（规划目录：{entry.bundle_dir}）"
        self.detail_label.configure(
            text=(
                f"{entry.native_id}\n本地：{local}\n"
                f"MinerU分段：{entry.mineru_archived_parts}/{entry.mineru_expected_parts or 0}\n"
                f"官网：{entry.pdf_url}"
            )
        )
        self._update_action_buttons(entry)

    def _entry_for_id(self, native_id: str) -> MofaLibraryEntry | None:
        return next((entry for entry in self.entries if entry.native_id == native_id), None)

    def _selected_entry(self) -> MofaLibraryEntry | None:
        selected = self.tree.selection()
        if not selected:
            return None
        focused = self.tree.focus()
        native_id = focused if focused in selected else selected[0]
        return self._entry_for_id(native_id)

    def _action_entry(self, native_id: str = "") -> MofaLibraryEntry | None:
        return self._entry_for_id(native_id) if native_id else self._selected_entry()

    def _update_action_buttons(self, entry: MofaLibraryEntry | None) -> None:
        pdf_ready = bool(entry and os.path.isfile(entry.pdf_path))
        self.open_pdf_btn.configure(state="normal" if pdf_ready else "disabled")
        self.reveal_pdf_btn.configure(state="normal" if pdf_ready else "disabled")

    def _build_context_menu(self) -> None:
        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="打开史料", command=self.open_context_pdf)
        self.context_menu.add_command(label="在文件夹中显示", command=self.reveal_context_pdf)
        self.context_menu.add_command(label="打开分页文件夹", command=self.open_context_split_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="生成/更新 MinerU 输入", command=self.split_context_pdf)
        self.context_menu.add_command(label="打开 MinerU 单页PDF", command=self.open_context_split_pdf)
        self.context_menu.add_command(label="定位 MinerU 单页PDF", command=self.reveal_context_split_pdf)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="在 MOFA 官网打开", command=self.open_context_official)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="复制本地路径", command=self.copy_context_path)
        self.context_menu.add_command(label="复制 MOFA ID", command=self.copy_context_id)
        self.context_menu.add_command(label="复制官网链接", command=self.copy_context_url)

    def _show_context_menu(self, event) -> str:
        native_id = self.tree.identify_row(event.y)
        if not native_id:
            return "break"
        self._context_native_id = native_id
        self.tree.selection_set(native_id)
        self.tree.focus(native_id)
        entry = self._entry_for_id(native_id)
        self._on_selection()
        pdf_ready = bool(entry and os.path.isfile(entry.pdf_path))
        split_folder_ready = bool(
            entry and os.path.isdir(self._mineru_feed_folder(entry))
        )
        self.context_menu.entryconfigure("打开史料", state="normal" if pdf_ready else "disabled")
        self.context_menu.entryconfigure("在文件夹中显示", state="normal" if pdf_ready else "disabled")
        self.context_menu.entryconfigure(
            "打开分页文件夹",
            state="normal" if split_folder_ready else "disabled",
        )
        self.context_menu.entryconfigure(
            "生成/更新 MinerU 输入",
            state=(
                "normal"
                if pdf_ready
                and not self._downloading
                and not self._splitting
                and not self._normalizing
                and not self._importing
                and not self._standardizing
                and not self._auditing
                else "disabled"
            ),
        )
        split_ready = bool(entry and os.path.isfile(entry.split_pdf_path))
        self.context_menu.entryconfigure(
            "打开 MinerU 单页PDF", state="normal" if split_ready else "disabled"
        )
        self.context_menu.entryconfigure(
            "定位 MinerU 单页PDF", state="normal" if split_ready else "disabled"
        )
        self.context_menu.entryconfigure("复制本地路径", state="normal" if pdf_ready else "disabled")
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def _on_double_click(self, event) -> str:
        native_id = self.tree.identify_row(event.y)
        if native_id:
            self.tree.selection_set(native_id)
            self.tree.focus(native_id)
            self.open_selected_pdf()
        return "break"

    def _ensure_pdf(self, entry: MofaLibraryEntry | None) -> str:
        if entry is not None and os.path.isfile(entry.pdf_path):
            return entry.pdf_path
        messagebox.showwarning(
            "本地史料不存在",
            "该 PDF 尚未下载，或已在文件管理器中被移动/删除。请刷新本地状态。",
        )
        self.refresh_local()
        return ""

    def _run_open_action(self, action, *, error_title: str) -> None:
        try:
            action()
        except Exception as exc:
            messagebox.showerror(error_title, str(exc))

    def open_selected_pdf(self) -> None:
        self._open_pdf(self._selected_entry())

    def reveal_selected_pdf(self) -> None:
        self._reveal_pdf(self._selected_entry())

    def open_context_pdf(self) -> None:
        self._open_pdf(self._action_entry(self._context_native_id))

    def reveal_context_pdf(self) -> None:
        self._reveal_pdf(self._action_entry(self._context_native_id))

    def open_context_split_pdf(self) -> None:
        self._open_split_pdf(self._action_entry(self._context_native_id))

    def reveal_context_split_pdf(self) -> None:
        self._reveal_split_pdf(self._action_entry(self._context_native_id))

    def _open_split_pdf(self, entry: MofaLibraryEntry | None) -> None:
        if entry and os.path.isfile(entry.split_pdf_path):
            self._run_open_action(
                lambda: open_path_in_system(entry.split_pdf_path),
                error_title="无法打开单页PDF",
            )

    def _reveal_split_pdf(self, entry: MofaLibraryEntry | None) -> None:
        if entry and os.path.isfile(entry.split_pdf_path):
            self._run_open_action(
                lambda: reveal_file_in_folder(entry.split_pdf_path),
                error_title="无法定位单页PDF",
            )

    def _open_pdf(self, entry: MofaLibraryEntry | None) -> None:
        path = self._ensure_pdf(entry)
        if path:
            self._run_open_action(lambda: open_path_in_system(path), error_title="无法打开史料")

    def _reveal_pdf(self, entry: MofaLibraryEntry | None) -> None:
        path = self._ensure_pdf(entry)
        if path:
            self._run_open_action(lambda: reveal_file_in_folder(path), error_title="无法定位史料")

    @staticmethod
    def _mineru_feed_folder(entry: MofaLibraryEntry) -> str:
        input_dir = os.path.join(entry.bundle_dir, "mineru", "input")
        chunks_dir = os.path.join(input_dir, "chunks")
        if entry.mineru_expected_parts > 1 and os.path.isdir(chunks_dir):
            return chunks_dir
        return input_dir

    def open_context_split_folder(self) -> None:
        entry = self._action_entry(self._context_native_id)
        split_dir = self._mineru_feed_folder(entry) if entry is not None else ""
        if not split_dir or not os.path.isdir(split_dir):
            messagebox.showwarning(
                "分页文件夹不存在",
                "该史料尚未生成 MinerU 输入，请先执行“生成 MinerU 输入”。",
            )
            return
        self._run_open_action(
            lambda: open_path_in_system(split_dir),
            error_title="无法打开分页文件夹",
        )

    def open_context_official(self) -> None:
        entry = self._action_entry(self._context_native_id)
        if entry and entry.pdf_url:
            self._run_open_action(
                lambda: webbrowser.open(entry.pdf_url, new=2),
                error_title="无法打开 MOFA 官网",
            )

    def _copy_text(self, value: str, label: str) -> None:
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.download_status_label.configure(text=f"已复制{label}。")

    def copy_context_path(self) -> None:
        entry = self._action_entry(self._context_native_id)
        if entry and os.path.isfile(entry.pdf_path):
            self._copy_text(entry.pdf_path, "本地路径")

    def copy_context_id(self) -> None:
        entry = self._action_entry(self._context_native_id)
        if entry:
            self._copy_text(entry.native_id, "MOFA ID")

    def copy_context_url(self) -> None:
        entry = self._action_entry(self._context_native_id)
        if entry:
            self._copy_text(entry.pdf_url, "官网链接")

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB"):
            if amount < 1024 or unit == "GB":
                return f"{amount:.1f}{unit}"
            amount /= 1024
        return f"{amount:.1f}GB"

    def _missing_entries(self, values: list[MofaLibraryEntry]) -> list[MofaLibraryEntry]:
        seen: set[str] = set()
        result: list[MofaLibraryEntry] = []
        for entry in values:
            if entry.pdf_exists or entry.native_id in seen:
                continue
            seen.add(entry.native_id)
            result.append(entry)
        return result

    def download_selected(self) -> None:
        selected_ids = set(self.tree.selection())
        if not selected_ids:
            messagebox.showwarning("请选择史料", "请先在表格中选择一个或多个目录事项。")
            return
        targets = self._missing_entries(
            [entry for entry in self.entries if entry.native_id in selected_ids]
        )
        self._confirm_and_start(targets, scope_label="选中范围")

    def split_context_pdf(self) -> None:
        entry = self._action_entry(self._context_native_id)
        self._start_split([entry] if entry else [])

    def split_selected_pdfs(self) -> None:
        selected_ids = set(self.tree.selection())
        targets = [
            entry for entry in self.entries
            if entry.native_id in selected_ids and entry.pdf_exists
        ]
        self._start_split(targets)

    def _start_split(self, targets: list[MofaLibraryEntry]) -> None:
        if (
            self._downloading
            or self._splitting
            or self._syncing
            or self._normalizing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            messagebox.showinfo("当前任务进行中", "请等待当前 MOFA 任务结束后再拆页。")
            return
        if not targets:
            messagebox.showwarning("请选择史料", "请先选择一份或多份已下载的 MOFA PDF。")
            return
        existing = sum(entry.split_pdf_exists for entry in targets)
        if not messagebox.askyesno(
            "确认生成 MinerU 输入",
            f"将处理 {len(targets)} 份 PDF，日文书籍双页按“右页 → 左页”输出。\n\n"
            "单页 PDF 超过 200 页时，会额外生成最多 200 页一段的 MinerU 投喂文件。\n"
            f"已有单页产物：{existing}（后台校验后跳过或更新）\n"
            "原始史料 PDF 不会被修改。是否继续？",
        ):
            return
        self._splitting = True
        self._set_split_controls(True)
        self.download_progress.set(0)
        self.download_status_label.configure(text=f"正在拆分 {len(targets)} 份 MOFA PDF…")
        threading.Thread(target=self._split_worker, args=(targets,), daemon=True).start()

    def _set_split_controls(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.download_selected_btn.configure(state=state)
        self.download_filtered_btn.configure(state=state)
        self.split_selected_btn.configure(state=state)
        self.sync_btn.configure(state=state)
        self.local_refresh_btn.configure(state=state)
        self.audit_btn.configure(state=state, text="验收中…" if self._auditing else "验收当前范围")
        self.normalize_names_btn.configure(state=state)
        self.standardize_mineru_btn.configure(state=state)

    def normalize_local_filenames(self) -> None:
        if (
            self._downloading
            or self._splitting
            or self._syncing
            or self._normalizing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            messagebox.showinfo("当前任务进行中", "请等待当前 MOFA 任务结束后再规范化文件名。")
            return
        targets = [entry for entry in self.service.list_entries(item_kind="") if entry.pdf_exists]
        pending = [
            entry for entry in targets
            if self.service.filename_normalization_needed(entry)
        ]
        if not pending:
            messagebox.showinfo("无需迁移", "本地 MOFA PDF 已全部采用可读文件名。")
            return
        if not messagebox.askyesno(
            "确认规范化 MOFA 文件名",
            f"将原地重命名 {len(pending)} 份本地史料及已有 MinerU 单页 PDF。\n\n"
            "不会重新下载，也不会修改 PDF 内容；SQLite、manifest 与拆页清单会同步更新。是否继续？",
        ):
            return
        self._normalizing = True
        self._set_split_controls(True)
        self.download_progress.set(0)
        self.download_status_label.configure(text=f"正在规范化 {len(pending)} 份 MOFA 文件…")
        threading.Thread(
            target=self._normalize_names_worker,
            args=(pending,),
            daemon=True,
        ).start()

    def _normalize_names_worker(self, targets: list[MofaLibraryEntry]) -> None:
        try:
            result = self.service.normalize_local_filenames(
                targets,
                on_progress=lambda done, total, title: self.after(
                    0,
                    lambda: self._apply_normalize_progress(done, total, title),
                ),
            )
        except Exception as exc:
            result = MofaFilenameNormalizationResult(
                total=len(targets),
                renamed_pdfs=0,
                renamed_split_pdfs=0,
                unchanged=0,
                failed=len(targets),
                errors=(str(exc),),
            )
        self.after(0, lambda: self._finish_normalize_names(result))

    def _apply_normalize_progress(self, done: int, total: int, title: str) -> None:
        self.download_progress.set(done / max(1, total))
        self.download_status_label.configure(text=f"文件名迁移 {done}/{total} · {title}")

    def _finish_normalize_names(self, result: MofaFilenameNormalizationResult) -> None:
        self._normalizing = False
        self._set_split_controls(False)
        self.refresh_local()
        self.download_status_label.configure(
            text=(
                f"文件名迁移完成 · PDF {result.renamed_pdfs} · "
                f"单页PDF {result.renamed_split_pdfs} · 失败 {result.failed}"
            )
        )
        if result.errors:
            messagebox.showerror("部分文件名迁移失败", "\n".join(result.errors[:10]))
        else:
            messagebox.showinfo(
                "MOFA 文件名规范化完成",
                f"史料 PDF：{result.renamed_pdfs}\n"
                f"MinerU 单页 PDF：{result.renamed_split_pdfs}\n"
                f"无需修改：{result.unchanged}",
            )

    def _split_worker(self, targets: list[MofaLibraryEntry]) -> None:
        created = skipped = failed = 0
        errors: list[str] = []
        for index, entry in enumerate(targets, start=1):
            try:
                if self.splitter.is_current(entry.pdf_path, entry.bundle_dir):
                    skipped += 1
                elif self.splitter.single_page_is_current(entry.pdf_path, entry.bundle_dir):
                    self.splitter.ensure_chunks(entry.split_pdf_path, entry.bundle_dir)
                    created += 1
                else:
                    self.splitter.split(entry.pdf_path, entry.bundle_dir)
                    created += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{entry.native_id}: {exc}")
            self.after(
                0,
                lambda done=index, total=len(targets), title=entry.title: self._apply_split_progress(
                    done, total, title
                ),
            )
        self.after(
            0,
            lambda: self._finish_split(created, skipped, failed, errors),
        )

    def _apply_split_progress(self, done: int, total: int, title: str) -> None:
        self.download_progress.set(done / max(1, total))
        self.download_status_label.configure(text=f"拆页 {done}/{total} · {title}")

    def _finish_split(
        self, created: int, skipped: int, failed: int, errors: list[str]
    ) -> None:
        self._splitting = False
        self._set_split_controls(False)
        self.refresh_local()
        self.download_status_label.configure(
            text=f"MinerU输入完成 · 新生成/更新 {created} · 跳过 {skipped} · 失败 {failed}"
        )
        if errors:
            messagebox.showerror("部分 PDF 拆页失败", "\n".join(errors[:10]))
        else:
            messagebox.showinfo(
                "MOFA MinerU 输入生成完成",
                f"新生成/更新：{created}\n已是最新版：{skipped}\n"
                "完整单页 PDF、200 页分段与清单已写入各史料的 mineru/input/ 目录。",
            )

    def download_filtered(self) -> None:
        self._confirm_and_start(
            self._missing_entries(self.entries),
            scope_label="当前筛选范围",
        )

    def _confirm_and_start(self, targets: list[MofaLibraryEntry], *, scope_label: str) -> None:
        if (
            self._downloading
            or self._splitting
            or self._normalizing
            or self._syncing
            or self._importing
            or self._standardizing
            or self._auditing
        ):
            messagebox.showinfo("当前任务进行中", "请等待当前 MOFA 任务结束。")
            return
        if not targets:
            messagebox.showinfo("无需下载", f"{scope_label}内没有缺失的 PDF。")
            return
        if not messagebox.askyesno(
            "确认 MOFA 批量下载",
            f"将下载{scope_label}内 {len(targets)} 份缺失 PDF。\n\n"
            "文件将写入 Historical_Documents/mofa；已存在文件会自动跳过。\n"
            "大范围下载可能需要较长时间，是否继续？",
        ):
            return
        items = self.service.catalog_items(entry.native_id for entry in targets)
        if len(items) != len(targets):
            messagebox.showerror("目录数据不完整", "部分选中条目无法从本地目录缓存恢复，请先同步官网目录。")
            return
        self._downloading = True
        self._set_download_controls(True)
        self.download_progress.set(0)
        self.download_status_label.configure(text=f"正在建立队列：{len(items)} 份…")
        threading.Thread(
            target=self._download_worker,
            args=(items,),
            daemon=True,
        ).start()

    def _set_download_controls(self, running: bool) -> None:
        normal = "disabled" if running else "normal"
        self.download_selected_btn.configure(state=normal)
        self.download_filtered_btn.configure(state=normal)
        self.split_selected_btn.configure(state=normal)
        self.sync_btn.configure(state=normal)
        self.local_refresh_btn.configure(state=normal)
        self.normalize_names_btn.configure(state=normal)
        self.pause_btn.configure(state="normal" if running else "disabled")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if not running:
            self.pause_btn.configure(text="暂停队列")

    def _download_worker(self, items) -> None:
        try:
            result = self.batch.run(items, on_progress=self._on_batch_progress)
        except Exception as exc:
            self.after(0, lambda message=str(exc): self._finish_batch(error=message))
            return
        self.after(0, lambda value=result: self._finish_batch(result=value))

    def _on_batch_progress(self, progress: MofaBatchProgress) -> None:
        self.after(0, lambda value=progress: self._apply_batch_progress(value))

    def _apply_batch_progress(self, progress: MofaBatchProgress) -> None:
        base = max(0, progress.current_index - 1)
        fraction = 0.0
        if progress.current_total_bytes:
            fraction = min(1.0, progress.current_bytes / progress.current_total_bytes)
        overall = (base + fraction) / max(1, progress.total)
        if progress.state in {"running", "completed", "cancelled"}:
            overall = progress.current_index / max(1, progress.total)
        self.download_progress.set(min(1.0, overall))
        if progress.state == "paused":
            text = f"队列已暂停 · 已完成 {progress.downloaded + progress.skipped}/{progress.total}"
        elif progress.state == "downloading":
            byte_text = self._format_bytes(progress.current_bytes)
            if progress.current_total_bytes:
                byte_text += f"/{self._format_bytes(progress.current_total_bytes)}"
            text = f"{progress.current_index}/{progress.total} · {byte_text} · {progress.current_title}"
        else:
            text = (
                f"完成 {progress.downloaded + progress.skipped + progress.failed}/{progress.total} · "
                f"新下载 {progress.downloaded} · 跳过 {progress.skipped} · 失败 {progress.failed}"
            )
        self.download_status_label.configure(text=text)

    def toggle_pause(self) -> None:
        if not self._downloading:
            return
        if self.batch.paused:
            self.batch.resume()
            self.pause_btn.configure(text="暂停队列")
            self.download_status_label.configure(text="队列已继续。")
        else:
            self.batch.pause()
            self.pause_btn.configure(text="继续队列")
            self.download_status_label.configure(text="将在当前 PDF 完成后暂停队列。")

    def stop_download(self) -> None:
        if not self._downloading:
            return
        if messagebox.askyesno("停止 MOFA 下载", "停止后会删除当前未完成的 .part 文件，已完成 PDF 保留。是否继续？"):
            self.batch.cancel()
            self.stop_btn.configure(state="disabled")
            self.download_status_label.configure(text="正在停止当前下载并保存已完成进度…")

    def _finish_batch(self, *, result: MofaBatchResult | None = None, error: str = "") -> None:
        self._downloading = False
        self._set_download_controls(False)
        self.refresh_local()
        if error:
            self.download_status_label.configure(text="MOFA 下载队列异常结束；已完成文件仍然保留。")
            messagebox.showerror("MOFA 下载失败", error)
            return
        if result is None:
            return
        status = "已停止" if result.cancelled else "已完成"
        self.download_status_label.configure(
            text=(
                f"队列{status} · 新下载 {result.downloaded} · "
                f"已存在 {result.skipped} · 失败 {result.failed}"
            )
        )
        messagebox.showinfo(
            f"MOFA 下载队列{status}",
            f"新下载：{result.downloaded}\n已存在/修复：{result.skipped}\n"
            f"失败：{result.failed}\n未完成项可再次按当前筛选范围继续下载。",
        )
