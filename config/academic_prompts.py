"""Academic prompt rendering helpers powered by Jinja2 templates."""

from services.template_service import TemplateService


def render_ocr_prompt() -> str:
    return TemplateService().render_prompt("ocr_prompt.jinja", {})


def render_analysis_prompt(translation_plugin_enum: str, prev_date: str | None = None) -> str:
    return TemplateService().render_prompt(
        "analysis_prompt.jinja",
        {
            "translation_plugin_enum": translation_plugin_enum,
            "prev_date": prev_date or "",
        },
    )

