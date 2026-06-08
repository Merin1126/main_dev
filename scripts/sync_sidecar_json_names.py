#!/usr/bin/env python3
"""递归扫描 JACAR_Downloads，将 sidecar JSON 文件名同步为与 PDF 同名（按 JACAR Ref 配对）。"""
from __future__ import annotations

import argparse
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.sidecar_filename_sync_service import SidecarFilenameSyncService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按 PDF 当前文件名重命名同 Ref 的抓取元数据 JSON（.json）"
    )
    parser.add_argument(
        "--dir",
        default=os.path.join(_PROJECT_ROOT, "JACAR_Downloads"),
        help="要处理的目录（默认：整个 JACAR_Downloads）",
    )
    args = parser.parse_args()

    target = os.path.abspath(args.dir)

    svc = SidecarFilenameSyncService(project_root=_PROJECT_ROOT)
    stats = svc.sync_directory(target)
    print("\n".join(stats.summary_lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
