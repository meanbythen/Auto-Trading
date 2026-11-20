# -*- coding: utf-8 -*-
"""滑点记录模块：符号约定、CSV 落盘、汇总统计。"""
import csv
import json
from datetime import datetime, timedelta

from src.account.slippage_log import (
    load_rows,
    quality_text,
    reject_summary,
    record_fill,
    summarize,
    summarize_by_side,
    summarize_by_session,
    summarize_by_symbol,
    summarize_text,
)


def test_record_sign_convention(tmp_path):
    # 买入方向：成交价高于理论价 → 正滑点（对你不利）
    slip = record_fill(
        tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
        theory_price=3000, fill_price=3002, tick=1, executor="ctp", dry_run=False,
    )
    assert slip == 2.0
    # 卖出方向：成交价低于理论价 → 正滑点
    slip = record_fill(
        tmp_path, symbol="fu2701", action="SELL_OPEN", volume=1,
        theory_price=3000, fill_price=2999, tick=1, executor="ctp", dry_run=False,
    )
    assert slip == 1.0
    # 平空是买入：成交价高于理论价 → 正滑点
    slip = record_fill(
        tmp_path, symbol="fu2701", action="CLOSE", volume=1, side_before="short",
        theory_price=3000, fill_price=3001, tick=1, executor="ctp", dry_run=False,
    )
    assert slip == 1.0
    # 没有理论价 → 不留滑点数字，但仍记一行
    slip = record_fill(
        tmp_path, symbol="fu2701", action="CLOSE", volume=1,
        theory_price=None, fill_price=3001, tick=1, executor="ctp", dry_run=False,
    )
    assert slip is None
    rows = load_rows(tmp_path)
    assert len(rows) == 4
    assert rows[3]["slip_ticks"] == ""


def test_strategy_action_values_use_the_same_sign_convention(tmp_path):
    """调度层传的是小写 buy_open/sell_open/close，也必须算出滑点。"""
    buy = record_fill(
        tmp_path, symbol="fu2701", action="buy_open", volume=1,
        theory_price=3000, fill_price=3002, tick=1, executor="ctp", dry_run=False,
    )
    close_long = record_fill(
        tmp_path, symbol="fu2701", action="close", volume=1, side_before="long",
        theory_price=3000, fill_price=2998, tick=1, executor="ctp", dry_run=False,
    )
    assumed = record_fill(
        tmp_path, symbol="fu2701", action="close", volume=1, side_before="long",
        theory_price=3000, fill_price=2990, tick=1, executor="gui", dry_run=False,
        assumed_fill=True,
    )
    assert buy == 2.0
    assert close_long == 2.0
    assert assumed == 0.0


def test_summarize_prefers_real_ctp(tmp_path):
    # 一笔 GUI 假定成交（滑点0）+ 两笔 CTP 真实成交
    record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                theory_price=3000, fill_price=3000, tick=1, executor="gui", dry_run=False)
    record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                theory_price=3000, fill_price=3001, tick=1, executor="ctp", dry_run=False)
    record_fill(tmp_path, symbol="fu2701", action="SELL_OPEN", volume=1,
                theory_price=3000, fill_price=2998, tick=1, executor="ctp", dry_run=False)
    info = summarize(tmp_path)
    assert info["used_real"] is True
    assert info["n"] == 2
    assert info["median"] == 1.5
    assert info["suggested"] >= 1.5


def test_summarize_empty(tmp_path):
    assert "还没有成交滑点记录" in summarize_text(tmp_path)


def test_summarize_by_symbol_keeps_products_apart(tmp_path):
    """生猪和燃油的滑点不能混成一个平均数。"""
    for slip in (1, 2, 3):
        record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                    theory_price=3000, fill_price=3000 + slip, tick=1,
                    executor="ctp", dry_run=False)
    for slip in (5, 6, 7):
        record_fill(tmp_path, symbol="lh2701", action="BUY_OPEN", volume=1,
                    theory_price=9000, fill_price=9000 + slip, tick=1,
                    executor="ctp", dry_run=False)
    # 样本不足 3 笔的品种不参与建议
    record_fill(tmp_path, symbol="c2701", action="BUY_OPEN", volume=1,
                theory_price=2300, fill_price=2301, tick=1, executor="ctp", dry_run=False)
    rows = summarize_by_symbol(tmp_path)
    assert [row["symbol"] for row in rows] == ["fu", "lh"]
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["fu"]["suggested"] == 2.5
    assert by_symbol["lh"]["p70"] > 5
    assert by_symbol["fu"]["used_real"] is True

    text = summarize_text(tmp_path)
    assert "分品种建议" in text
    assert "fu" in text and "lh" in text


def test_summarize_by_side_separates_buy_and_sell(tmp_path):
    for slip in (1, 2, 3):
        record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                    theory_price=3000, fill_price=3000 + slip, tick=1,
                    executor="ctp", dry_run=False)
    for slip in (5, 6, 7):
        record_fill(tmp_path, symbol="fu2701", action="SELL_OPEN", volume=1,
                    theory_price=3000, fill_price=3000 - slip, tick=1,
                    executor="ctp", dry_run=False)
    # GUI 假定成交不能掺进来
    record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                theory_price=3000, fill_price=3000, tick=1, executor="gui", dry_run=False)

    by_side = {row["key"]: row for row in summarize_by_side(tmp_path)}
    assert set(by_side) == {"买", "卖"}
    assert by_side["买"]["n"] == 3
    assert by_side["买"]["suggested"] == 2.5
    assert by_side["卖"]["suggested"] == 6.5


def _write_slip_row(root, ts: str, slip: int) -> None:
    path = root / "data" / "slippage_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["ts", "symbol", "action", "volume", "theory_price",
                             "fill_price", "slip_ticks", "tick", "executor", "dry_run"])
        writer.writerow([ts, "fu2701", "BUY_OPEN", 1, "3000", "3000", slip, "1", "ctp", "0"])


def test_old_header_is_migrated_on_read(tmp_path):
    """老文件的 10 列表头要先补成 11 列，否则新值会落到 None 键。"""
    _write_slip_row(tmp_path, "2026-11-19T10:05:00", 2)
    rows = load_rows(tmp_path)
    assert rows[0]["account"] == ""
    with (tmp_path / "data" / "slippage_log.csv").open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == [
        "ts", "symbol", "action", "volume", "theory_price",
        "fill_price", "slip_ticks", "tick", "executor", "dry_run", "account",
    ]


def test_append_after_migration_keeps_account_column(tmp_path):
    _write_slip_row(tmp_path, "2026-11-19T10:05:00", 2)
    record_fill(
        tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
        theory_price=3000, fill_price=3003, tick=1, executor="ctp",
        dry_run=False, account="sim",
    )
    rows = load_rows(tmp_path)
    assert rows[0]["account"] == ""
    assert rows[1]["account"] == "sim"
    assert rows[1]["slip_ticks"] == "3.00"


def test_summarize_by_session_splits_day_and_night(tmp_path):
    for slip in (1, 2, 3, 4):
        _write_slip_row(tmp_path, "2026-11-19T10:05:00", slip)
    for slip in (5, 6, 7):
        _write_slip_row(tmp_path, "2026-11-19T21:30:00", slip)

    by_session = {row["key"]: row for row in summarize_by_session(tmp_path)}
    assert by_session["日盘"]["n"] == 4
    assert by_session["夜盘"]["n"] == 3
    assert by_session["日盘"]["suggested"] < by_session["夜盘"]["suggested"]


def _write_event(root, ts: str, event: str, symbol: str) -> None:
    path = root / "data" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "event": event, "symbol": symbol}) + "\n")


def test_reject_summary_counts_symbols_and_reasons(tmp_path):
    now = datetime.now().isoformat(timespec="seconds")
    old = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    _write_event(tmp_path, now, "order_fail", "fu2701")
    _write_event(tmp_path, now, "order_fail", "fu2705")
    _write_event(tmp_path, now, "order_no_price", "lh2701")
    _write_event(tmp_path, now, "risk_block", "SA2701")
    _write_event(tmp_path, now, "strategy_health", "fu2701")
    _write_event(tmp_path, now, "order_sent", "fu2701")
    _write_event(tmp_path, old, "order_fail", "fu2701")

    info = reject_summary(tmp_path)
    assert info["total"] == 5
    reasons = {item["key"]: item["n"] for item in info["by_reason"]}
    assert reasons == {"柜台拒单": 2, "没有价格": 1, "风控拦截": 1, "健康停开": 1}
    symbols = {item["key"]: item["n"] for item in info["by_symbol"]}
    assert symbols["fu"] == 3
    assert symbols["lh"] == 1
    assert symbols["sa"] == 1


def test_quality_text_reports_missing_samples(tmp_path):
    text = quality_text(tmp_path)
    assert "成交质量" in text
    assert "按方向：真实样本不足 3 笔" in text
    assert "按时段：真实样本不足 3 笔" in text
    assert "拒单：近 30 天没有记录" in text


def test_summarize_can_filter_by_account(tmp_path):
    for slip in (1, 2, 3):
        record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                    theory_price=3000, fill_price=3000 + slip, tick=1,
                    executor="ctp", dry_run=False, account="sim")
    for slip in (5, 6, 7):
        record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                    theory_price=3000, fill_price=3000 + slip, tick=1,
                    executor="ctp", dry_run=False, account="live")

    sim = summarize(tmp_path, account="sim")
    live = summarize(tmp_path, account="live")
    assert sim["n"] == 3 and sim["suggested"] == 2.5
    assert live["n"] == 3 and live["suggested"] == 6.5
    assert summarize(tmp_path, account="missing")["n"] == 0


def test_quality_text_lists_all_three_blocks(tmp_path):
    for slip in (1, 2, 3):
        record_fill(tmp_path, symbol="fu2701", action="BUY_OPEN", volume=1,
                    theory_price=3000, fill_price=3000 + slip, tick=1,
                    executor="ctp", dry_run=False)
    _write_event(tmp_path, datetime.now().isoformat(timespec="seconds"), "order_fail", "fu2701")

    text = quality_text(tmp_path)
    assert "买 3笔" in text
    assert "按时段：" in text
    assert "近 30 天 1 次（柜台拒单 1 次）" in text
    assert "分品种  fu 1 次" in text
