"""盘前行情适配审计（session_audit）单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.strategy.market_state import (
    MarketFilterParams,
    evaluate_market_state,
)
from src.strategy.session_audit import (
    ENGINE_AUDIT_MODES,
    ENGINE_NAMES,
    run_pre_session_audit,
    format_audit_report,
    make_bars_provider,
    should_run_session_audit,
    SymbolAuditRow,
)


def _bars(closes: list[float], atr_half: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    closes_arr = np.array(closes, dtype=float)
    high = closes_arr + np.abs(rng.normal(0, atr_half, len(closes_arr))) + atr_half
    low = closes_arr - np.abs(rng.normal(0, atr_half, len(closes_arr))) - atr_half
    return pd.DataFrame({"high": high, "low": low, "close": closes_arr})


def _bars_provider_factory(bars_by_symbol: dict[str, pd.DataFrame]):
    """返回一个 bars_provider，从 dict 里取 K 线（测试注入用）。"""

    def fn(symbol: str, _tf: str, need: int) -> pd.DataFrame | None:
        df = bars_by_symbol.get(symbol)
        if df is None:
            return None
        if len(df) < need:
            return None
        return df.tail(need)

    return fn


def _trending_closes(n: int = 200, slope: float = 0.5) -> list[float]:
    rng = np.random.default_rng(1)
    t = np.arange(n, dtype=float)
    base = 100 + t * slope
    closes = base + rng.normal(0, 1.0, n)
    return closes.tolist()


def _ranging_closes(n: int = 200) -> list[float]:
    """来回震荡，ADX 低。"""
    rng = np.random.default_rng(2)
    arr = 100 + rng.normal(0, 0.8, n)
    return arr.tolist()


# —— 基础映射检查 ——


def test_engine_audit_modes_covers_all_registered() -> None:
    """ENGINE_AUDIT_MODES 必须覆盖当前所有在工厂里注册的引擎。"""
    from src.strategy.factory import REGISTRY

    for eng in REGISTRY:
        assert eng in ENGINE_AUDIT_MODES, f"{eng} 缺少审计模式映射"


def test_engine_names_match() -> None:
    """ENGINE_NAMES 必须覆盖 ENGINE_AUDIT_MODES 里全部引擎。"""
    for eng in ENGINE_AUDIT_MODES:
        assert eng in ENGINE_NAMES, f"{eng} 缺少中文名"


def test_daily_body_is_range_mode() -> None:
    assert ENGINE_AUDIT_MODES["daily_body"] == "range"


def test_trend_engines_use_trend_mode() -> None:
    for eng in (
        "trend_follow",
        "dual_ma",
        "bollinger",
        "sar_trend",
        "multi_factor",
        "multi_factor_adapt",
        "multi_vote",
        "multi_hybrid",
        "supertrend",
        "swing_trend",
    ):
        assert ENGINE_AUDIT_MODES[eng] == "trend", f"{eng} 应该用 trend 模式"


def test_morning_box_a_is_skipped() -> None:
    assert ENGINE_AUDIT_MODES["morning_box_a"] is None


def test_jesse_casper_skips_public_filter() -> None:
    assert ENGINE_AUDIT_MODES["jesse_casper"] is None


# —— run_pre_session_audit 核心逻辑 ——


def test_disabled_filter_returns_ok_for_all() -> None:
    """趋势策略自己的过滤关掉时放行；日内实体仍走自己的区间过滤。"""
    cfg = {
        "strategy": {
            "engine": "daily_body",
            "symbols": ["bu2612"],
            "timeframe": "15",
            "market_filter": {"enabled": False},
        }
    }
    rows = run_pre_session_audit(
        cfg, bars_provider=lambda *_a, **_kw: _bars(_trending_closes(n=200, slope=0.8), atr_half=3.0)
    )
    for r in rows:
        if r.ok is None:
            continue
        if r.engine == "daily_body":
            assert r.ok is False, f"日内实体在强单边里应被自己的区间过滤拦住，note={r.note}"
            continue
        assert r.ok is True, f"{r.engine} ok=False，note={r.note}"


def test_no_symbols_returns_empty() -> None:
    cfg = {"strategy": {"symbols": []}}
    rows = run_pre_session_audit(cfg)
    assert rows == []


def test_trending_bars_pass_for_trend_engines() -> None:
    """趋势行情（ADX 高 + ATR 中等分位）→ trend 模式的策略都 ok=True。"""
    closes = _trending_closes(n=200, slope=0.8)
    bars = _bars(closes, atr_half=2.0)
    bars_fn = _bars_provider_factory({"bu2612": bars})

    cfg = {
        "strategy": {
            "engine": "trend_follow",
            "symbols": ["bu2612"],
            "timeframe": "15",
            "market_filter": {"enabled": True, "adx_flat": 20, "atr_low_pct": 0.15},
        }
    }
    rows = run_pre_session_audit(cfg, bars_provider=bars_fn)

    trend_rows = [r for r in rows if r.expected_mode == "trend"]
    assert len(trend_rows) > 0
    # 趋势行情 + trend 模式 → 应该 ok
    for r in trend_rows:
        assert r.ok is True, f"{r.engine} ok=False: {r.note}"


def test_ranging_bars_pass_for_range_but_not_for_trend() -> None:
    """震荡行情（ADX 低）→ range 策略放行，trend 策略被拦。"""
    closes = _ranging_closes(n=200)
    bars = _bars(closes, atr_half=1.5)
    bars_fn = _bars_provider_factory({"bu2612": bars})

    cfg = {
        "strategy": {
            "engine": "daily_body",
            "symbols": ["bu2612"],
            "timeframe": "15",
            "market_filter": {"enabled": True, "adx_flat": 20, "atr_low_pct": 0.15},
        }
    }
    rows = run_pre_session_audit(cfg, bars_provider=bars_fn)

    range_rows = [r for r in rows if r.expected_mode == "range"]
    trend_rows = [r for r in rows if r.expected_mode == "trend"]

    # 日内实体 range 模式：震荡行情（ADX 低 + ATR 中等）→ 放行
    assert all(r.ok is True for r in range_rows), "日内实体 range 模式应该放行震荡行情"

    # trend 模式：ADX 低 → 被拦
    assert any(r.ok is False for r in trend_rows), "trend 策略在震荡行情里应该被拦"


def test_make_bars_provider_uses_kline_provider_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认拉 K 线必须走 runner 同款 KlineProvider，且同一轮内按品种缓存。

    回归：早期版本误 import 了不存在的 src.data.kline_store，异常被吞后
    所有品种都显示"K线不足（拉到 0 根）"。
    """
    import src.data

    calls: list[tuple[str, str]] = []

    class FakeProvider:
        def __init__(self, cfg: dict[str, Any]) -> None:
            self.cfg = cfg

        def fetch(self, symbol: str, period: str | None = None) -> pd.DataFrame:
            calls.append((symbol, str(period)))
            return _bars([100.0] * 120)

    monkeypatch.setattr(src.data, "KlineProvider", FakeProvider)

    bars_fn = make_bars_provider({"strategy": {"timeframe": "5"}})
    first = bars_fn("rb2701", "5", 100)
    assert first is not None and len(first) == 120
    assert calls == [("rb2701", "5")]

    # 同一品种第二次调用命中缓存，不再请求
    second = bars_fn("rb2701", "5", 100)
    assert second is not None
    assert calls == [("rb2701", "5")]

    # 需要根数超过拿到的手数时返回 None（上层按 K线不足处理）
    assert bars_fn("rb2701", "5", 130) is None

    # 换品种会重新请求
    assert bars_fn("bu2701", "5", 100) is not None
    assert calls[-1] == ("bu2701", "5")


def test_no_bars_provider_returns_skip() -> None:
    """K 线拉不到 → 每行 ok=None 且 note 里写原因。"""
    cfg = {
        "strategy": {
            "engine": "trend_follow",
            "symbols": ["bu2612"],
            "timeframe": "15",
            "market_filter": {"enabled": True},
        }
    }
    rows = run_pre_session_audit(cfg, bars_provider=lambda *_a, **_kw: None)
    for r in rows:
        if r.expected_mode is None:
            continue  # morning_box_a 跳过
        assert r.ok is None, f"{r.engine} ok 应该是 None（无数据）"
        assert "K线不足" in r.note


def test_morning_box_a_skipped() -> None:
    rows = run_pre_session_audit(
        {"strategy": {"symbols": ["bu2612"], "market_filter": {"enabled": True}}},
        bars_provider=lambda *_a, **_kw: _bars(_trending_closes()),
    )
    mb = [r for r in rows if r.engine == "morning_box_a"]
    assert mb, "应该有 morning_box_a 这一行"
    assert mb[0].ok is None
    assert "未接入" in mb[0].note


# —— format_audit_report ——


def test_report_counts_ok_block_skip() -> None:
    rows = [
        SymbolAuditRow("bu2612", "trend_follow", "趋势跟随", "trend", None, True, "[趋势] 行情适配"),
        SymbolAuditRow("bu2612", "dual_ma", "双均线", "trend", None, False, "ADX低…停手"),
        SymbolAuditRow("bu2612", "morning_box_a", "早盘箱体A", None, None, None, "未接入"),
    ]
    out = format_audit_report(rows, now=datetime(2026, 11, 5, 8, 55))
    text = "\n".join(out)
    assert "盘前行情适配审计" in text
    assert "2026-11-05 08:55" in text
    assert "[OK  ]" in text, f"got: {text!r}"
    assert "[BLOCK]" in text
    assert "[SKIP]" in text
    assert "汇总：1 项适配 / 1 项不适配 / 1 项跳过" in text


def test_empty_symbols_report() -> None:
    out = format_audit_report([], now=datetime(2026, 11, 5, 8, 55))
    assert "关注品种列表为空" in "\n".join(out)


# —— should_run_session_audit ——


def test_exactly_on_time_triggers() -> None:
    """正好落在 08:55 分钟点上（±3 分钟内）且没跑过 → True。"""
    t = datetime(2026, 11, 5, 8, 55)
    assert should_run_session_audit(t, None) is True


def test_before_window_does_not_trigger() -> None:
    t = datetime(2026, 11, 5, 8, 40)
    assert should_run_session_audit(t, None) is False


def test_after_an_hour_does_not_double_trigger() -> None:
    """同一时段已经跑过 → False。"""
    t1 = datetime(2026, 11, 5, 8, 55)
    t2 = t1 + timedelta(minutes=2)
    assert should_run_session_audit(t2, t1) is False


def test_next_day_after_run_triggers_again() -> None:
    """上一次触发已经是昨天的 08:55 → 今天的 08:55 应该再触发。"""
    yesterday = datetime(2026, 11, 4, 8, 55)
    today = datetime(2026, 11, 5, 8, 55)
    assert should_run_session_audit(today, yesterday) is True


def test_other_time_points() -> None:
    """另外两个盘前窗口：13:25 和 20:25。"""
    assert should_run_session_audit(datetime(2026, 11, 5, 13, 25), None) is True
    assert should_run_session_audit(datetime(2026, 11, 5, 20, 25), None) is True
    assert should_run_session_audit(datetime(2026, 11, 5, 12, 30), None) is False
    assert should_run_session_audit(datetime(2026, 11, 5, 22, 0), None) is False
