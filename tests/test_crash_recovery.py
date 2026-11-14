"""崩溃恢复：GUI 真下单在点击后、记账前中断时，下次启动熔断等人核对。"""

import json

import pytest

from src.scheduler.runner import TradingBot
from src.strategy.types import StrategyAction, StrategyDecision
from src.utils.state_store import default_state_path, load_state


def _cfg(root, *, executor: str = "gui", dry_run: bool = False) -> dict:
    """一套最小的配置。GUI 真下单时启动会检查遗留委托意图。"""
    return {
        "dry_run": dry_run,
        "trade": {"symbol": "bu2612", "executor": executor, "volume": 1, "gui": {}},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(root),
    }


def _intent_path(root):
    return root / "data" / "order_intent.json"


def _write_intent(root, *, executor: str = "gui", side: str = "buy_open", volume: int = 1) -> None:
    path = _intent_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": "2026-11-04T09:20:00",
                "symbol": "bu2612",
                "side": side,
                "volume": volume,
                "bar_id": "2026-11-04 09:15",
                "reason": "test",
                "executor": executor,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _intent_files(root):
    """新版按笔存放的意图文件（data/intents/<账>/ 下一笔一个）。"""
    folder = root / "data" / "intents" / "live"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def _events(root) -> str:
    path = root / "data" / "events.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_pending_gui_intent_halts_startup_until_confirmed(tmp_path):
    """点击后崩溃留下的意图文件要让下次启动熔断，人工核对后按钮解除。"""
    _write_intent(tmp_path)

    bot = TradingBot(_cfg(tmp_path))
    assert bot.risk.halted is True
    assert bot.risk.halt_reason.startswith("发现未完成的下单记录")
    assert "bu2612" in bot.risk.halt_reason
    assert _intent_path(tmp_path).exists()
    assert "pending_intent_found" in _events(tmp_path)

    ok, message = bot.confirm_positions_reviewed()
    assert ok is True
    assert "解除熔断" in message
    assert bot.risk.halted is False
    assert bot.risk.halt_reason == ""
    assert not _intent_path(tmp_path).exists()
    assert "position_review_confirmed" in _events(tmp_path)
    assert load_state(default_state_path(tmp_path))["halted"] is False

    # 再启动一次不会重新熔断
    assert TradingBot(_cfg(tmp_path)).risk.halted is False


def test_pending_sim_intent_is_cleared_without_halting(tmp_path):
    """非 GUI 通道由柜台回报兜底，残留文件直接清掉，不拦启动。"""
    _write_intent(tmp_path, executor="sim")

    bot = TradingBot(_cfg(tmp_path, executor="sim", dry_run=True))
    assert bot.risk.halted is False
    assert not _intent_path(tmp_path).exists()
    assert bot.confirm_positions_reviewed() == (False, "没有待核对的委托记录。")


def test_confirm_without_intent_returns_false(tmp_path):
    """没有遗留记录时点「对账确认」只提示，不改任何状态。"""
    bot = TradingBot(_cfg(tmp_path))
    assert bot.confirm_positions_reviewed() == (False, "没有待核对的委托记录。")
    assert bot.risk.halted is False


def test_pending_intent_matches_only_the_same_trade(tmp_path):
    """手工录入只在品种、方向、手数都对得上时，才算「这一笔」。"""
    _write_intent(tmp_path)
    bot = TradingBot(_cfg(tmp_path))

    assert bot.pending_intent_matches("bu2612", "buy_open", 1)
    assert bot.pending_intent_matches("BU2612", "buy_open", 1)
    assert bot.pending_intent_matches("bu612", "buy_open", 1)  # 郑商所三位年月写法
    assert bot.pending_intent_matches("bu2612", "buy_open", 2) is None
    assert bot.pending_intent_matches("bu2612", "sell_open", 1) is None
    assert bot.pending_intent_matches("bu2612", "close", 1) is None
    assert bot.pending_intent_matches("fu2701", "buy_open", 1) is None


def test_manual_entry_match_can_release_halt(tmp_path):
    """手工录入对上了遗留委托，确认后熔断解除、意图文件清掉。"""
    _write_intent(tmp_path)
    bot = TradingBot(_cfg(tmp_path))
    assert bot.risk.halted is True

    assert bot.pending_intent_matches("bu2612", "buy_open", 1)
    ok, message = bot.confirm_positions_reviewed()
    assert ok is True
    assert "解除熔断" in message
    assert bot.risk.halted is False
    assert not _intent_path(tmp_path).exists()
    assert bot.pending_intent_matches("bu2612", "buy_open", 1) is None


def test_close_intent_matches_manual_close_without_comparing_volume(tmp_path):
    """遗留的是平仓时，手工平仓只比品种和方向，手数由程序持仓决定。"""
    _write_intent(tmp_path, side="close", volume=1)
    bot = TradingBot(_cfg(tmp_path))

    assert bot.pending_intent_matches("bu2612", "close", 1)
    assert bot.pending_intent_matches("bu2612", "close", 3)
    assert bot.pending_intent_matches("bu2612", "buy_open", 1) is None


def test_order_intent_context_clears_after_normal_exit(tmp_path):
    """正常走完（成功或明确失败）后意图文件自动清掉，不用每个分支手写清理。"""
    bot = TradingBot(_cfg(tmp_path))
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "测试",
        entry_price=4000,
        meta={"bar_id": "2026-11-04 09:15"},
    )
    with bot._order_intent(decision, 1):
        files = _intent_files(tmp_path)
        assert len(files) == 1
        assert files[0].stem.startswith("2026")
    assert _intent_files(tmp_path) == []


def test_order_intent_context_keeps_file_when_body_raises(tmp_path):
    """点击后抛异常要保留意图文件，下次启动按未完成委托熔断，避免重复下单。"""
    bot = TradingBot(_cfg(tmp_path))
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "bu2612",
        "测试",
        entry_price=4000,
        meta={"bar_id": "2026-11-04 09:15"},
    )
    with pytest.raises(RuntimeError):
        with bot._order_intent(decision, 1):
            raise RuntimeError("点击后崩溃")
    files = _intent_files(tmp_path)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["symbol"] == "bu2612"
    assert payload["volume"] == 1
    assert payload["intent_id"] in files[0].name


def test_each_order_intent_is_its_own_file(tmp_path):
    """一笔一个文件：多笔崩溃遗留能同时存在、逐笔核对，不会被后一笔覆盖。"""
    folder = tmp_path / "data" / "intents" / "live"
    folder.mkdir(parents=True)
    for name, side, volume, ts in (
        ("20261104092000000000-1.json", "buy_open", 1, "2026-11-04T09:20:00"),
        ("20261104103000000000-2.json", "sell_open", 2, "2026-11-04T10:30:00"),
    ):
        (folder / name).write_text(
            json.dumps(
                {
                    "ts": ts,
                    "intent_id": name[:-5],
                    "symbol": "bu2612",
                    "side": side,
                    "volume": volume,
                    "executor": "gui",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    bot = TradingBot(_cfg(tmp_path))
    assert bot.risk.halted is True
    assert len(bot._pending_intents) == 2
    assert "共 2 笔" in bot.risk.halt_reason
    # 最新那一笔用于提示；两笔都能和手工录入对上。
    assert bot._pending_intent["side"] == "sell_open"
    assert bot.pending_intent_matches("bu2612", "buy_open", 1)
    assert bot.pending_intent_matches("bu2612", "sell_open", 2)

    ok, _ = bot.confirm_positions_reviewed()
    assert ok is True
    assert bot.risk.halted is False
    assert list(folder.glob("*.json")) == []


def test_rollover_rechecks_pending_intent(tmp_path):
    """新交易日不能把崩溃熔断顺手清掉：日切后要重新检查意图文件。"""
    bot = TradingBot(_cfg(tmp_path))
    bot.trading_day = "2020-01-01"
    _write_intent(tmp_path)

    assert bot._rollover_trading_day() is True
    assert bot.risk.halted is True
    assert bot.risk.halt_kind == "pending_intent"
    assert _intent_path(tmp_path).exists()


def test_event_log_rotates_when_file_gets_too_large(tmp_path, monkeypatch) -> None:
    """events.jsonl 过大时轮转成 .1，长期挂机不会一直追加到同一个大文件。"""
    import src.scheduler.runner as runner_mod

    monkeypatch.setattr(runner_mod, "EVENT_LOG_MAX_BYTES", 200)
    bot = TradingBot(_cfg(tmp_path, executor="sim", dry_run=True))

    bot._record_event("first", note="x" * 300)
    bot._record_event("second")

    assert (tmp_path / "data" / "events.jsonl.1").exists()
    assert '"event": "second"' in _events(tmp_path)
    assert '"event": "first"' not in _events(tmp_path)
