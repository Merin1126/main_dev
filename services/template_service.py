from __future__ import annotations

import os

from jinja2 import Environment, FileSystemLoader


class TemplateService:
    """Prompt 模板渲染服务（Singleton）。"""

    _instance: "TemplateService | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_once()
        return cls._instance

    def _init_once(self) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        templates_dir = os.path.join(project_root, "templates")
        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=False,
            trim_blocks=False,
            lstrip_blocks=False,
            keep_trailing_newline=False,
        )

    def render_prompt(self, template_name: str, context: dict) -> str:
        template = self.env.get_template(template_name)
        return template.render(**(context or {}))
