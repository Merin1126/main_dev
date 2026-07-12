from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from config.settings import Color
from services.mofa_batch_download_service import (
    MofaBatchDownloadService,
    MofaBatchProgress,
    MofaBatchResult,
)
from services.mofa_library_service import MofaLibraryEntry, MofaLibraryService


_KIND_LABELS = {
    "正文": "content",
    "全部类型": "",
    "扉页/目录": "front_matter",
    "索引": "index",
    "奥付": "colophon",
}
_READINESS_VALUES = ["全部状态", "未下载", "待OCR", "待导入", "待生成检索文本", "可检索"]


class MofaLibraryScreen(ctk.CTkFrame):
    """MOFA catalog inventory with local PDF/OCR/search readiness."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.BG_CONTENT, corner_radius=0, **kwargs)
        self.service = MofaLibraryService()
        self.entries: list[MofaLibraryEntry] = []
        self._syncing = False
        self.batch = MofaBatchDownloadService(db_service=self.service.db)
        self._downloading = False
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

        table_frame = ctk.CTkFrame(self, fg_color=Color.BG_CARD, corner_radius=10)
        table_frame.pack(fill="both", expand=True, padx=22, pady=(0, 10))
        style = ttk.Style()
        style.configure("Mofa.Treeview", rowheight=29, font=("PingFang SC", 11))
        style.configure("Mofa.Treeview.Heading", font=("PingFang SC", 11, "bold"))
        columns = ("year", "volume", "title", "pdf", "raw", "imported", "search", "status")
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
            "raw": "MinerU原始",
            "imported": "已导入",
            "search": "检索文本",
            "status": "当前阶段",
        }
        widths = {"year": 64, "volume": 68, "title": 430, "pdf": 58, "raw": 86, "imported": 68, "search": 78, "status": 116}
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

        self.detail_label = ctk.CTkLabel(
            self,
            text="选择条目后显示本地路径与官网地址。",
            anchor="w",
            justify="left",
            font=("PingFang SC", 11),
            text_color=Color.TEXT_GRAY,
        )
        self.detail_label.pack(fill="x", padx=24, pady=(0, 14))

    def on_show(self) -> None:
        """Refresh disk readiness whenever the route becomes visible."""
        if not self._syncing:
            self.refresh_local()

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
        for entry in self.entries:
            self.tree.insert(
                "",
                "end",
                iid=entry.native_id,
                values=(
                    entry.year,
                    entry.volume_code,
                    entry.title,
                    "✓" if entry.pdf_exists else "—",
                    "✓" if entry.mineru_raw_exists else "—",
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
                    f"MinerU原始 {summary.mineru_raw_ready}　已导入 {summary.imported_ready}　"
                    f"可检索 {summary.searchable}"
                )
            )
        else:
            self.summary_label.configure(
                text="本地尚无 MOFA 目录缓存，请点击“同步官网目录”。"
            )

    def sync_official_catalog(self) -> None:
        if self._syncing or self._downloading:
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
        selected = self.tree.selection()
        if not selected:
            return
        native_id = selected[0]
        entry = next((item for item in self.entries if item.native_id == native_id), None)
        if entry is None:
            return
        local = entry.pdf_path if entry.pdf_exists else f"未下载（规划目录：{entry.bundle_dir}）"
        self.detail_label.configure(
            text=f"{entry.native_id}\n本地：{local}\n官网：{entry.pdf_url}"
        )

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

    def download_filtered(self) -> None:
        self._confirm_and_start(
            self._missing_entries(self.entries),
            scope_label="当前筛选范围",
        )

    def _confirm_and_start(self, targets: list[MofaLibraryEntry], *, scope_label: str) -> None:
        if self._downloading:
            messagebox.showinfo("下载进行中", "当前已有 MOFA 下载队列正在运行。")
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
        self.sync_btn.configure(state=normal)
        self.local_refresh_btn.configure(state=normal)
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
