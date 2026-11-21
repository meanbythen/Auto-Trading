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
import json
import subprocess
import time

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
    parser.add_argument("--tick-record", action="store_true", help="常驻采集天勤实时 tick 并入库")
    parser.add_argument("--tick-status", action="store_true", help="打印天勤 tick 采集心跳")
    parser.add_argument(
        "--tick-bars",
        nargs=2,
        metavar=("START", "END"),
        help="用本地 tick 合成 K 线并写入 tick_bars 库",
    )
    parser.add_argument("--tick-download", action="store_true", help="专业版历史 tick 批量下载")
    parser.add_argument("--dry-run", action="store_true", help="配合 --tick-download：只列计划不下载")
    parser.add_argument(
        "--tick-inventory",
        action="store_true",
        help="写出本地 tick 库存 Excel（连续时段统计，不含逐笔）",
    )
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

    # P3 数据目录：合约快照按内容变化刷新，K 线/tick 库存每次启动对齐；失败不影响交易。
    try:
        from src.data.catalog import refresh_all

        catalog = refresh_all(root, cfg)
        logger.info(
            "数据目录已刷新：合约快照={} K线={} tick={}",
            catalog.get("instruments") or "无变化",
            catalog.get("klines", 0),
            catalog.get("ticks", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据目录刷新失败（不影响交易）: {}", exc)

    # P4 可观测：启动时汇总当天订单/成交/拒单与延迟，便于对照昨日留痕。
    try:
        from src.ops.journal import metrics_snapshot

        snapshot = metrics_snapshot()
        logger.info(
            "今日运行指标：报单 {} 成交 {} 拒单 {} 平均报单耗时 {}ms 平均成交耗时 {}ms",
            snapshot.get("orders_sent", 0),
            snapshot.get("fills", 0),
            snapshot.get("rejected", 0),
            snapshot.get("avg_send_ms", 0.0),
            snapshot.get("avg_fill_ms", 0.0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("运行指标汇总跳过: {}", exc)

    if args.tick_status:
        _print_tick_status(cfg)
        return
    if args.tick_record:
        _run_tick_record(cfg)
        return
    if args.tick_bars:
        _run_tick_bars(cfg, args.tick_bars[0], args.tick_bars[1])
        return
    if args.tick_download:
        _run_tick_download(cfg, dry_run=bool(args.dry_run))
        return

    bot = TradingBot(cfg)

    if args.once:
        bot.do_login()
        bot.scan_once(force=True)
        logger.info("--once 模式结束")
        return

    bot.run_forever()


def _tick_symbols(cfg: dict) -> list[str]:
    """tick 合成用哪些合约：优先 data.tick.symbols，空着就退回关注列表。"""
    tick_cfg = (cfg.get("data") or {}).get("tick") or {}
    symbols = [str(item) for item in (tick_cfg.get("symbols") or [])]
    if not symbols:
        symbols = [str(item) for item in (cfg.get("strategy") or {}).get("symbols") or []]
    return symbols


def _print_tick_status(cfg: dict) -> None:
    """打印 tick 采集心跳和库存概况。"""
    from src.data.tick.recorder import read_status, tick_settings
    from src.data.tick.vnpy_store import tick_overview

    settings = tick_settings(cfg)
    status = read_status(settings["status_file"])
    if not status:
        print(f"还没有 tick 采集心跳：{settings['status_file']}")
        print("启动采集：python app_entry.py --tick-record")
    else:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    try:
        overview = tick_overview(database=settings["database"])
    except Exception as exc:  # noqa: BLE001
        print(f"tick 库还读不了：{exc}")
        return
    if overview:
        print("tick 库存：")
        for item in overview:
            print(f"  {item['exchange']}.{item['symbol']}: {item['count']} 条 {item['start']} ~ {item['end']}")
    else:
        print(f"tick 库还是空的：{settings['database']}")


def _run_tick_record(cfg: dict) -> None:
    """常驻采集，Ctrl-C 停止。"""
    from src.data.tick.recorder import TickRecorder

    recorder = TickRecorder(cfg)
    recorder.start()
    logger.info("tick 采集已启动；Ctrl-C 停止")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl-C，正在停止 tick 采集")
    finally:
        recorder.stop()
    status = recorder.status()
    logger.info("tick 采集已停止：今日 {} 条，累计入库 {} 条", status["ticks_today"], status["ticks_total"])


def _run_tick_bars(cfg: dict, start: str, end: str) -> None:
    """按区间把本地 tick 合成 K 线，写入 tick_bars 库。"""
    from src.data.tick.recorder import tick_settings
    from src.data.tick.synthesize import synthesize_symbol

    symbols = _tick_symbols(cfg)
    if not symbols:
        raise SystemExit("配置里没有合约：请在 data.tick.symbols 或 strategy.symbols 至少填一个")
    settings = tick_settings(cfg)
    for symbol in symbols:
        report = synthesize_symbol(
            symbol,
            database=settings["database"],
            bar_store=settings["bar_store"],
            start=start,
            end=end,
            periods=settings["synth_periods"],
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))


def _run_tick_download(cfg: dict, *, dry_run: bool) -> None:
    """专业版历史 tick 批量下载。免费账号会得到明确的权限提示。"""
    from src.data.tick.downloader import download_settings, run_download

    settings = download_settings(cfg)
    if not settings["enabled"] and not dry_run:
        raise SystemExit(
            "历史 tick 下载还没启用：请把 data.tq.history_tick.enabled 设为 true（需专业版/试用权限）"
        )
    if not settings["main_cont_list"]:
        raise SystemExit("data.tq.history_tick.main_cont_list 是空的，请填写天勤主连代码，如 KQ.m@SHFE.fu")

    def report(text: str) -> None:
        print(text)

    summary = run_download(settings, dry_run=dry_run, on_progress=report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # Cursor 直接点运行本文件（不是 python -m）且无额外参数 → 打开图形界面
    # python -m src.main 时 __package__ == "src"，仍走命令行常驻逻辑
    if __package__ is None and len(sys.argv) == 1:
        from src.ui import run_app

        run_app()
    else:
        main()
"""  """
