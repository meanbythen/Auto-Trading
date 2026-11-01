"""启动时把价格设成对手价：已经是对手价就不再点，菜单里要点下方那一项。"""

from __future__ import annotations

from src.trader.price_mode import (
    PriceBox,
    choose_shown_price,
    ensure_opponent_price,
    pick_price_menu_item,
    format_limit_price,
    price_mode_blocks_order,
    price_text_matches,
    price_was_overwritten,
    text_is_opponent,
)


def _box(text: str) -> PriceBox:
    return PriceBox(left=600, top=1100, right=760, bottom=1140, text=text, anchor_x=656, anchor_y=1120)


class _FakePanel:
    """代替同花顺窗口，记录程序有没有去点菜单。"""

    def __init__(self, texts: list[str], items: list[tuple[str, tuple[int, int, int, int]]]) -> None:
        self.texts = list(texts)
        self.items = items
        self.opened = 0
        self.nudged = 0
        self.clicked: list[tuple[int, int, int, int]] = []
        self.closed = 0

    def read_box(self) -> PriceBox:
        return _box(self.texts.pop(0) if self.texts else "")

    def open_dropdown(self, box: PriceBox) -> None:
        self.opened += 1

    def nudge_dropdown(self, box: PriceBox) -> None:
        self.nudged += 1

    def list_menu_items(self) -> list[tuple[str, tuple[int, int, int, int]]]:
        if self.nudged and not self.items:
            self.items = [("对手价", (700, 1160, 780, 1190))]
        return list(self.items)

    def click_rect(self, rect: tuple[int, int, int, int]) -> None:
        self.clicked.append(rect)
        if self.texts:
            return
        self.texts.append("对手价")

    def close_menu(self) -> None:
        self.closed += 1


def test_typed_lot_replaces_opponent_price() -> None:
    """全选后打出的 1 进了价格框，就当成填报错误。价格仍是对手价则不算。"""
    assert price_was_overwritten("对手价", "1", "1")
    assert price_was_overwritten("对手价", "4658", "1")
    assert not price_was_overwritten("对手价", "对手价", "1")
    assert not price_was_overwritten(None, None, "1")


def test_value_latest_price_is_not_hidden_by_the_name() -> None:
    """框里是最新价时，控件名字里的对手价不能当成已经设好。"""
    assert choose_shown_price(["最新价"], ["对手价"]) == "最新价"
    assert choose_shown_price([], ["对手价"]) == "对手价"
    assert choose_shown_price(["对手价"], ["价格"]) == "对手价"
    assert price_mode_blocks_order("最新价") == "最新价"
    assert price_mode_blocks_order("对手价") == ""
    assert price_mode_blocks_order(None) == ""


def test_limit_price_text_matches_what_was_typed() -> None:
    assert format_limit_price(4000) == "4000"
    assert format_limit_price(4000.5) == "4000.5"
    assert price_text_matches("4000.00", "4000")
    assert not price_text_matches("对手价", "4000")
    assert not price_text_matches(None, "4000")


def test_latest_price_is_not_treated_as_opponent() -> None:
    assert text_is_opponent("对手价")
    assert text_is_opponent(" 对手价 ")
    assert text_is_opponent("对手")
    assert not text_is_opponent("最新价")
    assert not text_is_opponent("4532")


def test_menu_picks_the_item_under_the_price_box() -> None:
    box = _box("最新价")
    hidden = ("对手价", (10, 20, 80, 40))
    real = ("对手价", (690, 1168, 770, 1200))
    other = ("最新价", (800, 1168, 880, 1200))
    assert pick_price_menu_item([hidden, other, real], box) == real[1]


def test_already_opponent_does_not_open_the_menu() -> None:
    panel = _FakePanel(["对手价"], [])
    ok, msg = ensure_opponent_price({"trade": {"gui": {"price_input": [656, 1120]}}}, panel)
    assert ok
    assert "已经是对手价" in msg
    assert panel.opened == 0
    assert panel.clicked == []


def test_switches_latest_price_to_opponent() -> None:
    panel = _FakePanel(
        ["最新价", "对手价"],
        [("对手价", (690, 1168, 770, 1200))],
    )
    ok, msg = ensure_opponent_price({"trade": {"gui": {"price_input": [656, 1120]}}}, panel)
    assert ok
    assert panel.opened == 1
    assert panel.clicked == [(690, 1168, 770, 1200)]
    assert panel.closed == 1
    assert "不再修改价格" in msg


def test_missing_menu_item_closes_the_popup() -> None:
    panel = _FakePanel(["最新价"], [("排队价", (600, 1168, 680, 1200))])
    ok, msg = ensure_opponent_price({"trade": {"gui": {"price_input": [656, 1120]}}}, panel)
    assert not ok
    assert panel.nudged == 1
    assert panel.closed == 1
    assert "下一笔" in msg
    assert panel.clicked == []


def test_order_uses_opponent_without_opening_menu() -> None:
    """框里已经是对手价时，下单不再去点下拉。"""
    from src.trader.price_mode import confirm_opponent_before_click

    def fail_setter():
        raise AssertionError("不应该再去点价格菜单")

    ok, msg = confirm_opponent_before_click(lambda: "对手价", fail_setter)
    assert ok
    assert "直接下单" in msg


def test_order_retries_when_startup_missed_the_menu() -> None:
    """启动时没点到菜单，下单前发现还是最新价，就再选一次。选好了这一笔可以点。"""
    from src.trader.price_mode import confirm_opponent_before_click

    calls = {"n": 0}

    def setter():
        calls["n"] += 1
        return True, "已把同花顺价格设为对手价。之后开仓和平仓都不再修改价格。"

    ok, msg = confirm_opponent_before_click(lambda: "最新价", setter)
    assert ok
    assert calls["n"] == 1
    assert "对手价" in msg
