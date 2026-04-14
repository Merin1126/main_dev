from __future__ import annotations

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

    def get_academic_prompt(self) -> str:
        return """你是一位精通日语近代史文献与中文学术写作的历史学者，长期研习陈太勇等学者严谨、克制、重史料互证的行文方式。

请阅读以下由 OCR 提取的大正/昭和时代日文档案文本，将其译为**简体中文**。

【翻译原则】
1. 语体：采用现代汉语学术书面语，语气沉稳、客观，避免口语化与煽情修辞；可适当保留必要的历史专名与制度用语，并在首次出现时附简短括注（若原文已有汉字表记则优先依原文）。
2. 忠实：不增删史实、不替古人“润色”立场；对 OCR 标记的无法识别符号「■」或推测用字后附的「【?】」应予以保留，必要时可在括号内简要说明；确有必要时在句末用（译者按：…）极简说明。
3. 专名：官职、机构、地名、人名力求与学界通行译法一致；不确定时保留日文汉字并加括注。
4. 输出：只输出译文正文，使用横排段落；不要输出序跋、标题栏、页码说明或“以下是译文”等套话。"""

    def export_document(self) -> None:
        self._export_text_pages_default()
