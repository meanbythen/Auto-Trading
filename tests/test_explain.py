"""扫描解说：持仓状态按真实持仓判断，不按原因里的字眼猜。"""

from src.strategy.explain import _entry_verdict, _is_holding
from src.strategy.types import StrategyAction, StrategyDecision


class _Pos:
    def __init__(self, side: str, volume: int) -> None:
        self.side = side
        self.volume = volume


class _Strategy:
    def __init__(self, position: _Pos | None) -> None:
        self.positions = {"bu2612": position} if position is not None else {}


def test_flat_hold_with_stop_word_is_watching() -> None:
    """空仓时原因里出现「止损」两个字，也只是观望，不能显示成已有持仓。"""
    decision = StrategyDecision(
        StrategyAction.HOLD, "bu2612", "跳空太大，箱沿止损会被一次打穿，这段不追"
    )
    assert _is_holding(_Strategy(None), "bu2612") is False
    verdict = _entry_verdict(decision, False)
    assert "已有持仓" not in verdict
    assert "观望" in verdict


def test_zero_volume_is_not_holding() -> None:
    """持仓记录还在但手数为 0，算空仓。"""
    assert _is_holding(_Strategy(_Pos("long", 0)), "bu2612") is False


def test_open_position_is_reported_as_holding() -> None:
    """真持仓时，HOLD 才显示已有持仓。"""
    decision = StrategyDecision(StrategyAction.HOLD, "bu2612", "继续持有")
    assert _is_holding(_Strategy(_Pos("long", 2)), "bu2612") is True
    assert "已有持仓" in _entry_verdict(decision, True)
