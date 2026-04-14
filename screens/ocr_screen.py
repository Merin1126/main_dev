import os
import sys
import subprocess
import json
import threading
import hashlib
import re
from enum import Enum, auto
from google import genai
from google.genai import types
import io
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import fitz  # PyMuPDF
from PIL import Image, ImageTk
from docx import Document

from components.ui.button import Button
from config.settings import Color
from config.api_key_store import load_google_api_key as load_gemini_api_key
from utils.token_logger import log_gemini_usage

class OcrState(Enum):
    IDLE = auto()
    RUNNING = auto()
    DONE = auto()
    ERROR = auto()
    CANCELLED = auto()


# Font Awesome glyphs (Private Use Area) — Symbols Nerd Font
_TREE_ICON_FOLDER_CLOSED = "\uf07b"  # 折叠
_TREE_ICON_FOLDER_OPEN = "\uf07c"  # 展开
_TREE_ICON_PDF = "\uf1c1"


class OCRScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT, **kwargs)
        self.master = master
        self.gemini_api_key = (
            os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
        )

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.download_dir = os.path.join(base_dir, "JACAR_Downloads")
        self.ocr_cache_dir = os.path.join(base_dir, "OCR_Cache")
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)
        if not os.path.exists(self.ocr_cache_dir):
            os.makedirs(self.ocr_cache_dir)

        self.current_pdf = None
        self.current_page = 0
        self.zoom_factor = 1.0
        self.tk_image = None
        self.current_image_item = None
        self.pdf_files = []
        self.file_item_buttons = []
        self.selected_file_index = None
        self.selected_pdf_path = None
        self.expanded_folders = {}
        self._pdf_path_to_index = {}
        self.ocr_cancel_event = threading.Event()
        self.ocr_task_id = 0
        self.current_ocr_state = OcrState.IDLE
        self.ocr_pages = []
        self.current_ocr_page_index = 0
        self.session_prompt_non_cached = 0
        self.session_cached_tokens = 0
        self.session_output_tokens = 0
        self.session_total_tokens = 0
        self.session_cost_jpy = 0.0
        self.session_cost_cny = 0.0

        self._setup_ui()
        self._update_ui_by_state()
        self._load_file_list()

    def _build_icon_button(self, parent_frame, icon_unicode, text_str, fg_color, hover_color, command, height=36):
        """生成支持独立控制图标和文字大小的高级按钮。"""
        btn = Button(
            parent_frame,
            text="",
            height=height,
            fg_color=fg_color,
            hover_color=hover_color,
            command=command,
        )

        container = ctk.CTkFrame(btn, fg_color=Color.TRANSPARENT)
        container.place(relx=0.5, rely=0.5, anchor="center")

        btn.icon_lbl = ctk.CTkLabel(
            container,
            text=icon_unicode,
            font=("Symbols Nerd Font", 20, "bold"),
            text_color=Color.TEXT_WHITE,
            cursor="hand2",
        )
        btn.icon_lbl.pack(side="left", padx=(0, 6))

        btn.text_lbl = ctk.CTkLabel(
            container,
            text=text_str,
            font=("Arial", 14, "bold"),
            text_color=Color.TEXT_WHITE,
            cursor="hand2",
        )
        btn.text_lbl.pack(side="left")

        def _on_click(_event):
            if btn.cget("state") == "normal":
                command()

        def _on_hover(_event):
            if btn.cget("state") == "normal":
                btn.configure(fg_color=hover_color)

        def _on_leave(_event):
            if btn.cget("state") == "normal":
                btn.configure(fg_color=fg_color)

        for w in [container, btn.icon_lbl, btn.text_lbl]:
            w.bind("<Button-1>", _on_click)
            w.bind("<Enter>", _on_hover)
            w.bind("<Leave>", _on_leave)

        return btn

    def _setup_ui(self):
        self.paned_window = tk.PanedWindow(
            self, 
            orient="horizontal", 
            bg=Color.BG_MAIN_DARK,
            sashwidth=8,
            sashrelief="flat",
            sashcursor="sb_h_double_arrow",
            borderwidth=0
        )
        self.paned_window.pack(fill="both", expand=True, padx=5, pady=5)

        self.left_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        self.paned_window.add(self.left_frame, minsize=150, stretch="always")
        
        file_library_title = ctk.CTkFrame(self.left_frame, fg_color=Color.TRANSPARENT)
        file_library_title.pack(pady=10)
        ctk.CTkLabel(
            file_library_title,
            text="\U0000EAEB",
            font=("Symbols Nerd Font", 20, "bold")
        ).pack(side="left")
        ctk.CTkLabel(
            file_library_title,
            text=" 史料文件库",
            font=("Arial", 16, "bold")
        ).pack(side="left")
        
        list_container = ctk.CTkFrame(self.left_frame, fg_color=Color.TRANSPARENT)
        list_container.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.file_list_frame = ctk.CTkScrollableFrame(
            list_container,
            fg_color=Color.BG_PANEL,
            corner_radius=8
        )
        self.file_list_frame.pack(fill="both", expand=True)

        list_action_frame = ctk.CTkFrame(self.left_frame, fg_color=Color.TRANSPARENT)
        list_action_frame.pack(fill="x", padx=8, pady=(0, 10))

        Button(
            list_action_frame,
            text="打开史料文件库",
            height=38,
            command=self.open_download_folder
        ).pack(fill="x", pady=(0, 6))

        Button(
            list_action_frame,
            text="刷新列表",
            height=38,
            command=self.refresh_file_list
        ).pack(fill="x")

        self.mid_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        self.paned_window.add(self.mid_frame, minsize=400, stretch="always")
        
        toolbar = ctk.CTkFrame(self.mid_frame, fg_color=Color.TRANSPARENT)
        toolbar.pack(fill="x", pady=5, padx=5)
        
        Button(toolbar, text="➖ 缩小", width=60, height=30, command=self.zoom_out).pack(side="left", padx=5)
        Button(toolbar, text="➕ 放大", width=60, height=30, command=self.zoom_in).pack(side="left", padx=5)
        
        self.page_label = ctk.CTkLabel(toolbar, text="页码: 0 / 0", font=("Arial", 13, "bold"))
        self.page_label.pack(side="left", expand=True)
        
        Button(toolbar, text="◀ 上一页", width=80, height=30, command=self.prev_page).pack(side="left", padx=5)
        Button(toolbar, text="▶ 下一页", width=80, height=30, command=self.next_page).pack(side="left", padx=5)
        
        self.canvas = tk.Canvas(self.mid_frame, bg=Color.BG_PANEL, highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_motion)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)

        self.action_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        self.paned_window.add(self.action_frame, minsize=180, stretch="never")

        ctk.CTkLabel(
            self.action_frame,
            text="\U0000F013 操作区",
            font=("Symbols Nerd Font", 15, "bold")
        ).pack(pady=(12, 8), padx=10)

        ctk.CTkLabel(
            self.action_frame,
            text="选择模型：",
            font=("Arial", 13, "bold")
        ).pack(anchor="w", padx=12, pady=(0, 4))

        self.selected_model_var = ctk.StringVar(value="gemini-3-flash-preview")
        self.model_option_menu = ctk.CTkOptionMenu(
            self.action_frame,
            values=["gemini-3-flash-preview", "gemini-3.1-pro-preview"],
            variable=self.selected_model_var,
            command=lambda _value: self._refresh_usage_summary_label(),
            fg_color=Color.BG_HOVER,
            button_color=Color.PRIMARY
        )
        self.model_option_menu.pack(fill="x", padx=12, pady=(0, 10))

        self.btn_start_ocr = self._build_icon_button(
            self.action_frame,
            "\U0000EAD3",
            "开始 OCR 识别",
            Color.BTN_SUCCESS,
            Color.BTN_SUCCESS_HOVER,
            self.start_ocr_recognition,
        )
        self.btn_start_ocr.pack(pady=(4, 8), padx=12, fill="x")

        self.btn_force_reocr = self._build_icon_button(
            self.action_frame,
            "\U0000F079",
            "强制重新识别",
            Color.BTN_PRIMARY_ALT,
            Color.BTN_PRIMARY_ALT_HOVER,
            self.force_re_recognize,
        )
        self.btn_force_reocr.pack(pady=(0, 8), padx=12, fill="x")

        self.btn_start_current_page = self._build_icon_button(
            self.action_frame,
            "\U000F1653",
            "仅识别当前页",
            Color.BTN_SUCCESS_ALT,
            Color.BTN_SUCCESS_ALT_HOVER,
            self.start_current_page_ocr,
        )
        self.btn_start_current_page.pack(pady=(0, 8), padx=12, fill="x")

        self.btn_reocr_current_page = self._build_icon_button(
            self.action_frame,
            "\U0000F079",
            "重新识别当前页",
            Color.BTN_PRIMARY_ALT,
            Color.BTN_PRIMARY_ALT_HOVER,
            self.force_reocr_current_page,
        )
        self.btn_reocr_current_page.pack(pady=(0, 8), padx=12, fill="x")

        self.btn_cancel_ocr = self._build_icon_button(
            self.action_frame,
            "\U000F073A",
            "取消任务",
            Color.BTN_WARNING,
            Color.BTN_WARNING_HOVER,
            self.cancel_ocr_task,
        )
        self.btn_cancel_ocr.pack(pady=(0, 8), padx=12, fill="x")

        self.btn_clear_current_cache = self._build_icon_button(
            self.action_frame,
            "\U000F01B4",
            "删除当前缓存",
            Color.BG_BUTTON_MUTED,
            Color.BG_BUTTON_MUTED_HOVER,
            self.clear_current_file_cache,
        )
        self.btn_clear_current_cache.pack(pady=(0, 8), padx=12, fill="x")

        self.btn_clear_cache = self._build_icon_button(
            self.action_frame,
            "\U000F01B4",
            "删除全部缓存",
            Color.BG_BUTTON_MUTED_HOVER,
            Color.BG_BUTTON_NEUTRAL_HOVER,
            self.clear_ocr_cache,
        )
        self.btn_clear_cache.pack(pady=(0, 8), padx=12, fill="x")

        self.btn_export = self._build_icon_button(
            self.action_frame,
            "\U0000EA7F",
            "确认并导出文档",
            Color.PRIMARY,
            Color.PRIMARY_HOVER,
            self.export_document,
            height=40,
        )
        self.btn_export.pack(pady=(6, 14), padx=12, fill="x")

        self.ocr_progress_label = ctk.CTkLabel(
            self.action_frame,
            text="OCR 状态：等待选择文件",
            font=("Arial", 12),
            justify="left",
            anchor="w"
        )
        self.ocr_progress_label.pack(fill="x", padx=12, pady=(4, 6))
        self.action_frame.bind("<Configure>", self._on_action_frame_resize)

        self.ocr_progress_bar = ctk.CTkProgressBar(self.action_frame)
        self.ocr_progress_bar.pack(fill="x", padx=12, pady=(0, 12))
        self.ocr_progress_bar.set(0)
        self.usage_summary_label = ctk.CTkLabel(
            self.action_frame,
            text="本次任务累计：Token=0 | JPY=0.0000 | CNY=0.0000",
            font=("Arial", 12),
            justify="left",
            anchor="w"
        )
        self.usage_summary_label.pack(fill="x", padx=12, pady=(0, 12))

        self.right_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        self.paned_window.add(self.right_frame, minsize=260, stretch="always")

        ctk.CTkLabel(
            self.right_frame,
            text="\U0000F14B OCR 文字校对区",
            font=("Symbols Nerd Font", 16, "bold")
        ).pack(pady=10)

        text_page_toolbar = ctk.CTkFrame(self.right_frame, fg_color=Color.TRANSPARENT)
        text_page_toolbar.pack(fill="x", padx=10, pady=(0, 6))

        Button(text_page_toolbar, text="◀ 上一页", width=80, height=30, command=self.prev_ocr_page).pack(side="left", padx=(0, 6))
        Button(text_page_toolbar, text="▶ 下一页", width=80, height=30, command=self.next_ocr_page).pack(side="left")
        self.ocr_page_entry = ctk.CTkEntry(text_page_toolbar, width=60, placeholder_text="页码")
        self.ocr_page_entry.pack(side="left", padx=(8, 6))
        self.ocr_page_entry.bind("<Return>", self.jump_to_ocr_page_event)
        Button(text_page_toolbar, text="跳转", width=56, height=30, command=self.jump_to_ocr_page).pack(side="left")
        self.ocr_page_label = ctk.CTkLabel(text_page_toolbar, text="文字页码: 0 / 0", font=("Arial", 12, "bold"))
        self.ocr_page_label.pack(side="right")

        self.text_editor = ctk.CTkTextbox(self.right_frame, wrap="word", font=("Arial", 14), corner_radius=8)
        self.text_editor.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._set_ocr_pages(["👈 请在左侧选择一份已下载的史料 PDF 文件。\n\n此处将显示 Google API 提取的文本..."])

        # 设置默认分栏宽度比例（文件库:阅读器:操作区:校对区 = 2:5:1.6:3）
        self.after(120, self._apply_default_pane_ratio)

    def _apply_default_pane_ratio(self):
        total_width = self.paned_window.winfo_width()
        if total_width <= 1:
            self.after(120, self._apply_default_pane_ratio)
            return

        ratios = [2.0, 5.0, 1.6, 3.0]
        ratio_sum = sum(ratios)

        left_w = max(150, int(total_width * ratios[0] / ratio_sum))
        mid_w = max(400, int(total_width * ratios[1] / ratio_sum))
        action_w = max(180, int(total_width * ratios[2] / ratio_sum))
        right_w = max(260, int(total_width * ratios[3] / ratio_sum))

        # 通过设置 sash 位置来确定每一列初始宽度
        sash0 = left_w
        sash1 = left_w + mid_w
        sash2 = left_w + mid_w + action_w

        self.paned_window.sash_place(0, sash0, 0)
        self.paned_window.sash_place(1, sash1, 0)
        self.paned_window.sash_place(2, sash2, 0)

    def _on_action_frame_resize(self, event):
        wrap_length = max(80, event.width - 24)
        self.ocr_progress_label.configure(wraplength=wrap_length)
        self.usage_summary_label.configure(wraplength=wrap_length)

    def _reset_usage_summary(self):
        self.session_prompt_non_cached = 0
        self.session_cached_tokens = 0
        self.session_output_tokens = 0
        self.session_total_tokens = 0
        self.session_cost_jpy = 0.0
        self.session_cost_cny = 0.0
        self._refresh_usage_summary_label()

    def _refresh_usage_summary_label(self):
        current_model = self.selected_model_var.get() if hasattr(self, "selected_model_var") else "N/A"
        self.usage_summary_label.configure(
            text=(
                f"模型={current_model} | "
                f"本次任务累计：Token={self.session_total_tokens} | "
                f"JPY={self.session_cost_jpy:.4f} | "
                f"CNY={self.session_cost_cny:.4f}"
            )
        )

    def _accumulate_usage_summary(self, usage_summary):
        if not usage_summary:
            return
        self.session_prompt_non_cached += int(usage_summary.get("prompt_non_cached", 0))
        self.session_cached_tokens += int(usage_summary.get("cached_content_token_count", 0))
        self.session_output_tokens += int(usage_summary.get("candidates_token_count", 0))
        self.session_total_tokens += int(usage_summary.get("total_token_count", 0))
        self.session_cost_jpy += float(usage_summary.get("cost_jpy", 0.0))
        self.session_cost_cny += float(usage_summary.get("cost_cny", 0.0))
        self._refresh_usage_summary_label()

    def _set_ocr_state(self, new_state):
        def apply_state():
            self.current_ocr_state = new_state
            self._update_ui_by_state()
        if threading.current_thread() is threading.main_thread():
            apply_state()
        else:
            self.after(0, apply_state)

    def _update_ui_by_state(self):
        is_running = self.current_ocr_state == OcrState.RUNNING
        common_state = "disabled" if is_running else "normal"
        cancel_state = "normal" if is_running else "disabled"

        buttons_to_sync = [
            self.btn_start_ocr,
            self.btn_force_reocr,
            self.btn_start_current_page,
            self.btn_reocr_current_page,
            self.btn_clear_current_cache,
            self.btn_clear_cache,
        ]

        sync_color = Color.TEXT_WHITE if common_state == "normal" else Color.TEXT_HINT_SOFT
        for btn in buttons_to_sync:
            btn.configure(state=common_state)
            if hasattr(btn, "icon_lbl"):
                btn.icon_lbl.configure(text_color=sync_color)
            if hasattr(btn, "text_lbl"):
                btn.text_lbl.configure(text_color=sync_color)

        self.btn_cancel_ocr.configure(state=cancel_state)
        cancel_sync_color = Color.TEXT_WHITE if cancel_state == "normal" else Color.TEXT_HINT_SOFT
        if hasattr(self.btn_cancel_ocr, "icon_lbl"):
            self.btn_cancel_ocr.icon_lbl.configure(text_color=cancel_sync_color)
        if hasattr(self.btn_cancel_ocr, "text_lbl"):
            self.btn_cancel_ocr.text_lbl.configure(text_color=cancel_sync_color)

        for btn in self.file_item_buttons:
            if btn.winfo_exists():
                btn.configure(state=common_state)

    @staticmethod
    def _norm_path(p):
        return os.path.normpath(os.path.abspath(p))

    def _collect_pdfs_dfs(self, root_dir):
        """深度优先：先各子目录（按名排序），再当前目录下的 PDF（按名排序）。"""
        ordered = []

        def visit(d):
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

    def _toggle_folder_expanded(self, norm_path, child_host, folder_btn, display_name):
        cur = self.expanded_folders.get(norm_path, True)
        new_expanded = not cur
        self.expanded_folders[norm_path] = new_expanded
        icon = _TREE_ICON_FOLDER_OPEN if new_expanded else _TREE_ICON_FOLDER_CLOSED
        folder_btn.configure(text=f"{icon}  {display_name}")
        if new_expanded:
            child_host.pack(fill="x", padx=0, pady=0)
        else:
            child_host.pack_forget()

    def _render_dir_tree(self, parent, abs_dir, depth):
        """在 parent 内渲染 abs_dir 的一层子项（子文件夹 + PDF）。"""
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

        list_state = "disabled" if self.current_ocr_state == OcrState.RUNNING else "normal"

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
                hover_color=Color.BG_LIST_ITEM_HOVER,
                text_color=Color.TEXT_HINT,
                anchor="w",
                height=34,
                corner_radius=16,
                border_width=1,
                border_color=Color.BORDER_LIST_ITEM,
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
            cache_path = self._build_cache_path(fpath)
            cache_tag = "   🟢 [已缓存]" if os.path.exists(cache_path) else ""
            text = f"{_TREE_ICON_PDF}  {name}{cache_tag}"
            btn = ctk.CTkButton(
                parent,
                text=text,
                font=("Symbols Nerd Font", 12),
                fg_color=Color.TRANSPARENT,
                hover_color=Color.BG_LIST_ITEM_HOVER,
                text_color=Color.TEXT_HINT,
                anchor="w",
                height=34,
                corner_radius=16,
                border_width=1,
                border_color=Color.BORDER_LIST_ITEM,
                state=list_state,
                command=lambda i=idx: self.on_file_select(i),
            )
            btn.pack(fill="x", padx=(4 + depth * 18, 4), pady=2)
            self.file_item_buttons.append(btn)

    def _load_file_list(self):
        self.pdf_files.clear()
        self.file_item_buttons.clear()
        self.selected_file_index = None
        self._pdf_path_to_index.clear()

        for widget in self.file_list_frame.winfo_children():
            widget.destroy()

        if not os.path.exists(self.download_dir):
            self._auto_select_pdf_and_load_cache()
            return

        self.pdf_files = self._collect_pdfs_dfs(self.download_dir)
        self._pdf_path_to_index = {self._norm_path(p): i for i, p in enumerate(self.pdf_files)}

        if not self.pdf_files:
            self._auto_select_pdf_and_load_cache()
            return

        self._render_dir_tree(self.file_list_frame, self.download_dir, depth=0)
        self._auto_select_pdf_and_load_cache()

    def _auto_select_pdf_and_load_cache(self):
        if not self.pdf_files:
            self.selected_pdf_path = None
            self._set_ocr_pages(["👈 请在左侧选择一份已下载的史料 PDF 文件。\n\n此处将显示本地缓存或新的 OCR 识别结果..."])
            self.ocr_progress_label.configure(text="OCR 状态：等待选择文件")
            self.ocr_progress_bar.set(0)
            return

        sel_norm = self._norm_path(self.selected_pdf_path) if self.selected_pdf_path else None
        match = next(
            (p for p in self.pdf_files if self._norm_path(p) == sel_norm),
            None,
        )
        target_path = match if match is not None else self.pdf_files[0]
        target_index = self.pdf_files.index(target_path)
        self.on_file_select(target_index)

    def refresh_file_list(self):
        """手动刷新左侧文件列表"""
        self._load_file_list()
        messagebox.showinfo("提示", "史料文件库列表已刷新。")

    def open_download_folder(self):
        """在系统文件管理器中打开史料文件库目录"""
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

        try:
            if sys.platform.startswith("darwin"):
                subprocess.run(["open", self.download_dir], check=True)
            elif os.name == "nt":
                os.startfile(self.download_dir)
            else:
                subprocess.run(["xdg-open", self.download_dir], check=True)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开文件夹:\n{e}")
            
    def on_file_select(self, index):
        """处理列表点击事件"""
        if index < 0 or index >= len(self.pdf_files):
            return
        self._animate_file_press(index)
        self.selected_file_index = index
        self._refresh_file_item_styles()
        file_path = self.pdf_files[index]
        self.open_pdf(file_path)

    def _animate_file_press(self, index):
        if index < 0 or index >= len(self.file_item_buttons):
            return
        btn = self.file_item_buttons[index]
        btn.configure(height=31)
        self.after(80, lambda b=btn: b.winfo_exists() and b.configure(height=34))

    def _refresh_file_item_styles(self):
        for idx, btn in enumerate(self.file_item_buttons):
            if not btn.winfo_exists():
                continue
            if idx == self.selected_file_index:
                btn.configure(
                    fg_color=Color.BG_LIST_ITEM_ACTIVE,
                    hover_color=Color.BG_LIST_ITEM_ACTIVE_HOVER,
                    text_color=Color.TEXT_WHITE,
                    border_width=1,
                    border_color=Color.BORDER_LIST_ITEM_ACTIVE
                )
            else:
                btn.configure(
                    fg_color=Color.TRANSPARENT,
                    hover_color=Color.BG_LIST_ITEM_HOVER,
                    text_color=Color.TEXT_HINT,
                    border_width=1,
                    border_color=Color.BORDER_LIST_ITEM
                )

    def open_pdf(self, file_path):
        self.cancel_ocr_task(silent=True)

        if self.current_pdf:
            self.current_pdf.close()
        try:
            self.current_pdf = fitz.open(file_path)
            self.current_page = 0
            self.zoom_factor = 1.0
            self.selected_pdf_path = file_path
            self.render_page()

            if not self._load_cached_ocr_for_pdf(file_path):
                self._set_ocr_pages([f"已加载文件：{os.path.basename(file_path)}\n点击“开始 OCR 识别”后将调用 Gemini API。"])
                self.ocr_progress_label.configure(text="OCR 状态：文件已就绪，等待开始")
                self.ocr_progress_bar.set(0)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开 PDF 文件: {e}")

    def _load_cached_ocr_for_pdf(self, pdf_path):
        cache_path = self._build_cache_path(pdf_path)
        if not os.path.exists(cache_path):
            return False
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_text = f.read()
            pages = self._parse_cached_ocr_pages(cached_text)
            self._set_ocr_pages(pages)
            self.ocr_progress_label.configure(text="OCR 状态：已自动加载本地缓存")
            self.ocr_progress_bar.set(1)
            return True
        except Exception:
            self.ocr_progress_label.configure(text="OCR 状态：缓存读取失败，等待开始")
            self.ocr_progress_bar.set(0)
            return False

    def start_ocr_recognition(self):
        if not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return
        self._reset_usage_summary()
        self._set_ocr_state(OcrState.RUNNING)
        self.ocr_task_id += 1
        task_id = self.ocr_task_id
        self.ocr_cancel_event = threading.Event()
        self._set_ocr_pages([f"正在调用 Gemini API 提取 {os.path.basename(self.selected_pdf_path)} 的文字...\n请稍候，正在逐页识别。"])
        self.ocr_progress_label.configure(text="OCR 状态：准备开始")
        self.ocr_progress_bar.set(0)
        self._start_ocr_worker(self.selected_pdf_path, task_id)

    def start_current_page_ocr(self):
        if not self.current_pdf or not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return

        total_pages = len(self.current_pdf)
        page_index = self.current_page
        cache_path = self._build_cache_path(self.selected_pdf_path)

        pages_text = [""] * total_pages
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached_text = f.read()
                pages_text = self._parse_cached_ocr_pages(cached_text)
            except Exception:
                pages_text = []

        if len(pages_text) < total_pages:
            pages_text = list(pages_text) + [""] * (total_pages - len(pages_text))
        else:
            pages_text = list(pages_text[:total_pages])

        existing = (pages_text[page_index] or "").strip()
        if existing and existing not in {"（本页未识别到文本）", "未识别到文本内容。"}:
            messagebox.showinfo("提示", "当前页码已有识别记录，若想覆盖，请点击「重新识别当前页」")
            return

        self._start_single_page_worker(page_index, pages_text, total_pages)

    def force_reocr_current_page(self):
        if not self.current_pdf or not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return

        total_pages = len(self.current_pdf)
        page_index = self.current_page
        cache_path = self._build_cache_path(self.selected_pdf_path)

        pages_text = [""] * total_pages
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached_text = f.read()
                pages_text = self._parse_cached_ocr_pages(cached_text)
            except Exception:
                pages_text = []

        if len(pages_text) < total_pages:
            pages_text = list(pages_text) + [""] * (total_pages - len(pages_text))
        else:
            pages_text = list(pages_text[:total_pages])

        self._start_single_page_worker(page_index, pages_text, total_pages)

    def _start_single_page_worker(self, page_index, pages_text, total_pages):
        self._set_ocr_state(OcrState.RUNNING)
        self.ocr_task_id += 1
        task_id = self.ocr_task_id
        self.ocr_progress_label.configure(text="OCR 状态：正在单页识别...")
        self.ocr_progress_bar.set(0)

        worker = threading.Thread(
            target=self._run_single_page_worker,
            args=(page_index, pages_text, total_pages, task_id),
            daemon=True
        )
        worker.start()

    def _run_single_page_worker(self, page_index, pages_text, total_pages, task_id):
        try:
            if not self.current_pdf or not self.selected_pdf_path:
                raise RuntimeError("当前未加载有效的 PDF 文件。")

            page = self.current_pdf[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            image_bytes = pix.tobytes("png")

            api_key = (
                os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
                or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
                or load_gemini_api_key()
            )
            if not api_key:
                raise RuntimeError("未检测到 GOOGLE_GEMINI_API_KEY。请先配置该环境变量后再使用 OCR。")

            result = self._detect_text_from_image(
                image_bytes,
                api_key,
                file_name=f"{os.path.basename(self.selected_pdf_path)}_第{page_index + 1}页",
                model_name=self.selected_model_var.get(),
            )

            if task_id != self.ocr_task_id:
                return

            pages_text[page_index] = (result or "").strip() or "（本页未识别到文本）"
            cache_path = self._build_cache_path(self.selected_pdf_path)
            cache_payload = json.dumps({"format": "paged_v1", "pages": pages_text}, ensure_ascii=False)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(cache_payload)

            def _apply_single_page_success():
                if task_id != self.ocr_task_id:
                    return
                self._set_ocr_pages(pages_text)
                self.current_ocr_page_index = page_index
                self._show_current_ocr_page()
                self.ocr_progress_label.configure(text=f"OCR 状态：单页识别完成（第 {page_index + 1} 页）")
                self.ocr_progress_bar.set(1)
                self._set_ocr_state(OcrState.DONE)

            self.after(0, _apply_single_page_success)
        except Exception as e:
            def _apply_single_page_error(err=e):
                if task_id != self.ocr_task_id:
                    return
                messagebox.showerror("OCR 失败", f"单页识别失败:\n{err}")
                self.ocr_progress_label.configure(text="OCR 状态：单页识别失败")
                self.ocr_progress_bar.set(0)
                self._set_ocr_state(OcrState.ERROR)

            self.after(0, _apply_single_page_error)

    def _start_ocr_worker(self, file_path, task_id):
        worker = threading.Thread(target=self._run_ocr_worker, args=(file_path, task_id), daemon=True)
        worker.start()

    def _run_ocr_worker(self, file_path, task_id):
        try:
            ocr_pages, from_cache = self._extract_text_with_gemini_ocr(file_path, task_id)
            self.after(0, lambda: self._show_ocr_text_result(ocr_pages, task_id, from_cache))
        except RuntimeError as e:
            err_msg = str(e)
            if str(e) == "OCR_CANCELLED":
                self.after(0, lambda: self._handle_ocr_cancelled(task_id))
                return
            self.after(0, lambda msg=err_msg: self._handle_ocr_failed(task_id, msg))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda msg=err_msg: self._handle_ocr_failed(task_id, msg))

    def _show_ocr_text_result(self, ocr_pages, task_id, from_cache):
        if task_id != self.ocr_task_id:
            return
        self._set_ocr_state(OcrState.DONE)
        if not ocr_pages:
            ocr_pages = ["未识别到文本内容。"]
        self._set_ocr_pages(ocr_pages)
        if from_cache:
            self.ocr_progress_label.configure(text="OCR 状态：已完成（来自本地缓存）")
            self.ocr_progress_bar.set(1)
        else:
            self.ocr_progress_label.configure(text="OCR 状态：已完成")
            self.ocr_progress_bar.set(1)

    def _handle_ocr_cancelled(self, task_id):
        if task_id != self.ocr_task_id:
            return
        self._set_ocr_state(OcrState.CANCELLED)
        self.ocr_progress_label.configure(text="OCR 状态：已取消")
        self.ocr_progress_bar.set(0)
        self._set_ocr_pages(["OCR 任务已取消，已清理本次半成品文本。"])

    def _handle_ocr_failed(self, task_id, reason):
        if task_id != self.ocr_task_id:
            return
        self._set_ocr_state(OcrState.ERROR)
        self.ocr_progress_label.configure(text="OCR 状态：失败")
        self.ocr_progress_bar.set(0)
        messagebox.showerror("OCR 失败", reason)
        self.text_editor.insert("end", "\n\nOCR 失败，请检查网络连接和 GOOGLE_GEMINI_API_KEY 配置。")

    def _extract_text_with_gemini_ocr(self, pdf_path, task_id):
        api_key = (
            os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
            or load_gemini_api_key()
        )
        if not api_key:
            raise RuntimeError(
                "未检测到 GOOGLE_GEMINI_API_KEY。请先配置该环境变量后再使用 OCR。"
            )

        cache_path = self._build_cache_path(pdf_path)
        if os.path.exists(cache_path):
            self.after(0, lambda: self.ocr_progress_label.configure(text="OCR 状态：读取本地缓存中..."))
            self.after(0, lambda: self.ocr_progress_bar.set(1))
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_text = f.read()
                return self._parse_cached_ocr_pages(cached_text), True

        with fitz.open(pdf_path) as doc:
            if len(doc) == 0:
                return [""], False

            all_page_texts = []
            total_pages = len(doc)
            for page_index in range(total_pages):
                self._ensure_active_task(task_id)
                self._update_ocr_progress(task_id, page_index, total_pages)
                page = doc[page_index]

                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                image_bytes = pix.tobytes("png")
                page_text = self._detect_text_from_image(
                    image_bytes,
                    api_key,
                    file_name=f"{os.path.basename(pdf_path)}_第{page_index + 1}页",
                    model_name=self.selected_model_var.get(),
                )
                all_page_texts.append(page_text.strip() if page_text else "（本页未识别到文本）")

        cache_payload = json.dumps({"format": "paged_v1", "pages": all_page_texts}, ensure_ascii=False)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(cache_payload)
        return all_page_texts, False

    def _parse_cached_ocr_pages(self, cached_text):
        try:
            payload = json.loads(cached_text)
            if isinstance(payload, dict) and payload.get("format") == "paged_v1":
                pages = payload.get("pages", [])
                if isinstance(pages, list) and len(pages) > 0:
                    return [str(p) for p in pages]
        except json.JSONDecodeError:
            pass

        legacy_pattern = r"\n\n===== 第 \d+ / \d+ 页 =====\n"
        parts = re.split(legacy_pattern, cached_text)
        page_texts = [part.strip() for part in parts if part.strip()]
        if page_texts:
            return page_texts
        return [cached_text.strip() if cached_text.strip() else "未识别到文本内容。"]

    def _set_ocr_pages(self, pages):
        self.ocr_pages = [p if p is not None else "" for p in pages] if pages else [""]
        self.current_ocr_page_index = 0
        self._show_current_ocr_page()

    def _show_current_ocr_page(self):
        total = len(self.ocr_pages)
        if total == 0:
            self.ocr_pages = [""]
            total = 1
        self.current_ocr_page_index = max(0, min(self.current_ocr_page_index, total - 1))
        self.text_editor.delete("0.0", "end")
        self.text_editor.insert("0.0", self.ocr_pages[self.current_ocr_page_index])
        self.ocr_page_label.configure(text=f"文字页码: {self.current_ocr_page_index + 1} / {total}")
        if hasattr(self, "ocr_page_entry"):
            self.ocr_page_entry.delete(0, "end")
            self.ocr_page_entry.insert(0, str(self.current_ocr_page_index + 1))

    def _save_current_ocr_page(self):
        if not self.ocr_pages:
            self.ocr_pages = [""]
            self.current_ocr_page_index = 0
        self.ocr_pages[self.current_ocr_page_index] = self.text_editor.get("0.0", "end").strip()

    def prev_ocr_page(self):
        if len(self.ocr_pages) <= 1:
            return
        self._save_current_ocr_page()
        if self.current_ocr_page_index > 0:
            self.current_ocr_page_index -= 1
            self._show_current_ocr_page()

    def next_ocr_page(self):
        if len(self.ocr_pages) <= 1:
            return
        self._save_current_ocr_page()
        if self.current_ocr_page_index < len(self.ocr_pages) - 1:
            self.current_ocr_page_index += 1
            self._show_current_ocr_page()

    def jump_to_ocr_page_event(self, _event):
        self.jump_to_ocr_page()

    def jump_to_ocr_page(self):
        total = len(self.ocr_pages)
        if total <= 0:
            messagebox.showwarning("提示", "当前没有可跳转的文字页。")
            return

        raw = self.ocr_page_entry.get().strip()
        if not raw.isdigit():
            messagebox.showwarning("提示", "请输入有效的页码数字。")
            return

        target = int(raw)
        if target < 1 or target > total:
            messagebox.showwarning("提示", f"页码超出范围，请输入 1 到 {total}。")
            return

        self._save_current_ocr_page()
        self.current_ocr_page_index = target - 1
        self._show_current_ocr_page()

    def _build_cache_path(self, pdf_path):
        stat = os.stat(pdf_path)
        cache_key = f"{pdf_path}|{stat.st_mtime_ns}|{stat.st_size}"
        name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".txt"
        return os.path.join(self.ocr_cache_dir, name)

    def _ensure_active_task(self, task_id):
        if task_id != self.ocr_task_id or self.ocr_cancel_event.is_set():
            raise RuntimeError("OCR_CANCELLED")

    def _update_ocr_progress(self, task_id, page_index, total_pages):
        if task_id != self.ocr_task_id:
            return
        text = f"OCR 状态：正在识别第 {page_index + 1} / {total_pages} 页"
        self.after(0, lambda: self.ocr_progress_label.configure(text=text))
        ratio = 0 if total_pages <= 0 else ((page_index + 1) / total_pages)
        self.after(0, lambda: self.ocr_progress_bar.set(ratio))

    def cancel_ocr_task(self, silent=False):
        self.ocr_cancel_event.set()
        if not silent:
            self.ocr_progress_label.configure(text="OCR 状态：正在取消...")
            self.ocr_progress_bar.set(0)

    def clear_ocr_cache(self):
        confirmed = messagebox.askyesno(
            "严重警告",
            "这将删除所有历史档案的 OCR 识别记录（不可恢复），是否确定？"
        )
        if not confirmed:
            return

        if not os.path.exists(self.ocr_cache_dir):
            os.makedirs(self.ocr_cache_dir)
            messagebox.showinfo("提示", "缓存目录不存在，已自动创建。")
            self._load_file_list()
            return

        removed_count = 0
        failed_count = 0
        for filename in os.listdir(self.ocr_cache_dir):
            path = os.path.join(self.ocr_cache_dir, filename)
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                removed_count += 1
            except OSError:
                failed_count += 1

        if failed_count > 0:
            messagebox.showwarning("提示", f"已清理 {removed_count} 个缓存文件，另有 {failed_count} 个文件删除失败。")
        else:
            messagebox.showinfo("提示", f"缓存已清空，共删除 {removed_count} 个文件。")
        self._load_file_list()

    def clear_current_file_cache(self):
        if not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return

        current_pdf_name = os.path.basename(self.selected_pdf_path)
        cache_path = self._build_cache_path(self.selected_pdf_path)
        if not os.path.exists(cache_path):
            messagebox.showinfo("提示", "当前文件无缓存。")
            return

        try:
            os.remove(cache_path)
            messagebox.showinfo("提示", "当前文件缓存已删除。")
            self._load_file_list()
            self._set_ocr_state(OcrState.IDLE)
            self._set_ocr_pages([
                f"已删除 {current_pdf_name} 的 OCR 缓存。\n点击“开始 OCR 识别”或“\U0000F079 强制重新识别”生成新的文本。"
            ])
            self.ocr_progress_label.configure(text="OCR 状态：文件已就绪，等待开始")
            self.ocr_progress_bar.set(0)
        except OSError as e:
            messagebox.showerror("错误", f"删除当前缓存失败:\n{e}")

    def force_re_recognize(self):
        if not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return

        cache_path = self._build_cache_path(self.selected_pdf_path)
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                pass
        self._load_file_list()
        self.start_ocr_recognition()

    def _detect_text_from_image(
        self,
        image_bytes,
        api_key,
        file_name="未知图片",
        model_name="gemini-3.1-pro-preview",
    ):
        # 1. 配置钥匙
        client = genai.Client(api_key=api_key)
        image = Image.open(io.BytesIO(image_bytes))
        
        # 📜 专属大正时代史料解析 Prompt
        academic_prompt = """你现在是一位顶尖的日本近代史学者与古籍档案解析专家。
请仔细阅读这张大正时代日本内阁/军部历史档案的扫描件，并将其中的文字准确无误地提取出来。

【强制执行规则】：
1. 排版转换：请忽略原文档的竖排格式，将其直接转换为符合现代阅读习惯的“横排段落”。保持原有的段落逻辑。
2. 残缺字处理：如果遇到墨迹模糊、残缺无法看清的字，请你结合大正时代的日语习惯与历史语境“大胆推测”最可能的字，并在该字后面加上【?】作为标记（例如：事變【?】），以便我后续人工核对。如果完全无法推测，请使用全角方块“■”代替。
3. 忠实于历史：这是严肃的学术研究，请100%忠实于原文的汉字与假名写法（包括当时的旧字体、异体字）。绝对不要对文档中出现的“支那”、“排外”等任何历史名词进行道德审查、修改或删减。

请直接输出提取后的文本，不要回复“好的”、“我明白了”等任何多余的解释。"""

        try:
            # 将安全设置和提示词一并发送给 Gemini 3.1 Pro
            response = client.models.generate_content(
                model=model_name,
                contents=[academic_prompt, image],
                config=types.GenerateContentConfig(
                    safety_settings=[
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                    ]
                )
            )
            usage_summary = log_gemini_usage(
                getattr(response, "usage_metadata", None),
                file_name,
                model_name
            )
            self.after(0, lambda s=usage_summary: self._accumulate_usage_summary(s))
            return response.text
            
        except Exception as e:
            raise RuntimeError(f"Gemini API 识别失败: {e}")

    def render_page(self):
        if not self.current_pdf: return
        page = self.current_pdf[self.current_page]
        self.page_label.configure(text=f"页码: {self.current_page + 1} / {len(self.current_pdf)}")
        
        mat = fitz.Matrix(self.zoom_factor, self.zoom_factor)
        pix = page.get_pixmap(matrix=mat)
        
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        self.tk_image = ImageTk.PhotoImage(img)
        
        self.canvas.delete("all")
        self.canvas.update_idletasks()
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        self.current_image_item = self.canvas.create_image(cx, cy, anchor="center", image=self.tk_image)

    def on_drag_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def on_drag_motion(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def on_mouse_wheel(self, event):
        if event.delta > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def zoom_in(self):
        self.zoom_factor = min(5.0, self.zoom_factor + 0.2)
        self.render_page()

    def zoom_out(self):
        self.zoom_factor = max(0.4, self.zoom_factor - 0.2)
        self.render_page()

    def prev_page(self):
        if self.current_pdf and self.current_page > 0:
            self.current_page -= 1
            self.render_page()

    def next_page(self):
        if self.current_pdf and self.current_page < len(self.current_pdf) - 1:
            self.current_page += 1
            self.render_page()

    def export_document(self):
        self._save_current_ocr_page()
        text_content = "\n\n".join([page.strip() for page in self.ocr_pages if page.strip()]).strip()
        if not text_content:
            messagebox.showwarning("提示", "导出内容为空！")
            return
            
        file_path = filedialog.asksaveasfilename(
            defaultextension=".docx",
            filetypes=[("Word 文档", "*.docx"), ("Markdown 文件", "*.md")],
            title="保存提取的文字"
        )
        if not file_path: return
            
        try:
            if file_path.endswith('.md'):
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(text_content)
            elif file_path.endswith('.docx'):
                doc = Document()
                doc.add_paragraph(text_content)
                doc.save(file_path)
            messagebox.showinfo("成功", f"文件已成功保存至:\n{file_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"保存出错:\n{e}")