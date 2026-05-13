from __future__ import annotations

import os
import subprocess
import sys
import customtkinter as ctk
from tkinter import messagebox

from components.ui.button import Button
from config.settings import Color
from services.cache_service import CacheService
from utils.app_state import AppState

# Font Awesome glyphs (Private Use Area) — Symbols Nerd Font
_TREE_ICON_FOLDER_CLOSED = "\uf07b"  # 折叠
_TREE_ICON_FOLDER_OPEN = "\uf07c"  # 展开
_TREE_ICON_PDF = "\uf1c1"


class FileTreeSidebar(ctk.CTkFrame):
    """全局文件树侧栏：统一维护 PDF 列表、展开状态与当前选中状态。"""

    SIDEBAR_BG = ("#f3f5f8", "#212121")
    LIST_BG = ("#eef2f7", "#2b2b2b")
    ITEM_HOVER = ("#dbe8fb", "#2f4b75")
    ITEM_ACTIVE = ("#4a84db", "#3a5f96")
    ITEM_ACTIVE_HOVER = ("#3d73c9", "#476ea8")
    ITEM_TEXT = ("#2f3135", "#d7dbe1")
    ITEM_TEXT_ACTIVE = ("#ffffff", "#ffffff")
    ITEM_BORDER = ("#aac0de", "#3c5678")
    ITEM_BORDER_ACTIVE = ("#6a94cf", "#7ca4d7")
    TITLE_TEXT = ("#1f2937", "#ffffff")

    def __init__(self, master, width: int = 200, **kwargs):
        super().__init__(master, width=width, fg_color=self.SIDEBAR_BG, **kwargs)
        self.pack_propagate(False)
        self.cache_service = CacheService()
        self.expanded_folders: dict[str, bool] = {}
        self.pdf_files: list[str] = []
        self.file_item_buttons: list[ctk.CTkButton] = []
        self._pdf_path_to_index: dict[str, int] = {}
        self.selected_file_index: int | None = None

        self._project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.download_dir = os.path.join(self._project_root, "JACAR_Downloads")
        self._cache_dirs = [
            os.path.join(self._project_root, "OCR_Cache"),
            os.path.join(self._project_root, "Translation_Cache"),
            os.path.join(self._project_root, "Analysis_Cache"),
        ]

        self._setup_ui()
        AppState().subscribe_file_change(self._on_global_file_changed)
        self._load_file_list()

    def _setup_ui(self) -> None:
        file_library_title = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        file_library_title.pack(pady=10)
        ctk.CTkLabel(
            file_library_title,
            text="\U0000EAEB",
            font=("Symbols Nerd Font", 20, "bold"),
            text_color=self.TITLE_TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            file_library_title,
            text=" 史料文件库",
            font=("Arial", 16, "bold"),
            text_color=self.TITLE_TEXT,
        ).pack(side="left")

        list_container = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        list_container.pack(fill="both", expand=True, padx=5, pady=5)

        self.file_list_frame = ctk.CTkScrollableFrame(
            list_container,
            fg_color=self.LIST_BG,
            corner_radius=8,
        )
        self.file_list_frame.pack(fill="both", expand=True)

        list_action_frame = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        list_action_frame.pack(fill="x", padx=8, pady=(0, 10))

        Button(
            list_action_frame,
            text="打开史料文件库",
            height=38,
            command=self.open_download_folder,
        ).pack(fill="x", pady=(0, 6))

        Button(
            list_action_frame,
            text="刷新列表",
            height=38,
            command=self.refresh_file_list,
        ).pack(fill="x")

    @staticmethod
    def _norm_path(p: str) -> str:
        return os.path.normpath(os.path.abspath(p))

    def _collect_pdfs_dfs(self, root_dir: str) -> list[str]:
        """深度优先：先各子目录（按名排序），再当前目录下的 PDF（按名排序）。"""
        ordered: list[str] = []

        def visit(d: str) -> None:
            try:
                names = os.listdir(d)
            except OSError:
                return
            subdirs = sorted(
                n
                for n in names
                if os.path.isdir(os.path.join(d, n)) and not n.startswith(".")
            )
            pdfs_here = sorted(n for n in names if n.lower().endswith(".pdf"))
            for name in subdirs:
                visit(os.path.join(d, name))
            for name in pdfs_here:
                ordered.append(os.path.join(d, name))

        if os.path.isdir(root_dir):
            visit(root_dir)
        return ordered

    def _toggle_folder_expanded(self, norm_path: str, child_host, folder_btn, display_name: str) -> None:
        cur = self.expanded_folders.get(norm_path, True)
        new_expanded = not cur
        self.expanded_folders[norm_path] = new_expanded
        icon = _TREE_ICON_FOLDER_OPEN if new_expanded else _TREE_ICON_FOLDER_CLOSED
        folder_btn.configure(text=f"{icon}  {display_name}")
        if new_expanded:
            child_host.pack(fill="x", padx=0, pady=0)
        else:
            child_host.pack_forget()

    def _has_any_cache(self, pdf_path: str) -> bool:
        for d in self._cache_dirs:
            try:
                cache_path = self.cache_service.build_cache_path(pdf_path, d)
            except Exception:
                continue
            if os.path.exists(cache_path):
                return True
        return False

    def _render_dir_tree(self, parent, abs_dir: str, depth: int) -> None:
        try:
            names = os.listdir(abs_dir)
        except OSError:
            return

        subdirs = sorted(
            n
            for n in names
            if os.path.isdir(os.path.join(abs_dir, n)) and not n.startswith(".")
        )
        pdfs_here = sorted(n for n in names if n.lower().endswith(".pdf"))

        for name in subdirs:
            dpath = os.path.join(abs_dir, name)
            norm = self._norm_path(dpath)
            if norm not in self.expanded_folders:
                self.expanded_folders[norm] = True
            expanded = self.expanded_folders[norm]

            outer = ctk.CTkFrame(parent, fg_color=Color.TRANSPARENT)
            outer.pack(fill="x")

            icon = _TREE_ICON_FOLDER_OPEN if expanded else _TREE_ICON_FOLDER_CLOSED
            folder_btn = ctk.CTkButton(
                outer,
                text=f"{icon}  {name}",
                font=("Symbols Nerd Font", 12),
                fg_color=Color.TRANSPARENT,
                hover_color=self.ITEM_HOVER,
                text_color=self.ITEM_TEXT,
                anchor="w",
                height=34,
                corner_radius=16,
                border_width=1,
                border_color=self.ITEM_BORDER,
                state="normal",
            )
            folder_btn.pack(fill="x", padx=(4 + depth * 18, 4), pady=2)

            child_host = ctk.CTkFrame(outer, fg_color=Color.TRANSPARENT)
            self._render_dir_tree(child_host, dpath, depth + 1)
            if expanded:
                child_host.pack(fill="x", padx=0, pady=0)
            else:
                child_host.pack_forget()

            folder_btn.configure(
                command=lambda n=norm, ch=child_host, b=folder_btn, dn=name: self._toggle_folder_expanded(
                    n, ch, b, dn
                )
            )

        for name in pdfs_here:
            fpath = os.path.join(abs_dir, name)
            norm_f = self._norm_path(fpath)
            idx = self._pdf_path_to_index.get(norm_f)
            if idx is None:
                continue
            cache_tag = "   🟢 [已缓存]" if self._has_any_cache(fpath) else ""
            text = f"{_TREE_ICON_PDF}  {name}{cache_tag}"
            btn = ctk.CTkButton(
                parent,
                text=text,
                font=("Symbols Nerd Font", 12),
                fg_color=Color.TRANSPARENT,
                hover_color=self.ITEM_HOVER,
                text_color=self.ITEM_TEXT,
                anchor="w",
                height=34,
                corner_radius=16,
                border_width=1,
                border_color=self.ITEM_BORDER,
                command=lambda i=idx: self.on_file_select(i),
            )
            btn.pack(fill="x", padx=(4 + depth * 18, 4), pady=2)
            self.file_item_buttons.append(btn)

    def _load_file_list(self) -> None:
        self.pdf_files.clear()
        self.file_item_buttons.clear()
        self.selected_file_index = None
        self._pdf_path_to_index.clear()

        for widget in self.file_list_frame.winfo_children():
            widget.destroy()

        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir, exist_ok=True)
            return

        self.pdf_files = self._collect_pdfs_dfs(self.download_dir)
        self._pdf_path_to_index = {self._norm_path(p): i for i, p in enumerate(self.pdf_files)}

        if not self.pdf_files:
            return

        self._render_dir_tree(self.file_list_frame, self.download_dir, depth=0)
        self._auto_select_pdf()

    def _auto_select_pdf(self) -> None:
        if not self.pdf_files:
            return
        current = AppState().selected_pdf_path
        current_norm = self._norm_path(current) if current else None
        match = next((p for p in self.pdf_files if self._norm_path(p) == current_norm), None)
        target_path = match if match else self.pdf_files[0]
        target_idx = self.pdf_files.index(target_path)
        self.on_file_select(target_idx)

    def on_file_select(self, index: int) -> None:
        if index < 0 or index >= len(self.pdf_files):
            return
        self._animate_file_press(index)
        self.selected_file_index = index
        self._refresh_file_item_styles()
        AppState().set_selected_pdf(self.pdf_files[index])

    def _on_global_file_changed(self, pdf_path: str) -> None:
        if not pdf_path or not self.pdf_files:
            return
        norm_target = self._norm_path(pdf_path)
        idx = self._pdf_path_to_index.get(norm_target)
        if idx is None:
            return
        self.selected_file_index = idx
        self._refresh_file_item_styles()

    def _animate_file_press(self, index: int) -> None:
        if index < 0 or index >= len(self.file_item_buttons):
            return
        btn = self.file_item_buttons[index]
        btn.configure(height=31)
        self.after(80, lambda b=btn: b.winfo_exists() and b.configure(height=34))

    def _refresh_file_item_styles(self) -> None:
        for idx, btn in enumerate(self.file_item_buttons):
            if not btn.winfo_exists():
                continue
            if idx == self.selected_file_index:
                btn.configure(
                    fg_color=Color.BG_LIST_ITEM_ACTIVE,
                    hover_color=self.ITEM_ACTIVE_HOVER,
                    text_color=self.ITEM_TEXT_ACTIVE,
                    border_width=1,
                    border_color=self.ITEM_BORDER_ACTIVE,
                )
            else:
                btn.configure(
                    fg_color=Color.TRANSPARENT,
                    hover_color=self.ITEM_HOVER,
                    text_color=self.ITEM_TEXT,
                    border_width=1,
                    border_color=self.ITEM_BORDER,
                )

    def refresh_file_list(self) -> None:
        self._load_file_list()
        messagebox.showinfo("提示", "史料文件库列表已刷新。")

    def open_download_folder(self) -> None:
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir, exist_ok=True)
        try:
            if sys.platform.startswith("darwin"):
                subprocess.run(["open", self.download_dir], check=True)
            elif os.name == "nt":
                os.startfile(self.download_dir)
            else:
                subprocess.run(["xdg-open", self.download_dir], check=True)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开文件夹:\n{e}")
