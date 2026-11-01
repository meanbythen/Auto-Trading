# -*- coding: utf-8 -*-
"""滑点记录模块：符号约定、CSV 落盘、汇总统计。"""
from src.account.slippage_log import load_rows, record_fill, summarize, summarize_text


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
