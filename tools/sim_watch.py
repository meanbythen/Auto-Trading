"""
模拟盘短时观察
功能：用真实/缓存K线连续扫描若干轮，只模拟成交，不下真实单。
用法:
    python tools/sim_watch.py          # 默认扫 6 轮
    python tools/sim_watch.py --rounds 10
"""

from __future__ import annotations

import argparse
import sys
import time
from copy import deepcopy
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scheduler import TradingBot  # noqa: E402
from src.utils import load_config, setup_logger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟盘短时观察")
    parser.add_argument("--rounds", type=int, default=6, help="扫描轮数")
    parser.add_argument("--interval", type=int, default=5, help="每轮间隔秒")
    args = parser.parse_args()

    cfg = load_config()
    cfg = deepcopy(cfg)
    cfg["dry_run"] = True
    cfg.setdefault("trade", {})["executor"] = "sim"
    cfg.setdefault("data", {})["source"] = "akshare"
    root = Path(cfg["_project_root"])
    setup_logger(str(root / "logs"), "INFO")

    bot = TradingBot(cfg)
    logger.info("模拟观察开始：{} 轮，间隔 {} 秒，executor=sim", args.rounds, args.interval)
    bot.do_login()

    for i in range(1, args.rounds + 1):
        logger.info("---- 第 {}/{} 轮扫描 ----", i, args.rounds)
        bot.scan_once(force=True)
        if i < args.rounds:
            time.sleep(args.interval)

    logger.info("模拟观察结束。成交记录: data/sim_orders.jsonl ，事件: data/events.jsonl")


if __name__ == "__main__":
    main()
