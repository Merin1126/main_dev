import customtkinter as ctk
import os
from tkinter import messagebox

from components.ui.button import Button
from config.settings import Color
from screens.report_summary_window import ReportSummaryWindow
from utils.trace_report import convert_current_trace_to_md, delete_current_converted_trace_md
from config.api_key_store import (
    load_google_api_key as load_gemini_api_key,
    save_google_api_key as save_gemini_api_key,
    clear_google_api_key as clear_gemini_api_key,
    load_ocr_preprocess_config,
    save_ocr_preprocess_config,
    load_trace_config,
    save_trace_config,
    mask_api_key,
)
from config.settings import OCR_PREPROCESS_ENABLED, OCR_PREPROCESS_MODE


class SettingScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT, **kwargs)
        self.master = master
        self.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.report_window = None
        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        container = ctk.CTkScrollableFrame(self, corner_radius=10)
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            container,
            text="\U0000E690 系统与环境设置",
            font=("Symbols Nerd Font", 22, "bold")
        ).pack(anchor="w", padx=16, pady=(16, 8))

        ctk.CTkLabel(
            container,
            text="Google Gemini API Key（本地保存，不写入代码）",
            font=("Arial", 14)
        ).pack(anchor="w", padx=16, pady=(8, 6))

        self.api_entry = ctk.CTkEntry(
            container,
            width=520,
            height=40,
            show="*",
            placeholder_text="请输入 Google Gemini API Key"
        )
        self.api_entry.pack(anchor="w", padx=16, pady=(0, 8))

        self.api_hint_label = ctk.CTkLabel(
            container,
            text="当前状态：未配置",
            font=("Arial", 12)
        )
        self.api_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        Button(
            container,
            text="保存 API Key",
            width=180,
            height=40,
            command=self.save_api_key
        ).pack(anchor="w", padx=16, pady=(0, 10))

        Button(
            container,
            text="清空 API Key",
            width=180,
            height=40,
            fg_color=Color.BTN_WARNING,
            hover_color=Color.BTN_WARNING_HOVER,
            command=self.clear_api_key
        ).pack(anchor="w", padx=16, pady=(0, 10))

        ctk.CTkLabel(
            container,
            text="建议：优先使用环境变量；若必须本地保存，请不要共享项目目录与 .secrets 文件。",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE
        ).pack(anchor="w", padx=16, pady=(2, 16))

        ctk.CTkLabel(
            container,
            text="Gemini 调用追踪（本地 JSONL 日志）",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(8, 6))

        self.trace_enabled_var = ctk.BooleanVar(value=False)
        self.trace_full_text_var = ctk.BooleanVar(value=True)
        self.ocr_preprocess_enabled_var = ctk.BooleanVar(value=OCR_PREPROCESS_ENABLED)
        self.ocr_preprocess_mode_var = ctk.StringVar(value=OCR_PREPROCESS_MODE)

        ctk.CTkCheckBox(
            container,
            text="启用追踪日志（记录请求 Prompt、模型响应、缓存落盘）",
            variable=self.trace_enabled_var,
        ).pack(anchor="w", padx=16, pady=(0, 6))

        ctk.CTkCheckBox(
            container,
            text="记录完整文本（关闭则仅保留预览片段）",
            variable=self.trace_full_text_var,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="保存追踪设置",
            width=180,
            height=40,
            command=self.save_trace_settings,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        self.trace_hint_label = ctk.CTkLabel(
            container,
            text="追踪状态：未启用",
            font=("Arial", 12),
        )
        self.trace_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        ctk.CTkLabel(
            container,
            text="OCR 图像预处理（识别前增强）",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(8, 6))

        ctk.CTkCheckBox(
            container,
            text="启用 OCR 图像预处理",
            variable=self.ocr_preprocess_enabled_var,
        ).pack(anchor="w", padx=16, pady=(0, 6))

        ctk.CTkLabel(
            container,
            text="增强档位",
            font=("Arial", 12),
            text_color=Color.TEXT_HINT_TUPLE,
        ).pack(anchor="w", padx=16, pady=(0, 4))

        self.ocr_preprocess_mode_menu = ctk.CTkOptionMenu(
            container,
            width=180,
            values=["off", "mild", "strong"],
            variable=self.ocr_preprocess_mode_var,
        )
        self.ocr_preprocess_mode_menu.pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="保存 OCR 预处理设置",
            width=220,
            height=40,
            command=self.save_ocr_preprocess_settings,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        self.ocr_preprocess_hint_label = ctk.CTkLabel(
            container,
            text="OCR 预处理：未配置",
            font=("Arial", 12),
        )
        self.ocr_preprocess_hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        Button(
            container,
            text="转化当前的 Trace日志",
            width=220,
            height=40,
            command=self.convert_current_trace_log,
        ).pack(anchor="w", padx=16, pady=(0, 8))

        Button(
            container,
            text="删除当前已转化的 Trace 日志",
            width=260,
            height=40,
            fg_color=Color.BTN_WARNING,
            hover_color=Color.BTN_WARNING_HOVER,
            command=self.delete_current_trace_report,
        ).pack(anchor="w", padx=16, pady=(0, 12))

        ctk.CTkLabel(
            container,
            text="进度汇报与总结",
            font=("Arial", 14),
        ).pack(anchor="w", padx=16, pady=(10, 6))

        Button(
            container,
            text="打开总结页面",
            width=220,
            height=40,
            command=self.open_report_window,
        ).pack(anchor="w", padx=16, pady=(0, 12))

    def _load_config(self):
        key = load_gemini_api_key()
        if key:
            self.api_hint_label.configure(text=f"当前状态：已配置（{mask_api_key(key)}）")
        else:
            self.api_hint_label.configure(text="当前状态：未配置")
        trace_cfg = load_trace_config()
        self.trace_enabled_var.set(bool(trace_cfg.get("enabled", False)))
        self.trace_full_text_var.set(bool(trace_cfg.get("include_full_text", True)))
        preprocess_cfg = load_ocr_preprocess_config()
        self.ocr_preprocess_enabled_var.set(bool(preprocess_cfg.get("enabled", OCR_PREPROCESS_ENABLED)))
        self.ocr_preprocess_mode_var.set(str(preprocess_cfg.get("mode", OCR_PREPROCESS_MODE)))
        self._refresh_trace_hint()
        self._refresh_ocr_preprocess_hint()

    def _refresh_trace_hint(self):
        enabled = bool(self.trace_enabled_var.get())
        full_text = bool(self.trace_full_text_var.get())
        if enabled:
            detail = "完整文本" if full_text else "预览片段"
            self.trace_hint_label.configure(
                text=f"追踪状态：已启用（{detail}） | 输出目录：项目根目录/Gemini_Trace"
            )
        else:
            self.trace_hint_label.configure(text="追踪状态：未启用")

    def _refresh_ocr_preprocess_hint(self):
        enabled = bool(self.ocr_preprocess_enabled_var.get())
        mode = str(self.ocr_preprocess_mode_var.get()).strip().lower()
        if mode not in {"off", "mild", "strong"}:
            mode = OCR_PREPROCESS_MODE
            self.ocr_preprocess_mode_var.set(mode)
        if not enabled:
            self.ocr_preprocess_hint_label.configure(text="OCR 预处理：已关闭（等同 off）")
            return
        if mode == "off":
            mode_desc = "off（不增强）"
        elif mode == "strong":
            mode_desc = "strong（对比 1.5 / 锐度 1.2）"
        else:
            mode_desc = "mild（推荐；对比 1.3 / 锐度 1.1）"
        self.ocr_preprocess_hint_label.configure(text=f"OCR 预处理：已启用，当前 {mode_desc}")

    def save_api_key(self):
        raw_key = self.api_entry.get().strip()
        if not raw_key:
            messagebox.showwarning("提示", "请输入 API Key 后再保存。")
            return
        save_gemini_api_key(raw_key)
        self.api_entry.delete(0, "end")
        self.api_hint_label.configure(text=f"当前状态：已配置（{mask_api_key(raw_key)}）")
        messagebox.showinfo("成功", "Gemini API Key 已保存到本地安全配置。")

    def clear_api_key(self):
        removed = clear_gemini_api_key()
        os.environ.pop("GOOGLE_GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_VISION_API_KEY", None)  # 兼容清理旧变量
        self.api_entry.delete(0, "end")
        self.api_hint_label.configure(text="当前状态：未配置")
        if removed:
            messagebox.showinfo("成功", "已清空本地 Gemini API Key 配置。")
        else:
            messagebox.showinfo("提示", "未检测到本地 Gemini API Key 文件，当前已是未配置状态。")

    def save_trace_settings(self):
        enabled = bool(self.trace_enabled_var.get())
        include_full_text = bool(self.trace_full_text_var.get())
        save_trace_config(enabled=enabled, include_full_text=include_full_text)
        self._refresh_trace_hint()
        messagebox.showinfo("成功", "Gemini 追踪设置已保存。")

    def save_ocr_preprocess_settings(self):
        enabled = bool(self.ocr_preprocess_enabled_var.get())
        mode = str(self.ocr_preprocess_mode_var.get()).strip().lower()
        if mode not in {"off", "mild", "strong"}:
            messagebox.showwarning("提示", "OCR 预处理模式仅支持 off / mild / strong。")
            return
        try:
            save_ocr_preprocess_config(enabled=enabled, mode=mode)
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._refresh_ocr_preprocess_hint()
        messagebox.showinfo("成功", "OCR 预处理设置已保存。")

    def convert_current_trace_log(self):
        try:
            out = convert_current_trace_to_md(self.project_root)
        except FileNotFoundError as e:
            messagebox.showwarning("提示", str(e))
            return
        except Exception as e:
            messagebox.showerror("错误", f"转换 Trace 失败：{e}")
            return
        messagebox.showinfo("成功", f"Trace 报告已生成：\n{out}")

    def delete_current_trace_report(self):
        try:
            removed = delete_current_converted_trace_md(self.project_root)
        except Exception as e:
            messagebox.showerror("错误", f"删除 Trace 报告失败：{e}")
            return
        if removed is None:
            messagebox.showinfo("提示", "未找到可删除的已转化 Trace 报告（.md）。")
            return
        messagebox.showinfo("成功", f"已删除：\n{removed}")

    def open_report_window(self):
        if self.report_window is not None and self.report_window.winfo_exists():
            self.report_window.lift()
            self.report_window.focus_set()
            return
        self.report_window = ReportSummaryWindow(self, project_root=self.project_root)
        self.report_window.transient(self.winfo_toplevel())
        self.report_window.focus_set()
