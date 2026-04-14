from __future__ import annotations

from screens.base_screen import BaseDocumentScreen


class AnalysisScreen(BaseDocumentScreen):
    requires_image_input = False
    show_single_page_actions = False

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

    def get_academic_prompt(self) -> str:
        return """你是一位专攻日本近代政治与军事档案的研究者，擅长从史料档案中判断其史料价值与可用限度。

请仔细阅读以下由 OCR 提取的日文档案原文文本，输出**纯文本**的结构化分析（可用中文标点与小标题，但不要使用 Markdown 符号如 # 或 **）。

请严格按以下四部分依次撰写，每部分 2～5 句为宜；若某部分在文本中信息不足，请明确写“资料未提供足够信息”并说明缺什么。

【一、背景】
交代时间、发文/记录机关、文书类型（训令、报告、电报等）及与已知重大事件的可能关联。

【二、主体】
指出行为主体（省、部、军、个人等）、职衔与层级关系，谁向谁呈报或下达。

【三、态度】
概括立场、语气与修辞倾向（例如推诿、强硬、试探、例行通报），区分“事实陈述”与“价值判断”。

【四、价值】
评估该档案内容对研究主题的潜在用途：可确证什么、仅可作旁证什么、存在何种偏见或删节风险；给出使用建议（可与何类史料对读）。

除以上四部分外不要追加结语或客套话。"""

    def export_document(self) -> None:
        self._export_text_pages_default()
