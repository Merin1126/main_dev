"""史料标题重命名对话框。"""
from __future__ import annotations

import os

import customtkinter as ctk
from tkinter import messagebox

from components.ui.button import Button
from config.settings import Color
from services.document_rename_service import DocumentRenameService, resolve_existing_pdf_path
from utils.app_state import AppState
from utils.jacar_filename import parse_jacar_pdf_filename


class DocumentRenameDialog(ctk.CTkToplevel):
    def __init__(self, master, pdf_path: str, on_success=None, **kwargs):
        super().__init__(master, **kwargs)
        resolved = resolve_existing_pdf_path(pdf_path)
        if not resolved:
            self.destroy()
            messagebox.showerror(
                "无法重命名",
                f"找不到 PDF 文件：\n{pdf_path}",
                parent=master.winfo_toplevel() if hasattr(master, "winfo_toplevel") else master,
            )
            return
        self.pdf_path = resolved
        self._on_success = on_success
        self._service = DocumentRenameService()
        self._dialog_parent = master.winfo_toplevel() if hasattr(master, "winfo_toplevel") else master

        parts = parse_jacar_pdf_filename(self.pdf_path)
        if parts is None:
            self.destroy()
            messagebox.showerror(
                "无法重命名",
                "当前文件名不是 HRS 标准格式。\n"
                "本功能仅支持修改「」内的标题，且 JACAR Ref. 编号保持不变。",
                parent=self._dialog_parent,
            )
            return

        self.title("重命名史料标题")
        self.geometry("720x360")
        self.transient(master.winfo_toplevel())
        self.grab_set()

        pad = {"padx": 16, "pady": 6}
        ctk.CTkLabel(
            self,
            text="修改「」内标题（JACAR 编号与其它出处信息不变）",
            font=("Arial", 14, "bold"),
            anchor="w",
        ).pack(fill="x", **pad)

        ctk.CTkLabel(self, text=f"当前文件：{os.path.basename(self.pdf_path)}", anchor="w").pack(
            fill="x", **pad
        )
        ctk.CTkLabel(
            self,
            text=f"JACAR Ref. {parts.normalized_ref()}（不可修改）",
            text_color=("#64748b", "#94a3b8"),
            anchor="w",
        ).pack(fill="x", padx=16)

        ctk.CTkLabel(self, text="新标题（「」内文字）", anchor="w").pack(fill="x", padx=16, pady=(12, 4))
        self.title_entry = ctk.CTkEntry(self, height=36)
        self.title_entry.pack(fill="x", padx=16)
        self.title_entry.insert(0, parts.title)

        self.preview_label = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            wraplength=660,
            text_color=("#475569", "#a8b4c4"),
        )
        self.preview_label.pack(fill="x", padx=16, pady=8)
        self.title_entry.bind("<KeyRelease>", lambda _e: self._update_preview())
        self._update_preview()

        btn_row = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        btn_row.pack(fill="x", padx=16, pady=16)
        Button(btn_row, text="取消", width=100, command=self.destroy).pack(side="right", padx=(8, 0))
        Button(btn_row, text="确认重命名", width=120, command=self._on_confirm).pack(side="right")

        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")

    def _update_preview(self) -> None:
        title = self.title_entry.get().strip()
        try:
            new_path, parts = self._service.build_new_path(self.pdf_path, title)
            preview = parts.build_pdf_filename()
            if os.path.basename(new_path) == os.path.basename(self.pdf_path):
                preview += "\n（与当前文件名相同）"
            self.preview_label.configure(text=f"预览新文件名：\n{preview}")
        except ValueError as exc:
            self.preview_label.configure(text=str(exc))

    def _messagebox(self, fn, title: str, message: str) -> bool | None:
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            return fn(title, message, parent=self._dialog_parent)
        finally:
            if self.winfo_exists():
                try:
                    self.grab_set()
                except Exception:
                    pass

    def _on_confirm(self) -> None:
        new_title = self.title_entry.get().strip()
        if not self._messagebox(
            messagebox.askyesno,
            "确认重命名",
            "将重命名 PDF；若同目录存在同名 .json 抓取元数据，将一并重命名并更新 Title 等字段。\n"
            "并同步迁移 OCR / 分析 / 翻译缓存；若存在汇报导出与 Database_JSON，也会一并更新。\n\n是否继续？",
        ):
            return

        result = self._service.rename_title(self.pdf_path, new_title)
        if not result.success:
            self._messagebox(messagebox.showerror, "重命名失败", result.message)
            return

        try:
            self.grab_release()
        except Exception:
            pass
        messagebox.showinfo("重命名完成", result.message, parent=self._dialog_parent)
        AppState().set_selected_pdf(result.new_pdf_path)
        if callable(self._on_success):
            self._on_success(result.new_pdf_path)
        self.destroy()
