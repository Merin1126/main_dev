from __future__ import annotations

import json
import os
import re

import customtkinter as ctk

from config.academic_prompts import render_analysis_system, render_analysis_turn
from config.translation_prompts import TRANSLATION_PLUGINS
from screens.base_screen import BaseDocumentScreen


class AnalysisScreen(BaseDocumentScreen):
    requires_image_input = False
    show_single_page_actions = False
    #: v2.6.6：Analysis 切换为有状态 Chat Session，并通过 SDK 原生 JSON 约束格式
    use_chat_session = True
    chat_response_mime_type = "application/json"
    chat_temperature = 0.3

    screen_title = "史料分析"
    cache_dir_name = "Analysis_Cache"
    right_panel_title = "\uf080 史料价值甄别区"
    primary_action_label = "开始智能分析"
    task_short_name = "分析"
    progress_verb = "分析"
    force_full_label = "强制重新分析"
    export_dialog_title = "保存分析报告"
    empty_page_marker = "（本页暂无分析结果）"
    idle_editor_hint = (
        "👈 请在左侧选择一份已下载的史料 PDF。\n\n"
        "此处将按「背景—主体—态度—价值」四维度输出结构化甄别意见，便于复核与写作引用。"
    )

    def __init__(self, master, **kwargs):
        # 父类初始化过程中会触发 _show_current_ocr_page；先准备占位属性避免启动期访问报错
        self.form_entries: dict[str, ctk.CTkEntry] = {}
        self.form_textboxes: dict[str, ctk.CTkTextbox] = {}
        self.plugin_vars: dict[str, ctk.BooleanVar] = {}
        self.plugin_checkboxes: list[ctk.CTkCheckBox] = []
        self.plugin_wrap = None
        self.relevance_var = None
        self._debug_popup = None
        self._debug_popup_textbox = None
        self._last_raw_popup_signature = None
        self._raw_response_mode = False
        super().__init__(master, **kwargs)
        self.text_editor.pack_forget()
        self._build_controlled_form()
        # 父类初始化阶段可能已加载了当前页文本；表单建好后主动同步一次
        self._show_current_ocr_page()

    def _build_controlled_form(self) -> None:
        self.form_entries: dict[str, ctk.CTkEntry] = {}
        self.form_textboxes: dict[str, ctk.CTkTextbox] = {}
        self.plugin_vars: dict[str, ctk.BooleanVar] = {}
        self.plugin_checkboxes: list[ctk.CTkCheckBox] = []
        self.plugin_wrap = None
        self.relevance_var = ctk.StringVar(value="未设定")

        self.form_frame = ctk.CTkScrollableFrame(self.right_frame, corner_radius=8)
        self.form_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._ensure_bottom_save_area_after_form()

        self._add_section_title("⏳ 历史背景（Historical_Context）")
        self._add_entry_field("Date_Written", "成文时间（Date_Written）")
        self._add_entry_field("Author_Sender", "发文者（Author_Sender）")
        self._add_entry_field("Recipient", "收文对象（Recipient）")
        self._add_entry_field("Document_Type", "文书类型（Document_Type）")
        self._add_section_title("🧩 翻译插件 (Translation_Plugins)")
        plugin_wrap = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        plugin_wrap.pack(fill="x", pady=(2, 6))
        self.plugin_wrap = plugin_wrap
        for plugin_name in TRANSLATION_PLUGINS.keys():
            var = ctk.BooleanVar(value=False)
            self.plugin_vars[plugin_name] = var
            cb = ctk.CTkCheckBox(plugin_wrap, text=plugin_name, variable=var)
            self.plugin_checkboxes.append(cb)
        plugin_wrap.bind("<Configure>", self._on_plugin_wrap_resize)
        self.after(0, self._relayout_plugin_checkboxes)

        self._add_section_title("👥 实体与概念（Entities_and_Concepts）")
        self._add_text_field("Organizations", "组织机构（Organizations）", height=72)
        self._add_text_field("Key_Figures", "关键人物（Key_Figures）", height=72)
        self._add_text_field("All_Figures", "全部人物（All_Figures）", height=72)
        self._add_text_field("Locations", "核心地点（Locations）", height=72)
        self._add_text_field("Discourse_Keywords", "话语关键词（Discourse_Keywords）", height=72)

        self._add_section_title("🎯 话语分析（Discourse_Analysis）")
        self._add_text_field("Observation_Info", "观察信息（Observation_Info）", height=80)
        self._add_text_field("Core_Judgment", "核心判断（Core_Judgment）", height=80)
        self._add_text_field("Response_Action", "因应措施（Response_Action）", height=80)

        score_wrap = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        score_wrap.pack(fill="x", pady=(4, 8))
        ctk.CTkLabel(
            score_wrap,
            text="关联评分（Relevance_Score）",
            anchor="w",
            font=("Arial", 13, "bold"),
        ).pack(fill="x", pady=(4, 4))
        self.relevance_menu = ctk.CTkOptionMenu(
            score_wrap,
            values=["未设定", "1", "2", "3", "4", "5"],
            variable=self.relevance_var,
        )
        self.relevance_menu.pack(fill="x", pady=(0, 4))

    def _on_plugin_wrap_resize(self, _event) -> None:
        self._relayout_plugin_checkboxes()

    def _relayout_plugin_checkboxes(self) -> None:
        """根据容器宽度动态排布插件复选框：窄窗口自动换行，宽窗口横向排列。"""
        if self.plugin_wrap is None or not self.plugin_checkboxes:
            return
        width = self.plugin_wrap.winfo_width()
        if width <= 1:
            width = self.form_frame.winfo_width()
        if width <= 1:
            return

        # 估算单个复选框占位宽度（含间距），动态计算每行列数
        item_width = 145
        cols = max(1, width // item_width)

        for i in range(max(1, len(self.plugin_checkboxes))):
            self.plugin_wrap.grid_columnconfigure(i, weight=0)
        for i in range(cols):
            self.plugin_wrap.grid_columnconfigure(i, weight=1, uniform="plugin")

        for idx, cb in enumerate(self.plugin_checkboxes):
            cb.grid_forget()
            row = idx // cols
            col = idx % cols
            cb.grid(row=row, column=col, sticky="w", padx=(0, 12), pady=4)

    def _ensure_bottom_save_area_after_form(self) -> None:
        """将右侧底部的“保存修改”操作区稳定放在表单下方，避免 pack before 目标未管理导致崩溃。"""
        save_area = None
        for child in self.right_frame.winfo_children():
            if child is self.form_frame:
                continue
            # 查找包含“保存修改”按钮的直接子容器（base_screen 中的 text_action_frame）
            try:
                grandchildren = child.winfo_children()
            except Exception:
                continue
            for g in grandchildren:
                try:
                    text = g.cget("text")
                except Exception:
                    continue
                if isinstance(text, str) and "保存修改" in text:
                    save_area = child
                    break
            if save_area is not None:
                break

        if save_area is not None:
            save_area.pack_forget()
            save_area.pack(fill="x", padx=8, pady=(0, 10))

    def _add_section_title(self, title: str) -> None:
        ctk.CTkLabel(
            self.form_frame,
            text=title,
            anchor="w",
            font=("Arial", 15, "bold"),
        ).pack(fill="x", pady=(8, 6))

    def _add_entry_field(self, key: str, label: str) -> None:
        wrap = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        wrap.pack(fill="x", pady=(2, 6))
        ctk.CTkLabel(wrap, text=label, anchor="w", font=("Arial", 13)).pack(fill="x", pady=(2, 2))
        entry = ctk.CTkEntry(wrap)
        entry.pack(fill="x")
        self.form_entries[key] = entry

    def _add_text_field(self, key: str, label: str, height: int = 72) -> None:
        wrap = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        wrap.pack(fill="x", pady=(2, 8))
        ctk.CTkLabel(wrap, text=label, anchor="w", font=("Arial", 13)).pack(fill="x", pady=(2, 2))
        textbox = ctk.CTkTextbox(wrap, height=height)
        textbox.pack(fill="x")
        self.form_textboxes[key] = textbox

    def _clear_form(self) -> None:
        if self.relevance_var is None:
            return
        for entry in self.form_entries.values():
            entry.delete(0, "end")
        for textbox in self.form_textboxes.values():
            textbox.delete("0.0", "end")
        for var in self.plugin_vars.values():
            var.set(False)
        self.relevance_var.set("未设定")

    @staticmethod
    def _join_list(value) -> str:
        if isinstance(value, list):
            items = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(items)
        if isinstance(value, str):
            return value.strip()
        return ""

    @staticmethod
    def _split_list(text: str) -> list[str]:
        normalized = (text or "").replace("，", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def _set_entry_value(self, key: str, value) -> None:
        entry = self.form_entries[key]
        entry.delete(0, "end")
        entry.insert(0, "" if value is None else str(value))

    def _set_textbox_value(self, key: str, value) -> None:
        textbox = self.form_textboxes[key]
        textbox.delete("0.0", "end")
        text = self._join_list(value)
        if text:
            textbox.insert("0.0", text)

    def _show_current_ocr_page(self):
        super()._show_current_ocr_page()
        if self.relevance_var is None or not self.form_entries or not self.form_textboxes:
            return
        if not self.ocr_pages:
            self._clear_form()
            self._raw_response_mode = False
            return
        current_text = self.ocr_pages[self.current_ocr_page_index]
        try:
            payload = json.loads(current_text)
            if not isinstance(payload, dict):
                raise ValueError("JSON root is not an object")
        except Exception:
            self._clear_form()
            self._raw_response_mode = True
            self._maybe_show_raw_response_popup(current_text)
            return
        self._raw_response_mode = False

        ctx = payload.get("Historical_Context", {})
        ent = payload.get("Entities_and_Concepts", {})
        discourse = payload.get("Discourse_Analysis", {})

        self._set_entry_value("Date_Written", ctx.get("Date_Written", ""))
        self._set_entry_value("Author_Sender", ctx.get("Author_Sender", ""))
        self._set_entry_value("Recipient", ctx.get("Recipient", ""))
        self._set_entry_value("Document_Type", ctx.get("Document_Type", ""))
        plugins = ctx.get("Translation_Plugins", [])
        if not isinstance(plugins, list):
            plugins = []
        plugin_set = {str(p).strip() for p in plugins if str(p).strip()}
        for name, var in self.plugin_vars.items():
            var.set(name in plugin_set)

        self._set_textbox_value("Organizations", ent.get("Organizations", []))
        self._set_textbox_value("Key_Figures", ent.get("Key_Figures", []))
        self._set_textbox_value("All_Figures", ent.get("All_Figures", []))
        self._set_textbox_value("Locations", ent.get("Locations", []))
        self._set_textbox_value("Discourse_Keywords", ent.get("Discourse_Keywords", []))

        self._set_textbox_value("Observation_Info", discourse.get("Observation_Info", ""))
        self._set_textbox_value("Core_Judgment", discourse.get("Core_Judgment", ""))
        self._set_textbox_value("Response_Action", discourse.get("Response_Action", ""))

        score = discourse.get("Relevance_Score", "未设定")
        score_text = str(score).strip() if score is not None else "未设定"
        self.relevance_var.set(score_text if score_text in {"1", "2", "3", "4", "5"} else "未设定")

    def _is_system_hint_text(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        # 启动/引导/状态提示文案，不视为“模型异常原始响应”
        if t == type(self).idle_editor_hint:
            return True
        if getattr(type(self), "missing_full_ocr_notice", None) and t == type(self).missing_full_ocr_notice:
            return True
        if t.startswith("已加载文件：") and "后将调用 Gemini API" in t:
            return True
        if t.startswith("正在调用 Gemini API"):
            return True
        if t.startswith(f"{type(self).task_short_name} 任务已取消"):
            return True
        return False

    def _maybe_show_raw_response_popup(self, raw_text: str) -> None:
        if self._is_system_hint_text(raw_text):
            return
        signature = (
            getattr(self, "selected_pdf_path", None),
            self.current_ocr_page_index,
            hash(raw_text),
        )
        if signature == self._last_raw_popup_signature:
            return
        self._last_raw_popup_signature = signature
        self._show_raw_response_popup(raw_text)

    def _show_raw_response_popup(self, raw_text: str) -> None:
        popup = self._debug_popup
        if popup is None or not popup.winfo_exists():
            popup = ctk.CTkToplevel(self)
            popup.title("原始响应（调试）")
            popup.geometry("860x520")
            popup.transient(self.winfo_toplevel())

            ctk.CTkLabel(
                popup,
                text="当前页返回内容不是合法 JSON。以下为 Gemini 原始响应（只读）：",
                anchor="w",
                font=("Arial", 13, "bold"),
            ).pack(fill="x", padx=12, pady=(12, 6))

            box = ctk.CTkTextbox(popup, wrap="word")
            box.pack(fill="both", expand=True, padx=12, pady=(0, 10))
            box.configure(state="disabled")

            def _on_close():
                self._debug_popup = None
                self._debug_popup_textbox = None
                popup.destroy()

            popup.protocol("WM_DELETE_WINDOW", _on_close)
            self._debug_popup = popup
            self._debug_popup_textbox = box

        box = self._debug_popup_textbox
        if box is not None and box.winfo_exists():
            box.configure(state="normal")
            box.delete("0.0", "end")
            box.insert("0.0", raw_text or "")
            box.configure(state="disabled")
        self._debug_popup.lift()
        self._debug_popup.focus_force()

    def _save_current_ocr_page(self):
        if self._raw_response_mode:
            # 当前页是非 JSON 原始响应模式：保留原文，不用空表单覆盖缓存。
            return
        if not self.ocr_pages:
            self.ocr_pages = [""]
            self.current_ocr_page_index = 0
        if self.current_ocr_page_index < 0:
            self.current_ocr_page_index = 0
        if self.current_ocr_page_index >= len(self.ocr_pages):
            self.current_ocr_page_index = len(self.ocr_pages) - 1

        raw = (self.ocr_pages[self.current_ocr_page_index] or "").strip()
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        payload.setdefault("Document_ID", "")
        payload.setdefault("Citation_Metadata", {})
        ctx = payload.setdefault("Historical_Context", {})
        ent = payload.setdefault("Entities_and_Concepts", {})
        discourse = payload.setdefault("Discourse_Analysis", {})

        ctx["Date_Written"] = self.form_entries["Date_Written"].get().strip()
        ctx["Author_Sender"] = self.form_entries["Author_Sender"].get().strip()
        ctx["Recipient"] = self.form_entries["Recipient"].get().strip()
        ctx["Document_Type"] = self.form_entries["Document_Type"].get().strip()
        ctx["Translation_Plugins"] = [name for name, var in self.plugin_vars.items() if var.get()]

        ent["Organizations"] = self._split_list(self.form_textboxes["Organizations"].get("0.0", "end").strip())
        ent["Key_Figures"] = self._split_list(self.form_textboxes["Key_Figures"].get("0.0", "end").strip())
        ent["All_Figures"] = self._split_list(self.form_textboxes["All_Figures"].get("0.0", "end").strip())
        ent["Locations"] = self._split_list(self.form_textboxes["Locations"].get("0.0", "end").strip())
        ent["Discourse_Keywords"] = self._split_list(
            self.form_textboxes["Discourse_Keywords"].get("0.0", "end").strip()
        )

        discourse["Observation_Info"] = self.form_textboxes["Observation_Info"].get("0.0", "end").strip()
        discourse["Core_Judgment"] = self.form_textboxes["Core_Judgment"].get("0.0", "end").strip()
        discourse["Response_Action"] = self.form_textboxes["Response_Action"].get("0.0", "end").strip()
        score = self.relevance_var.get().strip()
        discourse["Relevance_Score"] = int(score) if score.isdigit() and score in {"1", "2", "3", "4", "5"} else None

        self.ocr_pages[self.current_ocr_page_index] = json.dumps(payload, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # v2.6.6 Chat Session 接入：system_prompt 与逐轮 turn_prompt
    # ------------------------------------------------------------------ #

    def get_system_prompt(self) -> str:
        """渲染 Analysis 会话的系统前缀（人物定义 + JSON Schema + 跨页继承法则）。

        过去版本里 `get_academic_prompt` 中"上一页元数据回灌"逻辑现在由 Chat Session 原生
        历史接管：模型在每一轮都能看到自己上一轮的 JSON 输出，从而自然继承档案元数据。
        """
        plugin_keys = "』、『".join(TRANSLATION_PLUGINS.keys())
        return render_analysis_system(translation_plugin_enum=f"『{plugin_keys}』")

    def get_turn_prompt(self, page_index: int, page_text: str) -> str:
        """渲染单页 turn_prompt（仅含当前页 OCR 文本的 <SOURCE_TEXT> 包装）。"""
        page_number = (page_index or 0) + 1
        return render_analysis_turn(page_number=page_number, page_text=page_text or "")

    def export_document(self) -> None:
        self._export_text_pages_default()

    def enrich_json_data(self, data: dict, pdf_path: str) -> dict:
        """在 JSON 写入硬盘前，利用正则解析文件名，自动注入元数据"""
        filename = os.path.basename(pdf_path)
        if filename.lower().endswith(".pdf"):
            filename = filename[:-4]

        # 匹配 core_scraper.py 中定义的标准文件名格式
        # 示例：戦前期外務省記録：「当地反帝国主義運動状況報告ノ件」、JACAR Ref. B03030289700（第（）—（）画像目）、『支那ニ於ケル利権回収問題一件/利権回収運動』（日本外交史料館）
        pattern = r"^(.*?)：「(.*?)」、JACAR Ref\.\s*(.*?)（.*?）、『(.*?)』[（\(](.*?)[）\)]$"
        match = re.match(pattern, filename)

        if match:
            level2, title, ref, parent, repo = match.groups()
            auto_cite = (
                f"{level2}：「{title}」、JACAR Ref. {ref}"
                f"（第（）—（）画像目）、『{parent}』（{repo}）"
            )

            # 将解析出的元数据强行插入大模型生成的 JSON 字典的最前方
            new_data = {
                "Document_ID": f"JACAR_{ref}",
                "Citation_Metadata": {
                    "Level2_Name": level2,
                    "Doc_Title": title,
                    "JACAR_Ref": ref,
                    "Image_Range": "（）—（）",  # 留空给后续大模型或人工确认
                    "Parent_Volume": parent,
                    "Repository": repo,
                    "Auto_Citation": auto_cite,
                },
            }
            # 合并大模型生成的其他部分（Historical_Context等）
            new_data.update(data)
            return new_data
        else:
            # 如果用户手动改坏了文件名导致正则失败，保留一个容错提示
            data["Citation_Metadata"] = {"Error": "文件名格式已被破坏或不标准，无法自动提取出处。"}
            return data
