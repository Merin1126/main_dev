from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk

import customtkinter as ctk
from tkinter import messagebox

from components.document_rename_dialog import DocumentRenameDialog
from components.ui.button import Button
from config.settings import Color
from services.cache_service import CacheService
from services.document_storage_service import DocumentStorageService
from services.sidebar_pdf_index_service import PdfListItem, SidebarPdfIndexService
from utils.app_state import AppState
from utils.jacar_filename import extract_jacar_ref_from_path

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
    ITEM_TEXT_MUTED = ("#5c6570", "#9aa3ad")
    ITEM_TEXT_ACTIVE = ("#ffffff", "#ffffff")
    ITEM_BORDER = ("#aac0de", "#3c5678")
    ITEM_BORDER_ACTIVE = ("#6a94cf", "#7ca4d7")
    TITLE_TEXT = ("#1f2937", "#ffffff")
    DEFAULT_WIDTH = 300

    def __init__(self, master, width: int | None = None, **kwargs):
        init_width = width if width is not None else self.DEFAULT_WIDTH
        super().__init__(master, width=init_width, fg_color=self.SIDEBAR_BG, **kwargs)
        self.pack_propagate(False)
        self.cache_service = CacheService()
        self.index_service = SidebarPdfIndexService()
        self.expanded_folders: dict[str, bool] = {}
        self.pdf_files: list[str] = []
        self.pdf_index: dict[str, PdfListItem] = {}
        self.visible_paths: set[str] = set()
        self.file_item_frames: list[ctk.CTkFrame] = []
        self._pdf_path_to_index: dict[str, int] = {}
        self.selected_file_index: int | None = None
        self._tooltip_win: tk.Toplevel | None = None
        self._tooltip_hide_after_id: str | None = None
        self._search_debounce_id: str | None = None

        self._project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.download_dir = os.path.join(self._project_root, "Historical_Documents", "jacar")
        self.legacy_download_dir = os.path.join(self._project_root, "JACAR_Downloads")
        self.download_roots = [self.download_dir, self.legacy_download_dir]
        self.storage_service = DocumentStorageService(
            project_root=self._project_root,
            cache_service=self.cache_service,
        )
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
        file_library_title.pack(pady=(10, 4))
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

        search_wrap = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        search_wrap.pack(fill="x", padx=8, pady=(0, 4))
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            search_wrap,
            textvariable=self.search_var,
            placeholder_text="搜索 Ref / 标题 / 卷名（联动 SQL）",
            height=32,
        )
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", self._on_search_changed)
        self.search_entry.bind("<Return>", lambda _e: self._apply_search())

        stats_row = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        stats_row.pack(fill="x", padx=10, pady=(0, 4))
        self.search_stats_label = ctk.CTkLabel(
            stats_row,
            text="",
            font=("Arial", 11),
            text_color=self.ITEM_TEXT_MUTED,
            anchor="w",
        )
        self.search_stats_label.pack(side="left", fill="x", expand=True)
        self._folder_toggle_btn = Button(
            stats_row,
            text="一键折叠",
            width=72,
            height=26,
            fontSize=12,
            fg_color=Color.BG_BUTTON_MUTED,
            hover_color=Color.BG_BUTTON_MUTED_HOVER,
            command=self.toggle_all_folders,
        )
        self._folder_toggle_btn.pack(side="right", padx=(6, 0))

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
        ).pack(fill="x", pady=(0, 6))

        Button(
            list_action_frame,
            text="重命名标题",
            height=38,
            command=self.rename_selected_title,
        ).pack(fill="x")

    @staticmethod
    def _norm_path(p: str) -> str:
        return os.path.normpath(os.path.abspath(p))

    def _on_search_changed(self, _event=None) -> None:
        if self._search_debounce_id is not None:
            try:
                self.after_cancel(self._search_debounce_id)
            except (tk.TclError, ValueError):
                pass
        self._search_debounce_id = self.after(180, self._apply_search)

    def _apply_search(self) -> None:
        self._search_debounce_id = None
        needle = (self.search_var.get() or "").strip()
        self.visible_paths = self.index_service.filter_paths(self.pdf_index, needle)
        self._render_file_views()
        self._update_search_stats()
        self._update_folder_toggle_label()
        if self.selected_file_index is not None:
            path = self.pdf_files[self.selected_file_index]
            if path not in self.visible_paths:
                self.selected_file_index = None
                self._refresh_file_item_styles()

    def _update_search_stats(self) -> None:
        total = len(self.pdf_files)
        shown = len(self.visible_paths)
        needle = (self.search_var.get() or "").strip()
        if needle:
            self.search_stats_label.configure(text=f"显示 {shown} / 共 {total} 条（SQL + 本地）")
        else:
            self.search_stats_label.configure(text=f"共 {total} 条史料")

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

    def _collect_all_folder_paths(self) -> list[str]:
        paths: list[str] = []

        def visit(abs_dir: str) -> None:
            try:
                names = os.listdir(abs_dir)
            except OSError:
                return
            for name in sorted(names):
                if name.startswith("."):
                    continue
                sub = os.path.join(abs_dir, name)
                if os.path.isdir(sub):
                    paths.append(self._norm_path(sub))
                    visit(sub)

        for root in self.download_roots:
            if os.path.isdir(root):
                visit(root)
        return paths

    def _any_folder_expanded(self) -> bool:
        for folder_path in self._collect_all_folder_paths():
            if self.expanded_folders.get(folder_path, True):
                return True
        return False

    def _update_folder_toggle_label(self) -> None:
        if (self.search_var.get() or "").strip():
            self._folder_toggle_btn.configure(text="折叠/展开", state="disabled")
            return
        self._folder_toggle_btn.configure(state="normal")
        if self._any_folder_expanded():
            self._folder_toggle_btn.configure(text="一键折叠")
        else:
            self._folder_toggle_btn.configure(text="一键展开")

    def toggle_all_folders(self) -> None:
        if (self.search_var.get() or "").strip():
            return
        expand = not self._any_folder_expanded()
        for folder_path in self._collect_all_folder_paths():
            self.expanded_folders[folder_path] = expand
        selected_idx = self.selected_file_index
        self._render_file_views()
        if selected_idx is not None:
            self.selected_file_index = selected_idx
            self._refresh_file_item_styles()
        self._update_folder_toggle_label()

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
        self._update_folder_toggle_label()

    def _has_any_cache(self, pdf_path: str) -> bool:
        bundle = self.storage_service.resolve_bundle_from_pdf(pdf_path)
        for kind in ("ocr", "translation", "analysis"):
            try:
                cache_path, _layout = self.storage_service.resolve_read_path_with_fallback(bundle, kind)
            except Exception:
                continue
            if os.path.exists(cache_path):
                return True
        return False

    def _folder_has_visible_pdfs(self, abs_dir: str) -> bool:
        prefix = self._norm_path(abs_dir) + os.sep
        for path in self.visible_paths:
            if path == self._norm_path(abs_dir) or path.startswith(prefix):
                return True
        return False

    def _cancel_tooltip_hide(self) -> None:
        if self._tooltip_hide_after_id is not None:
            try:
                self.after_cancel(self._tooltip_hide_after_id)
            except (tk.TclError, ValueError):
                pass
            self._tooltip_hide_after_id = None

    def _hide_tooltip(self) -> None:
        self._cancel_tooltip_hide()
        if self._tooltip_win is not None:
            try:
                if self._tooltip_win.winfo_exists():
                    self._tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tooltip_win = None

    def _position_tooltip(self, tw: tk.Toplevel, anchor: tk.Misc) -> None:
        tw.update_idletasks()
        tw_w = max(tw.winfo_width(), 1)
        tw_h = max(tw.winfo_height(), 1)
        ax = anchor.winfo_rootx()
        ay = anchor.winfo_rooty()
        aw = max(anchor.winfo_width(), 1)
        ah = max(anchor.winfo_height(), 1)
        # 紧贴当前悬停的标签：左对齐、紧贴其下沿
        x = ax
        y = ay + ah + 4
        screen_w = anchor.winfo_screenwidth()
        screen_h = anchor.winfo_screenheight()
        if x + tw_w > screen_w - 12:
            x = max(8, screen_w - tw_w - 12)
        if y + tw_h > screen_h - 20:
            y = max(8, ay - tw_h - 4)
        tw.geometry(f"+{x}+{y}")

    def _show_tooltip(self, anchor: tk.Misc, text: str) -> None:
        self._hide_tooltip()
        if not text:
            return
        tw = tk.Toplevel(anchor)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(
            tw,
            text=text,
            justify="left",
            bg="#1e293b",
            fg="#f8fafc",
            font=("Arial", 11),
            padx=10,
            pady=6,
            wraplength=420,
        ).pack()
        self._position_tooltip(tw, anchor)
        self._tooltip_win = tw

    def _bind_tooltip(self, widget: tk.Misc, text: str) -> None:
        def _on_enter(_event=None) -> None:
            self._cancel_tooltip_hide()
            self._show_tooltip(widget, text)

        def _on_leave(_event=None) -> None:
            self._cancel_tooltip_hide()
            self._tooltip_hide_after_id = self.after(80, self._hide_tooltip)

        widget.bind("<Enter>", _on_enter)
        widget.bind("<Leave>", _on_leave)

    def _render_pdf_item(
        self,
        parent,
        *,
        idx: int,
        item: PdfListItem,
        depth: int,
    ) -> ctk.CTkFrame:
        cache_tag = " 🟢" if self._has_any_cache(item.path) else ""
        row = ctk.CTkFrame(
            parent,
            fg_color=Color.TRANSPARENT,
            corner_radius=16,
            border_width=1,
            border_color=self.ITEM_BORDER,
            height=52,
        )
        row.pack(fill="x", padx=(4 + depth * 14, 4), pady=2)
        row.pack_propagate(False)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            row,
            text=_TREE_ICON_PDF,
            font=("Symbols Nerd Font", 13),
            text_color=self.ITEM_TEXT,
            width=20,
        ).grid(row=0, column=0, rowspan=2, padx=(8, 4), pady=6, sticky="nw")

        line1 = f"{item.line1}{cache_tag}"
        ref_label = ctk.CTkLabel(
            row,
            text=line1,
            font=("Arial", 12, "bold"),
            text_color=self.ITEM_TEXT,
            anchor="w",
        )
        ref_label.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 0))

        summary_label = ctk.CTkLabel(
            row,
            text=item.line2,
            font=("Arial", 10),
            text_color=self.ITEM_TEXT_MUTED,
            anchor="w",
        )
        summary_label.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 6))

        tooltip_text = item.tooltip_text
        for widget in row.winfo_children():
            widget.bind("<Button-1>", lambda _e, i=idx: self.on_file_select(i))
        row.bind("<Button-1>", lambda _e, i=idx: self.on_file_select(i))
        row.configure(cursor="hand2")
        ref_label.configure(cursor="hand2")
        summary_label.configure(cursor="hand2")
        row._pdf_idx = idx  # type: ignore[attr-defined]
        # 仅在 Ref 行与摘要行悬停时显示全文，并锚定到对应标签位置
        self._bind_tooltip(ref_label, tooltip_text)
        self._bind_tooltip(summary_label, tooltip_text)
        self.file_item_frames.append(row)
        return row

    def _render_dir_tree(self, parent, abs_dir: str, depth: int) -> None:
        if not self._folder_has_visible_pdfs(abs_dir):
            return

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
            if not self._folder_has_visible_pdfs(dpath):
                continue
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
            folder_btn.pack(fill="x", padx=(4 + depth * 14, 4), pady=2)

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
            if norm_f not in self.visible_paths:
                continue
            idx = self._pdf_path_to_index.get(norm_f)
            if idx is None:
                continue
            item = self.pdf_index.get(norm_f)
            if item is None:
                continue
            self._render_pdf_item(parent, idx=idx, item=item, depth=depth)

    def _render_flat_results(self) -> None:
        ordered = [
            self.pdf_index[path]
            for path in self.pdf_files
            if path in self.visible_paths and path in self.pdf_index
        ]
        if not ordered:
            ctk.CTkLabel(
                self.file_list_frame,
                text="无匹配史料，请调整搜索词。",
                text_color=self.ITEM_TEXT_MUTED,
            ).pack(anchor="w", padx=10, pady=10)
            return
        for item in ordered:
            idx = self._pdf_path_to_index.get(item.path)
            if idx is None:
                continue
            self._render_pdf_item(self.file_list_frame, idx=idx, item=item, depth=0)

    def _render_file_views(self) -> None:
        self._hide_tooltip()
        self.file_item_frames.clear()
        for widget in self.file_list_frame.winfo_children():
            widget.destroy()

        if not self.pdf_files:
            return

        needle = (self.search_var.get() or "").strip()
        if needle:
            self._render_flat_results()
        else:
            for root in self.download_roots:
                if os.path.isdir(root):
                    self._render_dir_tree(self.file_list_frame, root, depth=0)

        self._refresh_file_item_styles()
        self._update_folder_toggle_label()

    def _load_file_list(self) -> None:
        self.pdf_files.clear()
        self.file_item_frames.clear()
        self.pdf_index.clear()
        self._pdf_path_to_index.clear()

        os.makedirs(self.download_dir, exist_ok=True)
        seen: set[str] = set()
        seen_refs: set[str] = set()
        self.pdf_files = []
        for root in self.download_roots:
            for path in self._collect_pdfs_dfs(root):
                normalized = self._norm_path(path)
                ref = (extract_jacar_ref_from_path(normalized) or "").upper()
                if normalized in seen or (ref and ref in seen_refs):
                    continue
                seen.add(normalized)
                if ref:
                    seen_refs.add(ref)
                self.pdf_files.append(normalized)
        self.pdf_index = self.index_service.build_index(self.pdf_files)
        self._pdf_path_to_index = {p: i for i, p in enumerate(self.pdf_files)}
        needle = (self.search_var.get() or "").strip()
        self.visible_paths = self.index_service.filter_paths(self.pdf_index, needle)

        self._render_file_views()
        self._update_search_stats()
        self._auto_select_pdf()

    def _auto_select_pdf(self) -> None:
        if not self.pdf_files:
            return
        current = AppState().selected_pdf_path
        current_norm = self._norm_path(current) if current else None
        match = next((p for p in self.pdf_files if p == current_norm), None)
        if match and match not in self.visible_paths:
            match = None
        target_path = match if match else next((p for p in self.pdf_files if p in self.visible_paths), None)
        if not target_path:
            return
        target_idx = self.pdf_files.index(target_path)
        self.on_file_select(target_idx)

    def on_file_select(self, index: int) -> None:
        if index < 0 or index >= len(self.pdf_files):
            return
        path = self.pdf_files[index]
        if path not in self.visible_paths:
            return
        self._animate_file_press(index)
        self.selected_file_index = index
        self._refresh_file_item_styles()
        AppState().set_selected_pdf(path)

    def _on_global_file_changed(self, pdf_path: str) -> None:
        if not pdf_path or not self.pdf_files:
            return
        norm_target = self._norm_path(pdf_path)
        idx = self._pdf_path_to_index.get(norm_target)
        if idx is None:
            return
        self.selected_file_index = idx
        self._refresh_file_item_styles()

    def _find_row_for_index(self, index: int) -> ctk.CTkFrame | None:
        for row in self.file_item_frames:
            if getattr(row, "_pdf_idx", None) == index:
                return row
        return None

    def _animate_file_press(self, index: int) -> None:
        row = self._find_row_for_index(index)
        if row is None:
            return
        row.configure(height=48)
        self.after(80, lambda r=row: r.winfo_exists() and r.configure(height=52))

    def _refresh_file_item_styles(self) -> None:
        for row in self.file_item_frames:
            if not row.winfo_exists():
                continue
            item_idx = getattr(row, "_pdf_idx", None)
            is_active = item_idx == self.selected_file_index
            labels = [c for c in row.winfo_children() if isinstance(c, ctk.CTkLabel)]
            if is_active:
                row.configure(
                    fg_color=Color.BG_LIST_ITEM_ACTIVE,
                    border_color=self.ITEM_BORDER_ACTIVE,
                )
                for child in labels:
                    child.configure(text_color=self.ITEM_TEXT_ACTIVE)
            else:
                row.configure(
                    fg_color=Color.TRANSPARENT,
                    border_color=self.ITEM_BORDER,
                )
                if labels:
                    labels[0].configure(text_color=self.ITEM_TEXT)
                if len(labels) > 1:
                    labels[1].configure(text_color=self.ITEM_TEXT)
                if len(labels) > 2:
                    labels[2].configure(text_color=self.ITEM_TEXT_MUTED)

    def rename_selected_title(self) -> None:
        if self.selected_file_index is None or not self.pdf_files:
            messagebox.showwarning("提示", "请先在列表中选择一条史料 PDF。")
            return
        pdf_path = self.pdf_files[self.selected_file_index]

        def _after_rename(new_path: str) -> None:
            self._load_file_list()
            norm = self._norm_path(new_path)
            idx = self._pdf_path_to_index.get(norm)
            if idx is not None:
                self.on_file_select(idx)

        DocumentRenameDialog(self.winfo_toplevel(), pdf_path, on_success=_after_rename)

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
