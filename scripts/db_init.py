"""HRS SQLite 初始化与健康检查脚本。

用法（在项目根目录执行）：
    python scripts/db_init.py            # 创建/迁移并打印状态
    python scripts/db_init.py --path X   # 指定数据库文件路径

行为：
- 触发 `DbService` 启动逻辑，自动应用 `database/schema/` 下的迁移；
- 打印数据库文件位置、已应用版本、表清单与 integrity_check 结果。
"""
from __future__ import annotations

import argparse
import os
import sys

# 允许从仓库根目录直接执行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.db_service import DbService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="HRS SQLite 初始化")
    parser.add_argument("--path", default=None, help="自定义数据库文件路径（默认 database/hrs.sqlite3）")
    args = parser.parse_args()

    db = DbService(db_path=args.path)
    print(f"🗄️  数据库位置: {db.db_path}")

    versions = db.applied_schema_versions()
    print(f"✅ 已应用迁移版本: {len(versions)} 个")
    for v, name, ts in versions:
        print(f"    - {v:03d}_{name}  (applied_at={ts})")

    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    print(f"📋 当前表清单 ({len(rows)} 张):")
    for r in rows:
        print(f"    - {r['name']}")

    print(f"🔍 integrity_check: {db.integrity_check()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
