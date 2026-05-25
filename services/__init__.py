"""HRS 服务层入口（懒加载）。

为什么用 `__getattr__` 懒加载：
- 这些服务的依赖较重（fitz、google-genai、jinja2 等），任何一个缺失都会
  让 `from services import X` 整体失败。
- 改为按需加载后，`from services import DbService` 只会触发
  `services.db_service` 的导入，不会被 `PdfService` 的 `fitz` 依赖拖垮，
  从而在脚本与测试场景中更可靠。
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

__all__ = ["PdfService", "CacheService", "LlmService", "TemplateService", "DbService", "ReportService"]

_SERVICE_MODULES = {
    "PdfService": "services.pdf_service",
    "CacheService": "services.cache_service",
    "LlmService": "services.llm_service",
    "TemplateService": "services.template_service",
    "DbService": "services.db_service",
    "ReportService": "services.report_service",
}


def __getattr__(name: str):
    module_path = _SERVICE_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module 'services' has no attribute {name!r}")
    module = import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


if TYPE_CHECKING:  # 仅静态分析时暴露具体类型，避免运行时强加载
    from .pdf_service import PdfService
    from .cache_service import CacheService
    from .llm_service import LlmService
    from .template_service import TemplateService
    from .db_service import DbService
    from .report_service import ReportService
