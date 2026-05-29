from __future__ import annotations

import os
import platform
import subprocess
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from components.ui.button import Button
from config.settings import (
    Color,
    GEMINI_SUMMARY_MODEL_DEFAULT,
    GEMINI_SUMMARY_MODEL_LABELS,
    GEMINI_SUMMARY_MODEL_OPTIONS,
)
from services import ReportService


class ReportSummaryWindow(ctk.CTkToplevel):
    def __init__(self, master, project_root: str, **kwargs):
        super().__init__(master, fg_color=("#f5f6f8", "#1f1f23"), **kwargs)
        self.project_root = os.path.abspath(project_root)
        self.report_service = ReportService(self.project_root)
        self.index_data: dict | None = None
        self.selection_state: dict[str, bool] = {}
        self.row_vars: dict[str, ctk.BooleanVar] = {}
        self.row_widgets: list[ctk.CTkFrame] = []
        self.active_folder = "全部"
        self.folder_buttons: dict[str, ctk.CTkButton] = {}
        self.running = False

        self.compare_dir_var = ctk.StringVar(value=self.report_service.default_comparison_dir())
        self.summary_dir_var = ctk.StringVar(value=self.report_service.default_summary_dir())
        self.include_incomplete_var = ctk.BooleanVar(value=False)
        self.summary_model_var = ctk.StringVar(value=GEMINI_SUMMARY_MODEL_DEFAULT)

        self.title("总结页面")
        self.geometry("1080x760")
        self.minsize(920, 620)
        self.attributes("-topmost", True)
        self._build_ui()
        self.reload_index()

    def _build_ui(self) -> None:
        root = ctk.CTkFrame(self, corner_radius=10)
        root.pack(fill="both", expand=True, padx=12, pady=12)

        title = ctk.CTkLabel(
            root,
            text="进度汇报中心",
            font=("Arial", 24, "bold"),
            text_color=Color.TEXT,
        )
        title.pack(anchor="w", padx=14, pady=(14, 6))

        self.index_hint = ctk.CTkLabel(root, text="索引状态：未加载", font=("Arial", 12), text_color=Color.TEXT_MUTED)
        self.index_hint.pack(anchor="w", padx=14, pady=(0, 10))

        ctrl = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        ctrl.pack(fill="x", padx=14, pady=(0, 8))
        self.btn_build_index = Button(ctrl, text="构建索引", width=130, command=self.build_index)
        self.btn_build_index.pack(side="left", padx=(0, 8))
        self.btn_reload_index = Button(ctrl, text="刷新索引", width=130, command=self.reload_index)
        self.btn_reload_index.pack(side="left", padx=(0, 8))
        self.btn_delete_index = Button(
            ctrl,
            text="删除索引",
            width=130,
            fg_color=Color.BTN_WARNING,
            hover_color=Color.BTN_WARNING_HOVER,
            command=self.delete_index,
        )
        self.btn_delete_index.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(root, text="按文件夹筛选：", font=("Arial", 13, "bold")).pack(anchor="w", padx=14, pady=(4, 4))
        self.folder_filter_frame = ctk.CTkScrollableFrame(
            root,
            orientation="horizontal",
            height=44,
            fg_color=("#f2f4f8", "#252932"),
            corner_radius=10,
            border_width=1,
            border_color=("#d7dde7", "#3c4452"),
        )
        self.folder_filter_frame.pack(fill="x", padx=14, pady=(0, 8))

        self.list_frame = ctk.CTkScrollableFrame(
            root,
            fg_color=("#f2f4f8", "#252932"),
            corner_radius=12,
            border_width=1,
            border_color=("#d7dde7", "#3c4452"),
            height=330,
        )
        self.list_frame.pack(fill="both", expand=True, padx=14, pady=(6, 10))

        opts = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        opts.pack(fill="x", padx=14, pady=(0, 8))
        Button(opts, text="全选", width=90, command=self.select_all).pack(side="left", padx=(0, 6))
        Button(opts, text="全不选", width=90, command=self.select_none).pack(side="left", padx=(0, 10))
        ctk.CTkCheckBox(
            opts,
            text="包含未完成文档导出",
            variable=self.include_incomplete_var,
        ).pack(side="left")

        self._path_row(
            root,
            label="内容1输出目录：",
            var=self.compare_dir_var,
            choose_cmd=lambda: self.pick_output_dir(self.compare_dir_var),
            open_cmd=lambda: self.open_folder(self.compare_dir_var.get()),
        )
        self._path_row(
            root,
            label="内容2输出目录：",
            var=self.summary_dir_var,
            choose_cmd=lambda: self.pick_output_dir(self.summary_dir_var),
            open_cmd=lambda: self.open_folder(self.summary_dir_var.get()),
        )

        model_section = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        model_section.pack(fill="x", padx=14, pady=(0, 6))
        model_row = ctk.CTkFrame(model_section, fg_color=Color.TRANSPARENT)
        model_row.pack(fill="x")
        ctk.CTkLabel(model_row, text="内容2 Gemini 模型：", width=120, anchor="w").pack(side="left")
        self.summary_model_menu = ctk.CTkOptionMenu(
            model_row,
            values=list(GEMINI_SUMMARY_MODEL_OPTIONS),
            variable=self.summary_model_var,
            fg_color=Color.BG_HOVER,
            button_color=Color.PRIMARY,
            command=self._on_summary_model_changed,
        )
        self.summary_model_menu.pack(side="left", fill="x", expand=True)
        self.summary_model_hint = ctk.CTkLabel(
            model_section,
            text="",
            font=("Arial", 11),
            text_color=Color.TEXT_MUTED,
            anchor="w",
        )
        self.summary_model_hint.pack(anchor="w", padx=(124, 0), pady=(2, 0))
        self._on_summary_model_changed(self.summary_model_var.get())

        action = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        action.pack(fill="x", padx=14, pady=(6, 6))
        self.btn_export_1 = Button(action, text="导出内容1", width=140, command=self.export_content_1)
        self.btn_export_1.pack(side="left", padx=(0, 8))
        self.btn_export_2 = Button(action, text="导出内容2", width=140, command=self.export_content_2)
        self.btn_export_2.pack(side="left", padx=(0, 8))
        self.btn_export_both = Button(
            action,
            text="一键导出两者",
            width=160,
            fg_color=Color.BTN_SUCCESS_ALT,
            hover_color=Color.BTN_SUCCESS_ALT_HOVER,
            command=self.export_both,
        )
        self.btn_export_both.pack(side="left")

        self.progress_label = ctk.CTkLabel(root, text="就绪", font=("Arial", 12), text_color=Color.TEXT_MUTED)
        self.progress_label.pack(anchor="w", padx=14, pady=(2, 4))
        self.progress = ctk.CTkProgressBar(root, height=12, corner_radius=6)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=14, pady=(0, 14))

    def _path_row(self, root, *, label: str, var: ctk.StringVar, choose_cmd, open_cmd) -> None:
        row = ctk.CTkFrame(root, fg_color=Color.TRANSPARENT)
        row.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(row, text=label, width=120, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        Button(row, text="选择", width=78, command=choose_cmd).pack(side="left", padx=(0, 6))
        Button(row, text="打开", width=78, command=open_cmd).pack(side="left")

    def _on_summary_model_changed(self, model_name: str) -> None:
        label = GEMINI_SUMMARY_MODEL_LABELS.get(str(model_name or "").strip(), str(model_name or ""))
        self.summary_model_hint.configure(text=label)

    def _selected_summary_model(self) -> str:
        model = str(self.summary_model_var.get() or "").strip()
        if model in GEMINI_SUMMARY_MODEL_OPTIONS:
            return model
        return GEMINI_SUMMARY_MODEL_DEFAULT

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for btn in (
            self.btn_build_index,
            self.btn_reload_index,
            self.btn_delete_index,
            self.btn_export_1,
            self.btn_export_2,
            self.btn_export_both,
        ):
            btn.configure(state=state)
        if hasattr(self, "summary_model_menu"):
            self.summary_model_menu.configure(state=state)

    def _post_progress(self, current: int, total: int, message: str) -> None:
        def _ui():
            self.progress_label.configure(text=message)
            if total > 0:
                self.progress.set(max(0.0, min(1.0, current / total)))
        self.after(0, _ui)

    def _run_in_worker(self, target, *, done_text: str):
        if self.running:
            return
        self._set_running(True)
        self.progress.set(0)
        self.progress_label.configure(text="正在执行...")

        def _job():
            err = None
            try:
                target()
            except Exception as e:
                err = e
                tb = traceback.format_exc()
                print(tb)

            def _finish():
                self._set_running(False)
                if err is None:
                    self.progress.set(1)
                    self.progress_label.configure(text=done_text)
                else:
                    self.progress_label.configure(text=f"失败：{err}")
                    messagebox.showerror("执行失败", str(err))

            self.after(0, _finish)

        threading.Thread(target=_job, daemon=True).start()

    def build_index(self) -> None:
        def _work():
            self._post_progress(0, 1, "正在构建索引...")
            payload = self.report_service.build_index(
                comparison_dir=self.compare_dir_var.get().strip(),
                summary_dir=self.summary_dir_var.get().strip(),
            )
            self.index_data = payload
            self.after(0, self.reload_index)

        self._run_in_worker(_work, done_text="索引构建完成")

    def reload_index(self) -> None:
        try:
            self.index_data = self.report_service.load_index()
        except FileNotFoundError:
            self.index_data = None
            self.index_hint.configure(text="索引状态：未找到索引（请先点击“构建索引”）")
            self._render_folder_filters([])
            self._render_rows([])
            return
        except Exception as e:
            self.index_data = None
            self.index_hint.configure(text=f"索引状态：加载失败（{e}）")
            self._render_folder_filters([])
            self._render_rows([])
            return

        total = int(self.index_data.get("total_documents", 0))
        ready = int(self.index_data.get("ready_documents", 0))
        docx_n = int(self.index_data.get("comparison_docx_found", 0))
        md_n = int(self.index_data.get("summary_md_found", 0))
        ts = self.index_data.get("generated_at", "-")
        self.index_hint.configure(
            text=(
                f"索引状态：已加载 | 文档 {total} | 可导出 {ready} | "
                f"已有 DOCX {docx_n} | 已有总结 MD {md_n} | 生成于 {ts}"
            )
        )
        entries = self.index_data.get("entries", []) or []
        old_state = dict(self.selection_state)
        self.selection_state = {}
        for entry in entries:
            pdf_path = str(entry.get("pdf_path") or "")
            if not pdf_path:
                continue
            default_checked = bool(entry.get("ready", False))
            self.selection_state[pdf_path] = old_state.get(pdf_path, default_checked)
        self._render_folder_filters(entries)
        self._render_rows(entries)

    def delete_index(self) -> None:
        if not messagebox.askyesno("确认", "确定删除当前索引吗？"):
            return
        try:
            deleted = self.report_service.delete_index()
        except Exception as e:
            messagebox.showerror("错误", f"删除索引失败：{e}")
            return
        if deleted:
            messagebox.showinfo("成功", "索引已删除。")
        else:
            messagebox.showinfo("提示", "索引不存在。")
        self.reload_index()

    @staticmethod
    def _entry_folder(entry: dict) -> str:
        folder = str(entry.get("folder") or "").strip()
        if folder:
            return folder
        rel = str(entry.get("pdf_rel_path") or "")
        if rel:
            return rel.split(os.sep)[0]
        return "未分类"

    @staticmethod
    def _entry_sort_key(entry: dict) -> tuple:
        has_issues = bool(entry.get("issues", []))
        ready = bool(entry.get("ready", False))
        if has_issues:
            bucket = 2
        elif ready:
            bucket = 0
        else:
            bucket = 1
        return (bucket, str(entry.get("folder") or ""), str(entry.get("pdf_rel_path") or ""))

    def _filtered_and_sorted_entries(self, entries: list[dict]) -> list[dict]:
        if self.active_folder == "全部":
            scoped = list(entries)
        else:
            scoped = [e for e in entries if self._entry_folder(e) == self.active_folder]
        scoped.sort(key=self._entry_sort_key)
        return scoped

    def _set_active_folder(self, folder: str) -> None:
        self.active_folder = folder
        for name, btn in self.folder_buttons.items():
            if name == folder:
                btn.configure(fg_color=Color.PRIMARY, text_color=Color.TEXT_WHITE)
            else:
                btn.configure(fg_color=Color.TRANSPARENT, text_color=Color.TEXT)
        entries = (self.index_data or {}).get("entries", []) or []
        self._render_rows(entries)

    def _render_folder_filters(self, entries: list[dict]) -> None:
        for child in self.folder_filter_frame.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass
        self.folder_buttons = {}

        folders = sorted({self._entry_folder(e) for e in entries if str(e.get("pdf_path") or "")})
        options = ["全部"] + folders
        if self.active_folder not in options:
            self.active_folder = "全部"

        for name in options:
            btn = ctk.CTkButton(
                self.folder_filter_frame,
                text=name,
                height=30,
                width=max(82, min(220, 20 + len(name) * 16)),
                corner_radius=12,
                fg_color=Color.PRIMARY if name == self.active_folder else Color.TRANSPARENT,
                text_color=Color.TEXT_WHITE if name == self.active_folder else Color.TEXT,
                hover_color=Color.BG_HOVER,
                command=lambda v=name: self._set_active_folder(v),
            )
            btn.pack(side="left", padx=(4, 6), pady=6)
            self.folder_buttons[name] = btn

    def _render_rows(self, entries: list[dict]) -> None:
        for w in self.row_widgets:
            try:
                w.destroy()
            except tk.TclError:
                pass
        self.row_widgets = []
        self.row_vars = {}
        visible_entries = self._filtered_and_sorted_entries(entries)
        if not visible_entries:
            row = ctk.CTkLabel(self.list_frame, text="暂无索引数据。", text_color=Color.TEXT_MUTED)
            row.pack(anchor="w", padx=10, pady=10)
            self.row_widgets.append(row)
            return

        for entry in visible_entries:
            pdf_path = str(entry.get("pdf_path") or "")
            if not pdf_path:
                continue
            ready = bool(entry.get("ready", False))
            issues = entry.get("issues", []) or []
            row = ctk.CTkFrame(self.list_frame, fg_color=("#e8edf5", "#2d3340"), corner_radius=10)
            row.pack(fill="x", padx=8, pady=6)
            var = ctk.BooleanVar(value=self.selection_state.get(pdf_path, ready))
            var.trace_add("write", lambda *_args, p=pdf_path, v=var: self.selection_state.__setitem__(p, bool(v.get())))
            self.row_vars[pdf_path] = var

            top = ctk.CTkFrame(row, fg_color=Color.TRANSPARENT)
            top.pack(fill="x", padx=10, pady=(8, 2))
            ctk.CTkCheckBox(
                top,
                text=f"[{self._entry_folder(entry)}] {entry.get('pdf_rel_path', pdf_path)}",
                variable=var,
            ).pack(side="left", fill="x", expand=True)
            status = "可导出" if ready else "未就绪"
            status_color = Color.TEXT_SUCCESS if ready else Color.TEXT_WARNING
            ctk.CTkLabel(top, text=status, text_color=status_color).pack(side="right")

            meta = f"页数: {entry.get('page_count', 0)} | OCR: {entry.get('ocr_pages', 0)} | Analysis: {entry.get('analysis_pages', 0)}"
            ctk.CTkLabel(row, text=meta, font=("Arial", 12), text_color=Color.TEXT_MUTED).pack(anchor="w", padx=14, pady=(0, 2))

            has_docx = bool(entry.get("comparison_docx_exists"))
            has_md = bool(entry.get("summary_md_exists"))
            docx_tag = "内容1 DOCX ✓" if has_docx else "内容1 DOCX —"
            md_tag = "内容2 总结 MD ✓" if has_md else "内容2 总结 MD —"
            export_color = Color.TEXT_SUCCESS if (has_docx and has_md) else (
                Color.TEXT_WARNING if (has_docx or has_md) else Color.TEXT_MUTED
            )
            ctk.CTkLabel(
                row,
                text=f"汇报导出：{docx_tag}  |  {md_tag}",
                font=("Arial", 12),
                text_color=export_color,
            ).pack(anchor="w", padx=14, pady=(0, 2))
            export_notes = entry.get("export_notes") or []
            if export_notes:
                ctk.CTkLabel(
                    row,
                    text="；".join(str(x) for x in export_notes[:2]),
                    font=("Arial", 11),
                    text_color=Color.TEXT_MUTED,
                ).pack(anchor="w", padx=14, pady=(0, 2))

            if issues:
                ctk.CTkLabel(
                    row,
                    text="问题: " + "; ".join(str(x) for x in issues[:3]),
                    font=("Arial", 12),
                    text_color=Color.RED,
                ).pack(anchor="w", padx=14, pady=(0, 8))
            else:
                ctk.CTkLabel(row, text="问题: 无", font=("Arial", 12), text_color=Color.TEXT_MUTED).pack(
                    anchor="w", padx=14, pady=(0, 8)
                )
            self.row_widgets.append(row)

    def select_all(self) -> None:
        entries = (self.index_data or {}).get("entries", []) or []
        for entry in self._filtered_and_sorted_entries(entries):
            pdf_path = str(entry.get("pdf_path") or "")
            if pdf_path:
                self.selection_state[pdf_path] = True
        for path, var in self.row_vars.items():
            if path in self.selection_state:
                var.set(self.selection_state[path])

    def select_none(self) -> None:
        entries = (self.index_data or {}).get("entries", []) or []
        for entry in self._filtered_and_sorted_entries(entries):
            pdf_path = str(entry.get("pdf_path") or "")
            if pdf_path:
                self.selection_state[pdf_path] = False
        for path, var in self.row_vars.items():
            if path in self.selection_state:
                var.set(self.selection_state[path])

    def selected_paths(self) -> set[str]:
        return {path for path, checked in self.selection_state.items() if bool(checked)}

    def pick_output_dir(self, var: ctk.StringVar) -> None:
        chosen = filedialog.askdirectory(title="选择输出目录", initialdir=var.get() or self.project_root)
        if chosen:
            var.set(chosen)

    def open_folder(self, folder: str) -> None:
        path = os.path.abspath(folder or "")
        os.makedirs(path, exist_ok=True)
        system = platform.system().lower()
        if system == "darwin":
            subprocess.run(["open", path], check=False)
            return
        if system == "windows":
            os.startfile(path)  # type: ignore[attr-defined]
            return
        subprocess.run(["xdg-open", path], check=False)

    def _ensure_index_and_selection(self) -> set[str] | None:
        if not self.index_data:
            messagebox.showwarning("提示", "请先构建或刷新索引。")
            return None
        selected = self.selected_paths()
        if not selected:
            messagebox.showwarning("提示", "请先勾选至少一份史料。")
            return None
        return selected

    def export_content_1(self) -> None:
        selected = self._ensure_index_and_selection()
        if selected is None:
            return

        def _work():
            result = self.report_service.export_comparison_docx(
                index_data=self.index_data or {},
                selected_pdf_paths=selected,
                output_dir=self.compare_dir_var.get().strip(),
                include_incomplete=bool(self.include_incomplete_var.get()),
                progress_cb=self._post_progress,
            )
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "内容1导出完成",
                    f"成功 {result.success}，失败 {result.failed}\nManifest:\n{result.manifest_path}",
                ),
            )

        self._run_in_worker(_work, done_text="内容1导出完成")

    def export_content_2(self) -> None:
        selected = self._ensure_index_and_selection()
        if selected is None:
            return

        def _work():
            result = self.report_service.generate_summaries(
                index_data=self.index_data or {},
                selected_pdf_paths=selected,
                output_dir=self.summary_dir_var.get().strip(),
                model_name=self._selected_summary_model(),
                include_incomplete=bool(self.include_incomplete_var.get()),
                progress_cb=self._post_progress,
            )
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "内容2导出完成",
                    f"成功 {result.success}，失败 {result.failed}，跳过 {result.skipped}\nManifest:\n{result.manifest_path}",
                ),
            )

        self._run_in_worker(_work, done_text="内容2导出完成")

    def export_both(self) -> None:
        selected = self._ensure_index_and_selection()
        if selected is None:
            return

        def _work():
            self._post_progress(0, 1, "阶段1/2：导出内容1")
            result1 = self.report_service.export_comparison_docx(
                index_data=self.index_data or {},
                selected_pdf_paths=selected,
                output_dir=self.compare_dir_var.get().strip(),
                include_incomplete=bool(self.include_incomplete_var.get()),
                progress_cb=lambda c, t, m: self._post_progress(c, t, f"[内容1] {m}"),
            )
            self._post_progress(0, 1, "阶段2/2：导出内容2")
            result2 = self.report_service.generate_summaries(
                index_data=self.index_data or {},
                selected_pdf_paths=selected,
                output_dir=self.summary_dir_var.get().strip(),
                model_name=self._selected_summary_model(),
                include_incomplete=bool(self.include_incomplete_var.get()),
                progress_cb=lambda c, t, m: self._post_progress(c, t, f"[内容2] {m}"),
            )
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "一键导出完成",
                    (
                        f"内容1：成功 {result1.success}，失败 {result1.failed}\n"
                        f"Manifest: {result1.manifest_path}\n\n"
                        f"内容2：成功 {result2.success}，失败 {result2.failed}，跳过 {result2.skipped}\n"
                        f"Manifest: {result2.manifest_path}"
                    ),
                ),
            )

        self._run_in_worker(_work, done_text="内容1+内容2导出完成")
