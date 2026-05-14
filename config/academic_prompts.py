"""学术 Prompt 渲染辅助（Jinja2 模板驱动）。

v2.6.6 起 Analysis / Translation 改为有状态 Chat Session，模板按 system / turn 物理拆分：
- OCR：仍走单次 `generate_content`，`render_ocr_prompt()` 一锤定音。
- Analysis：`render_analysis_system()` 渲染系统指令，`render_analysis_turn()` 渲染逐轮包装。
"""

from services.template_service import TemplateService


def render_ocr_prompt() -> str:
    return TemplateService().render_prompt("ocr_prompt.jinja", {})


def render_analysis_system(translation_plugin_enum: str) -> str:
    """渲染 Analysis 的 system_instruction（人物定义 + JSON Schema + 全局规则）。"""
    return TemplateService().render_prompt(
        "analysis_system.jinja",
        {"translation_plugin_enum": translation_plugin_enum},
    )


def render_analysis_turn(page_number: int, page_text: str) -> str:
    """渲染 Analysis 每一轮的 turn_prompt（仅含当前页 OCR 文本的包装标签）。"""
    return TemplateService().render_prompt(
        "analysis_turn.jinja",
        {
            "page_number": page_number,
            "page_text": page_text or "",
        },
    )
