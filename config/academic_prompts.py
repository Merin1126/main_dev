"""学术 Prompt 渲染辅助（Jinja2 模板驱动）。

当前策略：
- OCR：单次 `generate_content`，`render_ocr_prompt()` 一锤定音。
- Analysis：无状态逐页生成，`render_analysis_system()` 用于 context cache 静态指令，
  `render_analysis_turn()` 负责每页动态任务包装。
- Translation：保留有状态 Chat Session 的 system / turn 拆分。
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


def render_analysis_turn(page_number: int, page_text: str, context_capsule: str = "") -> str:
    """渲染 Analysis 单页动态 prompt（当前页文本 + 可选回忆胶囊）。"""
    return TemplateService().render_prompt(
        "analysis_turn.jinja",
        {
            "page_number": page_number,
            "page_text": page_text or "",
            "context_capsule": context_capsule or "",
        },
    )
