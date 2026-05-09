from __future__ import annotations

import hashlib
import json
import os

import customtkinter as ctk

from config.translation_prompts import TRANSLATION_BASE_PROMPT, TRANSLATION_PLUGINS
from screens.base_screen import BaseDocumentScreen


class TranslationScreen(BaseDocumentScreen):
    requires_image_input = False

    screen_title = "史料翻译"
    cache_dir_name = "Translation_Cache"
    right_panel_title = "\uf1ab 翻译区"
    primary_action_label = "开始翻译"
    task_short_name = "翻译"
    progress_verb = "翻译"
    force_full_label = "强制重新翻译"
    single_page_label = "仅翻译当前页"
    re_single_label = "重新翻译当前页"
    export_dialog_title = "保存译文"
    empty_page_marker = "（本页暂无译文）"
    idle_editor_hint = (
        "👈 请在左侧选择一份已下载的史料 PDF。\n\n"
        "此处将显示逐页日译中文结果（简体中文），风格贴近学术论文引文。"
    )

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.plugin_status_label = ctk.CTkLabel(
            self.right_frame,
            text="⚙️ 当前翻译组装：等待调用 API...",
            font=("Arial", 12, "bold"),
            text_color="gray",
        )
        self.plugin_status_label.pack(fill="x", padx=10, pady=(0, 4), before=self.text_editor)

    def get_academic_prompt(self, page_index: int = None) -> str:
        base_prompt = TRANSLATION_BASE_PROMPT
        active_plugins = []
        context_info = ""
        global_ocr_context = ""
        prev_translation_context = ""

        if self.selected_pdf_path and page_index is not None and page_index >= 0:
            stat = os.stat(self.selected_pdf_path)
            cache_key = f"{self.selected_pdf_path}|{stat.st_mtime_ns}|{stat.st_size}"
            name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".txt"
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            # [读取1] Analysis 缓存 (提取发文背景与翻译插件)
            analysis_cache_path = os.path.join(base_dir, "Analysis_Cache", name)
            if os.path.exists(analysis_cache_path):
                try:
                    with open(analysis_cache_path, "r", encoding="utf-8") as f:
                        analysis_data = json.loads(f.read())
                    pages = analysis_data.get("pages", [])
                    if page_index < len(pages):
                        page_json_str = pages[page_index]
                        page_data = json.loads(page_json_str) if page_json_str else {}
                        ctx = page_data.get("Historical_Context", {})

                        active_plugins = ctx.get("Translation_Plugins", [])
                        date = ctx.get("Date_Written", "未知")
                        sender = ctx.get("Author_Sender", "未知")
                        recipient = ctx.get("Recipient", "未知")
                        doc_type = ctx.get("Document_Type", "未知")

                        if any(x != "未知" for x in [date, sender, recipient, doc_type]):
                            context_info = (
                                f"\n\n【翻译背景参数注入】\n"
                                f"根据档案上下文，本页文书类型为：{doc_type}。\n"
                                f"发文时间：{date} | 发文者：{sender} | 收文者：{recipient}。\n"
                                f"请在翻译时，务必结合上述双方身份地位，精准把握公文敬语、谦语及权力关系。"
                            )
                except Exception:
                    pass

            # [读取2] OCR 全局缓存 (上帝视角防瞎)
            ocr_cache_path = os.path.join(base_dir, "OCR_Cache", name)
            if os.path.exists(ocr_cache_path):
                try:
                    with open(ocr_cache_path, "r", encoding="utf-8") as f:
                        ocr_data = json.loads(f.read())
                    ocr_pages = ocr_data.get("pages", [])
                    full_text = "\n\n---下页分割线---\n\n".join([str(p) for p in ocr_pages])
                    global_ocr_context = (
                        f"\n\n【全局上下文参考（仅供理解语境，绝对不要翻译）】\n"
                        f"为了保证专有名词连贯，以下是该份档案的完整 OCR 原文供你参考：\n"
                        f"========== 全局原文开始 ==========\n{full_text}\n========== 全局原文结束 ==========\n"
                        f"⚠️ 警告：你【只需要】翻译本次输入给你的当前页文本，严禁翻译上述全局参考文本！"
                    )
                except Exception:
                    pass

            # [读取3] 上一页翻译结果 (防止断句碎裂)
            if page_index > 0 and self.ocr_pages and page_index - 1 < len(self.ocr_pages):
                prev_trans = self.ocr_pages[page_index - 1]
                if prev_trans and "未识别到文本" not in prev_trans:
                    tail_text = prev_trans[-200:]
                    prev_translation_context = (
                        f"\n\n【上一页译文接续参考】\n"
                        f"上一页的译文结尾如下：\n...{tail_text}\n"
                        f"请根据此结尾，流畅地接续翻译当前页面的内容。"
                    )

        # 组装最终 Prompt
        final_prompt = base_prompt + context_info
        plugin_names = []
        if active_plugins:
            plugin_texts = []
            for p in active_plugins:
                if p in TRANSLATION_PLUGINS:
                    plugin_texts.append(TRANSLATION_PLUGINS[p])
                    plugin_names.append(f"[{p}]")
            if plugin_texts:
                final_prompt += "\n\n" + "\n\n".join(plugin_texts)

        final_prompt += prev_translation_context + global_ocr_context

        # 动态更新 UI 反馈标签
        display_names = " + ".join(plugin_names) if plugin_names else "无附加插件 (纯核心底座)"
        ui_text = f"⚙️ 当前翻译组装：核心底座 + {display_names}"
        if hasattr(self, "plugin_status_label"):
            self.after(0, lambda: self.plugin_status_label.configure(text=ui_text))

        return final_prompt

    def export_document(self) -> None:
        self._export_text_pages_default()
