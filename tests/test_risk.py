"""
风控单元测试
功能：验证「过频、无仓平仓、日亏损熔断」会被正确拦截。
原理：不启动软件，直接调用 RiskGuardian，适合初学者理解风控逻辑。
"""

from src.risk.guardian import RiskGuardian


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
