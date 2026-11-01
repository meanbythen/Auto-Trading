"""顶部持仓卡的文字：只描述程序自己记下的仓。"""

from src.utils.state_store import format_position_text


def test_flat_book_says_empty() -> None:
    assert format_position_text({}) == "空仓"
    assert format_position_text({"bu2612": {"side": "flat", "volume": 0}}) == "空仓"


def test_open_position_shows_side_price_and_stop() -> None:
    text = format_position_text(
        {
            "bu2612": {
                "side": "short",
                "volume": 1,
                "entry_price": 4662,
                "trail_stop": 4700,
            }
        }
    )
    assert text == "bu2612 空1手 @4662 止损4700"
