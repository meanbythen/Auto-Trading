"""
程序入口
功能：加载配置 → 初始化日志 → 启动 TradingBot。

推荐启动方式（在项目根目录）:
    python -m src.main
    python -m src.main --gui

也可直接在 Cursor 里运行本文件（已兼容路径问题）。

可选参数:
    --gui           打开图形界面（推荐）
    --once          只扫一次信号后退出
    --sim-test      模拟盘完整功能测试
    --config 路径    指定配置文件
"""

from __future__ import annotations

# !!! 必须在 import subprocess 之前修正 sys.path
# 直接运行 src/main.py 时，Python 会把 src/ 放进路径首位，
# 旧目录名 signal 会遮住标准库 signal，导致 subprocess 报错。
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
_ROOT = _SRC_DIR.parent
_src_str = str(_SRC_DIR)
_root_str = str(_ROOT)
if _src_str in sys.path:
    sys.path.remove(_src_str)
if _root_str not in sys.path:
    sys.path.insert(0, _root_str)

import argparse
import subprocess

from loguru import logger

from src.scheduler import TradingBot
from src.utils import load_config, setup_logger


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="期货自动交易机器人")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只执行一次登录+扫描")
    parser.add_argument("--sim-test", action="store_true", help="运行模拟盘完整测试")
    parser.add_argument("--gui", action="store_true", help="启动图形控制台")
    return parser.parse_args()


def main() -> None:
    """主函数。"""
    args = parse_args()

    if args.gui:
        from src.ui import run_app

        run_app()
        return

    if args.sim_test:
        script = _ROOT / "tools" / "sim_full_test.py"
        raise SystemExit(subprocess.call([sys.executable, str(script)]))

    cfg = load_config(args.config)
    log_cfg = cfg.get("logging", {})
    root = Path(cfg["_project_root"])
    setup_logger(
        log_dir=str(root / log_cfg.get("dir", "logs")),
        level=str(log_cfg.get("level", "INFO")),
    )

    logger.info("======== Auto-Trading 启动 ========")
    logger.info("项目目录: {}", root)
    logger.info("当前为【{}】模式", "模拟 dry_run" if cfg.get("dry_run") else "实盘相关")

    bot = TradingBot(cfg)

    if args.once:
        bot.do_login()
        bot.scan_once(force=True)
        logger.info("--once 模式结束")
        return

    bot.run_forever()


if __name__ == "__main__":
    # Cursor 直接点运行本文件（不是 python -m）且无额外参数 → 打开图形界面
    # python -m src.main 时 __package__ == "src"，仍走命令行常驻逻辑
    if __package__ is None and len(sys.argv) == 1:
        from src.ui import run_app

        run_app()
    else:
        main()
"""  """