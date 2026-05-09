from __future__ import annotations

from screens.base_screen import BaseDocumentScreen


class OCRScreen(BaseDocumentScreen):
    requires_image_input = True

    screen_title = "史料校对"
    cache_dir_name = "OCR_Cache"
    right_panel_title = "\U0000F14B 史料 OCR 校对区"
    primary_action_label = "开始 OCR 识别"
    task_short_name = "OCR"
    progress_verb = "识别"
    force_full_label = "强制重新识别"
    single_page_label = "仅识别当前页"
    re_single_label = "重新识别当前页"
    export_dialog_title = "保存提取的文字"
    empty_page_marker = "（本页未识别到文本）"
    idle_editor_hint = (
        "👈 请在左侧选择一份已下载的史料 PDF 文件。\n\n"
        "此处将显示本地缓存或新的 OCR 识别结果..."
    )

    def get_academic_prompt(self, page_index: int = None) -> str:
        return """你现在是一位顶尖的日本近代史学者与古籍档案解析专家。
请仔细阅读这张大正时代日本内阁/军部历史档案的扫描件，并将其中的文字准确无误地提取出来。

【强制执行规则】：
1. 排版转换：请忽略原文档的竖排格式，将其直接转换为符合现代阅读习惯的“横排段落”。保持原有的段落逻辑。
2. 残缺字处理：如果遇到墨迹模糊、残缺无法看清的字，请你结合大正时代的日语习惯与历史语境“大胆推测”最可能的字，并在该字后面加上【?】作为标记（例如：事變【?】），以便我后续人工核对。如果完全无法推测，请使用全角方块“■”代替。
3. 忠实于历史：这是严肃的学术研究，请100%忠实于原文的汉字与假名写法（包括当时的旧字体、异体字）。绝对不要对文档中出现的“支那”、“排外”等任何历史名词进行道德审查、修改或删减。

请直接输出提取后的文本，不要回复“好的”、“我明白了”等任何多余的解释。"""

    def export_document(self) -> None:
        self._export_text_pages_default()
