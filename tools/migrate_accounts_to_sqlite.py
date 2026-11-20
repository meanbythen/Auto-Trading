"""
把账号注册表迁移到 SQLite：建表 + 从旧配置导入。

功能：
    1. 在 data/trading.db 建 accounts / broker_profiles（建表语句见 src/account/schema.sql）；
    2. 优先读 config/accounts.yaml；没有时回退旧 config/config.yaml 的 ctp_profiles；
    3. 按主键 upsert，可以反复执行。

用法：
    python tools/migrate_accounts_to_sqlite.py --dry-run   # 内存里试跑，不碰磁盘
    python tools/migrate_accounts_to_sqlite.py             # 写入 data/trading.db
    python tools/migrate_accounts_to_sqlite.py --db data/trading_test.db

说明：本工具不在运行链路上，P0 仍以 config/accounts.yaml 为准；密码只搬引用不搬明文。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 确保项目根在路径上
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml
from loguru import logger

from src.account.sqlite_migrate import DEFAULT_DB_REL, migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账号注册表 > SQLite（建表 + 旧配置导入）")
    parser.add_argument("--root", default=str(_ROOT), help="项目根目录（默认仓库根）")
    parser.add_argument("--db", default="", help=f"目标库（默认 <root>/{DEFAULT_DB_REL.as_posix()}）")
    parser.add_argument("--dry-run", action="store_true", help="用内存库试跑，不写磁盘")
    args = parser.parse_args(argv)

    root = Path(args.root)
    db_path = Path(args.db) if args.db else root / DEFAULT_DB_REL
    try:
        result = migrate(root, db_path, dry_run=args.dry_run)
    except (OSError, ValueError, sqlite3.Error, yaml.YAMLError) as exc:
        logger.error("账号迁移失败：{}", exc)
        return 1

    problems = result["problems"]
    if problems:
        logger.error("注册表校验未通过，未写入数据库：")
        for item in problems:
            logger.error("  - {}", item)
        return 1

    logger.info(
        "{}：来源 {}，连接模板 {} 个、账号 {} 个，写入 {} 行 -> {}",
        "试运行（内存库）" if result["dry_run"] else "迁移完成",
        result["source"],
        result["profiles"],
        result["accounts"],
        result["written"],
        result["db"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
