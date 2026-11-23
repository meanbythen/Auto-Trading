"""
多品种报单权限探针：用远离现价的限价单验证每个合约能不能被柜台接受。

功能：逐合约报一笔「卖开」限价单，价格挂在涨停价下方几跳（离现价至少 2%），
      柜台接受后立刻撤单，全程不应该成交。用来区分「没有报单权限」和
      「程序自己的价格/时段问题」。

原理：委托走和常驻相同的 CtpTrader.place_order；但使用交易台会话，
      不超时自动撤，由本脚本看到「已报」后马上撤，方便截柜台原文。

安全：
    - 必须加 --yes；真实账户还必须设置环境变量 CTP_PROBE_LIVE=1。
    - 同一时间只允许一个实例操作交易账户（常驻开着会直接拒绝）。
    - 只在交易时段内运行；商品 10:15～10:30 休市自动跳过。
    - 价格至少离现价 2%，且不高于涨停价减一跳；成交手数必须为 0。

用法：
    python tools/ctp_order_probe.py --kind sim --symbols fu2701 lh2703 --yes
    CTP_PROBE_LIVE=1 python tools/ctp_order_probe.py --kind live --symbols fu2701 lh2703 --yes

    --gap-pct 2.0    价格至少离现价百分之几
    --volume 1       每笔报单手数
    --cap-check      额外验证本机单笔上限（不向柜台发单）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROBE_ENV = "CTP_PROBE_LIVE"


def _align(price: float, tick: float) -> float:
    """按最小变动价位向下取整，避免柜台因价格不是整数倍拒单。"""
    step = Decimal(str(tick or 1))
    if step <= 0:
        step = Decimal("1")
    raw = Decimal(str(price))
    return float((raw / step).to_integral_value(rounding=ROUND_FLOOR) * step)


def _tick_for(cfg: dict, symbol: str, fallback: float) -> float:
    """品种表里的最小变动价位；没填才退回 CTP 配置里的默认值。"""
    from src.risk.universe_table import product_key

    contracts = (cfg.get("risk") or {}).get("contracts") or {}
    box = contracts.get(product_key(symbol)) if isinstance(contracts, dict) else {}
    raw = (box or {}).get("price_tick")
    try:
        tick = float(raw if raw not in (None, "") else fallback)
    except (TypeError, ValueError):
        tick = fallback
    return tick if tick > 0 else 1.0


def _in_session(cfg: dict, symbol: str, now: datetime) -> tuple[bool, str]:
    """这个合约现在能不能报单。休市、午休、非交易日都返回原因。"""
    from src.utils.session_clock import in_session_windows, tradable_windows_for_symbol
    from src.utils.trading_day import is_trading_day

    if not is_trading_day(now.date()):
        return False, "今天不是交易日"
    windows = tradable_windows_for_symbol(cfg, symbol)
    if windows and not in_session_windows(windows, now, grace_sec=0):
        return False, "当前不在交易时段（商品 10:15～10:30、11:30～13:30 等休市不报单）"
    return True, ""


def _far_sell_price(last: float, upper: float, tick: float, gap_pct: float) -> tuple[float, str]:
    """卖开限价：离现价至少 gap_pct，且不高于涨停价减一跳。返回 (价格, 跳过原因)。"""
    if last <= 0 or upper <= 0:
        return 0.0, "柜台没有给出最新价或涨停价"
    target = last * (1.0 + max(0.0, gap_pct) / 100.0) + tick
    cap = upper - tick
    price = _align(min(target, cap), tick)
    if price <= 0 or cap <= last:
        return 0.0, f"涨停价 {upper} 离现价 {last} 太近，挂远单可能成交，跳过"
    if (price - last) / last * 100.0 < gap_pct:
        return 0.0, f"涨停价 {upper} 离现价 {last} 不足 {gap_pct:.1f}%，跳过"
    return price, ""


def _wait_terminal(gateway: object, order_ref: str, seconds: float) -> str:
    """等柜台把这一笔结束；超时原样返回当前状态。"""
    from src.trader.ctp_logic import is_terminal

    deadline = time.time() + max(0.0, seconds)
    status = ""
    while time.time() < deadline:
        order = gateway.table.get(order_ref)  # type: ignore[attr-defined]
        status = str(getattr(order, "status", "") or "")
        if order is None or is_terminal(status):
            return status
        time.sleep(0.05)
    return status


def _probe_one(
    trader: object,
    cfg: dict,
    symbol: str,
    *,
    volume: int,
    gap_pct: float,
) -> dict[str, object]:
    """给一个合约发远价卖开，确认柜台接受后撤单。返回一行结果。"""
    from src.trader.ctp_orders import ctp_order

    gateway = trader.gateway  # type: ignore[attr-defined]
    tick = _tick_for(cfg, symbol, float(getattr(trader.settings, "price_tick", 1.0)))
    ok, text = gateway.query_price_band(symbol)
    band = dict(getattr(gateway, "_instrument", None) or {})
    if not ok:
        return {"symbol": symbol, "result": "查涨跌停失败", "detail": text, "filled": 0}
    last = float(band.get("last") or 0)
    upper = float(band.get("upper") or 0)
    price, skip = _far_sell_price(last, upper, tick, gap_pct)
    if skip:
        return {
            "symbol": symbol,
            "result": "跳过",
            "detail": skip,
            "last": last,
            "upper": upper,
            "filled": 0,
        }

    req = ctp_order(
        symbol,
        "sell_open",
        volume,
        price,
        remark="ctp_order_probe.py 远价报单权限探针",
    )
    started = time.time()
    result = trader.place_order(req)  # type: ignore[attr-defined]
    orders = list((result.raw or {}).get("orders") or [])
    first = orders[0] if orders else {}
    ref = str(first.get("order_ref") or "")
    status = str(first.get("status") or "")
    filled = int(first.get("filled") or 0)
    accepted = status in {"submitted", "partial", "filled"}

    cancel_note = ""
    if ref and not filled:
        ok_cancel, note = gateway.cancel(ref)
        cancel_note = note if ok_cancel else f"撤单失败：{note}"
        if ok_cancel:
            _wait_terminal(gateway, ref, float(getattr(trader.settings, "cancel_wait_sec", 3.0)) + 2.0)
        order = gateway.table.get(ref)
        filled = int(getattr(order, "filled", 0) or filled)

    return {
        "symbol": symbol,
        "result": "接受" if accepted else "拒绝",
        "detail": result.message,
        "status": status,
        "price": price,
        "last": last,
        "upper": upper,
        "filled": filled,
        "cancel": cancel_note,
        "ms": int((time.time() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="多品种报单权限探针（远价挂单，不成交）")
    parser.add_argument("--kind", choices=["sim", "live"], default="live", help="连哪个操作账")
    parser.add_argument("--symbols", nargs="+", default=None, help="要测的合约；默认取多品种实盘清单")
    parser.add_argument("--volume", type=int, default=1, help="每笔手数")
    parser.add_argument("--gap-pct", type=float, default=2.0, help="挂单价至少离现价百分之几")
    parser.add_argument("--cap-check", action="store_true", help="额外验证本机单笔上限（不发单）")
    parser.add_argument("--yes", action="store_true", help="确认真的要向柜台报单")
    args = parser.parse_args()

    if not args.yes:
        print("没有真的连柜台。要执行权限探针，请加 --yes。")
        if args.kind == "live":
            print(f"真实账户还必须设置环境变量 {PROBE_ENV}=1。")
        print(f"本次计划：{args.kind} {args.symbols or '多品种实盘清单'} {args.volume} 手，离现价 {args.gap_pct}%")
        return 2
    if args.kind == "live" and os.environ.get(PROBE_ENV) != "1":
        print(f"真实账户必须同时设置环境变量 {PROBE_ENV}=1，本次没有发单。")
        return 2
    if args.volume <= 0:
        print("手数必须大于 0。")
        return 2

    from src.utils.config_loader import load_config
    from src.utils.single_instance import acquire

    if not acquire():
        print("程序已在运行：最稳妥的做法是先停止常驻/关闭交易台，再跑这个探针，")
        print("否则同一账号两条连接会互相把对方踢下线。")
        return 2

    cfg = load_config()
    symbols = [str(item).strip() for item in (args.symbols or []) if str(item).strip()]
    if not symbols:
        symbols = [
            str(item).strip()
            for item in (((cfg.get("live") or {}).get("multi_symbol") or {}).get("symbols") or [])
            if str(item).strip()
        ]
    if not symbols:
        print("没有可测合约：请用 --symbols 指定，或在 live.multi_symbol.symbols 里配置。")
        return 2

    now = datetime.now()
    ready: list[str] = []
    for symbol in symbols:
        ok, why = _in_session(cfg, symbol, now)
        if ok:
            ready.append(symbol)
        else:
            print(f"{symbol}：{why}，跳过")
    if not ready:
        print("没有一个合约现在能报单，本次退出。")
        return 1

    from src.trader.ctp_orders import prepare_live_ctp_cfg
    from src.trader.ctp_trader import CtpTrader

    cfg = prepare_live_ctp_cfg(cfg, args.kind)
    trader = CtpTrader(cfg)
    max_volume = int(getattr(trader.settings, "max_order_volume", 0) or 0)
    print(
        f"连接{args.kind}账户 {trader.settings.user_id}；本机单笔上限 {max_volume} 手；"
        f"报单提醒阈值 {trader.settings.order_count_warn} 笔（只提醒不拦单）。"
    )
    ok, message = trader.connect()
    if not ok:
        print(f"连接失败：{message}")
        return 1
    print(f"连接成功：{message}")

    rows: list[dict[str, object]] = []
    try:
        for symbol in ready:
            if args.volume > max_volume > 0:
                print(f"{symbol}：{args.volume} 手超过本机单笔上限 {max_volume}，跳过")
                rows.append({"symbol": symbol, "result": "跳过", "detail": "超过本机单笔上限", "filled": 0})
                continue
            row = _probe_one(trader, cfg, symbol, volume=args.volume, gap_pct=args.gap_pct)
            rows.append(row)
            print(
                f"{symbol}：{row.get('result')} | 价格 {row.get('price', '-')}（最新 {row.get('last', '-')}，"
                f"涨停 {row.get('upper', '-')}）| {row.get('detail', '')}"
                + (f" | {row['cancel']}" if row.get("cancel") else "")
            )
            if int(row.get("filled") or 0) > 0:
                print(f"!!! {symbol} 意外成交 {row['filled']} 手，请立刻人工检查持仓")
            time.sleep(0.8)

        if args.cap_check:
            from src.trader.ctp_logic import check_order_instruction

            symbol = ready[0]
            tick = _tick_for(cfg, symbol, float(trader.settings.price_tick))
            blocked = check_order_instruction(
                symbol, 1.0, max_volume + 1, price_tick=tick, max_volume=max_volume
            )
            print(
                f"本机单笔上限验证：{max_volume + 1} 手 → "
                + (f"已拦住（{blocked}）" if blocked else "没有拦住")
            )
    finally:
        # 防御：万一有挂单没撤掉，退出前再撤一次。
        for order in list(getattr(trader.gateway.table, "orders", {}).values()):
            if str(getattr(order, "status", "")) in {"pending", "submitted", "partial"}:
                trader.gateway.cancel(order.order_ref)
        trader.shutdown()

    rejected = [row for row in rows if row.get("result") == "拒绝"]
    filled = [row for row in rows if int(row.get("filled") or 0) > 0]
    print("=" * 56)
    print(f"汇总：接受 {len([r for r in rows if r.get('result') == '接受'])}，"
          f"拒绝 {len(rejected)}，跳过 {len([r for r in rows if r.get('result') in {'跳过', '查涨跌停失败'}])}")
    for row in rejected:
        print(f"  拒绝 {row.get('symbol')}：{row.get('detail')}")
    if filled:
        print("  有合约意外成交，请人工核对持仓后再决定是否继续。")
    return 0 if not rejected and not filled else 1


if __name__ == "__main__":
    raise SystemExit(main())
