"""调用 inject_db_to_html 生成发布页 HTML 预览。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from services.db_service import DbService

logger = logging.getLogger(__name__)


class HtmlPreviewService:
    def __init__(
        self,
        *,
        project_root: str | None = None,
        db_service: DbService | None = None,
    ) -> None:
        root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.project_root = os.path.abspath(root)
        self.db_service = db_service or DbService()

    def generate_report_html(
        self,
        *,
        enrich: bool = False,
        enrich_cat: str | None = None,
        release: bool = False,
    ) -> str:
        from scripts.inject_db_to_html import (
            DEFAULT_ENRICH_CAT,
            DEFAULT_ENRICH_DOWNLOADS_SUBDIR,
            DEFAULT_HTML,
            DEFAULT_HTML_DRAWER,
            DEFAULT_RELEASE_HTML,
            enrich_document_details,
            fetch_documents,
            inject_to_html,
        )

        db_path = Path(self.db_service.db_path)
        if not db_path.is_file():
            raise FileNotFoundError(f"数据库不存在: {db_path}")

        data = fetch_documents(db_path)
        cat = (enrich_cat or DEFAULT_ENRICH_CAT).strip() or DEFAULT_ENRICH_CAT
        subdir = cat if enrich else DEFAULT_ENRICH_DOWNLOADS_SUBDIR

        if enrich:
            try:
                import markdown  # noqa: F401
            except ImportError:
                logger.warning(
                    "未安装 markdown，summary_html 将回退；建议 pip install markdown"
                )
            enrich_document_details(
                data,
                self.project_root,
                enrich_cat=cat,
                enrich_downloads_subdir=subdir,
            )

        if release:
            html_path = DEFAULT_HTML_DRAWER
            output_path = DEFAULT_RELEASE_HTML
        else:
            html_path = DEFAULT_HTML
            output_path = None

        out = inject_to_html(data, html_path=html_path, output_path=output_path)
        return str(out.resolve())
