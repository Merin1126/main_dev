import customtkinter as ctk
import os
from tkinter import messagebox

from components.ui.button import Button
from config.api_key_store import (
    load_google_api_key,
    save_google_api_key,
    clear_google_api_key,
    mask_api_key,
)


class SettingScreen(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.master = master
        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        container = ctk.CTkFrame(self, corner_radius=10)
        container.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            container,
            text="⚙️ 系统与环境设置",
            font=("Arial", 22, "bold")
        ).pack(anchor="w", padx=16, pady=(16, 8))

        ctk.CTkLabel(
            container,
            text="Google Vision API Key（本地保存，不写入代码）",
            font=("Arial", 14)
        ).pack(anchor="w", padx=16, pady=(8, 6))

        self.api_entry = ctk.CTkEntry(
            container,
            width=520,
            height=40,
            show="*",
            placeholder_text="请输入 Google Vision API Key"
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
            fg_color="#b45309",
            hover_color="#92400e",
            command=self.clear_api_key
        ).pack(anchor="w", padx=16, pady=(0, 10))

        ctk.CTkLabel(
            container,
            text="建议：优先使用环境变量；若必须本地保存，请不要共享项目目录与 .secrets 文件。",
            font=("Arial", 12),
            text_color=("#444444", "#b0b0b0")
        ).pack(anchor="w", padx=16, pady=(2, 16))

    def _load_config(self):
        key = load_google_api_key()
        if key:
            self.api_hint_label.configure(text=f"当前状态：已配置（{mask_api_key(key)}）")
        else:
            self.api_hint_label.configure(text="当前状态：未配置")

    def save_api_key(self):
        raw_key = self.api_entry.get().strip()
        if not raw_key:
            messagebox.showwarning("提示", "请输入 API Key 后再保存。")
            return
        save_google_api_key(raw_key)
        self.api_entry.delete(0, "end")
        self.api_hint_label.configure(text=f"当前状态：已配置（{mask_api_key(raw_key)}）")
        messagebox.showinfo("成功", "API Key 已保存到本地安全配置。")

    def clear_api_key(self):
        removed = clear_google_api_key()
        os.environ.pop("GOOGLE_VISION_API_KEY", None)
        self.api_entry.delete(0, "end")
        self.api_hint_label.configure(text="当前状态：未配置")
        if removed:
            messagebox.showinfo("成功", "已清空本地 API Key 配置。")
        else:
            messagebox.showinfo("提示", "未检测到本地 API Key 文件，当前已是未配置状态。")
