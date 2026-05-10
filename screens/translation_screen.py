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

    def _build_rag_context(self, analysis_cache_path: str, ocr_cache_path: str, page_index: int) -> tuple[str, str]:
        """提取知识图谱与滑动窗口，返回 (知识图谱文本, 滑动窗口文本)"""
        glossary = {
            "Organizations": set(),
            "Key_Figures": set(),
            "Locations": set(),
            "Discourse_Keywords": set(),
        }
        arc_lines = []
        sliding_window_text = ""

        # 1. 提取全局图谱与大纲
        if os.path.exists(analysis_cache_path):
            try:
                with open(analysis_cache_path, "r", encoding="utf-8") as f:
                    analysis_data = json.loads(f.read())
                pages = analysis_data.get("pages", [])

                for i, page_str in enumerate(pages):
                    if not page_str or "未识别到文本" in page_str:
                        continue
                    try:
                        p_data = json.loads(page_str)
                        # 提炼大纲
                        judgement = p_data.get("Discourse_Analysis", {}).get("Core_Judgment", "")
                        if judgement and "未提及" not in judgement:
                            arc_lines.append(f"第{i + 1}页：{judgement}")

                        # 提炼词汇去重
                        ent = p_data.get("Entities_and_Concepts", {})
                        for k in glossary.keys():
                            items = ent.get(k, [])
                            if isinstance(items, list):
                                for item in items:
                                    if item and "未提及" not in str(item):
                                        glossary[k].add(str(item).strip())
                    except Exception:
                        continue
            except Exception:
                pass

        # 2. 提取滑动窗口 (前后各2页)
        if os.path.exists(ocr_cache_path):
            try:
                with open(ocr_cache_path, "r", encoding="utf-8") as f:
                    ocr_data = json.loads(f.read())
                ocr_pages = ocr_data.get("pages", [])

                start_idx = max(0, page_index - 2)
                end_idx = min(len(ocr_pages), page_index + 3)

                window_parts = []
                for i in range(start_idx, end_idx):
                    if i == page_index:
                        # 当前页不需要放在窗口中，系统会自动作为原文传入
                        continue
                    window_parts.append(f"【第{i + 1}页参考原文】\n{ocr_pages[i]}")

                if window_parts:
                    sliding_window_text = "\n\n".join(window_parts)
            except Exception:
                pass

        # 3. 格式化并挂载防火墙
        orgs = "、".join(glossary["Organizations"]) or "无"
        figs = "、".join(glossary["Key_Figures"]) or "无"
        locs = "、".join(glossary["Locations"]) or "无"
        words = "、".join(glossary["Discourse_Keywords"]) or "无"
        arc_text = "\n".join(arc_lines) if arc_lines else "暂无"

        rag_text = (
            f"【全局知识图谱（全书统一译名表）】\n"
            f"人物：{figs}\n组织：{orgs}\n地点：{locs}\n主观词汇：{words}\n\n"
            f"【全书剧情大纲（事件背景参考）】\n{arc_text}\n\n"
            f"【⚠️ 文风与事实防火墙 ⚠️】\n"
            f"以上“剧情大纲”由现代学术助手生成，仅供你作为事实背景、人物关系和事件走向的参考（防止你产生理解歧义）。\n"
            f"你必须严格坚守『半文半白』与『历史厚重感』的全局翻译法则，绝对不可抄袭或模仿上述大纲中的现代白话文措辞！"
        )

        return rag_text, sliding_window_text

    def get_academic_prompt(self, page_index: int = None) -> str:
        base_prompt = TRANSLATION_BASE_PROMPT
        active_plugins = []
        context_info = ""
        rag_context = ""
        sliding_window_context = ""
        prev_translation_context = ""

        if self.selected_pdf_path and page_index is not None and page_index >= 0:
            stat = os.stat(self.selected_pdf_path)
            cache_key = f"{self.selected_pdf_path}|{stat.st_mtime_ns}|{stat.st_size}"
            name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".txt"
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            analysis_cache_path = os.path.join(base_dir, "Analysis_Cache", name)
            ocr_cache_path = os.path.join(base_dir, "OCR_Cache", name)

            # [读取1] 当前页的精准参数与插件
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

            # [读取2] 构建 RAG 知识图谱与滑动窗口
            rag_text, sliding_text = self._build_rag_context(analysis_cache_path, ocr_cache_path, page_index)
            if rag_text:
                rag_context = f"\n\n{rag_text}"
            if sliding_text:
                sliding_window_context = (
                    f"\n\n【局部滑动窗口（上下文原文参考）】\n"
                    f"为保证跨页断句与专有名词的连贯，以下是当前页前后的原文片段：\n"
                    f"{sliding_text}\n"
                    f"⚠️ 警告：你【只需要】翻译本次输入给你的当前页文本，严禁翻译上述参考文本！"
                )

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

        final_prompt += rag_context + sliding_window_context + prev_translation_context

        # 动态更新 UI 反馈标签
        display_names = " + ".join(plugin_names) if plugin_names else "无附加插件 (纯核心底座)"
        ui_text = f"⚙️ 引擎组装：核心底座 + {display_names} | 🟢 RAG 图谱已注入"
        if hasattr(self, "plugin_status_label"):
            self.after(0, lambda: self.plugin_status_label.configure(text=ui_text))

        return final_prompt

    def export_document(self) -> None:
        self._export_text_pages_default()
