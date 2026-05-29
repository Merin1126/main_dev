"""将 SQLite documents 全量元数据注入汇报用静态 HTML 演示页。

用法（在项目根目录执行）：
    python scripts/inject_db_to_html.py
    python scripts/inject_db_to_html.py --db-path database/hrs.sqlite3
    python scripts/inject_db_to_html.py --html-path Reports/HRS_Database_Demo.html

输出：
    Reports/HRS_Database_Report_YYYYMMDD_HHMMSS.html
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.db_service import DbService  # noqa: E402

DEFAULT_HTML = _PROJECT_ROOT / "Reports" / "HRS_Database_Demo.html"
RAW_DATA_PATTERN = re.compile(r"const\s+rawData\s*=\s*\[.*?\];", re.DOTALL)


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(DbService.default_db_path())


def fetch_documents(db_path: Path) -> list[dict[str, str]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(search_keyword), ''), '未分类') AS cat,
                COALESCE(NULLIF(TRIM(repo_name), ''), NULLIF(TRIM(source), ''), '未知') AS type,
                COALESCE(title, '') AS title,
                native_id AS ref,
                COALESCE(parent_name, '') AS coll
            FROM documents
            WHERE status != 'failed'
            ORDER BY cat, type, ref
            """
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        {
            "cat": str(row["cat"] or "未分类"),
            "type": str(row["type"] or "未知"),
            "title": str(row["title"] or ""),
            "ref": str(row["ref"] or ""),
            "coll": str(row["coll"] or ""),
        }
        for row in rows
    ]


def inject_to_html(
    data: list[dict[str, str]],
    *,
    html_path: Path,
    output_path: Path | None = None,
) -> Path:
    if not html_path.is_file():
        raise FileNotFoundError(f"HTML 模板不存在: {html_path}")

    json_data = json.dumps(data, ensure_ascii=False, indent=4)
    replacement = f"const rawData = {json_data};"

    html_content = html_path.read_text(encoding="utf-8")
    new_html_content, count = RAW_DATA_PATTERN.subn(replacement, html_content, count=1)
    if count == 0:
        raise RuntimeError(
            "未在 HTML 中找到 `const rawData = [...];` 块，请确认模板格式与 HRS_Database_Demo.html 一致。"
        )

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = html_path.with_name(f"HRS_Database_Report_{ts}.html")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(new_html_content, encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="将 SQLite documents 注入汇报 HTML 演示页")
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite 路径（默认 database/hrs.sqlite3）",
    )
    parser.add_argument(
        "--html-path",
        default=str(DEFAULT_HTML),
        help="源 HTML 模板路径（默认 Reports/HRS_Database_Demo.html）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出 HTML 路径（默认 Reports/HRS_Database_Report_<时间戳>.html）",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db_path)
    html_path = Path(args.html_path).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve() if args.output else None

    data = fetch_documents(db_path)
    out = inject_to_html(data, html_path=html_path, output_path=output_path)

    print(f"数据库: {db_path}")
    print(f"模板:   {html_path}")
    print(f"成功注入 {len(data)} 条记录")
    print(f"已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
