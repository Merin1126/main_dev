from __future__ import annotations

import os
import glob
import json
import re
import hashlib
import threading
import time
import random
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from enum import Enum, auto
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
from components.ui.button import Button
from config.settings import (
    Color,
    ANALYSIS_EXPLICIT_CACHE_ENABLED,
    ANALYSIS_CACHE_TTL_SECONDS,
    ANALYSIS_CACHE_AUTO_REFRESH_TTL,
)
from config.api_key_store import load_google_api_key as load_gemini_api_key
from services import CacheService, LlmService, PdfService
from services.report_service import ReportService
from utils.app_state import AppState
from utils.docx_export import write_pages_to_docx
from utils.open_path import open_path_in_system
from utils.token_logger import log_context_cache_event, storage_hours_between

class DocumentTaskState(Enum):
    IDLE = auto()
    RUNNING = auto()
    DONE = auto()
    ERROR = auto()
    CANCELLED = auto()


DOC_TASK_CANCELLED = "DOC_TASK_CANCELLED"
DEBUG_LOG_PATH = "/Users/merin/本地文稿/Historical Records Scraper/main_dev/.cursor/debug-b75604.log"
DEBUG_SESSION_ID = "b75604"


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # region agent log
    try:
        payload = {
            "sessionId": DEBUG_SESSION_ID,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # endregion


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
    #: 非超时类瞬时错误（503/429/断连）重试总次数（含首次调用）
    api_retryable_max_attempts: int = 2
    #: 非超时类重试退避基准秒数
    api_retry_backoff_base_sec: int = 2
    #: 非超时类重试退避上限秒数
    api_retry_backoff_cap_sec: int = 6
    #: 非超时类重试退避抖动上限秒数（用于错开并发重试）
    api_retry_backoff_jitter_sec: float = 0.8
    #: 连续 API 请求最小间隔秒数（软限流，0 表示不限制）
    api_min_request_interval_sec: float = 0.0
    #: Chat Session 历史轮次达到阈值后自动滚动重建（<=0 关闭）
    chat_history_rotate_threshold: int = 12
    #: 是否以有状态 Chat Session 调用 LLM；OCR/Analysis 已切换为无状态生成，Translation 保留 True。
    use_chat_session: bool = False
    #: Chat Session 的响应 MIME 类型；旧字段，保留兼容 Translation。
    chat_response_mime_type: str = "text/plain"
    #: Chat Session 的温度参数（仅在 use_chat_session=True 时生效）；旧字段，保留兼容。
    chat_temperature: float = 0.3
    #: 单次生成（stateless）/ Chat 共用的响应 MIME 类型 alias。Analysis 应覆盖为 "application/json"。
    #: 若子类未设置，运行时回退到 `chat_response_mime_type` 以保证 Translation 等旧路径不被破坏。
    generation_response_mime_type: str | None = None
    #: 单次生成（stateless）/ Chat 共用的温度参数 alias。若子类未设置则回退到 `chat_temperature`。
    generation_temperature: float | None = None

    @classmethod
    def _effective_generation_response_mime_type(cls) -> str:
        value = getattr(cls, "generation_response_mime_type", None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return cls.chat_response_mime_type
        return str(value)

    @classmethod
    def _effective_generation_temperature(cls) -> float:
        value = getattr(cls, "generation_temperature", None)
        if value is None:
            return cls.chat_temperature
        try:
            return float(value)
        except (TypeError, ValueError):
            return cls.chat_temperature

    def get_academic_prompt(self, page_index: int = None) -> str:
        """返回发送给 Gemini 的学术/任务提示词（用于 OCR 单次调用路径）。

        路由约定：
        - OCR：仍走单次调用并使用本方法返回的 prompt；
        - Translation：走有状态 Chat Session，由 `get_system_prompt()` + `get_turn_prompt()` 分担；
        - Analysis：走无状态 generate + explicit context cache + context capsule，
          同样由 `get_system_prompt()`（静态进入缓存）+ `get_turn_prompt()`（动态每页）分担。
        本方法对 Analysis/Translation 不再被调用，可保留默认实现。
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

    @staticmethod
    def _extract_transcription_text(text: str) -> str:
        """兼容 OCR XML 输出：优先抽取 <transcription> 内容，无标签时回退原文。"""
        raw = (text or "").strip()
        if not raw:
            return ""
        match = re.search(r"<transcription>\s*(.*?)\s*</transcription>", raw, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return raw
        return (match.group(1) or "").strip()

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
        raw_text = (pages[page_index] or "").strip()
        normalized = self._extract_transcription_text(raw_text)
        if self._ocr_cached_plaintext_is_usable(normalized):
            return normalized
        # 容错：若 XML 抽取失败或标签为空，回退旧版纯文本缓存。
        if self._ocr_cached_plaintext_is_usable(raw_text):
            return raw_text
        if not self._ocr_cached_plaintext_is_usable(normalized):
            return ""
        return normalized

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
        export_pages = [
            self._extract_transcription_text(str(page or ""))
            for page in (self.ocr_pages or [])
        ]
        export_pages = [p for p in export_pages if p.strip()]
        if not export_pages:
            messagebox.showwarning("提示", "导出内容为空！")
            return
        text_content = "\n\n".join(export_pages).strip()
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
                write_pages_to_docx(file_path, export_pages)
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
            min_interval_sec=type(self).api_min_request_interval_sec,
        )
        self.download_dir = os.path.join(base_dir, "JACAR_Downloads")
        self.document_cache_dir = os.path.join(base_dir, cls.cache_dir_name)
        self._ocr_cache_dir = os.path.join(base_dir, "OCR_Cache")
        self._analysis_cache_dir = os.path.join(base_dir, "Analysis_Cache")
        self._translation_cache_dir = os.path.join(base_dir, "Translation_Cache")
        self._report_service = ReportService(project_root=base_dir)
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
        #: 有状态 Chat 会话句柄（仅 Translation 等启用 `use_chat_session` 的子类使用）；
        #: 每次任务开始时通过 `_prepare_task_context()` 重建。Analysis 已切换为 stateless 路径，
        #: 该字段在 Analysis 任务期间始终为 None。
        self.current_chat = None
        self.current_context_cache_name = ""
        self.current_context_cache_meta: dict = {}
        self.session_prompt_non_cached = 0
        self.session_cached_tokens = 0
        self.session_output_tokens = 0
        self.session_total_tokens = 0
        self.session_cost_jpy = 0.0
        self.session_cost_cny = 0.0
        self.session_api_call_pages = 0
        self.session_cache_loaded_pages = 0
        self.session_max_tokens_per_call = 0

        #: 任务级 cache rebuild 计数（每次 _prepare_task_context 重置）。
        #: 配合 `_MAX_CACHE_REBUILDS_PER_TASK` 用于防雪崩：超限后改走 plain prompt，
        #: 避免因 cache 反复失效导致每页都付一次 cache write 费。
        self._task_cache_rebuild_count: int = 0
        self._task_cache_disabled: bool = False

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

        self.document_title_bar = ctk.CTkFrame(
            self.mid_frame,
            fg_color=("#e8edf2", "#2a3038"),
            corner_radius=8,
            border_width=1,
            border_color=("#cbd5e1", "#3c4452"),
        )
        self.document_title_bar.pack(fill="x", padx=5, pady=(0, 4))
        self.document_title_label = ctk.CTkLabel(
            self.document_title_bar,
            text="请从左侧史料文件库选择 PDF",
            font=("Arial", 12),
            text_color=Color.TEXT,
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.document_title_label.pack(fill="x", anchor="w", padx=10, pady=8)
        self.document_title_bar.bind("<Configure>", self._sync_document_title_wrap)
        self._copy_toast_win = None
        self._copy_toast_after_id = None
        for widget in (self.document_title_bar, self.document_title_label):
            widget.bind("<Button-1>", self._on_document_title_click)
        
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

        ctk.CTkLabel(
            self.action_frame,
            text="打开关联文件",
            font=("Arial", 12, "bold"),
        ).pack(anchor="w", padx=12, pady=(4, 4))

        # 操作区较窄（paned minsize≈180）：并排两列时右侧按钮会被裁切，故改为纵向全宽。
        artifact_cache_col = ctk.CTkFrame(self.action_frame, fg_color=Color.TRANSPARENT)
        artifact_cache_col.pack(fill="x", padx=12, pady=(0, 4))
        for label, cmd in (
            ("OCR 缓存", self.open_ocr_cache_file),
            ("分析缓存", self.open_analysis_cache_file),
            ("翻译缓存", self.open_translation_cache_file),
        ):
            Button(
                artifact_cache_col,
                text=label,
                height=30,
                command=cmd,
            ).pack(fill="x", pady=(0, 4))

        Button(
            self.action_frame,
            text="汇报(导出2)",
            height=30,
            command=self.open_summary_report_file,
        ).pack(fill="x", padx=12, pady=(0, 8))

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
            self._update_selected_document_title(None)
            return
        if self.selected_pdf_path == pdf_path:
            return
        self.open_pdf(pdf_path)

    def _sync_document_title_wrap(self, _event=None) -> None:
        if not hasattr(self, "document_title_label"):
            return
        try:
            width = max(200, int(self.document_title_bar.winfo_width()) - 24)
        except tk.TclError:
            return
        self.document_title_label.configure(wraplength=width)

    def _update_selected_document_title(self, file_path: str | None) -> None:
        if not hasattr(self, "document_title_label"):
            return
        if not file_path:
            self.document_title_label.configure(
                text="请从左侧史料文件库选择 PDF",
                cursor="",
            )
            self.document_title_bar.configure(cursor="")
            self._sync_document_title_wrap()
            return
        full_name = os.path.basename(file_path)
        self.document_title_label.configure(text=full_name, cursor="hand2")
        self.document_title_bar.configure(cursor="hand2")
        self._sync_document_title_wrap()

    def _document_title_clipboard_text(self) -> str:
        raw = (self.document_title_label.cget("text") or "").strip()
        if not raw or raw == "请从左侧史料文件库选择 PDF":
            return ""
        return os.path.splitext(raw)[0]

    def _on_document_title_click(self, _event=None) -> None:
        text = self._document_title_clipboard_text()
        if not text:
            return
        top = self.winfo_toplevel()
        top.clipboard_clear()
        top.clipboard_append(text)
        top.update_idletasks()
        self._show_copy_success_toast()

    def _show_copy_success_toast(self, *, duration_ms: int = 500) -> None:
        self._dismiss_copy_success_toast()
        toast = ctk.CTkToplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(fg_color=("#1e293b", "#334155"))
        ctk.CTkLabel(
            toast,
            text="复制成功",
            font=("Arial", 13),
            text_color=("#f8fafc", "#f1f5f9"),
        ).pack(padx=18, pady=10)
        toast.update_idletasks()
        bar = self.document_title_bar
        x = bar.winfo_rootx() + max(bar.winfo_width(), 1) // 2
        y = bar.winfo_rooty() + max(bar.winfo_height(), 1) // 2
        w = max(toast.winfo_width(), 1)
        h = max(toast.winfo_height(), 1)
        toast.geometry(f"+{x - w // 2}+{y - h // 2}")
        self._copy_toast_win = toast
        self._copy_toast_after_id = self.after(duration_ms, self._dismiss_copy_success_toast)

    def _dismiss_copy_success_toast(self) -> None:
        if self._copy_toast_after_id is not None:
            try:
                self.after_cancel(self._copy_toast_after_id)
            except (tk.TclError, ValueError):
                pass
            self._copy_toast_after_id = None
        win = self._copy_toast_win
        self._copy_toast_win = None
        if win is not None:
            try:
                if win.winfo_exists():
                    win.destroy()
            except tk.TclError:
                pass

    def open_pdf(self, file_path):
        self.cancel_ocr_task(silent=True)
        # 切换 PDF 即视为新文档：清空旧 Chat Session 上下文。
        self.current_chat = None
        self.current_context_cache_name = ""
        self.current_context_cache_meta = {}

        if self.current_pdf:
            self.pdf_service.close()
        try:
            self.current_pdf = self.pdf_service.open_pdf(file_path)
            self.current_page = 0
            self.zoom_factor = 1.0
            self.selected_pdf_path = file_path
            self._update_selected_document_title(file_path)
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
            self._update_selected_document_title(None)
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
        # 关键修复：单页任务开始前必须清空历史取消状态，
        # 否则会被 _ensure_active_task() 立即判定为 DOC_TASK_CANCELLED。
        self.ocr_cancel_event = threading.Event()
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
        pdf_path_for_task = self.selected_pdf_path
        # 默认任务收尾会删除远端 cache；retryable 错误下保留以便断点续传命中复用。
        preserve_for_resume = False
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

            # 单页任务同样初始化一次任务上下文（Analysis: explicit cache；Translation: chat）。
            self._prepare_task_context(api_key, model_name)

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
            preserve_for_resume = type(self)._is_retryable_error(e)
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
        finally:
            self._cleanup_task_context_cache(pdf_path_for_task, preserve_for_resume=preserve_for_resume)

    def _start_ocr_worker(self, file_path, task_id):
        worker = threading.Thread(target=self._run_ocr_worker, args=(file_path, task_id), daemon=True)
        worker.start()

    def _run_ocr_worker(self, file_path, task_id):
        # 默认任务收尾会删除远端 cache；retryable 错误下保留以便断点续传命中复用。
        preserve_for_resume = False
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
            preserve_for_resume = type(self)._is_retryable_error(e)
            self.after(0, lambda msg=err_msg: self._handle_ocr_failed(task_id, msg))
        except Exception as e:
            err_msg = str(e)
            preserve_for_resume = type(self)._is_retryable_error(e)
            self.after(0, lambda msg=err_msg: self._handle_ocr_failed(task_id, msg))
        finally:
            self._cleanup_task_context_cache(file_path, preserve_for_resume=preserve_for_resume)

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

        # 进入逐页循环前初始化任务上下文：Analysis 走 explicit cache，Translation 仍走 Chat Session。
        self._prepare_task_context(api_key, self.selected_model_var.get())

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

    def _selected_pdf_or_warn(self) -> str | None:
        path = self.selected_pdf_path
        if not path or not os.path.isfile(path):
            messagebox.showwarning("提示", "请先在左侧史料文件库选择一条 PDF。")
            return None
        return path

    def _open_existing_file(self, path: str, *, label: str) -> None:
        if not path or not os.path.isfile(path):
            messagebox.showinfo(
                "文件不存在",
                f"{label}不存在。\n\n预期路径：\n{path or '(无法解析路径)'}",
            )
            return
        try:
            open_path_in_system(path)
        except FileNotFoundError:
            messagebox.showinfo("文件不存在", f"{label}不存在。\n\n{path}")
        except Exception as exc:
            messagebox.showerror("打开失败", f"无法打开 {label}：\n{exc}")

    def open_ocr_cache_file(self) -> None:
        pdf_path = self._selected_pdf_or_warn()
        if not pdf_path:
            return
        cache_path = self._build_ocr_cache_path(pdf_path)
        self._open_existing_file(cache_path, label="OCR 缓存文件")

    def open_analysis_cache_file(self) -> None:
        pdf_path = self._selected_pdf_or_warn()
        if not pdf_path:
            return
        cache_path = self.cache_service.build_cache_path(pdf_path, self._analysis_cache_dir)
        self._open_existing_file(cache_path, label="分析缓存文件")

    def open_translation_cache_file(self) -> None:
        pdf_path = self._selected_pdf_or_warn()
        if not pdf_path:
            return
        cache_path = self.cache_service.build_cache_path(pdf_path, self._translation_cache_dir)
        self._open_existing_file(cache_path, label="Translation 缓存文件")

    def open_summary_report_file(self) -> None:
        """打开进度汇报「内容2」生成的 summary Markdown（导出2）。"""
        pdf_path = self._selected_pdf_or_warn()
        if not pdf_path:
            return
        probe = self._report_service.probe_export_artifacts(pdf_path)
        md_path = str(probe.get("summary_md_path") or "")
        if not probe.get("summary_md_exists"):
            expected = self._report_service.expected_summary_md_path(
                pdf_path,
                self._report_service.default_summary_dir(),
            )
            messagebox.showinfo(
                "文件不存在",
                "汇报文件（内容2 总结 MD）不存在。\n\n"
                f"预期路径：\n{expected}\n\n"
                "请先在「进度汇报中心」生成内容2 总结。",
            )
            return
        self._open_existing_file(md_path, label="汇报文件（导出2）")

    @staticmethod
    def _iso_utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _to_iso_datetime(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        iso = getattr(value, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                return str(value)
        return str(value)

    def _is_analysis_context_cache_enabled(self) -> bool:
        return bool(
            ANALYSIS_EXPLICIT_CACHE_ENABLED
            and not type(self).requires_image_input
            and type(self).cache_dir_name == "Analysis_Cache"
        )

    def _uses_stateless_analysis_generation(self) -> bool:
        """Analysis 无状态通道：单次 generate + explicit context cache + context capsule。"""
        return bool(
            not type(self).requires_image_input
            and type(self).cache_dir_name == "Analysis_Cache"
        )

    @staticmethod
    def _effective_context_cache_ttl_seconds() -> int:
        try:
            ttl = int(ANALYSIS_CACHE_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = 7200
        return max(300, ttl)

    #: 命中复用时若远端 cache 剩余 TTL 低于该阈值（秒），主动续期至完整 TTL，
    #: 避免长任务中途过期触发 CACHED_CONTENT_INVALID 雪崩式重建。
    _CONTEXT_CACHE_TTL_HEADROOM_SEC: int = 1800  # 30 分钟

    #: 单次任务允许的 cache rebuild 次数上限。超过后改走 plain prompt（带 system_instruction、
    #: 不带 cached_content），避免每页都付一次 cache write 费的雪崩成本。
    _MAX_CACHE_REBUILDS_PER_TASK: int = 2

    @classmethod
    def _remaining_seconds_to_expire(cls, expire_iso: str) -> int | None:
        """解析 ISO 时间字符串，返回距当前的剩余秒数；解析失败返回 None。"""
        if not expire_iso:
            return None
        text = str(expire_iso).strip()
        if not text:
            return None
        try:
            # 兼容尾部 'Z' 的 UTC 标记
            normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
            expire_dt = datetime.fromisoformat(normalized)
        except Exception:
            return None
        if expire_dt.tzinfo is None:
            expire_dt = expire_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return int((expire_dt - now).total_seconds())

    def _build_context_cache_payload_text(self, pdf_path: str) -> str:
        """拼接 OCR 全文作为 explicit cache 的静态上下文。

        【页号连续性】空 OCR 页改为写入占位行而非跳过，确保模型在缓存内看到
        与实际 PDF 一致的页号序列，避免"丢页"假象。`has_content` 仍按是否
        存在非空页判断，全空 PDF 直接返回空串，跳过 cache 上传。
        """
        total_pages = self.pdf_service.count_pages(pdf_path)
        chunks = [
            "以下是该 PDF 的 OCR 全文（按页拼接）。后续问答请以此为文档上下文。",
        ]
        has_content = False
        for idx in range(total_pages):
            page_text = (self._get_ocr_text_for_page(pdf_path, idx) or "").strip()
            if page_text:
                has_content = True
                chunks.append(f"\n[Page {idx + 1}]\n{page_text}")
            else:
                chunks.append(f"\n[Page {idx + 1}] (本页 OCR 缺失或空白)")
        return "\n".join(chunks).strip() if has_content else ""

    def _resolve_context_cache_tokens(self, cache_obj, meta: dict | None) -> int:
        """优先 CachedContent.usage_metadata，其次 sidecar 中的 cache_token_count。"""
        count = self.llm_service.extract_context_cache_token_count(cache_obj)
        if count > 0:
            return count
        if isinstance(meta, dict):
            try:
                return max(0, int(meta.get("cache_token_count", 0) or 0))
            except (TypeError, ValueError):
                return 0
        return 0

    @staticmethod
    def _context_cache_storage_anchor(meta: dict | None) -> str:
        if not isinstance(meta, dict):
            return ""
        return str(
            meta.get("storage_cost_anchor_at") or meta.get("created_at") or ""
        ).strip()

    def _build_context_cache_cost_log_payload(
        self,
        *,
        pdf_path: str | None,
        meta: dict | None,
        cache_obj=None,
        model_name: str | None = None,
    ) -> dict:
        """组装 reuse/delete 写入 api_cache_cost_log 的字段。"""
        meta = meta if isinstance(meta, dict) else {}
        resolved_model = (
            (model_name or "").strip()
            or str(meta.get("model", "")).strip()
            or (self.selected_model_var.get() if hasattr(self, "selected_model_var") else "")
        )
        if resolved_model.startswith("models/"):
            resolved_model = resolved_model[len("models/") :]
        anchor = self._context_cache_storage_anchor(meta)
        if not anchor and cache_obj is not None:
            anchor = self.llm_service.extract_context_cache_create_time_iso(cache_obj)
        return {
            "model_name": resolved_model,
            "cache_tokens": self._resolve_context_cache_tokens(cache_obj, meta),
            "storage_hours": storage_hours_between(anchor) if anchor else 0.0,
            "file_name": os.path.basename(pdf_path) if pdf_path else "",
        }

    def _emit_context_cache_storage_cost(
        self,
        *,
        event: str,
        pdf_path: str | None,
        cache_name: str,
        meta: dict | None,
        cache_obj=None,
        model_name: str | None = None,
        cache_path: str | None = None,
        advance_anchor: bool = False,
    ) -> None:
        """记录 reuse/delete 的 storage 切片（create 的 write 在 llm_service 内结算）。"""
        if not self._is_analysis_context_cache_enabled():
            return
        payload = self._build_context_cache_cost_log_payload(
            pdf_path=pdf_path,
            meta=meta,
            cache_obj=cache_obj,
            model_name=model_name,
        )
        storage_hours = float(payload.get("storage_hours", 0) or 0)
        if storage_hours <= 0 and event == "reuse":
            return
        try:
            log_context_cache_event(
                event=event,
                model_name=str(payload.get("model_name", "") or ""),
                cache_name=cache_name,
                cache_tokens=int(payload.get("cache_tokens", 0) or 0),
                storage_hours=storage_hours,
                file_name=str(payload.get("file_name", "") or ""),
                reused=(event == "reuse"),
                bill_write=False,
            )
        except Exception:
            return
        if advance_anchor and cache_path and isinstance(meta, dict):
            try:
                updated = dict(meta)
                updated["storage_cost_anchor_at"] = self._iso_utc_now()
                tokens = int(payload.get("cache_tokens", 0) or 0)
                if tokens > 0:
                    updated["cache_token_count"] = tokens
                self.cache_service.write_context_meta(cache_path, updated)
            except Exception:
                pass

    def _build_context_source_fingerprint(
        self,
        *,
        pdf_path: str,
        model_name: str,
        system_prompt: str,
        source_text: str,
    ) -> str:
        """计算用于复用判定的 fingerprint。

        【设计原则】fingerprint 仅基于"同一份内容"维度，**不**包含 `pdf_path`：
        - `source_text` 已经覆盖了 OCR 全文实际内容；
        - `st_mtime_ns + st_size` 用作内容微变动的快速判别；
        - `model_name + system_prompt` 保证模型/指令变更时强制重建。

        这样用户把同一份 PDF 移动到另一个目录、或重命名后，仍能复用之前的
        cache，避免白付一次 cache write 费。
        """
        stat = os.stat(pdf_path)
        system_hash = hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()
        source_hash = hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()
        seed = (
            f"{stat.st_mtime_ns}|{stat.st_size}|"
            f"{model_name}|{system_hash}|{source_hash}"
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def _clear_context_cache_for_pdf(self, pdf_path: str, *, try_remote_delete: bool) -> None:
        """清理某 PDF 对应的本地 sidecar + 远端 cache。

        【孤儿防护】当 `try_remote_delete=True` 且远端删除失败时（网络异常/超时/SDK 报错），
        **不再删除本地 sidecar**，而是把它就地改写为 `pending_delete=true`，等启动巡检
        （`patrol_orphan_context_caches`）兜底再删一次，避免静默泄漏远端 cache 持续计费。
        """
        if not pdf_path:
            return
        cache_path = self._build_cache_path(pdf_path)
        meta = self.cache_service.read_context_meta(cache_path)
        cache_name = str(meta.get("cache_name", "")).strip() if isinstance(meta, dict) else ""
        remote_delete_failed = False
        if try_remote_delete and cache_name:
            cache_obj = None
            cost_log = None
            try:
                cache_obj = self.llm_service.get_context_cache(cache_name=cache_name)
            except Exception:
                cache_obj = None
            cost_log = self._build_context_cache_cost_log_payload(
                pdf_path=pdf_path,
                meta=meta if isinstance(meta, dict) else None,
                cache_obj=cache_obj,
            )
            try:
                self.llm_service.delete_context_cache(
                    cache_name=cache_name,
                    screen_name=type(self).__name__,
                    task_name=type(self).task_short_name,
                    selected_pdf_path=pdf_path,
                    cost_log=cost_log,
                )
            except Exception:
                remote_delete_failed = True
        if try_remote_delete and remote_delete_failed and cache_name and isinstance(meta, dict):
            # 远端删除失败：保留 sidecar 并打 pending_delete 标记 + 时间戳，启动巡检会兜底再删一次。
            try:
                marked_meta = dict(meta)
                marked_meta["pending_delete"] = True
                marked_meta["pending_delete_marked_at"] = self._iso_utc_now()
                self.cache_service.write_context_meta(cache_path, marked_meta)
            except Exception:
                # 即便 sidecar 改写失败也无需阻塞业务流，启动巡检的"远端列表反向比对"路径仍能兜底。
                pass
        else:
            self.cache_service.delete_context_meta(cache_path)
        if self.selected_pdf_path == pdf_path:
            self.current_context_cache_name = ""
            self.current_context_cache_meta = {}

    def _resolve_context_cache_name(
        self,
        *,
        api_key: str,
        model_name: str,
        system_prompt: str,
    ) -> str | None:
        if not self._is_analysis_context_cache_enabled():
            return None
        pdf_path = self.selected_pdf_path
        if not pdf_path:
            return None

        source_text = self._build_context_cache_payload_text(pdf_path)
        if not source_text:
            return None

        ttl_seconds = self._effective_context_cache_ttl_seconds()
        cache_path = self._build_cache_path(pdf_path)
        system_prompt_version = hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()
        source_fingerprint = self._build_context_source_fingerprint(
            pdf_path=pdf_path,
            model_name=model_name,
            system_prompt=system_prompt,
            source_text=source_text,
        )
        old_meta = self.cache_service.read_context_meta(cache_path)
        if isinstance(old_meta, dict):
            old_cache_name = str(old_meta.get("cache_name", "")).strip()
            old_model = str(old_meta.get("model", "")).strip()
            old_fingerprint = str(old_meta.get("source_fingerprint", "")).strip()
            old_prompt_ver = str(old_meta.get("system_prompt_version", "")).strip()
            # `pending_delete=true` 的 sidecar 表示远端 cache 已尝试删除但失败，
            # 远端状态不可信（可能已删/可能仍在/可能损坏），一律不复用，等巡检兜底清理。
            old_pending_delete = bool(old_meta.get("pending_delete"))
            if (
                old_cache_name
                and not old_pending_delete
                and old_model == model_name
                and old_fingerprint == source_fingerprint
                and old_prompt_ver == system_prompt_version
            ):
                try:
                    self.llm_service.update_api_key(api_key)
                    cache_obj = self.llm_service.get_context_cache(cache_name=old_cache_name)
                    expire_iso = self._to_iso_datetime(getattr(cache_obj, "expire_time", ""))
                    remaining_sec = self._remaining_seconds_to_expire(expire_iso)
                    headroom = int(self._CONTEXT_CACHE_TTL_HEADROOM_SEC)
                    needs_refresh = bool(
                        ANALYSIS_CACHE_AUTO_REFRESH_TTL
                        or (remaining_sec is not None and remaining_sec < headroom)
                    )
                    if needs_refresh:
                        # 续期失败不应该把"已经能用"的旧 cache 拖垮，因此用独立 try 包裹；
                        # 失败时退化为"仅使用 get 拿到的旧 cache_obj"，远端会按原 TTL 自然过期。
                        try:
                            refreshed_obj = self.llm_service.update_context_cache_ttl(
                                cache_name=old_cache_name,
                                ttl_seconds=ttl_seconds,
                                screen_name=type(self).__name__,
                                task_name=type(self).task_short_name,
                                selected_pdf_path=self.selected_pdf_path,
                            )
                            cache_obj = refreshed_obj or cache_obj
                        except Exception:
                            # 续期失败：保留旧 cache_obj 继续复用，避免整体重建带来的 cache write 费。
                            pass
                    create_iso = (
                        str(old_meta.get("created_at", "")).strip()
                        or self.llm_service.extract_context_cache_create_time_iso(cache_obj)
                        or self._iso_utc_now()
                    )
                    cache_tokens = self._resolve_context_cache_tokens(cache_obj, old_meta)
                    storage_anchor = (
                        self._context_cache_storage_anchor(old_meta) or create_iso
                    )
                    refreshed_meta = {
                        "cache_name": old_cache_name,
                        "model": model_name,
                        "created_at": create_iso,
                        "storage_cost_anchor_at": storage_anchor,
                        "cache_token_count": cache_tokens,
                        "expire_time": self._to_iso_datetime(getattr(cache_obj, "expire_time", "")),
                        "source_fingerprint": source_fingerprint,
                        "system_prompt_version": system_prompt_version,
                    }
                    self._emit_context_cache_storage_cost(
                        event="reuse",
                        pdf_path=pdf_path,
                        cache_name=old_cache_name,
                        meta=refreshed_meta,
                        cache_obj=cache_obj,
                        model_name=model_name,
                    )
                    refreshed_meta["storage_cost_anchor_at"] = self._iso_utc_now()
                    self.cache_service.write_context_meta(cache_path, refreshed_meta)
                    self.current_context_cache_name = old_cache_name
                    self.current_context_cache_meta = refreshed_meta
                    try:
                        self.llm_service.trace_context_cache_lifecycle(
                            screen_name=type(self).__name__,
                            task_name=type(self).task_short_name,
                            selected_pdf_path=self.selected_pdf_path,
                            event="context_cache_reused",
                            cache_name=old_cache_name,
                            model_name=model_name,
                            ttl_seconds=ttl_seconds,
                            source_fingerprint=source_fingerprint,
                            reason="meta_fingerprint_hit",
                        )
                    except Exception:
                        pass
                    return old_cache_name
                except Exception:
                    try:
                        self.llm_service.trace_context_cache_lifecycle(
                            screen_name=type(self).__name__,
                            task_name=type(self).task_short_name,
                            selected_pdf_path=self.selected_pdf_path,
                            event="context_cache_invalidated",
                            cache_name=old_cache_name,
                            model_name=model_name,
                            source_fingerprint=source_fingerprint,
                            reason="stale_or_remote_lookup_failed",
                        )
                    except Exception:
                        pass
                    try:
                        cost_log = self._build_context_cache_cost_log_payload(
                            pdf_path=pdf_path,
                            meta=old_meta if isinstance(old_meta, dict) else None,
                            cache_obj=cache_obj,
                            model_name=model_name,
                        )
                        self.llm_service.delete_context_cache(
                            cache_name=old_cache_name,
                            screen_name=type(self).__name__,
                            task_name=type(self).task_short_name,
                            selected_pdf_path=self.selected_pdf_path,
                            cost_log=cost_log,
                        )
                    except Exception:
                        pass

        self.llm_service.update_api_key(api_key)
        created = self.llm_service.create_context_cache(
            model_name=model_name,
            system_instruction=system_prompt,
            cache_text=source_text,
            ttl_seconds=ttl_seconds,
            display_name=f"analysis:{os.path.basename(pdf_path)}",
        )
        new_cache_name = str(getattr(created, "name", "")).strip()
        if not new_cache_name:
            return None
        create_iso = (
            self.llm_service.extract_context_cache_create_time_iso(created)
            or self._iso_utc_now()
        )
        cache_tokens = self._resolve_context_cache_tokens(created, None)
        new_meta = {
            "cache_name": new_cache_name,
            "model": model_name,
            "created_at": create_iso,
            "storage_cost_anchor_at": create_iso,
            "cache_token_count": cache_tokens,
            "expire_time": self._to_iso_datetime(getattr(created, "expire_time", "")),
            "source_fingerprint": source_fingerprint,
            "system_prompt_version": system_prompt_version,
        }
        self.cache_service.write_context_meta(cache_path, new_meta)
        self.current_context_cache_name = new_cache_name
        self.current_context_cache_meta = new_meta
        try:
            self.llm_service.trace_context_cache_lifecycle(
                screen_name=type(self).__name__,
                task_name=type(self).task_short_name,
                selected_pdf_path=self.selected_pdf_path,
                event="context_cache_created",
                cache_name=new_cache_name,
                model_name=model_name,
                ttl_seconds=ttl_seconds,
                source_fingerprint=source_fingerprint,
                reason="create_new_context_cache",
                extra_payload={"system_prompt_version": system_prompt_version},
            )
        except Exception:
            pass
        return new_cache_name

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

    @classmethod
    def _is_retryable_error(cls, err: Exception) -> bool:
        if cls._is_timeout_error(err):
            return True
        msg = str(err).lower()
        keywords = (
            "503",
            "unavailable",
            "429",
            "resource_exhausted",
            "rate limit",
            "server disconnected without sending a response",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
        )
        return any(k in msg for k in keywords)

    def _call_page_with_retry(self, *, task_id: int, page_index: int, total_pages: int, invoke_fn):
        timeout_max_attempts = max(1, int(type(self).api_timeout_max_attempts))
        retryable_max_attempts = max(1, int(type(self).api_retryable_max_attempts))
        retry_base = max(1, int(type(self).api_retry_backoff_base_sec))
        retry_cap = max(retry_base, int(type(self).api_retry_backoff_cap_sec))
        retry_jitter = max(0.0, float(type(self).api_retry_backoff_jitter_sec))
        timeout_attempts = 0
        retryable_attempts = 0
        last_err = None
        _debug_log(
            "run1",
            "H1",
            "screens/base_screen.py:_call_page_with_retry:entry",
            "retry loop entered",
            {
                "task": type(self).task_short_name,
                "pageIndex": page_index,
                "totalPages": total_pages,
                "timeoutMaxAttempts": timeout_max_attempts,
                "retryableMaxAttempts": retryable_max_attempts,
                "retryBase": retry_base,
                "retryCap": retry_cap,
                "retryJitter": retry_jitter,
            },
        )
        while True:
            self._ensure_active_task(task_id)
            try:
                return invoke_fn()
            except Exception as e:
                last_err = e
                is_timeout = self._is_timeout_error(e)
                _debug_log(
                    "run1",
                    "H2",
                    "screens/base_screen.py:_call_page_with_retry:exception",
                    "invoke exception captured",
                    {
                        "task": type(self).task_short_name,
                        "pageIndex": page_index,
                        "isTimeout": is_timeout,
                        "isRetryable": type(self)._is_retryable_error(e),
                        "errorType": type(e).__name__,
                        "error": str(e)[:260],
                    },
                )
                if is_timeout:
                    timeout_attempts += 1
                    if timeout_attempts >= timeout_max_attempts:
                        _debug_log(
                            "run1",
                            "H3",
                            "screens/base_screen.py:_call_page_with_retry:timeout-exhausted",
                            "timeout retries exhausted",
                            {
                                "task": type(self).task_short_name,
                                "pageIndex": page_index,
                                "timeoutAttempts": timeout_attempts,
                                "timeoutMaxAttempts": timeout_max_attempts,
                            },
                        )
                        raise
                    wait_s = min(retry_cap, retry_base * (2 ** max(0, timeout_attempts - 1)))
                    if retry_jitter > 0:
                        wait_s += random.uniform(0.0, retry_jitter)
                    _debug_log(
                        "run1",
                        "H4",
                        "screens/base_screen.py:_call_page_with_retry:timeout-wait",
                        "timeout retry scheduled",
                        {
                            "task": type(self).task_short_name,
                            "pageIndex": page_index,
                            "attempt": timeout_attempts + 1,
                            "waitSeconds": round(wait_s, 3),
                        },
                    )
                    self.after(
                        0,
                        lambda a=timeout_attempts, w=wait_s: self.ocr_progress_label.configure(
                            text=self._status_colon(
                                f"第 {page_index + 1}/{total_pages} 页请求超时，正在进行第 {a + 1} 次重试（{w}s 后）"
                            )
                        ),
                    )
                    time.sleep(wait_s)
                    continue

                if not type(self)._is_retryable_error(e):
                    _debug_log(
                        "run1",
                        "H5",
                        "screens/base_screen.py:_call_page_with_retry:non-retryable",
                        "error treated as non-retryable",
                        {
                            "task": type(self).task_short_name,
                            "pageIndex": page_index,
                            "errorType": type(e).__name__,
                            "error": str(e)[:260],
                        },
                    )
                    raise

                retryable_attempts += 1
                if retryable_attempts >= retryable_max_attempts:
                    _debug_log(
                        "run1",
                        "H6",
                        "screens/base_screen.py:_call_page_with_retry:retryable-exhausted",
                        "retryable retries exhausted",
                        {
                            "task": type(self).task_short_name,
                            "pageIndex": page_index,
                            "retryableAttempts": retryable_attempts,
                            "retryableMaxAttempts": retryable_max_attempts,
                        },
                    )
                    raise
                wait_s = min(retry_cap, retry_base * (2 ** max(0, retryable_attempts - 1)))
                if retry_jitter > 0:
                    wait_s += random.uniform(0.0, retry_jitter)
                _debug_log(
                    "run1",
                    "H7",
                    "screens/base_screen.py:_call_page_with_retry:retryable-wait",
                    "retryable error retry scheduled",
                    {
                        "task": type(self).task_short_name,
                        "pageIndex": page_index,
                        "attempt": retryable_attempts + 1,
                        "waitSeconds": round(wait_s, 3),
                        "error": str(e)[:180],
                    },
                )
                self.after(
                    0,
                    lambda a=retryable_attempts, w=wait_s: self.ocr_progress_label.configure(
                        text=self._status_colon(
                            f"第 {page_index + 1}/{total_pages} 页服务繁忙/连接中断，正在进行第 {a + 1} 次重试（{w}s 后）"
                        )
                    ),
                )
                time.sleep(wait_s)
        raise last_err

    def cancel_ocr_task(self, silent=False):
        """取消当前任务。

        【R3 弹窗确认】仅当用户主动点击「取消任务」按钮（`silent=False`）、
        且当前正在运行的是会产生远端 context cache 的 Analysis 任务时，
        先弹窗明确告知后果（远端 cache 删除 + 下次重新分析需重付 cache write 费），
        用户确认后才继续取消。`silent=True`（如 `open_pdf` 切换文件时的静默取消）
        不弹窗，保留原有行为。
        """
        is_running = self.current_ocr_state == DocumentTaskState.RUNNING
        if (
            not silent
            and is_running
            and self._uses_stateless_analysis_generation()
        ):
            confirmed = messagebox.askyesno(
                "⚠️ 取消确认",
                (
                    f"取消当前「{type(self).task_short_name}」任务将立即触发以下操作：\n\n"
                    "• 删除 Google 端的 explicit context cache；\n"
                    "• 本地已完成页的分析结果保留（可作为断点续传起点）；\n"
                    "• 下一次重新开始分析时，需要重新支付一次 cache write 费用，"
                    "并占用一段 cache storage 时段。\n\n"
                    "若是临时网络波动，建议等待自动重试；\n"
                    "若确实需要中止，请点击「是」。"
                ),
            )
            if not confirmed:
                return
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
        self._clear_context_cache_for_pdf(self.selected_pdf_path, try_remote_delete=True)
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
        self._clear_context_cache_for_pdf(self.selected_pdf_path, try_remote_delete=True)
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                pass
        self.start_ocr_recognition()

    def _prepare_analysis_context_cache(self, api_key: str, model_name: str) -> str:
        """Analysis 任务级缓存准备：创建/复用 explicit cache 并记录当前 cache 名称。"""
        self.current_chat = None
        self.current_context_cache_name = ""
        if not self._uses_stateless_analysis_generation():
            return ""
        if not self._is_analysis_context_cache_enabled():
            raise RuntimeError(
                "Analysis 显式上下文缓存未启用。请在 config/settings.py 中打开 ANALYSIS_EXPLICIT_CACHE_ENABLED。"
            )
        self.llm_service.update_api_key(api_key)
        system_prompt = self.get_system_prompt()
        cached_content = self._resolve_context_cache_name(
            api_key=api_key,
            model_name=model_name,
            system_prompt=system_prompt,
        )
        if not cached_content:
            raise RuntimeError("Analysis context cache 准备失败：未获取到有效 cached_content。")
        self.current_context_cache_name = cached_content
        return cached_content

    def _prepare_task_context(self, api_key: str, model_name: str) -> None:
        """任务级上下文准备：
        - Analysis（无状态生成）走 explicit context cache 路径；
        - 其余启用了 chat session 的子类（如 Translation）按需创建 Chat 实例。
        """
        self.current_chat = None
        self.current_context_cache_name = ""
        # 每次任务起点重置 rebuild 计数与 plain-prompt 降级标志。
        self._task_cache_rebuild_count = 0
        self._task_cache_disabled = False
        if self._uses_stateless_analysis_generation():
            self._prepare_analysis_context_cache(api_key, model_name)
            return
        if not type(self).use_chat_session:
            return
        self.llm_service.update_api_key(api_key)
        system_prompt = self.get_system_prompt()
        cached_content = self._resolve_context_cache_name(
            api_key=api_key,
            model_name=model_name,
            system_prompt=system_prompt,
        )
        try:
            self.current_chat = self.llm_service.start_chat_session(
                model_name=model_name,
                system_instruction=system_prompt,
                response_mime_type=type(self).chat_response_mime_type,
                temperature=type(self).chat_temperature,
                cached_content=cached_content,
                screen_name=type(self).__name__,
                task_name=type(self).task_short_name,
                selected_pdf_path=self.selected_pdf_path,
            )
            self.current_context_cache_name = cached_content or ""
        except RuntimeError as e:
            if cached_content and "CACHED_CONTENT_INVALID" in str(e):
                # 显式缓存过期/失效后仅重建一次，避免无限重试。
                # 尝试同步删除远端旧 cache（15s 短超时保护）：即使删除失败也不会阻塞重建，
                # 但能减少远端孤儿 + 重复 storage 计费的概率。
                self._clear_context_cache_for_pdf(self.selected_pdf_path, try_remote_delete=True)
                rebuilt_cache = self._resolve_context_cache_name(
                    api_key=api_key,
                    model_name=model_name,
                    system_prompt=system_prompt,
                )
                self.current_chat = self.llm_service.start_chat_session(
                    model_name=model_name,
                    system_instruction=system_prompt,
                    response_mime_type=type(self).chat_response_mime_type,
                    temperature=type(self).chat_temperature,
                    cached_content=rebuilt_cache,
                    screen_name=type(self).__name__,
                    task_name=type(self).task_short_name,
                    selected_pdf_path=self.selected_pdf_path,
                )
                self.current_context_cache_name = rebuilt_cache or ""
                return
            raise

    def _start_new_chat_session(self, api_key: str, model_name: str) -> None:
        """Deprecated（保留兼容）：原始命名暗示总是创建 chat session，已被 `_prepare_task_context` 取代。

        请在新代码中使用 `_prepare_task_context(...)`；本函数仅作为薄包装存在，
        避免破坏可能存在的外部子类调用点。
        """
        self._prepare_task_context(api_key, model_name)

    # ------------------------------------------------------------------ #
    # R1: 启动孤儿 cache 巡检
    # ------------------------------------------------------------------ #

    #: 启动后延迟多少毫秒再触发巡检（让 UI 先就绪，避免和初次渲染抢线程）。
    _ORPHAN_PATROL_START_DELAY_MS: int = 5000

    def schedule_orphan_context_cache_patrol(self) -> None:
        """登记一次后台巡检：清理本机/远端遗留的孤儿 context cache。

        - 仅在子类启用 Analysis stateless cache 链路时实际跑（其它任务无 sidecar）；
        - 用 `after` 延迟 + 守护线程执行，**绝不阻塞 UI 启动**；
        - 任何异常都被静默吞掉，巡检失败不影响后续业务。
        """
        if not self._is_analysis_context_cache_enabled():
            return
        try:
            self.after(
                int(self._ORPHAN_PATROL_START_DELAY_MS),
                lambda: threading.Thread(
                    target=self._run_orphan_patrol_safely,
                    daemon=True,
                ).start(),
            )
        except Exception:
            pass

    def _run_orphan_patrol_safely(self) -> None:
        """巡检线程入口：先加载 API key，再执行 `patrol_orphan_context_caches`。"""
        try:
            api_key = (
                os.getenv("GOOGLE_GEMINI_API_KEY", "").strip()
                or os.getenv("GOOGLE_VISION_API_KEY", "").strip()
                or load_gemini_api_key()
            )
            if not api_key:
                return
            self.llm_service.update_api_key(api_key)
            stats = self.patrol_orphan_context_caches()
            _debug_log(
                "run_patrol",
                "R1",
                "screens/base_screen.py:patrol_orphan_context_caches",
                "orphan context cache patrol finished",
                stats,
            )
        except Exception as e:
            _debug_log(
                "run_patrol",
                "R1",
                "screens/base_screen.py:patrol_orphan_context_caches",
                "orphan patrol failed silently",
                {"error": str(e)[:240]},
            )

    def patrol_orphan_context_caches(self) -> dict[str, int]:
        """巡检并清理孤儿 context cache。

        步骤：
        1. 扫 `document_cache_dir` 下所有 `*.context.json` sidecar；
        2. 对 `pending_delete=true` 的 sidecar：再次尝试远端 delete，成功则连本地一起删；
        3. 对正常 sidecar：`get_context_cache` 验证远端状态：
           - 远端 404/异常 → 删本地 sidecar（防止下次错误命中）；
           - 远端存在 → 计入 known set，等下一次正常复用；
        4. 反向扫 `list_context_caches`，找远端有但 `display_name` 是 Analysis 创建且
           不在 known set 中的远端孤儿（极少数：进程崩溃前 cache 已创建但 sidecar 未落盘），
           直接删除。
        """
        stats: dict[str, int] = {
            "scanned_sidecars": 0,
            "removed_dead_local": 0,
            "completed_pending_delete": 0,
            "removed_remote_orphan": 0,
            "errors": 0,
        }
        if not self._is_analysis_context_cache_enabled():
            return stats
        cache_dir = self.document_cache_dir
        if not cache_dir or not os.path.isdir(cache_dir):
            return stats

        known_cache_names: set[str] = set()
        sidecar_paths = glob.glob(os.path.join(cache_dir, "*.context.json"))
        for sidecar_path in sidecar_paths:
            stats["scanned_sidecars"] += 1
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if not isinstance(meta, dict):
                    continue
                cache_name = str(meta.get("cache_name", "")).strip()
                if not cache_name:
                    continue
                pending = bool(meta.get("pending_delete"))

                if pending:
                    # 上一次 delete 失败留下的 pending：再试一次。
                    try:
                        cost_log = self._build_context_cache_cost_log_payload(
                            pdf_path=None,
                            meta=meta,
                            cache_obj=None,
                            model_name=str(meta.get("model", "")).strip() or None,
                        )
                        self.llm_service.delete_context_cache(
                            cache_name=cache_name,
                            screen_name=type(self).__name__,
                            task_name=type(self).task_short_name,
                            selected_pdf_path=None,
                            cost_log=cost_log,
                        )
                        try:
                            os.remove(sidecar_path)
                        except OSError:
                            pass
                        stats["completed_pending_delete"] += 1
                    except Exception:
                        # 仍然失败：保留 sidecar，下次启动再试。
                        stats["errors"] += 1
                    continue

                # 正常 sidecar：验证远端存活。
                try:
                    self.llm_service.get_context_cache(cache_name=cache_name)
                    known_cache_names.add(cache_name)
                except Exception:
                    # 远端 404 / 不可达 → 清掉死 sidecar，避免后续误命中。
                    try:
                        os.remove(sidecar_path)
                    except OSError:
                        pass
                    stats["removed_dead_local"] += 1
            except Exception:
                stats["errors"] += 1
                continue

        # 反向扫远端，找游离的远端 cache。
        try:
            all_remote = self.llm_service.list_context_caches()
            for remote_cache in all_remote:
                name = str(getattr(remote_cache, "name", "")).strip()
                display = str(getattr(remote_cache, "display_name", "") or "").strip()
                if not name or name in known_cache_names:
                    continue
                # 仅清理 Analysis 自己创建的（前缀约定见 `_resolve_context_cache_name`）。
                if not display.startswith("analysis:"):
                    continue
                try:
                    create_iso = self.llm_service.extract_context_cache_create_time_iso(
                        remote_cache
                    )
                    cost_log = {
                        "model_name": str(getattr(remote_cache, "model", "") or "").replace(
                            "models/", ""
                        ),
                        "cache_tokens": self.llm_service.extract_context_cache_token_count(
                            remote_cache
                        ),
                        "storage_hours": storage_hours_between(create_iso)
                        if create_iso
                        else 0.0,
                        "file_name": display,
                    }
                    self.llm_service.delete_context_cache(
                        cache_name=name,
                        screen_name=type(self).__name__,
                        task_name=type(self).task_short_name,
                        selected_pdf_path=None,
                        cost_log=cost_log,
                    )
                    stats["removed_remote_orphan"] += 1
                except Exception:
                    stats["errors"] += 1
        except Exception:
            # list 失败（无权限 / 配额 / 网络）：忽略，下次启动再试。
            pass

        return stats

    def _cleanup_task_context_cache(
        self,
        pdf_path: str | None,
        *,
        preserve_for_resume: bool = False,
    ) -> None:
        """任务收尾：Analysis 任务的远端 context cache 清理。

        - `preserve_for_resume=False`（默认）：删除远端 cache + 本地 sidecar，
          适用于"成功完成 / 用户主动取消 / 非 retryable 错误"等场景；
        - `preserve_for_resume=True`：仅清理内存中的引用，**保留 sidecar 与远端 cache**，
          专为"网络/超时等 retryable 错误"设计，方便下一次断点续传直接命中复用，
          避免重复支付 cache write 费。
        """
        if not self._uses_stateless_analysis_generation():
            return
        target_pdf = pdf_path or self.selected_pdf_path
        self.current_chat = None
        if not target_pdf:
            return
        if preserve_for_resume:
            # 仅切断 in-memory 引用，sidecar 与远端 cache 由下次任务复用或 TTL 自然过期。
            if self.selected_pdf_path == target_pdf:
                self.current_context_cache_name = ""
                self.current_context_cache_meta = {}
            return
        self._clear_context_cache_for_pdf(target_pdf, try_remote_delete=True)

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

        if self._uses_stateless_analysis_generation():
            turn_prompt = self.get_turn_prompt(page_index, source_text or "")
            gen_mime = type(self)._effective_generation_response_mime_type()
            gen_temp = type(self)._effective_generation_temperature()
            max_rebuilds = max(0, int(type(self)._MAX_CACHE_REBUILDS_PER_TASK))

            # 任务级 plain-prompt 降级：若本任务前面页已触发过 cache rebuild 上限，
            # 后续页直接走「带 system_instruction、不带 cached_content」路径，
            # 不再尝试重建 cache，避免每页都付一次 cache write 费。
            if self._task_cache_disabled:
                system_prompt_fallback = self.get_system_prompt()
                result_text, usage_summary = self.llm_service.generate_text_once(
                    screen_name=type(self).__name__,
                    task_name=type(self).task_short_name,
                    selected_pdf_path=self.selected_pdf_path,
                    file_name=file_name,
                    model_name=model_name,
                    prompt_text=turn_prompt,
                    behavior_name=type(self).task_short_name,
                    page_index=page_index,
                    enrich_json_data=enrich_hook,
                    response_mime_type=gen_mime,
                    temperature=gen_temp,
                    cached_content=None,
                    system_instruction=system_prompt_fallback,
                )
                self.after(0, lambda s=usage_summary: self._accumulate_usage_summary(s))
                return result_text

            cached_content = self.current_context_cache_name or None
            if not cached_content:
                cached_content = self._prepare_analysis_context_cache(api_key, model_name)
            try:
                result_text, usage_summary = self.llm_service.generate_text_once(
                    screen_name=type(self).__name__,
                    task_name=type(self).task_short_name,
                    selected_pdf_path=self.selected_pdf_path,
                    file_name=file_name,
                    model_name=model_name,
                    prompt_text=turn_prompt,
                    behavior_name=type(self).task_short_name,
                    page_index=page_index,
                    enrich_json_data=enrich_hook,
                    response_mime_type=gen_mime,
                    temperature=gen_temp,
                    cached_content=cached_content,
                )
            except RuntimeError as e:
                if cached_content and "CACHED_CONTENT_INVALID" in str(e):
                    # 同步尝试删除远端无效 cache（15s 短超时保护），减少远端孤儿计费概率。
                    self._clear_context_cache_for_pdf(self.selected_pdf_path, try_remote_delete=True)
                    self._task_cache_rebuild_count += 1
                    if self._task_cache_rebuild_count > max_rebuilds:
                        # 触发雪崩防护：本任务从这一页开始改走 plain prompt（不再重建 cache）。
                        self._task_cache_disabled = True
                        system_prompt_fallback = self.get_system_prompt()
                        result_text, usage_summary = self.llm_service.generate_text_once(
                            screen_name=type(self).__name__,
                            task_name=type(self).task_short_name,
                            selected_pdf_path=self.selected_pdf_path,
                            file_name=file_name,
                            model_name=model_name,
                            prompt_text=turn_prompt,
                            behavior_name=type(self).task_short_name,
                            page_index=page_index,
                            enrich_json_data=enrich_hook,
                            response_mime_type=gen_mime,
                            temperature=gen_temp,
                            cached_content=None,
                            system_instruction=system_prompt_fallback,
                        )
                    else:
                        rebuilt_cache = self._prepare_analysis_context_cache(api_key, model_name)
                        result_text, usage_summary = self.llm_service.generate_text_once(
                            screen_name=type(self).__name__,
                            task_name=type(self).task_short_name,
                            selected_pdf_path=self.selected_pdf_path,
                            file_name=file_name,
                            model_name=model_name,
                            prompt_text=turn_prompt,
                            behavior_name=type(self).task_short_name,
                            page_index=page_index,
                            enrich_json_data=enrich_hook,
                            response_mime_type=gen_mime,
                            temperature=gen_temp,
                            cached_content=rebuilt_cache,
                        )
                else:
                    raise
            self.after(0, lambda s=usage_summary: self._accumulate_usage_summary(s))
            return result_text

        if type(self).use_chat_session and self.current_chat is not None:
            rotate_threshold = max(0, int(type(self).chat_history_rotate_threshold))
            if rotate_threshold > 0:
                history_count = self.llm_service.get_chat_history_count(self.current_chat)
                _debug_log(
                    "run4",
                    "H13",
                    "screens/base_screen.py:_request_page_text:rotate-check",
                    "chat rotation check snapshot",
                    {
                        "task": type(self).task_short_name,
                        "pageIndex": page_index,
                        "historyCount": history_count,
                        "rotateThreshold": rotate_threshold,
                        "timeoutSec": int(type(self).api_request_timeout_sec),
                        "timeoutMaxAttempts": int(type(self).api_timeout_max_attempts),
                    },
                )
                if history_count >= rotate_threshold:
                    _debug_log(
                        "run3",
                        "H12",
                        "screens/base_screen.py:_request_page_text:rotate",
                        "chat session rotated by history threshold",
                        {
                            "task": type(self).task_short_name,
                            "pageIndex": page_index,
                            "historyCount": history_count,
                            "threshold": rotate_threshold,
                        },
                    )
                    self._start_new_chat_session(api_key, model_name)
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
            # OCR 路径：单次 generate_content（无 chat history，无 explicit cache）。
            # 注：Analysis 的无状态分支在函数前部已 `return`，本分支不会被 Analysis 命中。
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
