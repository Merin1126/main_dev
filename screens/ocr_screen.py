from __future__ import annotations

from config.academic_prompts import render_ocr_prompt
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
        return render_ocr_prompt()

    def export_document(self) -> None:
        self._export_text_pages_default()
