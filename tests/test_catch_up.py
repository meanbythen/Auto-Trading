"""
启动回溯补开仓的三道门：时间、ATR 偏离、止损路径。
不连真实行情，只用假 K 线和假策略探测结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

from src.scheduler import catch_up as catch_up_module
from src.scheduler.catch_up import (
    CatchUpSettings,
    _atr_at_end,
    _prefix_bars,
    _probe_open,
    _truncate_by_end,
    find_catch_up_open,
    stop_was_touched,
)
from src.strategy.types import StrategyAction, StrategyDecision


def _bars(end: datetime, n: int = 40, step_min: int = 5, base: float = 100.0) -> pd.DataFrame:
    """造一段足够算 ATR 的假 K 线。最后一根收在 end。"""
    idx = [end - timedelta(minutes=step_min * (n - 1 - i)) for i in range(n)]
    rows = []
    price = base
    for i in range(n):
        # 轻微波动，方便 ATR > 0，又不会乱扫止损。
        high = price + 1.0
        low = price - 1.0
        close = price + (0.2 if i % 2 == 0 else -0.2)
        rows.append({"open": price, "high": high, "low": low, "close": close, "volume": 100})
        price = close
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_stop_was_touched_long_and_short() -> None:
    frame = pd.DataFrame(
        {
            "high": [105.0, 106.0],
            "low": [99.0, 98.0],
        }
    )
    assert stop_was_touched(side="long", stop_price=98.5, after_signal=frame) is True
    assert stop_was_touched(side="long", stop_price=90.0, after_signal=frame) is False
    assert stop_was_touched(side="short", stop_price=105.5, after_signal=frame) is True
    assert stop_was_touched(side="short", stop_price=110.0, after_signal=frame) is False


def test_catch_up_rejects_when_signal_too_old() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    closed = _bars(now - timedelta(minutes=25), n=40)
    settings = CatchUpSettings(enabled=True, lookback_bars=3, max_age_minutes=20)

    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓",
        entry_price=100.0,
        stop_price=95.0,
    )

    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_rejects_when_price_moved_too_far() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    closed = _bars(now - timedelta(minutes=5), n=40, base=100.0)
    # 把现价（最后收盘）拉得很远，超过 0.5 ATR。
    closed = closed.copy()
    closed.iloc[-1, closed.columns.get_loc("close")] = 200.0
    closed.iloc[-1, closed.columns.get_loc("high")] = 201.0

    settings = CatchUpSettings(
        enabled=True, lookback_bars=3, max_age_minutes=20, max_entry_atr=0.5, atr_period=14
    )
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓",
        entry_price=100.0,
        stop_price=90.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_rejects_when_stop_already_hit() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    # 信号在倒数第 2 根；最后一根把最低打穿止损。
    closed = _bars(now - timedelta(minutes=5), n=40, base=100.0)
    closed = closed.copy()
    signal_pos = len(closed) - 2
    closed.iloc[signal_pos, closed.columns.get_loc("close")] = 100.0
    closed.iloc[-1, closed.columns.get_loc("low")] = 90.0
    closed.iloc[-1, closed.columns.get_loc("high")] = 101.0
    closed.iloc[-1, closed.columns.get_loc("close")] = 100.5

    settings = CatchUpSettings(enabled=True, lookback_bars=3, max_age_minutes=20)
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓",
        entry_price=float(closed.iloc[signal_pos]["close"]),
        stop_price=95.0,
    )

    def _probe(_cfg, _symbol, prefix, *_a, **_k):
        # 只在截到倒数第 2 根时给出开仓，模拟「最新一根没信号、前一根有」。
        if len(prefix) == signal_pos + 1:
            return open_decision
        return None

    with patch("src.scheduler.catch_up._probe_open", side_effect=_probe):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_hit_when_all_gates_pass() -> None:
    now = datetime(2026, 10, 27, 10, 0, 0)
    closed = _bars(now - timedelta(minutes=5), n=40, base=100.0)
    settings = CatchUpSettings(
        enabled=True, lookback_bars=3, max_age_minutes=20, max_entry_atr=0.5, atr_period=14
    )
    entry = float(closed.iloc[-1]["close"])
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "假开仓仍有效",
        entry_price=entry,
        stop_price=entry - 10.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="bu2612",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is not None
    assert hit.decision.action == StrategyAction.BUY_OPEN
    assert len(hit.prefix) == len(closed)


def test_catch_up_settings_from_cfg_defaults() -> None:
    s = CatchUpSettings.from_cfg({})
    assert s.enabled is True
    assert s.lookback_bars == 3
    assert s.max_age_minutes == 20
    assert s.max_entry_atr == 0.5
    assert s.max_chase_atr == 2.0

    s2 = CatchUpSettings.from_cfg(
        {"strategy": {"catch_up_on_start": {"enabled": False, "lookback_bars": 1}}}
    )
    assert s2.enabled is False
    assert s2.lookback_bars == 1

    s3 = CatchUpSettings.from_cfg(
        {"strategy": {"catch_up_on_start": {"max_chase_atr": "bad"}}}
    )
    assert s3.max_chase_atr == 2.0


def test_catch_up_rejects_chasing_big_bar_end() -> None:
    """11-05 夜盘：4216→4308 那根收在 4288，止损只差 60 点，补开就是贴着止损进场。"""
    now = datetime(2026, 11, 5, 21, 10, 30)
    closed = _bars(now - timedelta(minutes=0), n=40, base=100.0)
    closed = closed.copy()
    # 最后一根做成大阳线：低点 90，收在 108，振幅远大于 ATR。
    last = len(closed) - 1
    closed.iloc[last, closed.columns.get_loc("low")] = 90.0
    closed.iloc[last, closed.columns.get_loc("close")] = 108.0
    closed.iloc[last, closed.columns.get_loc("high")] = 108.5

    settings = CatchUpSettings(
        enabled=True,
        lookback_bars=1,
        max_age_minutes=20,
        max_entry_atr=0.5,
        max_chase_atr=1.0,
        atr_period=14,
    )
    entry = float(closed.iloc[last]["close"])
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "假开仓追高",
        entry_price=entry,
        stop_price=entry - 10.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is None


def test_catch_up_allows_normal_bar() -> None:
    """普通振幅的 K 线不能被追高门误杀。"""
    now = datetime(2026, 11, 5, 21, 10, 30)
    closed = _bars(now - timedelta(minutes=0), n=40, base=100.0)
    closed = closed.copy()
    last = len(closed) - 1
    entry = float(closed.iloc[last]["close"])
    # 收盘价距最低只有 2 元，远小于 2×ATR。
    closed.iloc[last, closed.columns.get_loc("low")] = entry - 2.0
    closed.iloc[last, closed.columns.get_loc("high")] = entry + 0.5

    settings = CatchUpSettings(
        enabled=True,
        lookback_bars=1,
        max_age_minutes=20,
        max_entry_atr=0.5,
        max_chase_atr=2.0,
        atr_period=14,
    )
    open_decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "假开仓正常",
        entry_price=entry,
        stop_price=entry - 10.0,
    )
    with patch("src.scheduler.catch_up._probe_open", return_value=open_decision):
        hit = find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=closed,
            settings=settings,
            now=now,
        )
    assert hit is not None


def test_forming_bar_is_not_used_as_now() -> None:
    """持仓时拼进来的那根写着 22:55，但它还没走完；拿它当现在会在 22:50 就触发清仓。"""
    from src.scheduler.runner import _eval_now

    index = pd.DatetimeIndex(["2026-10-23 22:45", "2026-10-23 22:50"])
    close = pd.Series(5400.0, index=index)
    closed = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 10}
    )
    forming = pd.DataFrame(
        {
            "open": [5400.0],
            "high": [5401.0],
            "low": [5399.0],
            "close": [5400.0],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex(["2026-10-23 22:55"]),
    )
    eval_bars = pd.concat([closed, forming])
    now = _eval_now(eval_bars, closed, catch_up=False)
    assert now is not None
    assert abs((datetime.now() - now).total_seconds()) < 120
    # 只看已收盘的K线时也用墙上时钟：周末过后最后一根可能停在周五收盘，
    # 拿它当现在会误命中 22:55 的清仓窗口。
    now = _eval_now(closed, closed, catch_up=False)
    assert now is not None
    assert abs((datetime.now() - now).total_seconds()) < 120


def test_runner_skips_catch_up_when_already_long(tmp_path) -> None:
    """重启后程序还记着多仓时，第一轮扫描不能再走回溯补开。"""
    from src.scheduler.runner import TradingBot
    from src.strategy.types import PositionState

    cfg = {
        "dry_run": True,
        "trade": {"symbol": "fu2701", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["fu2701"],
            "catch_up_on_start": {"enabled": True, "lookback_bars": 3},
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000},
        "data": {},
        "_project_root": str(tmp_path),
    }
    bot = TradingBot(cfg)
    bot.strategy.positions["fu2701"] = PositionState(
        side="long",
        volume=1,
        entry_price=5259.0,
        initial_stop=5231.0,
        trail_stop=5231.0,
        symbol="fu2701",
    )
    bot._catch_up_pending = True
    assert bot._symbol_is_flat("fu2701") is False

    called = {"catch_up": False}

    def _fake_find(**_kwargs):
        called["catch_up"] = True
        return None

    with patch("src.scheduler.catch_up.find_catch_up_open", side_effect=_fake_find):
        with patch.object(bot.kline, "fetch", return_value=_bars(datetime(2026, 10, 27, 10, 15), n=40)):
            with patch.object(bot, "_evaluate_symbol") as ev:
                from src.strategy.types import StrategyAction, StrategyDecision

                ev.return_value = StrategyDecision(StrategyAction.HOLD, "fu2701", "观望")
                with patch.object(bot, "_execute"):
                    bot.scan_symbol("fu2701")

    assert called["catch_up"] is False
    assert bot._catch_up_pending is False


class _FakeProbe:
    """只实现 evaluate，用来验证 _probe_open 的传参和动作过滤。"""

    def __init__(self, decision: StrategyDecision, *, uses_trend: bool = False, reject_now: bool = False):
        self.decision = decision
        self.uses_trend_bars = uses_trend
        self.reject_now = reject_now
        self.calls: list[dict[str, object]] = []

    def evaluate(self, symbol: str, bars: pd.DataFrame, **kwargs: object) -> StrategyDecision:
        self.calls.append(kwargs)
        if self.reject_now and "now" in kwargs:
            raise TypeError("unexpected keyword argument 'now'")
        return self.decision


def test_truncate_by_end_keeps_signal_side_and_returns_original_on_bad_index() -> None:
    """辅助周期只截到信号那一根；索引不可比时原样返回，不吞掉整段数据。"""
    index = pd.DatetimeIndex(["2026-10-27 09:00", "2026-10-27 09:05"])
    frame = pd.DataFrame({"close": [1.0, 2.0]}, index=index)
    cut = _truncate_by_end(frame, datetime(2026, 10, 27, 9, 0))
    assert cut is not None
    assert list(cut["close"]) == [1.0]
    empty = _truncate_by_end(frame, datetime(2026, 10, 27, 8, 0))
    assert empty is not None
    assert empty.empty
    assert _truncate_by_end(None, datetime(2026, 10, 27, 9, 0)) is None

    weird = pd.DataFrame({"close": [1.0, 2.0]}, index=["a", "b"])
    assert _truncate_by_end(weird, datetime(2026, 10, 27, 9, 0)) is weird


def test_prefix_bars_returns_none_when_not_enough_rows() -> None:
    frame = _bars(datetime(2026, 10, 27, 10, 0), n=2)
    newest = _prefix_bars(frame, 0)
    assert newest is not None
    assert len(newest) == 2
    older = _prefix_bars(frame, 1)
    assert older is not None
    assert len(older) == 1
    assert _prefix_bars(frame, 2) is None
    assert _prefix_bars(None, 0) is None  # type: ignore[arg-type]


def test_atr_at_end_rejects_short_missing_columns_and_flat_market(monkeypatch) -> None:
    end = datetime(2026, 10, 27, 10, 0)
    assert _atr_at_end(_bars(end, n=5), 14) is None
    assert _atr_at_end(_bars(end, n=20).drop(columns=["high"]), 14) is None
    flat = pd.DataFrame({"high": [100.0] * 20, "low": [100.0] * 20, "close": [100.0] * 20})
    assert _atr_at_end(flat, 14) is None
    assert _atr_at_end(_bars(end, n=20), 14) > 0

    monkeypatch.setattr(
        catch_up_module, "average_true_range", lambda *a, **k: pd.Series(dtype=float)
    )
    assert _atr_at_end(_bars(end, n=20), 14) is None


def test_stop_was_touched_rejects_bad_frame_and_unknown_side() -> None:
    only_close = pd.DataFrame({"close": [100.0]})
    assert stop_was_touched(side="long", stop_price=99.0, after_signal=only_close) is False
    frame = pd.DataFrame({"high": [101.0], "low": [99.0]})
    assert stop_was_touched(side="flat", stop_price=100.0, after_signal=frame) is False
    assert stop_was_touched(side="long", stop_price=99.0, after_signal=pd.DataFrame()) is False


def test_probe_open_only_returns_open_actions(monkeypatch) -> None:
    bar_time = datetime(2026, 10, 27, 10, 0)
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "假开仓", entry_price=100.0, stop_price=95.0
    )
    probe = _FakeProbe(decision)
    monkeypatch.setattr(catch_up_module, "create_strategy", lambda cfg: probe)
    got = _probe_open({}, "fu2701", _bars(bar_time, n=20), None, None, bar_time)
    assert got is decision
    assert probe.calls[0]["now"] == bar_time
    assert "trend_bars" not in probe.calls[0]

    holding = _FakeProbe(StrategyDecision(StrategyAction.HOLD, "fu2701", "观望"))
    monkeypatch.setattr(catch_up_module, "create_strategy", lambda cfg: holding)
    assert _probe_open({}, "fu2701", _bars(bar_time, n=20), None, None, bar_time) is None


def test_probe_open_uses_trend_bars_and_falls_back_on_old_signature(monkeypatch) -> None:
    bar_time = datetime(2026, 10, 27, 10, 0)
    trend = _bars(bar_time, n=10)
    decision = StrategyDecision(
        StrategyAction.SELL_OPEN, "fu2701", "旧签名", entry_price=100.0, stop_price=105.0
    )
    probe = _FakeProbe(decision, uses_trend=True, reject_now=True)
    seen_cfg = []

    def _create(cfg):
        seen_cfg.append(cfg)
        return probe

    monkeypatch.setattr(catch_up_module, "create_strategy", _create)
    got = _probe_open(
        {"a": 1}, "fu2701", _bars(bar_time, n=20), None, trend, bar_time, {"b": 2}
    )
    assert got is decision
    assert seen_cfg == [{"b": 2}]  # 单独传的策略配置优先于整份配置
    assert probe.calls[0]["trend_bars"] is trend
    assert "now" in probe.calls[0]  # 先按新签名调一次
    assert set(probe.calls[1]) == {"trail_bars"}  # TypeError 后退回最小参数


def test_find_catch_up_disabled_or_empty_returns_none() -> None:
    end = datetime(2026, 10, 27, 10, 0)
    closed = _bars(end, n=20)
    assert (
        find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=closed,
            settings=CatchUpSettings(enabled=False),
            now=end,
        )
        is None
    )
    assert (
        find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=pd.DataFrame(),
            settings=CatchUpSettings(),
            now=end,
        )
        is None
    )


def test_find_catch_up_skips_bars_without_timestamp() -> None:
    """索引全是 NaT、又没有 datetime 列时，往前每一根都只能跳过。"""
    closed = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0]},
        index=pd.DatetimeIndex([pd.NaT]),
    )
    hit = find_catch_up_open(
        cfg={},
        symbol="fu2701",
        closed=closed,
        settings=CatchUpSettings(lookback_bars=2),
        now=datetime(2026, 10, 27, 10, 0),
    )
    assert hit is None


def test_find_catch_up_clamps_future_signal_to_zero_age(monkeypatch) -> None:
    """信号时间比 now 还晚时按 0 分钟算，仍然继续做后面的门，而不是直接丢弃。"""
    end = datetime(2026, 10, 27, 10, 5)
    closed = _bars(end, n=20)
    called: list[int] = []
    monkeypatch.setattr(catch_up_module, "_probe_open", lambda *a, **k: called.append(1))
    hit = find_catch_up_open(
        cfg={},
        symbol="fu2701",
        closed=closed,
        settings=CatchUpSettings(lookback_bars=1, max_age_minutes=0),
        now=datetime(2026, 10, 27, 10, 0),
    )
    assert hit is None
    assert called == [1]  # 时间门放行，确实走到了探测


def test_find_catch_up_skips_signal_without_prices_then_uses_older_bar(monkeypatch) -> None:
    now = datetime(2026, 10, 27, 10, 0)
    closed = _bars(now - timedelta(minutes=5), n=40)
    good_entry = float(closed.iloc[-2]["close"])
    good = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "fu2701",
        "旧一根正常",
        entry_price=good_entry,
        stop_price=good_entry - 10.0,
    )
    bad = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "缺价", entry_price=0.0, stop_price=0.0
    )
    seen: list[int] = []

    def _probe(_cfg, _symbol, prefix, *_a, **_k):
        seen.append(len(prefix))
        return bad if len(prefix) == len(closed) else good

    monkeypatch.setattr(catch_up_module, "_probe_open", _probe)
    hit = find_catch_up_open(
        cfg={},
        symbol="fu2701",
        closed=closed,
        settings=CatchUpSettings(lookback_bars=2, max_age_minutes=20),
        now=now,
    )
    assert seen == [len(closed), len(closed) - 1]
    assert hit is not None
    assert hit.decision is good
    assert hit.bar_time == closed.index[-2].to_pydatetime()


def test_find_catch_up_skips_bar_without_atr_then_uses_older(monkeypatch) -> None:
    now = datetime(2026, 10, 27, 10, 0)
    closed = _bars(now - timedelta(minutes=5), n=40)
    entry = float(closed.iloc[-2]["close"])
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "假开仓", entry_price=entry, stop_price=entry - 10.0
    )
    monkeypatch.setattr(catch_up_module, "_probe_open", lambda *a, **k: decision)
    calls: list[int] = []

    def _atr(bars, period):
        calls.append(period)
        return None if len(calls) == 1 else 5.0

    monkeypatch.setattr(catch_up_module, "_atr_at_end", _atr)
    hit = find_catch_up_open(
        cfg={},
        symbol="fu2701",
        closed=closed,
        settings=CatchUpSettings(lookback_bars=2, max_age_minutes=20),
        now=now,
    )
    assert len(calls) == 2
    assert hit is not None
    assert hit.atr == 5.0
    assert hit.bar_time == closed.index[-2].to_pydatetime()


def test_find_catch_up_checks_forming_bar_against_stop(monkeypatch) -> None:
    """正在走的那根也要参与止损判断；信号不是最新一根时与已收盘后续合并。"""
    now = datetime(2026, 10, 27, 10, 0)
    closed = _bars(now, n=40)
    entry = float(closed.iloc[-1]["close"])
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN, "fu2701", "假开仓", entry_price=entry, stop_price=entry - 5.0
    )
    monkeypatch.setattr(catch_up_module, "_probe_open", lambda *a, **k: decision)
    settings = CatchUpSettings(lookback_bars=2, max_age_minutes=20)
    forming_time = now + timedelta(minutes=5)

    dangerous = pd.DataFrame(
        {
            "open": [entry],
            "high": [entry + 1.0],
            "low": [entry - 10.0],
            "close": [entry],
        },
        index=pd.DatetimeIndex([forming_time]),
    )
    assert (
        find_catch_up_open(
            cfg={},
            symbol="fu2701",
            closed=closed,
            forming=dangerous,
            settings=settings,
            now=now,
        )
        is None
    )

    safe = dangerous.copy()
    safe["low"] = entry - 1.0
    hit = find_catch_up_open(
        cfg={},
        symbol="fu2701",
        closed=closed,
        forming=safe,
        settings=settings,
        now=now,
    )
    assert hit is not None
    assert hit.bar_time == closed.index[-1].to_pydatetime()



def test_catch_up_settings_from_cfg_tolerates_non_dict_strategy_value() -> None:
    """strategy.catch_up_on_start 是非 dict（比如字符串）时安全回退到默认值。"""
    s = CatchUpSettings.from_cfg({"strategy": {"catch_up_on_start": "bad"}})
    assert s.enabled is True  # 默认
    assert s.lookback_bars == 3
    assert s.max_chase_atr == 2.0


def test_catch_up_settings_from_cfg_clamps_atr_period_minimum() -> None:
    """atr_period 过小会被夹到 2（最小合法 ATR 周期）。"""
    s = CatchUpSettings.from_cfg({"strategy": {"catch_up_on_start": {"atr_period": 1}}})
    assert s.atr_period == 2


def test_catch_up_settings_from_cfg_clamps_max_chase_non_negative() -> None:
    s = CatchUpSettings.from_cfg(
        {"strategy": {"catch_up_on_start": {"max_chase_atr": -5}}}
    )
    assert s.max_chase_atr == 0.0


def test_bar_time_returns_none_for_empty_frame() -> None:
    from src.scheduler.catch_up import _bar_time

    assert _bar_time(None) is None
    assert _bar_time(pd.DataFrame()) is None


def test_bar_time_falls_back_to_datetime_column_when_index_is_nat() -> None:
    from src.scheduler.catch_up import _bar_time

    frame = pd.DataFrame(
        {"datetime": pd.to_datetime(["2026-10-27 10:00:00"]), "close": [100.0]},
        index=pd.DatetimeIndex([pd.NaT]),
    )
    result = _bar_time(frame)
    assert result is not None
    assert result.year == 2026
    assert result.hour == 10


def test_bar_time_returns_none_when_both_index_and_datetime_are_nat() -> None:
    from src.scheduler.catch_up import _bar_time

    frame = pd.DataFrame(
        {"datetime": pd.to_datetime(["2026-10-27 10:00:00"]), "close": [100.0]},
        index=pd.DatetimeIndex([pd.NaT]),
    )
    # 把 datetime 列也替换成 NaT
    frame = frame.copy()
    frame.iloc[0, frame.columns.get_loc("datetime")] = pd.NaT
    assert _bar_time(frame) is None
