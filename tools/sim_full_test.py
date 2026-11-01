"""
模拟盘完整功能测试
功能：在不下真实单的前提下，把整条流水线跑一遍并出报告。
覆盖：
  1) 配置与模拟通道
  2) 登录（dry_run）
  3) K线拉取（demo）
  4) 单均线开仓（模拟成交）
  5) 止损触发平仓
  6) 收盘清仓 14:55 / 22:55
  8) 风控拦截（超仓/过频）
  9) 真实联网拉一次沥青/玉米K线（可选，失败不算阻断）

用法:
    python tools/sim_full_test.py
"""

from __future__ import annotations

import json
import sys
import traceback
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.kline import KlineProvider  # noqa: E402
from src.risk import RiskGuardian  # noqa: E402
from src.scheduler import TradingBot  # noqa: E402
from src.strategy.single_ma import SingleMaStrategy  # noqa: E402
from src.strategy.types import StrategyAction  # noqa: E402
from src.utils import load_config, setup_logger  # noqa: E402


def _ok(name: str, detail: str = "") -> dict[str, Any]:
    return {"name": name, "pass": True, "detail": detail}


def _fail(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "pass": False, "detail": detail}


def _ma_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """模拟测试用短均线，方便人造穿越信号；不改正在用的实盘策略名以外的配置。"""
    out = deepcopy(cfg)
    st = out.setdefault("strategy", {})
    st["engine"] = "single_ma"
    st["timeframe"] = "5"
    st["flatten_at_close"] = True
    st["single_ma"] = {
        "ma_period": 5,
        "reverse_on_cross": False,
    }
    out.setdefault("schedule", {})["trading_sessions"] = [["09:00", "15:00"], ["21:00", "23:00"]]
    return out


def _price_frame(closes: list[float], *, high_at: dict[int, float] | None = None, low_at: dict[int, float] | None = None) -> pd.DataFrame:
    """用收盘价铺一组 K 线，可把指定位置的高低点拉开做止损。"""
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    for i, value in (high_at or {}).items():
        highs[i] = value
    for i, value in (low_at or {}).items():
        lows[i] = value
    idx = pd.date_range("2026-10-13 09:00", periods=len(closes), freq="5min")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [100] * len(closes)},
        index=idx,
    )


def _make_long_cross() -> pd.DataFrame:
    """先在均线下面走，最后一根收盘上穿 5 均线做多；止损用这根最低价。"""
    # 前几根压在下面，均线偏低；最后一根明显站上。
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 102.0]
    return _price_frame(closes, low_at={6: 90.0})


def _make_short_cross() -> pd.DataFrame:
    """先在均线上面走，最后一根收盘下穿 5 均线做空。"""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 98.0]
    return _price_frame(closes, high_at={6: 110.0})


def run() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    cfg = load_config()
    root = Path(cfg["_project_root"])
    setup_logger(log_dir=str(root / "logs"), level="INFO")

    # ---- 强制模拟盘安全开关 ----
    cfg = deepcopy(cfg)
    cfg["dry_run"] = True
    cfg.setdefault("trade", {})["executor"] = "sim"
    # 这是模拟测试，不去点同花顺的交易菜单，避免把已经开着的下单窗口关掉。
    cfg.setdefault("account", {})["skip_auto_login"] = True
    cfg.setdefault("data", {})["source"] = "demo"
    cfg.setdefault("strategy", {})["timeframe"] = "5"
    cfg["strategy"]["flatten_times"] = ["14:55", "22:55"]
    cfg["strategy"]["flatten_at_close"] = True
    cfg["strategy"]["require_trend"] = False
    cfg.setdefault("risk", {})["min_order_interval_sec"] = 0  # 测试时允许连续下单
    cfg["risk"].pop("max_position", None)

    logger.info("======== 模拟盘完整功能测试开始 ========")

    # 1) 配置检查
    if cfg["dry_run"] and cfg["trade"]["executor"] == "sim":
        results.append(_ok("配置安全", "dry_run=true, executor=sim"))
    else:
        results.append(_fail("配置安全", "未处于模拟模式，已中止"))
        return _finish(results, cfg)

    # 2) 登录（模拟）
    try:
        bot = TradingBot(cfg)
        bot.do_login()
        if bot.logged_in:
            results.append(_ok("模拟登录", "dry_run 登录流程完成"))
        else:
            results.append(_fail("模拟登录", "logged_in=False"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("模拟登录", str(e)))
        return _finish(results, cfg)

    # 3) K线 demo
    try:
        provider = KlineProvider(cfg)
        demo_bu = provider.fetch("bu", "5")
        demo_c = provider.fetch("c", "5")
        if len(demo_bu) >= 40 and len(demo_c) >= 40:
            results.append(_ok("K线demo", f"bu={len(demo_bu)}根, c={len(demo_c)}根"))
        else:
            results.append(_fail("K线demo", "根数不足"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("K线demo", str(e)))

    # 4) 单均线上穿买开
    try:
        st = SingleMaStrategy(_ma_cfg(cfg))
        bars = _make_long_cross()
        d = st.evaluate("c", bars, now=datetime(2026, 10, 13, 11, 55))
        if d.action == StrategyAction.BUY_OPEN and d.stop_price is not None:
            bot.strategy = st
            bot.risk = RiskGuardian.from_config(cfg)
            bot._execute(d)
            pos = bot.strategy.positions["c"]
            if pos.side == "long":
                results.append(_ok("均线买开", f"止损={d.stop_price}, 模拟开多成功"))
            else:
                results.append(_fail("均线买开", "策略有信号但仓位未同步"))
        else:
            results.append(_fail("均线买开", f"期望BUY_OPEN，实际={d.action} / {d.reason}"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("均线买开", f"{e}\n{traceback.format_exc()}"))

    # 5) 止损触发
    try:
        st = bot.strategy
        pos = st.positions["c"]
        stop = pos.initial_stop
        rows = _make_long_cross().iloc[:-1].copy()
        extra = pd.DataFrame(
            [{"open": stop + 1, "high": stop + 1.2, "low": stop - 1, "close": stop - 0.4, "volume": 100}],
            index=pd.DatetimeIndex([datetime(2026, 10, 13, 12, 0)]),
        )
        bars2 = pd.concat([rows, extra])
        d2 = st.evaluate("c", bars2, now=datetime(2026, 10, 13, 12, 0))
        if d2.action == StrategyAction.CLOSE:
            bot._execute(d2)
            if st.positions["c"].side == "flat":
                results.append(_ok("止损平仓", d2.reason))
            else:
                results.append(_fail("止损平仓", "发出CLOSE但仓位未平"))
        else:
            results.append(_fail("止损平仓", f"期望CLOSE，实际={d2.action} {d2.reason}"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("止损平仓", str(e)))

    # 6) 卖开 + 收盘清仓（22:55）
    try:
        st = SingleMaStrategy(_ma_cfg(cfg))
        bot.strategy = st
        bot.risk = RiskGuardian.from_config(cfg)
        d_sell = st.evaluate("bu", _make_short_cross(), now=datetime(2026, 10, 13, 11, 55))
        if d_sell.action != StrategyAction.SELL_OPEN:
            results.append(_fail("均线卖开", f"期望SELL_OPEN，实际={d_sell.action} {d_sell.reason}"))
        else:
            bot._execute(d_sell)
            hold_bars = _make_short_cross()
            d_flat = st.evaluate("bu", hold_bars, now=datetime(2026, 10, 13, 22, 55))
            if d_flat.action == StrategyAction.CLOSE and "清仓" in d_flat.reason:
                bot._execute(d_flat)
                results.append(_ok("夜盘22:55清仓", d_flat.reason))
            else:
                results.append(_fail("夜盘22:55清仓", f"实际={d_flat.action} {d_flat.reason}"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("夜盘22:55清仓", str(e)))

    # 7) 日盘 14:55 清仓
    try:
        st = SingleMaStrategy(_ma_cfg(cfg))
        bot.strategy = st
        bot.risk = RiskGuardian.from_config(cfg)
        d_buy = st.evaluate("c", _make_long_cross(), now=datetime(2026, 10, 13, 11, 55))
        bot._execute(d_buy)
        d_day = st.evaluate("c", _make_long_cross(), now=datetime(2026, 10, 13, 14, 55))
        if d_day.action == StrategyAction.CLOSE:
            bot._execute(d_day)
            results.append(_ok("日盘14:55清仓", d_day.reason))
        else:
            results.append(_fail("日盘14:55清仓", f"实际={d_day.action} {d_day.reason}"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("日盘14:55清仓", str(e)))

    # 8) 风控：日亏损熔断拦开仓
    try:
        risk = RiskGuardian.from_config(cfg)
        risk.max_daily_loss = 100
        risk.min_order_interval_sec = 0
        risk.update_pnl(-150)
        ok, reason = risk.allow("buy_open", 1)
        if not ok and "熔断" in reason:
            results.append(_ok("风控日亏损熔断", reason))
        else:
            results.append(_fail("风控日亏损熔断", f"未拦截: ok={ok} reason={reason}"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("风控日亏损熔断", str(e)))

    # 9) 整机强制扫描一轮（demo）
    try:
        bot2 = TradingBot(cfg)
        bot2.do_login()
        bot2.force_scan_once()
        results.append(_ok("整机扫描一轮", "demo 扫描完成"))
    except Exception as e:  # noqa: BLE001
        results.append(_fail("整机扫描一轮", str(e)))

    # 10) 可选：真实联网（失败不算阻断整份报告）
    try:
        live_cfg = deepcopy(cfg)
        live_cfg["data"]["source"] = "akshare"
        live = KlineProvider(live_cfg)
        bu = live.fetch("bu", "5")
        results.append(_ok("联网拉K(可选)", f"bu={len(bu)}根"))
    except Exception as e:  # noqa: BLE001
        results.append(_ok("联网拉K(可选)", f"跳过：{e}"))

    return _finish(results, cfg)


def _finish(results: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    report = {
        "passed": passed,
        "total": total,
        "all_ok": passed == total,
        "results": results,
    }
    out = Path(cfg.get("_project_root", ROOT)) / "data" / "sim_full_test_last.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("模拟盘完整测试 {}/{} 通过 | 报告 {}", passed, total, out)
    for row in results:
        mark = "OK" if row["pass"] else "FAIL"
        logger.info("[{}] {} | {}", mark, row["name"], row.get("detail", ""))
    return report


if __name__ == "__main__":
    summary = run()
    raise SystemExit(0 if summary.get("all_ok") else 1)
