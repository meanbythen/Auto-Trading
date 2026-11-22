"""
风控单元测试
功能：验证「过频、无仓平仓、日亏损熔断」会被正确拦截。
原理：不启动软件，直接调用 RiskGuardian，适合初学者理解风控逻辑。
"""

from src.risk.guardian import RiskGuardian


def test_open_waits_after_close_fill() -> None:
    """手机或本程序刚平仓后，不能立刻再开；配置页「下单最小间隔」要生效。"""
    g = RiskGuardian(min_order_interval_sec=60)
    g.on_order_filled("sell_open", 1, "lh2701")
    g.on_order_filled("close", 1, "lh2701")
    ok, reason = g.allow("sell_open", 1, symbol="lh2701")
    assert ok is False
    assert "不足" in reason
    assert "60" in reason


def test_second_open_waits_after_first_open() -> None:
    """两笔开仓之间也要等够间隔。"""
    g = RiskGuardian(min_order_interval_sec=60)
    g.on_order_filled("buy_open", 1, "bu2612")
    ok, reason = g.allow("buy_open", 1, symbol="bu2612")
    assert ok is False
    assert "不足" in reason


def test_force_close_ignores_interval() -> None:
    """收盘清仓带 force，不被间隔挡住。"""
    g = RiskGuardian(min_order_interval_sec=60)
    g.on_order_filled("buy_open", 1, "bu2612")
    ok, reason = g.allow("close", 1, symbol="bu2612", force=True)
    assert ok is True


def test_block_when_no_position_close() -> None:
    """没有持仓时不允许平仓。"""
    g = RiskGuardian(min_order_interval_sec=0)
    ok, reason = g.allow("close", 1)
    assert ok is False
    assert "无持仓" in reason


def test_allow_buy_then_update_position() -> None:
    """买开通过后，持仓应增加。"""
    g = RiskGuardian(min_order_interval_sec=0)
    ok, _ = g.allow("buy_open", 1)
    assert ok is True
    g.on_order_filled("buy_open", 1)
    assert g.current_position == 1


def test_close_flat_symbol_blocked_when_another_is_open() -> None:
    """玉米有仓时，不能把没仓的沥青也当成可以平。"""
    g = RiskGuardian(min_order_interval_sec=0)
    g.on_order_filled("sell_open", 1, symbol="c2612")
    ok, reason = g.allow("close", 1, symbol="bu2612")
    assert ok is False
    assert "bu2612" in reason
    ok_c, _ = g.allow("close", 1, symbol="c2612")
    assert ok_c is True


def test_clear_halt_only_daily_loss_is_auto_clearable() -> None:
    """崩溃/遗留委托熔断不能被日切或改限额顺手清掉。"""
    g = RiskGuardian(min_order_interval_sec=0)
    g.set_halt("pending_intent", "发现未完成的下单记录")
    assert g.clear_halt("daily_loss") is False
    assert g.halted is True
    assert g.halt_kind == "pending_intent"

    assert g.clear_halt() is True
    assert g.halted is False
    assert g.halt_kind == ""


def test_daily_loss_halt_can_be_auto_cleared() -> None:
    g = RiskGuardian(min_order_interval_sec=0)
    g.set_halt("daily_loss", "亏太多")
    assert g.clear_halt("daily_loss") is True
    assert g.halted is False


def test_crash_halt_is_not_overwritten_by_daily_loss() -> None:
    g = RiskGuardian(min_order_interval_sec=0)
    g.set_halt("pending_intent", "发现未完成的下单记录")
    g.set_halt("daily_loss", "亏太多")
    assert g.halted is True
    assert g.halt_kind == "pending_intent"
    assert "未完成的下单记录" in g.halt_reason


def test_multiplier_does_not_treat_cu_as_corn() -> None:
    """cu 的字母里含有 c，手数计算必须用铜自己的乘数，不能用玉米。"""
    from src.risk.sizing import PositionSizer

    sizer = PositionSizer(
        {
            "risk": {
                "sizing_mode": "fixed",
                "contracts": {
                    "c": {"multiplier": 10},
                    "cu": {"multiplier": 5},
                    "bu": {"multiplier": 10},
                },
            },
            "trade": {"volume": 1},
        }
    )
    assert sizer.multiplier_for("c2612") == 10
    assert sizer.multiplier_for("cu2612") == 5
    assert sizer.multiplier_for("bu2612") == 10
    # 没单独配置的品种不要借玉米的乘数
    bare = PositionSizer({"risk": {"contracts": {"c": {"multiplier": 8}}}, "trade": {"volume": 1}})
    assert bare.multiplier_for("cu2612") == 10


def test_risk_pct_without_stop_refuses_to_open() -> None:
    """按风险百分比算仓时缺止损必须拒单，不能退回固定手数开出裸仓。"""
    from src.risk.sizing import PositionSizer

    sizer = PositionSizer(
        {
            "risk": {"sizing_mode": "risk_pct", "risk_per_trade_pct": 1, "capital": 100000},
            "trade": {"volume": 3},
        }
    )

    result = sizer.calc_open("bu2612", 4000.0, None)

    assert result.volume == 0
    assert "止损" in result.reason

    zero_stop = sizer.calc_open("bu2612", 4000.0, 0.0)
    assert zero_stop.volume == 0


def test_close_symbol_match_ignores_letter_case() -> None:
    """风控净仓和信号合约大小写不一致时，仍应允许平仓。"""
    g = RiskGuardian(min_order_interval_sec=0)
    g.on_order_filled("buy_open", 1, symbol="FU2701")
    ok, _ = g.allow("close", 1, symbol="fu2701")
    assert ok is True


def test_block_when_daily_loss_halted() -> None:
    """当日亏损熔断后，不能再开新仓；平仓仍可。"""
    g = RiskGuardian(max_daily_loss=100, min_order_interval_sec=0)
    g.update_pnl(-150)
    assert g.halted is True
    ok, reason = g.allow("buy_open", 1)
    assert ok is False
    assert "熔断" in reason
    g.on_order_filled("buy_open", 1)  # 测试用：先造持仓
    g.halted = True
    ok_close, _ = g.allow("close", 1)
    assert ok_close is True


def test_pure_rules_use_explicit_time_and_do_not_mutate_state() -> None:
    from src.risk.rules import (
        RiskPolicy,
        RiskRuleState,
        apply_fill,
        daily_loss_reason,
        evaluate_order,
    )

    policy = RiskPolicy(max_daily_loss=100, min_order_interval_sec=60, max_orders_per_day=3)
    state = RiskRuleState(
        current_position=2,
        positions={"bu2612": 2},
        orders_today=1,
        open_orders_today=1,
        last_order_ts=100,
        last_open_ts=100,
    )

    blocked, _ = evaluate_order(policy, state, "buy_open", 1, symbol="bu2612", now=120)
    allowed, _ = evaluate_order(policy, state, "buy_open", 1, symbol="bu2612", now=160)
    changed = apply_fill(state, "buy_open", 1, symbol="bu2612", now=160)

    assert blocked is False
    assert allowed is True
    assert state.current_position == 2
    assert state.positions == {"bu2612": 2}
    assert state.orders_today == 1
    assert changed.current_position == 3
    assert changed.positions == {"bu2612": 3}
    assert changed.orders_today == 2
    assert changed.open_orders_today == 2
    assert changed.last_order_ts == 160
    assert "当日亏损" in daily_loss_reason(policy, RiskRuleState(daily_pnl=-100))


def test_risk_service_restart_restores_daily_counters_loss_and_timestamps(tmp_path) -> None:
    from contextlib import closing

    from src.account.trading_db import connect
    from src.risk.service import RiskService

    cfg = {
        "risk": {"min_order_interval_sec": 0, "max_daily_loss": 100},
        "storage": {"sqlite_enabled": True},
    }
    service = RiskService.from_config(cfg, root=tmp_path, account_id="acc_restart")
    service.write_alerts = False
    service.trading_day = "2026-11-20"
    service.on_order_filled("buy_open", 2, "bu2612", now=1000)
    service.update_pnl(-125)

    restored = RiskService.from_config(cfg, root=tmp_path, account_id="acc_restart")
    assert restored.restore({"trading_day": "2026-11-19"}) == "2026-11-20"
    assert restored.daily_pnl == -125
    assert restored.orders_today == 1
    assert restored.open_orders_today == 1
    assert restored.last_order_ts == 1000
    assert restored.last_open_ts == 1000
    assert restored.current_position == 2
    assert restored.positions == {"bu2612": 2}
    assert restored.halted is True
    assert restored.halt_kind == "daily_loss"

    with closing(connect(tmp_path / "data" / "trading.db")) as conn:
        row = conn.execute(
            "SELECT trading_day, payload_json FROM risk_state WHERE account_id = 'acc_restart'"
        ).fetchone()
    assert row is not None
    assert row["trading_day"] == "2026-11-20"
    assert '"daily_pnl":-125.0' in row["payload_json"]


def test_risk_decisions_are_queryable_in_jsonl_and_sqlite(tmp_path) -> None:
    import json
    from contextlib import closing

    from src.account.trading_db import connect
    from src.kernel.risk_chain import TradeIntent, run_pretrade_gates
    from src.risk.service import RiskService

    service = RiskService.from_config(
        {"risk": {"min_order_interval_sec": 0}, "storage": {"sqlite_enabled": True}},
        root=tmp_path,
        account_id="acc_audit",
    )
    service.trading_day = "2026-11-20"
    blocked = run_pretrade_gates(
        TradeIntent(symbol="bu2612", side="buy_open", volume=1, orders_paused=True),
        service,
    )
    passed = run_pretrade_gates(
        TradeIntent(symbol="bu2612", side="buy_open", volume=1, bar_id="bar-1"),
        service,
    )

    assert blocked.ok is False
    assert passed.ok is True
    rows = service.decisions(trading_day="2026-11-20")
    assert [(row["gate"], row["code"]) for row in rows] == [("early", "order_paused"), ("send", "pass")]
    assert rows[1]["intent"]["bar_id"] == "bar-1"
    assert rows[1]["allowed"] is True

    lines = (tmp_path / "data" / "risk_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["code"] == "order_paused"
    with closing(connect(tmp_path / "data" / "trading.db")) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM risk_decisions WHERE account_id = 'acc_audit'"
        ).fetchone()[0]
    assert count == 2


def test_risk_sqlite_failure_preserves_json_and_trade_result(tmp_path, monkeypatch) -> None:
    from src.risk.service import RiskService
    from src.risk.state_store import JsonRiskStateStore

    service = RiskService.from_config(
        {"risk": {"min_order_interval_sec": 0}, "storage": {"sqlite_enabled": True}},
        root=tmp_path,
        account_id="acc_failure",
    )

    def _broken(*_args, **_kwargs) -> None:
        raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(service.store.sqlite, "save", _broken)
    monkeypatch.setattr(service.store.sqlite, "append_decision", _broken)
    ok, _ = service.allow("buy_open", 2, symbol="bu2612", now=100)
    service.on_order_filled("buy_open", 2, "bu2612", now=100)

    assert ok is True
    saved = JsonRiskStateStore(tmp_path).load("acc_failure")
    assert saved is not None
    assert saved["open_orders_today"] == 1
    assert saved["positions"] == {"bu2612": 2}
    lines = (tmp_path / "data" / "risk_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_risk_sqlite_disabled_keeps_json_authoritative(tmp_path) -> None:
    from src.risk.service import RiskService

    cfg = {"risk": {"min_order_interval_sec": 0}, "storage": {"sqlite_enabled": False}}
    service = RiskService.from_config(cfg, root=tmp_path, account_id="acc_json")
    service.trading_day = "2026-11-20"
    ok, _ = service.allow("buy_open", 1, symbol="bu2612", now=100)
    service.on_order_filled("buy_open", 1, "bu2612", now=100)

    restored = RiskService.from_config(cfg, root=tmp_path, account_id="acc_json")
    restored.restore()
    assert ok is True
    assert restored.open_orders_today == 1
    assert restored.last_order_ts == 100
    assert len(restored.decisions(trading_day="2026-11-20")) == 1
    assert not (tmp_path / "data" / "trading.db").exists()


def test_portfolio_limits_default_to_observe() -> None:
    from src.risk.portfolio import PortfolioLimits

    assert PortfolioLimits.from_config({}).mode == "observe"
    assert PortfolioLimits.from_config({"risk": {"portfolio": {"mode": "enforce"}}}).mode == "enforce"
    assert PortfolioLimits.from_config({"risk": {"portfolio": {"mode": "bogus"}}}).mode == "observe"


def test_portfolio_snapshot_nets_across_accounts() -> None:
    from src.risk.portfolio import PortfolioSnapshot

    snapshot = PortfolioSnapshot.from_rows(
        [
            {"book_id": "acc_a", "symbol": "fu2701", "side": "long", "volume": 2},
            {"book_id": "acc_b", "symbol": "fu2701", "side": "short", "volume": 1},
            {"book_id": "acc_b", "symbol": "bu2612", "side": "long", "volume": 3},
        ]
    )
    assert snapshot.net_lots("fu2701") == 1
    assert snapshot.gross_lots() == 6
    assert snapshot.digest() == snapshot.digest()


def test_portfolio_observe_records_but_does_not_block(tmp_path) -> None:
    from src.kernel.risk_chain import TradeIntent, run_send_gates
    from src.risk.portfolio import PortfolioLimits, PortfolioRisk, PortfolioSnapshot
    from src.risk.service import RiskService

    cfg = {
        "risk": {
            "min_order_interval_sec": 0,
            "portfolio": {"mode": "observe", "max_symbol_lots": 1},
        }
    }
    service = RiskService.from_config(cfg, root=tmp_path, account_id="acc_a")
    service.write_alerts = False
    service.portfolio = PortfolioRisk(
        PortfolioLimits.from_config(cfg),
        snapshot=PortfolioSnapshot.from_rows(
            [{"book_id": "acc_b", "symbol": "fu2701", "side": "long", "volume": 2}]
        ),
    )
    intent = TradeIntent(symbol="fu2701", side="buy_open", volume=1)

    assert run_send_gates(intent, service).ok is True
    gates = [row["gate"] for row in service.decisions()]
    assert "portfolio" in gates
    portfolio_rows = [row for row in service.decisions() if row["gate"] == "portfolio"]
    assert "超过上限" in portfolio_rows[-1]["reason"]


def test_portfolio_enforce_blocks_over_limit(tmp_path) -> None:
    from src.kernel.risk_chain import TradeIntent, run_send_gates
    from src.risk.portfolio import PortfolioLimits, PortfolioRisk, PortfolioSnapshot
    from src.risk.service import RiskService

    cfg = {
        "risk": {
            "min_order_interval_sec": 0,
            "portfolio": {"mode": "enforce", "max_symbol_lots": 2},
        }
    }
    service = RiskService.from_config(cfg, root=tmp_path, account_id="acc_a")
    service.write_alerts = False
    service.portfolio = PortfolioRisk(
        PortfolioLimits.from_config(cfg),
        snapshot=PortfolioSnapshot.from_rows(
            [{"book_id": "acc_b", "symbol": "fu2701", "side": "long", "volume": 2}]
        ),
    )
    result = run_send_gates(TradeIntent(symbol="fu2701", side="buy_open", volume=1), service)

    assert result.ok is False
    assert result.code == "portfolio_symbol_limit"


def test_portfolio_limits_read_heat_and_ttl() -> None:
    from src.risk.portfolio import PortfolioLimits

    limits = PortfolioLimits.from_config(
        {
            "risk": {
                "portfolio": {
                    "mode": "enforce",
                    "max_heat_pct": 6,
                    "pending_ttl_sec": 15,
                }
            }
        }
    )
    assert limits.max_heat_pct == 6.0
    assert limits.pending_ttl_sec == 15.0
    # 总手数敞口已被风险热度替换，旧键不再参与判断。
    assert not hasattr(limits, "max_gross_lots")


def _heat_cfg(mode: str = "enforce", max_heat_pct: float = 10) -> dict:
    return {
        "risk": {
            "capital": 100_000,
            "risk_per_trade_pct": 1,
            "portfolio": {"mode": mode, "max_heat_pct": max_heat_pct, "pending_ttl_sec": 60},
        }
    }


def test_portfolio_heat_reserves_pending_open_and_release() -> None:
    """第一笔通过后先占热度，第二笔同轮会被在途预留挡住；释放后恢复。"""
    from src.risk.portfolio import PortfolioLimits, PortfolioRisk, PortfolioSnapshot

    cfg = _heat_cfg()
    now = [1000.0]
    risk = PortfolioRisk(
        PortfolioLimits.from_config(cfg),
        snapshot=PortfolioSnapshot.from_rows([]),
        cfg=cfg,
        clock=lambda: now[0],
    )
    first = risk.check(
        symbol="bu2612", side="buy_open", volume=10, entry_price=4000, stop_price=3900
    )
    second = risk.check(
        symbol="fu2701", side="buy_open", volume=10, entry_price=4000, stop_price=3900
    )

    assert first.blocked is False
    assert first.risk_cash == 10_000
    assert second.blocked is True
    assert second.code == "portfolio_heat_limit"

    risk.release("bu2612", "buy_open")
    again = risk.check(
        symbol="fu2701", side="buy_open", volume=10, entry_price=4000, stop_price=3900
    )
    assert again.blocked is False


def test_portfolio_heat_observe_records_without_blocking() -> None:
    from src.risk.portfolio import PortfolioLimits, PortfolioRisk, PortfolioSnapshot

    cfg = _heat_cfg(mode="observe")
    risk = PortfolioRisk(
        PortfolioLimits.from_config(cfg),
        snapshot=PortfolioSnapshot.from_rows(
            [{"book_id": "acc_b", "symbol": "fu2701", "side": "long", "volume": 10,
              "entry_price": 4000, "stop_price": 3900}]
        ),
        cfg=cfg,
    )
    decision = risk.check(
        symbol="bu2612", side="buy_open", volume=10, entry_price=4000, stop_price=3900
    )

    assert decision.blocked is True
    assert decision.enforced is False
    assert decision.code == "portfolio_heat_limit"


def test_portfolio_read_failure_blocks_open_but_allows_close(tmp_path) -> None:
    """持仓读不到时开仓失败关闭；平仓不读库，照常放行。"""
    from src.risk.portfolio import PortfolioLimits, PortfolioRisk

    cfg = {
        "risk": {
            "capital": 100_000,
            "portfolio": {"mode": "enforce", "max_symbol_lots": 2},
        }
    }
    risk = PortfolioRisk(PortfolioLimits.from_config(cfg), root=tmp_path, cfg=cfg)

    opened = risk.check(symbol="bu2612", side="buy_open", volume=1)
    closed = risk.check(symbol="bu2612", side="close", volume=1)

    assert opened.blocked is True
    assert opened.code == "portfolio_unavailable"
    assert closed.blocked is False
    assert closed.code == "portfolio_skip"


def test_risk_service_blocks_open_on_portfolio_error(tmp_path) -> None:
    from src.kernel.risk_chain import TradeIntent, run_send_gates
    from src.risk.portfolio import PortfolioLimits, PortfolioRisk
    from src.risk.service import RiskService

    cfg = {
        "risk": {
            "min_order_interval_sec": 0,
            "capital": 100_000,
            "portfolio": {"mode": "enforce", "max_symbol_lots": 2},
        }
    }
    service = RiskService.from_config(cfg, root=tmp_path, account_id="acc_a")
    service.write_alerts = False
    service.portfolio = PortfolioRisk(
        PortfolioLimits.from_config(cfg), root=tmp_path / "missing", cfg=cfg
    )
    service.on_order_filled("buy_open", 1, "bu2612", now=0)

    result = run_send_gates(TradeIntent(symbol="bu2612", side="buy_open", volume=1), service)
    close_result = run_send_gates(
        TradeIntent(symbol="bu2612", side="close", volume=1), service
    )

    assert result.ok is False
    assert result.code == "portfolio_unavailable"
    assert close_result.ok is True
