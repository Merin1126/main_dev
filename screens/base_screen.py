from __future__ import annotations

import os
import json
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum, auto
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
from docx import Document

from components.ui.button import Button
from config.settings import Color
from config.api_key_store import load_google_api_key as load_gemini_api_key
from services import CacheService, LlmService, PdfService
from utils.app_state import AppState

class DocumentTaskState(Enum):
    IDLE = auto()
    RUNNING = auto()
    DONE = auto()
    ERROR = auto()
    CANCELLED = auto()


DOC_TASK_CANCELLED = "DOC_TASK_CANCELLED"


class BaseDocumentScreen(ctk.CTkFrame, ABC):
    """文档阅读 + Gemini 多页处理模板基类。子类配置文案、缓存目录与提示词。"""

    screen_title: str = ""
    cache_dir_name: str = ""
    right_panel_title: str = ""
    primary_action_label: str = ""
    task_short_name: str = "任务"
    progress_verb: str = "处理"
    force_full_label: str = "强制重新处理"
    single_page_label: str = "仅处理当前页"
    re_single_label: str = "重新处理当前页"
    export_dialog_title: str = "保存文档"
    empty_page_marker: str = "（本页无结果）"
    idle_editor_hint: str = (
        "👈 请在左侧选择一份已下载的史料 PDF 文件。\n\n"
        "此处将显示本地缓存或由 Gemini 生成的新结果..."
    )
    #: True：多模态看图（OCR）；False：仅基于 OCR_Cache 中的文本调用 Gemini（翻译/甄别流水线）
    requires_image_input: bool = False
    #: 是否显示「仅处理当前页」「重新处理当前页」；整份档案-only 的屏幕（如史料分析）设为 False
    show_single_page_actions: bool = True
    #: 翻译/甄别页：当前 PDF 尚未在「史料校对」完成全书可用 OCR 时的提示（输入区 + 状态栏）
    missing_full_ocr_notice: str = (
        "该PDF文件还未提取OCR文字，请提取并手动校对史料信息"
    )
    #: 单页 Gemini 请求超时时间（秒）
    api_request_timeout_sec: int = 120
    #: 页级超时重试总次数（含首次调用）
    api_timeout_max_attempts: int = 3
    #: v2.6.6 起：是否以有状态 Chat Session 调用 LLM。OCR 走单次调用，Analysis/Translation 启用。
    use_chat_session: bool = False
    #: Chat Session 的响应 MIME 类型；Analysis 子类应覆盖为 "application/json"。
    chat_response_mime_type: str = "text/plain"
    #: Chat Session 的温度参数（仅在 use_chat_session=True 时生效）。
    chat_temperature: float = 0.3

    def get_academic_prompt(self, page_index: int = None) -> str:
        """返回发送给 Gemini 的学术/任务提示词（用于 OCR 单次调用路径）。

        v2.6.6 起，Analysis / Translation 改走 Chat Session（`use_chat_session=True`），
        系统指令与每轮消息由 `get_system_prompt()` 与 `get_turn_prompt()` 分别提供，
        本方法对它们不再被调用，可保留默认实现。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 未实现 get_academic_prompt；如已切换 Chat Session，请实现 get_system_prompt/get_turn_prompt。"
        )

    def get_system_prompt(self) -> str:
        """Chat Session 的系统前缀（仅当 use_chat_session=True 时调用）。子类必须覆盖。"""
        raise NotImplementedError(
            f"{type(self).__name__} 开启 use_chat_session 后必须覆盖 get_system_prompt()。"
        )

    def get_turn_prompt(self, page_index: int, page_text: str) -> str:
        """Chat Session 单页 turn_prompt（仅当 use_chat_session=True 时调用）。子类必须覆盖。"""
        raise NotImplementedError(
            f"{type(self).__name__} 开启 use_chat_session 后必须覆盖 get_turn_prompt()。"
        )

    @abstractmethod
    def export_document(self) -> None:
        """将右侧多页文本导出为文件（子类可调用 _export_text_pages_default）。"""

    def _status_colon(self, tail: str) -> str:
        return f"{type(self).task_short_name} 状态：{tail}"

    def _trace_cache_write(self, *, cache_path: str, cache_kind: str, content: str, page_index: int | None) -> None:
        self.llm_service.trace_cache_write(
            screen_name=type(self).__name__,
            task_name=type(self).task_short_name,
            selected_pdf_path=self.selected_pdf_path,
            cache_path=cache_path,
            cache_kind=cache_kind,
            content=content,
            page_index=page_index,
        )

    def _hint_no_pdf_selected(self):
        return [type(self).idle_editor_hint]

    def _hint_pdf_ready(self, basename: str):
        pl = type(self).primary_action_label
        return [f"已加载文件：{basename}\n点击「{pl}」后将调用 Gemini API。"]

    def _hint_task_cancelled(self):
        return [f"{type(self).task_short_name} 任务已取消，已清理本次半成品文本。"]

    def _hint_cleared_file_cache(self, pdf_name: str):
        pl = type(self).primary_action_label
        fl = type(self).force_full_label
        return [f"已删除 {pdf_name} 的本地缓存。\n点击「{pl}」或「{fl}」重新生成。"]

    def _clear_all_cache_warning_message(self) -> str:
        return (
            f"这将删除本工作台全部「{type(self).task_short_name}」缓存记录（不可恢复），是否确定？"
        )

    MISSING_OCR_FOR_CURRENT_PAGE_MSG = "请先在「史料校对」页面完成当前页的 OCR 提取与校对！"

    def _build_ocr_cache_path(self, pdf_path: str) -> str:
        """与 OCR 页相同的哈希规则，定位 OCR_Cache 下的 paged_v1 文件。"""
        return self.cache_service.build_cache_path(pdf_path, self._ocr_cache_dir)

    @staticmethod
    def _ocr_cached_plaintext_is_usable(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if "未识别到文本" in t:
            return False
        return True

    def _get_ocr_text_for_page(self, pdf_path: str, page_index: int) -> str:
        """
        读取 OCR_Cache 中对应 PDF 的 paged_v1，返回该页已校对底稿。
        无文件、越界、空或无效 OCR 占位文案时返回 ""。
        """
        cache_path = self._build_ocr_cache_path(pdf_path)
        if not os.path.isfile(cache_path):
            return ""
        pages = self.cache_service.read_paged_cache(cache_path)
        if page_index < 0 or page_index >= len(pages):
            return ""
        text = (pages[page_index] or "").strip()
        if not self._ocr_cached_plaintext_is_usable(text):
            return ""
        return text

    def _pdf_has_complete_ocr_baseline(self, pdf_path: str, page_count: int) -> bool:
        """与全书翻译/甄别预检一致：每一页在 OCR_Cache 中均有可用文本。"""
        if page_count <= 0 or not pdf_path:
            return False
        for i in range(page_count):
            if not self._get_ocr_text_for_page(pdf_path, i):
                return False
        return True

    def _warn_missing_ocr_for_current_page(self) -> bool:
        """若当前页无可用 OCR 底稿则弹窗并返回 True（表示应中断操作）。"""
        if type(self).requires_image_input:
            return False
        if not self.selected_pdf_path or not self.current_pdf:
            return False
        page_index = self.current_page
        if self._get_ocr_text_for_page(self.selected_pdf_path, page_index):
            return False
        messagebox.showwarning("缺少 OCR 底稿", self.MISSING_OCR_FOR_CURRENT_PAGE_MSG)
        return True

    def _preflight_all_pages_have_ocr(self, pdf_path: str) -> bool:
        """全书处理前检查每一页是否均有可用 OCR；失败时弹窗。"""
        if type(self).requires_image_input:
            return True
        try:
            total = self.pdf_service.count_pages(pdf_path)
            for pi in range(total):
                if not self._get_ocr_text_for_page(pdf_path, pi):
                    messagebox.showwarning(
                        "缺少 OCR 底稿",
                        f"第 {pi + 1} 页尚无可用 OCR 文本。请先在「史料校对」页面完成全书 OCR 提取与校对后再试。",
                    )
                    return False
        except Exception as e:
            messagebox.showerror("错误", f"无法检查 OCR 底稿：{e}")
            return False
        return True

    def _export_text_pages_default(self, dialog_title: str | None = None) -> None:
        self._save_current_ocr_page()
        text_content = "\n\n".join([page.strip() for page in self.ocr_pages if page.strip()]).strip()
        if not text_content:
            messagebox.showwarning("提示", "导出内容为空！")
            return
        title = dialog_title or type(self).export_dialog_title
        file_path = filedialog.asksaveasfilename(
            defaultextension=".docx",
            filetypes=[("Word 文档", "*.docx"), ("Markdown 文件", "*.md")],
            title=title,
        )
        if not file_path:
            return
        try:
            if file_path.endswith(".md"):
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(text_content)
            elif file_path.endswith(".docx"):
                doc = Document()
                doc.add_paragraph(text_content)
                doc.save(file_path)
            messagebox.showinfo("成功", f"文件已成功保存至:\n{file_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"保存出错:\n{e}")

    def __init__(self, master, **kwargs):
        cls = type(self)
        if not cls.screen_title or not cls.cache_dir_name or not cls.right_panel_title or not cls.primary_action_label:
            raise TypeError(
                f"{cls.__name__} must set class attributes: screen_title, cache_dir_name, "
                "right_panel_title, primary_action_label"
            )
        super().__init__(master, fg_color=Color.TRANSPARENT, **kwargs)
        self.master = master
        self.gemini_api_key = (
            os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
        )

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._project_root = base_dir
        self.cache_service = CacheService()
        self.pdf_service = PdfService()
        self.llm_service = LlmService(
            api_key=self.gemini_api_key,
            project_root=base_dir,
            timeout_sec=type(self).api_request_timeout_sec,
        )
        self.download_dir = os.path.join(base_dir, "JACAR_Downloads")
        self.document_cache_dir = os.path.join(base_dir, cls.cache_dir_name)
        self._ocr_cache_dir = os.path.join(base_dir, "OCR_Cache")
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)
        if not os.path.exists(self.document_cache_dir):
            os.makedirs(self.document_cache_dir)
        if not os.path.exists(self._ocr_cache_dir):
            os.makedirs(self._ocr_cache_dir)

        self.current_pdf = None
        self.current_page = 0
        self.zoom_factor = 1.0
        self.tk_image = None
        self.current_image_item = None
        self.selected_pdf_path = None
        self.ocr_cancel_event = threading.Event()
        self.ocr_task_id = 0
        self.current_ocr_state = DocumentTaskState.IDLE
        self.ocr_pages = []
        self.current_ocr_page_index = 0
        #: v2.6.6 起：有状态 Chat 会话句柄，每次任务开始时通过 _start_new_chat_session() 重建
        self.current_chat = None
        self.session_prompt_non_cached = 0
        self.session_cached_tokens = 0
        self.session_output_tokens = 0
        self.session_total_tokens = 0
        self.session_cost_jpy = 0.0
        self.session_cost_cny = 0.0
        self.session_api_call_pages = 0
        self.session_cache_loaded_pages = 0
        self.session_max_tokens_per_call = 0

        self._setup_ui()
        self._update_ui_by_state()
        AppState().subscribe_file_change(self.on_global_file_changed)
        global_selected = AppState().selected_pdf_path
        if global_selected and os.path.exists(global_selected):
            self.on_global_file_changed(global_selected)

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
            type(self).primary_action_label,
            Color.BTN_SUCCESS,
            Color.BTN_SUCCESS_HOVER,
            self.start_ocr_recognition,
        )
        self.btn_start_ocr.pack(pady=(4, 8), padx=12, fill="x")

        self.btn_force_reocr = self._build_icon_button(
            self.action_frame,
            "\U0000F079",
            type(self).force_full_label,
            Color.BTN_PRIMARY_ALT,
            Color.BTN_PRIMARY_ALT_HOVER,
            self.force_re_recognize,
        )
        self.btn_force_reocr.pack(pady=(0, 8), padx=12, fill="x")

        if type(self).show_single_page_actions:
            self.btn_start_current_page = self._build_icon_button(
                self.action_frame,
                "\U000F1653",
                type(self).single_page_label,
                Color.BTN_SUCCESS_ALT,
                Color.BTN_SUCCESS_ALT_HOVER,
                self.start_current_page_ocr,
            )
            self.btn_start_current_page.pack(pady=(0, 8), padx=12, fill="x")

            self.btn_reocr_current_page = self._build_icon_button(
                self.action_frame,
                "\U0000F079",
                type(self).re_single_label,
                Color.BTN_PRIMARY_ALT,
                Color.BTN_PRIMARY_ALT_HOVER,
                self.force_reocr_current_page,
            )
            self.btn_reocr_current_page.pack(pady=(0, 8), padx=12, fill="x")
        else:
            self.btn_start_current_page = None
            self.btn_reocr_current_page = None

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
            text=self._status_colon("等待选择文件"),
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
            text="成本监控初始化中...",
            font=("Arial", 12),
            justify="left",
            anchor="w"
        )
        self.usage_summary_label.pack(fill="x", padx=12, pady=(0, 12))

        self.right_frame = ctk.CTkFrame(self.paned_window, corner_radius=10)
        self.paned_window.add(self.right_frame, minsize=260, stretch="always")

        ctk.CTkLabel(
            self.right_frame,
            text=type(self).right_panel_title,
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
        self.text_editor.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        text_action_frame = ctk.CTkFrame(self.right_frame, fg_color=Color.TRANSPARENT)
        text_action_frame.pack(fill="x", padx=8, pady=(0, 10))
        Button(
            text_action_frame,
            text="💾 保存修改",
            height=38,
            fg_color=Color.BTN_SUCCESS,
            hover_color=Color.BTN_SUCCESS_HOVER,
            command=self.save_edits_to_disk,
        ).pack(fill="x")

        self._set_ocr_pages([type(self).idle_editor_hint])

        # 设置默认分栏宽度比例（阅读器:操作区:校对区 = 5:1.6:3）
        self.after(120, self._apply_default_pane_ratio)

    def _apply_default_pane_ratio(self):
        total_width = self.paned_window.winfo_width()
        if total_width <= 1:
            self.after(120, self._apply_default_pane_ratio)
            return

        ratios = [5.0, 1.6, 3.0]
        ratio_sum = sum(ratios)

        mid_w = max(400, int(total_width * ratios[0] / ratio_sum))
        action_w = max(180, int(total_width * ratios[1] / ratio_sum))
        right_w = max(260, int(total_width * ratios[2] / ratio_sum))

        # 通过设置 sash 位置来确定每一列初始宽度
        sash0 = mid_w
        sash1 = mid_w + action_w

        self.paned_window.sash_place(0, sash0, 0)
        self.paned_window.sash_place(1, sash1, 0)

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
        self.session_api_call_pages = 0
        self.session_cache_loaded_pages = 0
        self.session_max_tokens_per_call = 0
        self._refresh_usage_summary_label()

    def _refresh_usage_summary_label(self):
        current_model = self.selected_model_var.get() if hasattr(self, "selected_model_var") else "N/A"
        avg_tokens = (
            self.session_total_tokens / self.session_api_call_pages
            if self.session_api_call_pages > 0 else 0.0
        )
        usage_title = f"{type(self).task_short_name} 成本监控"
        self.usage_summary_label.configure(
            text=(
                f"{usage_title}\n"
                f"模型={current_model} | API调用页数={self.session_api_call_pages} | 本地缓存页数={self.session_cache_loaded_pages}\n"
                f"输入(非缓存)={self.session_prompt_non_cached} | 缓存命中={self.session_cached_tokens} | 输出={self.session_output_tokens}\n"
                f"总Token={self.session_total_tokens} | 平均/页={avg_tokens:.1f} | 峰值/页={self.session_max_tokens_per_call}\n"
                f"预估费用：JPY={self.session_cost_jpy:.4f} | CNY={self.session_cost_cny:.4f}"
            )
        )

    def _accumulate_usage_summary(self, usage_summary):
        if not usage_summary:
            return
        current_total = int(usage_summary.get("total_token_count", 0))
        self.session_prompt_non_cached += int(usage_summary.get("prompt_non_cached", 0))
        self.session_cached_tokens += int(usage_summary.get("cached_content_token_count", 0))
        self.session_output_tokens += int(usage_summary.get("candidates_token_count", 0))
        self.session_total_tokens += current_total
        self.session_cost_jpy += float(usage_summary.get("cost_jpy", 0.0))
        self.session_cost_cny += float(usage_summary.get("cost_cny", 0.0))
        self.session_api_call_pages += 1
        self.session_max_tokens_per_call = max(self.session_max_tokens_per_call, current_total)
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
        is_running = self.current_ocr_state == DocumentTaskState.RUNNING
        common_state = "disabled" if is_running else "normal"
        cancel_state = "normal" if is_running else "disabled"

        buttons_to_sync = [
            self.btn_start_ocr,
            self.btn_force_reocr,
            self.btn_clear_current_cache,
            self.btn_clear_cache,
        ]
        if self.btn_start_current_page is not None:
            buttons_to_sync.insert(2, self.btn_start_current_page)
            buttons_to_sync.insert(3, self.btn_reocr_current_page)

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

    def on_global_file_changed(self, pdf_path: str):
        if not pdf_path:
            return
        if self.selected_pdf_path == pdf_path:
            return
        self.open_pdf(pdf_path)

    def open_pdf(self, file_path):
        self.cancel_ocr_task(silent=True)
        # 切换 PDF 即视为新文档：清空旧 Chat Session 上下文。
        self.current_chat = None

        if self.current_pdf:
            self.pdf_service.close()
        try:
            self.current_pdf = self.pdf_service.open_pdf(file_path)
            self.current_page = 0
            self.zoom_factor = 1.0
            self.selected_pdf_path = file_path
            self.render_page()

            if not self._load_cached_ocr_for_pdf(file_path):
                total_pages = len(self.current_pdf)
                if not type(self).requires_image_input:
                    if not self._pdf_has_complete_ocr_baseline(file_path, total_pages):
                        notice = type(self).missing_full_ocr_notice
                        self._set_ocr_pages([notice])
                        self.ocr_progress_label.configure(text=self._status_colon(notice))
                    else:
                        self._set_ocr_pages(
                            self._hint_pdf_ready(os.path.basename(file_path))
                        )
                        self.ocr_progress_label.configure(
                            text=self._status_colon("文件已就绪，等待开始")
                        )
                else:
                    self._set_ocr_pages(
                        self._hint_pdf_ready(os.path.basename(file_path))
                    )
                    self.ocr_progress_label.configure(
                        text=self._status_colon("文件已就绪，等待开始")
                    )
                self.ocr_progress_bar.set(0)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开 PDF 文件: {e}")

    def _load_cached_ocr_for_pdf(self, pdf_path):
        cache_path = self._build_cache_path(pdf_path)
        if not os.path.exists(cache_path):
            return False
        try:
            pages = self.cache_service.read_paged_cache(cache_path)
            self._set_ocr_pages(pages)
            self.ocr_progress_label.configure(text=self._status_colon("已自动加载本地缓存"))
            self.ocr_progress_bar.set(1)
            return True
        except Exception:
            self.ocr_progress_label.configure(text=self._status_colon("缓存读取失败，等待开始"))
            self.ocr_progress_bar.set(0)
            return False

    def start_ocr_recognition(self):
        if not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return
        if not type(self).requires_image_input and not self._preflight_all_pages_have_ocr(
            self.selected_pdf_path
        ):
            return
        self._reset_usage_summary()
        self._set_ocr_state(DocumentTaskState.RUNNING)
        self.ocr_task_id += 1
        task_id = self.ocr_task_id
        self.ocr_cancel_event = threading.Event()
        pv = type(self).progress_verb
        self._set_ocr_pages(
            [
                f"正在调用 Gemini API {pv} {os.path.basename(self.selected_pdf_path)} ...\n"
                f"请稍候，正在逐页{pv}。"
            ]
        )
        self.ocr_progress_label.configure(text=self._status_colon("准备开始"))
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
                pages_text = self.cache_service.read_paged_cache(cache_path)
            except Exception:
                pages_text = []

        if len(pages_text) < total_pages:
            pages_text = list(pages_text) + [""] * (total_pages - len(pages_text))
        else:
            pages_text = list(pages_text[:total_pages])

        existing = (pages_text[page_index] or "").strip()
        sentinels = {
            "（本页未识别到文本）",
            "未识别到文本内容。",
            type(self).empty_page_marker,
        }
        if existing and existing not in sentinels:
            messagebox.showinfo(
                "提示",
                f"当前页已有内容，若想覆盖，请点击「{type(self).re_single_label}」",
            )
            return

        if self._warn_missing_ocr_for_current_page():
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
                pages_text = self.cache_service.read_paged_cache(cache_path)
            except Exception:
                pages_text = []

        if len(pages_text) < total_pages:
            pages_text = list(pages_text) + [""] * (total_pages - len(pages_text))
        else:
            pages_text = list(pages_text[:total_pages])

        if self._warn_missing_ocr_for_current_page():
            return

        self._start_single_page_worker(page_index, pages_text, total_pages)

    def _start_single_page_worker(self, page_index, pages_text, total_pages):
        self._reset_usage_summary()
        self._set_ocr_state(DocumentTaskState.RUNNING)
        self.ocr_task_id += 1
        task_id = self.ocr_task_id
        self.ocr_progress_label.configure(
            text=self._status_colon(f"正在单页{type(self).progress_verb}...")
        )
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

            api_key = (
                os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
                or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
                or load_gemini_api_key()
            )
            if not api_key:
                raise RuntimeError(
                    "未检测到 GOOGLE_GEMINI_API_KEY。请先配置该环境变量后再使用本功能。"
                )
            self.llm_service.update_api_key(api_key)

            file_tag = f"{os.path.basename(self.selected_pdf_path)}_第{page_index + 1}页"
            model_name = self.selected_model_var.get()

            # 单页任务同样初始化一次 Chat Session（即使只发一轮，也借助 system_instruction 走原生强约束）
            self._start_new_chat_session(api_key, model_name)

            if type(self).requires_image_input:
                image_bytes = self.pdf_service.get_page_bytes(page_index)
                result = self._call_page_with_retry(
                    task_id=task_id,
                    page_index=page_index,
                    total_pages=total_pages,
                    invoke_fn=lambda: self._request_page_text(
                        api_key,
                        file_name=file_tag,
                        model_name=model_name,
                        image_bytes=image_bytes,
                        page_index=page_index,
                    ),
                )
            else:
                ocr_text = self._get_ocr_text_for_page(self.selected_pdf_path, page_index)
                if not ocr_text:
                    raise RuntimeError(self.MISSING_OCR_FOR_CURRENT_PAGE_MSG)
                result = self._call_page_with_retry(
                    task_id=task_id,
                    page_index=page_index,
                    total_pages=total_pages,
                    invoke_fn=lambda: self._request_page_text(
                        api_key,
                        file_name=file_tag,
                        model_name=model_name,
                        source_text=ocr_text,
                        page_index=page_index,
                    ),
                )

            if task_id != self.ocr_task_id:
                return

            pages_text[page_index] = (result or "").strip() or type(self).empty_page_marker
            cache_path = self._build_cache_path(self.selected_pdf_path)
            cache_payload = self.cache_service.write_paged_cache(cache_path, pages_text)
            self._trace_cache_write(
                cache_path=cache_path,
                cache_kind=type(self).cache_dir_name,
                content=cache_payload,
                page_index=page_index,
            )

            def _apply_single_page_success():
                if task_id != self.ocr_task_id:
                    return
                self._set_ocr_pages(pages_text)
                self.current_ocr_page_index = page_index
                self._show_current_ocr_page()
                self.ocr_progress_label.configure(
                    text=self._status_colon(
                        f"单页{type(self).progress_verb}完成（第 {page_index + 1} 页）"
                    )
                )
                self.ocr_progress_bar.set(1)
                self._set_ocr_state(DocumentTaskState.DONE)

            self.after(0, _apply_single_page_success)
        except Exception as e:
            def _apply_single_page_error(err=e):
                if task_id != self.ocr_task_id:
                    return
                messagebox.showerror(
                    f"{type(self).task_short_name} 失败",
                    f"单页{type(self).progress_verb}失败:\n{err}",
                )
                self.ocr_progress_label.configure(
                    text=self._status_colon(f"单页{type(self).progress_verb}失败")
                )
                self.ocr_progress_bar.set(0)
                self._set_ocr_state(DocumentTaskState.ERROR)

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
            if str(e) == "DOC_TASK_CANCELLED":
                self.after(0, lambda: self._handle_ocr_cancelled(task_id))
                return
            if self.ocr_cancel_event.is_set():
                self.after(0, lambda: self._handle_ocr_cancelled(task_id))
                return
            self.after(0, lambda msg=err_msg: self._handle_ocr_failed(task_id, msg))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda msg=err_msg: self._handle_ocr_failed(task_id, msg))

    def _show_ocr_text_result(self, ocr_pages, task_id, from_cache):
        if task_id != self.ocr_task_id:
            return
        self._set_ocr_state(DocumentTaskState.DONE)
        if not ocr_pages:
            ocr_pages = ["未识别到文本内容。"]
        self._set_ocr_pages(ocr_pages)
        if from_cache:
            self.session_cache_loaded_pages = max(self.session_cache_loaded_pages, len(ocr_pages))
            self._refresh_usage_summary_label()
            self.ocr_progress_label.configure(text=self._status_colon("已完成（来自本地缓存）"))
            self.ocr_progress_bar.set(1)
        else:
            self.ocr_progress_label.configure(text=self._status_colon("已完成"))
            self.ocr_progress_bar.set(1)

    def _handle_ocr_cancelled(self, task_id):
        if task_id != self.ocr_task_id:
            return
        self._set_ocr_state(DocumentTaskState.CANCELLED)
        self.ocr_progress_label.configure(text=self._status_colon("已取消"))
        self.ocr_progress_bar.set(0)
        self._set_ocr_pages(self._hint_task_cancelled())

    def _handle_ocr_failed(self, task_id, reason):
        if task_id != self.ocr_task_id:
            return
        self._set_ocr_state(DocumentTaskState.ERROR)
        self.ocr_progress_label.configure(text=self._status_colon("失败"))
        self.ocr_progress_bar.set(0)
        full_reason = str(reason)
        if "请求超时" in full_reason:
            done = 0
            total = self.pdf_service.get_page_count() if self.current_pdf else 0
            if self.selected_pdf_path:
                cache_path = self._build_cache_path(self.selected_pdf_path)
                done = len(self.cache_service.read_paged_cache(cache_path))
            full_reason += (
                f"\n\n网络连接可能无响应，当前已保存进度：{done}/{total} 页。"
                "\n排查网络后可直接再次点击开始，系统会自动从断点继续。"
            )
            self.ocr_progress_label.configure(text=self._status_colon("网络无响应（已保留断点进度）"))
        messagebox.showerror(f"{type(self).task_short_name} 失败", full_reason)
        self.text_editor.insert(
            "end",
            f"\n\n{type(self).task_short_name} 失败，请检查网络连接和 GOOGLE_GEMINI_API_KEY 配置。",
        )

    def _extract_text_with_gemini_ocr(self, pdf_path, task_id):
        api_key = (
            os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
            or load_gemini_api_key()
        )
        if not api_key:
            raise RuntimeError(
                "未检测到 GOOGLE_GEMINI_API_KEY。请先配置该环境变量后再使用本功能。"
            )
        self.llm_service.update_api_key(api_key)

        if not self.current_pdf or self.selected_pdf_path != pdf_path:
            self.current_pdf = self.pdf_service.open_pdf(pdf_path)

        total_pages = self.pdf_service.get_page_count()
        if total_pages == 0:
            return [""], False

        cache_path = self._build_cache_path(pdf_path)
        all_page_texts = [""] * total_pages
        start_page = 0

        if os.path.exists(cache_path):
            cached_pages = self.cache_service.read_paged_cache(cache_path)
            if cached_pages:
                usable = min(len(cached_pages), total_pages)
                all_page_texts[:usable] = cached_pages[:usable]

                first_incomplete = usable
                for i in range(usable):
                    if not str(all_page_texts[i]).strip():
                        first_incomplete = i
                        break

                if first_incomplete >= total_pages:
                    self.after(
                        0,
                        lambda: self.ocr_progress_label.configure(
                            text=self._status_colon("已完成（来自本地缓存）")
                        ),
                    )
                    self.after(0, lambda: self.ocr_progress_bar.set(1))
                    return all_page_texts, True

                start_page = first_incomplete
                self.after(
                    0,
                    lambda s=start_page, t=total_pages: self.ocr_progress_label.configure(
                        text=self._status_colon(f"检测到断点缓存：已完成 {s}/{t} 页，继续处理中...")
                    ),
                )

        # v2.6.6：进入逐页循环前，初始化（或覆盖）Chat Session，作为 Analysis/Translation 的多轮通道。
        self._start_new_chat_session(api_key, self.selected_model_var.get())

        for page_index in range(start_page, total_pages):
            self._ensure_active_task(task_id)
            self._update_ocr_progress(task_id, page_index, total_pages)
            file_tag = f"{os.path.basename(pdf_path)}_第{page_index + 1}页"
            model_name = self.selected_model_var.get()

            if type(self).requires_image_input:
                image_bytes = self.pdf_service.get_page_bytes(page_index)
                page_text = self._call_page_with_retry(
                    task_id=task_id,
                    page_index=page_index,
                    total_pages=total_pages,
                    invoke_fn=lambda: self._request_page_text(
                        api_key,
                        file_name=file_tag,
                        model_name=model_name,
                        image_bytes=image_bytes,
                        page_index=page_index,
                    ),
                )
            else:
                ocr_src = self._get_ocr_text_for_page(pdf_path, page_index)
                if not ocr_src:
                    raise RuntimeError(
                        f"第 {page_index + 1} 页缺少可用的 OCR 文本。"
                        "请先在「史料校对」页面完成 OCR 提取与校对！"
                    )
                page_text = self._call_page_with_retry(
                    task_id=task_id,
                    page_index=page_index,
                    total_pages=total_pages,
                    invoke_fn=lambda: self._request_page_text(
                        api_key,
                        file_name=file_tag,
                        model_name=model_name,
                        source_text=ocr_src,
                        page_index=page_index,
                    ),
                )
            marker = type(self).empty_page_marker
            all_page_texts[page_index] = page_text.strip() if page_text else marker
            partial_payload = self.cache_service.write_paged_cache(cache_path, all_page_texts[: page_index + 1])
            self._trace_cache_write(
                cache_path=cache_path,
                cache_kind=type(self).cache_dir_name,
                content=partial_payload,
                page_index=page_index,
            )

        cache_payload = self.cache_service.write_paged_cache(cache_path, all_page_texts)
        self._trace_cache_write(
            cache_path=cache_path,
            cache_kind=type(self).cache_dir_name,
            content=cache_payload,
            page_index=None,
        )
        return all_page_texts, False

    def _parse_cached_ocr_pages(self, cached_text):
        return self.cache_service.parse_paged_text(cached_text)

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
        self._sync_pdf_page_to_text_page()

    def _save_current_ocr_page(self):
        if not self.ocr_pages:
            self.ocr_pages = [""]
            self.current_ocr_page_index = 0
        self.ocr_pages[self.current_ocr_page_index] = self.text_editor.get("0.0", "end").strip()

    def _can_bind_pdf_and_text_page(self) -> bool:
        if not self.current_pdf:
            return False
        total_pdf_pages = self.pdf_service.get_page_count()
        if total_pdf_pages <= 0:
            return False
        # 只有当右侧是有效分页结果（页数与 PDF 一致）时才启用双向绑定，避免提示文案干扰。
        return len(self.ocr_pages) == total_pdf_pages

    def _sync_pdf_page_to_text_page(self) -> None:
        if not self._can_bind_pdf_and_text_page():
            return
        target = max(0, min(self.current_ocr_page_index, self.pdf_service.get_page_count() - 1))
        if target == self.current_page:
            return
        self.current_page = target
        self.render_page()

    def _sync_text_page_to_pdf_page(self) -> None:
        if not self._can_bind_pdf_and_text_page():
            return
        target = max(0, min(self.current_page, len(self.ocr_pages) - 1))
        if target == self.current_ocr_page_index:
            return
        self._save_current_ocr_page()
        self.current_ocr_page_index = target
        self._show_current_ocr_page()

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

    def save_edits_to_disk(self) -> None:
        if not self.selected_pdf_path:
            messagebox.showwarning("提示", "请先在左侧选择一个 PDF 文件。")
            return
        self._save_current_ocr_page()
        if not self.ocr_pages:
            messagebox.showwarning("提示", "没有可保存的内容。")
            return
        cache_path = self._build_cache_path(self.selected_pdf_path)
        pages = ["" if p is None else str(p) for p in self.ocr_pages]
        try:
            self.cache_service.write_paged_cache(cache_path, pages)
        except OSError as e:
            messagebox.showerror("错误", f"保存失败：{e}")
            return
        messagebox.showinfo("提示", "当前修改已成功保存至本地缓存！")

    def _build_cache_path(self, pdf_path):
        return self.cache_service.build_cache_path(pdf_path, self.document_cache_dir)

    def _ensure_active_task(self, task_id):
        if task_id != self.ocr_task_id or self.ocr_cancel_event.is_set():
            raise RuntimeError("DOC_TASK_CANCELLED")

    def _update_ocr_progress(self, task_id, page_index, total_pages):
        if task_id != self.ocr_task_id:
            return
        text = self._status_colon(
            f"正在{type(self).progress_verb}第 {page_index + 1} / {total_pages} 页"
        )
        self.after(0, lambda: self.ocr_progress_label.configure(text=text))
        ratio = 0 if total_pages <= 0 else ((page_index + 1) / total_pages)
        self.after(0, lambda: self.ocr_progress_bar.set(ratio))

    @staticmethod
    def _is_timeout_error(err: Exception) -> bool:
        msg = str(err)
        return ("请求超时" in msg) or ("timed out" in msg.lower())

    def _call_page_with_retry(self, *, task_id: int, page_index: int, total_pages: int, invoke_fn):
        max_attempts = max(1, int(type(self).api_timeout_max_attempts))
        last_err = None
        for attempt in range(1, max_attempts + 1):
            self._ensure_active_task(task_id)
            try:
                return invoke_fn()
            except Exception as e:
                last_err = e
                if not self._is_timeout_error(e) or attempt >= max_attempts:
                    raise
                wait_s = min(8, 2 * attempt)
                self.after(
                    0,
                    lambda a=attempt, w=wait_s: self.ocr_progress_label.configure(
                        text=self._status_colon(
                            f"第 {page_index + 1}/{total_pages} 页请求超时，正在进行第 {a + 1} 次重试（{w}s 后）"
                        )
                    ),
                )
                time.sleep(wait_s)
        raise last_err

    def cancel_ocr_task(self, silent=False):
        self.ocr_cancel_event.set()
        if not silent:
            timeout_s = max(1, int(type(self).api_request_timeout_sec))
            self.ocr_progress_label.configure(
                text=self._status_colon(f"正在取消...等待当前页请求结束（最长约 {timeout_s}s）")
            )
            self.ocr_progress_bar.set(0)

    def clear_ocr_cache(self):
        confirmed = messagebox.askyesno("严重警告", self._clear_all_cache_warning_message())
        if not confirmed:
            return
        removed_count, failed_count = self.cache_service.clear_directory(self.document_cache_dir)

        if failed_count > 0:
            messagebox.showwarning("提示", f"已清理 {removed_count} 个缓存文件，另有 {failed_count} 个文件删除失败。")
        else:
            if removed_count == 0:
                messagebox.showinfo("提示", "缓存目录不存在或为空，已确保目录可用。")
            else:
                messagebox.showinfo("提示", f"缓存已清空，共删除 {removed_count} 个文件。")
        # 由全局侧栏负责文件列表渲染与刷新

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
            self._set_ocr_state(DocumentTaskState.IDLE)
            self._set_ocr_pages(self._hint_cleared_file_cache(current_pdf_name))
            self.ocr_progress_label.configure(text=self._status_colon("文件已就绪，等待开始"))
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
        self.start_ocr_recognition()

    def _start_new_chat_session(self, api_key: str, model_name: str) -> None:
        """在进入逐页循环前调用：渲染 system_prompt 并新建/覆盖 self.current_chat。

        - 仅当 `use_chat_session=True` 时生效；否则将 `self.current_chat` 置空走 OCR 单次通道；
        - 新文档加载或"重新开始"会自动调用一次，从而覆盖旧会话上下文。
        """
        self.current_chat = None
        if not type(self).use_chat_session:
            return
        self.llm_service.update_api_key(api_key)
        system_prompt = self.get_system_prompt()
        self.current_chat = self.llm_service.start_chat_session(
            model_name=model_name,
            system_instruction=system_prompt,
            response_mime_type=type(self).chat_response_mime_type,
            temperature=type(self).chat_temperature,
            screen_name=type(self).__name__,
            task_name=type(self).task_short_name,
            selected_pdf_path=self.selected_pdf_path,
        )

    def _request_page_text(
        self,
        api_key: str,
        file_name: str = "未知",
        model_name: str = "gemini-3.1-pro-preview",
        *,
        image_bytes: bytes | None = None,
        source_text: str | None = None,
        page_index: int = None,
    ):
        self.llm_service.update_api_key(api_key)
        enrich_hook = self.enrich_json_data if hasattr(self, "enrich_json_data") else None

        if type(self).use_chat_session and self.current_chat is not None:
            # Analysis / Translation：走有状态 Chat Session 通道
            turn_prompt = self.get_turn_prompt(page_index, source_text or "")
            result_text, usage_summary = self.llm_service.send_chat_message(
                self.current_chat,
                screen_name=type(self).__name__,
                task_name=type(self).task_short_name,
                selected_pdf_path=self.selected_pdf_path,
                file_name=file_name,
                model_name=model_name,
                turn_prompt=turn_prompt,
                behavior_name=type(self).task_short_name,
                page_index=page_index,
                enrich_json_data=enrich_hook,
            )
        else:
            # OCR 或未启用 Chat Session 的子类：单次 generate_content
            result_text, usage_summary = self.llm_service.detect_text(
                screen_name=type(self).__name__,
                task_name=type(self).task_short_name,
                selected_pdf_path=self.selected_pdf_path,
                file_name=file_name,
                model_name=model_name,
                academic_prompt=self.get_academic_prompt(page_index),
                behavior_name=type(self).task_short_name,
                page_index=page_index,
                image_bytes=image_bytes,
                source_text=source_text,
                enrich_json_data=enrich_hook,
            )

        self.after(0, lambda s=usage_summary: self._accumulate_usage_summary(s))
        return result_text

    def _detect_text_from_image(
        self,
        api_key: str,
        file_name: str = "未知",
        model_name: str = "gemini-3.1-pro-preview",
        *,
        image_bytes: bytes | None = None,
        source_text: str | None = None,
        page_index: int = None,
    ):
        """兼容旧调用名，统一转发到 `_request_page_text`。"""
        return self._request_page_text(
            api_key=api_key,
            file_name=file_name,
            model_name=model_name,
            image_bytes=image_bytes,
            source_text=source_text,
            page_index=page_index,
        )

    def render_page(self):
        if not self.current_pdf:
            return
        total_pages = self.pdf_service.get_page_count()
        self.page_label.configure(text=f"页码: {self.current_page + 1} / {total_pages}")
        self.tk_image = self.pdf_service.render_page_image(self.current_page, self.zoom_factor)

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
            self._sync_text_page_to_pdf_page()

    def next_page(self):
        if self.current_pdf and self.current_page < len(self.current_pdf) - 1:
            self.current_page += 1
            self.render_page()
            self._sync_text_page_to_pdf_page()
