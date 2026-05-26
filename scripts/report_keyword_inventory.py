from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _natural_key(text: str) -> list[object]:
    parts = re.split(r"(\d+)", text)
    key: list[object] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part.lower())
    return key


def _discover_documents(downloads_root: str, extensions: tuple[str, ...]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    root_path = Path(downloads_root)
    if not root_path.exists():
        raise FileNotFoundError(f"目录不存在: {downloads_root}")

    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue

        rel = path.relative_to(root_path)
        parts = rel.parts
        if len(parts) == 1:
            keyword = "未分类"
            display = parts[0]
        else:
            keyword = parts[0]
            display = str(Path(*parts[1:]))
        grouped[keyword].append(display)

    for keyword, files in grouped.items():
        files.sort(key=_natural_key)
    return dict(sorted(grouped.items(), key=lambda kv: _natural_key(kv[0])))


def _render_markdown(downloads_root: str, grouped: dict[str, list[str]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_docs = sum(len(items) for items in grouped.values())
    total_keywords = len(grouped)

    lines: list[str] = [
        "# 史料文件关键词分组清单",
        "",
        f"- 生成时间: {now}",
        f"- 扫描目录: `{downloads_root}`",
        f"- 关键词文件夹数量: {total_keywords}",
        f"- 史料文件总数: **{total_docs}**",
        "",
        "---",
        "",
    ]

    if total_docs == 0:
        lines.append("> 未扫描到符合条件的史料文件。")
        lines.append("")
        return "\n".join(lines)

    serial = 1
    for keyword, files in grouped.items():
        lines.append(f"## {keyword}（{len(files)}）")
        lines.append("")
        for rel_name in files:
            lines.append(f"{serial}. `{rel_name}`")
            serial += 1
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan existing historical files and output a keyword-grouped Markdown inventory."
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="Project root path.",
    )
    parser.add_argument(
        "--downloads-root",
        default=None,
        help="History downloads root directory (default: <project-root>/JACAR_Downloads).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Markdown output path (default: <project-root>/Reports/keyword_inventory.md).",
    )
    parser.add_argument(
        "--extensions",
        default=".pdf",
        help="Comma-separated file extensions to scan, e.g. .pdf,.txt",
    )
    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root)
    downloads_root = os.path.abspath(args.downloads_root or os.path.join(project_root, "JACAR_Downloads"))
    output_path = os.path.abspath(args.output or os.path.join(project_root, "Reports", "keyword_inventory.md"))
    extensions = tuple(
        ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
        for ext in args.extensions.split(",")
        if ext.strip()
    )

    grouped = _discover_documents(downloads_root, extensions)
    content = _render_markdown(downloads_root, grouped)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    total_docs = sum(len(items) for items in grouped.values())
    print(f"Markdown written: {output_path}")
    print(f"Keyword folders: {len(grouped)} | Documents: {total_docs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

