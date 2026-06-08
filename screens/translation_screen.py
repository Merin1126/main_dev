from __future__ import annotations

import hashlib
import json
import os

import customtkinter as ctk

from config.translation_prompts import (
    TRANSLATION_PLUGINS,
    render_translation_system,
    render_translation_turn,
)
from components.editor_search import EditorSearchController
from screens.base_screen import BaseDocumentScreen


class TranslationScreen(BaseDocumentScreen):
    requires_image_input = False
    #: v2.6.6：翻译切换为有状态 Chat Session
    use_chat_session = True
    chat_response_mime_type = "text/plain"
    chat_temperature = 0.3

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
        self._editor_search = EditorSearchController(self.right_frame, self.text_editor)
        self._editor_search.attach()

    # ------------------------------------------------------------------ #
    # 内部工具：加载本档案的 Analysis JSON 数据并做文档级聚合
    # ------------------------------------------------------------------ #

    def _analysis_cache_path_for_selected(self) -> str | None:
        if not self.selected_pdf_path:
            return None
        stat = os.stat(self.selected_pdf_path)
        cache_key = f"{self.selected_pdf_path}|{stat.st_mtime_ns}|{stat.st_size}"
        name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".txt"
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, "Analysis_Cache", name)

    def _load_analysis_pages(self) -> list[dict]:
        """返回与本档案对应的 Analysis JSON 每页解析结果（解析失败的页用空 dict 占位）。"""
        path = self._analysis_cache_path_for_selected()
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                analysis_data = json.loads(f.read())
        except Exception:
            return []
        pages = analysis_data.get("pages", []) or []
        parsed: list[dict] = []
        for page_str in pages:
            try:
                parsed.append(json.loads(page_str) if page_str else {})
            except Exception:
                parsed.append({})
        return parsed

    def _aggregate_active_plugins(self, analysis_pages: list[dict]) -> list[str]:
        """跨页聚合启用过的翻译滤镜，按 TRANSLATION_PLUGINS 的声明顺序去重输出。"""
        seen: set[str] = set()
        for page in analysis_pages:
            ctx = (page or {}).get("Historical_Context", {}) or {}
            for name in ctx.get("Translation_Plugins", []) or []:
                if isinstance(name, str) and name in TRANSLATION_PLUGINS:
                    seen.add(name)
        return [k for k in TRANSLATION_PLUGINS.keys() if k in seen]

    def _build_document_context_summary(self, analysis_pages: list[dict]) -> str:
        """汇总各页 `Core_Judgment` 形成「全书剧情大纲」，作为系统前缀的一部分注入。"""
        lines: list[str] = []
        for idx, page in enumerate(analysis_pages):
            disc = (page or {}).get("Discourse_Analysis", {}) or {}
            judgement = (disc.get("Core_Judgment") or "").strip()
            if judgement and "未提及" not in judgement:
                lines.append(f"第{idx + 1}页：{judgement}")
        return "\n".join(lines).strip()

    def _build_context_info_for_page(self, analysis_pages: list[dict], page_index: int) -> str:
        """从本档案 Analysis 数据中提取该页元数据，组装"翻译背景参数注入"小段。"""
        if page_index < 0 or page_index >= len(analysis_pages):
            return ""
        page = analysis_pages[page_index] or {}
        ctx = page.get("Historical_Context", {}) or {}
        date = (ctx.get("Date_Written") or "未知")
        sender = (ctx.get("Author_Sender") or "未知")
        recipient = (ctx.get("Recipient") or "未知")
        doc_type = (ctx.get("Document_Type") or "未知")
        if all(str(x).strip() in ("", "未知") for x in (date, sender, recipient, doc_type)):
            return ""
        return (
            "【翻译背景参数注入】\n"
            f"根据档案上下文，本页文书类型为：{doc_type}。\n"
            f"发文时间：{date} | 发文者：{sender} | 收文者：{recipient}。\n"
            "请在翻译时，务必结合上述双方身份地位，精准把握公文敬语、谦语及权力关系。"
        )

    # ------------------------------------------------------------------ #
    # v2.6.6 Chat Session 接入：system_prompt（文档级一次性）与 turn_prompt（逐轮）
    # ------------------------------------------------------------------ #

    def get_system_prompt(self) -> str:
        """渲染翻译会话的系统前缀：底座 + 文档级聚合插件 + 全书剧情大纲。

        【⚠️ 设计变更】v2.6.6 起：
        - `active_plugins` 由本档案各页 Analysis 中启用过的滤镜聚合得出；
        - `context_summary` 改为全书 `Core_Judgment` 汇总，而不是滑动窗口；
        - `prev_page_raw` / `prev_translation_context` 完全删除：跨页连贯性由 Chat Session 原生历史承担。
        """
        analysis_pages = self._load_analysis_pages()
        active_plugins = self._aggregate_active_plugins(analysis_pages)
        context_summary = self._build_document_context_summary(analysis_pages)
        # 缓存数据用于本次会话内 turn_prompt 渲染（避免每页重新读盘）
        self._cached_analysis_pages = analysis_pages
        self._cached_active_plugins = active_plugins

        # UI 反馈
        plugin_names = " + ".join(f"[{p}]" for p in active_plugins) if active_plugins else "无附加插件 (纯核心底座)"
        ui_text = f"⚙️ 引擎组装：核心底座 + {plugin_names} | 🟢 全书剧情大纲已注入 system"
        if hasattr(self, "plugin_status_label"):
            self.after(0, lambda: self.plugin_status_label.configure(text=ui_text))

        return render_translation_system(
            active_plugins=active_plugins,
            context_summary=context_summary,
        )

    def get_turn_prompt(self, page_index: int, page_text: str) -> str:
        """渲染单页 turn_prompt：仅当前页原文 + 衔接指令（不再注入相邻页原文/上一页译文）。"""
        analysis_pages = getattr(self, "_cached_analysis_pages", None) or self._load_analysis_pages()
        context_info = self._build_context_info_for_page(analysis_pages, page_index or 0)
        return render_translation_turn(
            page_number=(page_index or 0) + 1,
            page_text=page_text or "",
            context_info=context_info,
        )

    def export_document(self) -> None:
        self._export_text_pages_default()
