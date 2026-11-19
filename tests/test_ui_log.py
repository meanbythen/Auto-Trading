"""运行窗口日志过滤：明细进文件，窗口只留跟交易有关的。"""

from types import SimpleNamespace

from src.utils.ui_log import ui_sink_filter


def _record(*, name: str, message: str, level_no: int = 20, ui=None) -> dict:
    extra = {} if ui is None else {"ui": ui}
    return {
        "name": name,
        "message": message,
        "level": SimpleNamespace(no=level_no),
        "extra": extra,
    }


def test_warnings_always_show() -> None:
    rec = _record(
        name="src.data.kline",
        message="拉取失败",
        level_no=30,
    )
    assert ui_sink_filter(rec) is True


def test_kline_info_hidden() -> None:
    rec = _record(
        name="src.data.kline",
        message="拉取K线 symbol=fu2701 period=15分钟",
    )
    assert ui_sink_filter(rec) is False


def test_reconcile_ok_hidden_mismatch_shown() -> None:
    ok = _record(
        name="src.trader.ctp_trader",
        message="柜台对账通过: 持仓一致。可用资金 32743.76",
    )
    assert ui_sink_filter(ok) is False
    bad = _record(
        name="src.trader.ctp_trader",
        message="柜台持仓不一致: 差 1 手",
        level_no=30,
    )
    assert ui_sink_filter(bad) is True


def test_broker_query_info_hidden() -> None:
    rec = _record(
        name="src.trader.ctp_trader",
        message="柜台持仓查询：柜台持仓：lh2701 卖 1 手。可用资金 13878.21",
    )
    assert ui_sink_filter(rec) is False


def test_order_info_shown() -> None:
    rec = _record(
        name="src.trader.ctp_td",
        message="已向柜台申报 fu2701 1 手",
    )
    assert ui_sink_filter(rec) is True


def test_bind_ui_flag() -> None:
    assert ui_sink_filter(_record(name="src.strategy.explain", message="观望", ui=False)) is False
    assert ui_sink_filter(_record(name="src.strategy.explain", message="符合开仓", ui=True)) is True


def test_scan_brief_only_flags_ui_when_text_changes(monkeypatch) -> None:
    from src.strategy import explain

    monkeypatch.setattr(explain, "_last_brief", {})
    seen: list[bool] = []

    class _Catch:
        def bind(self, **kwargs):
            seen.append(bool(kwargs.get("ui")))
            return self

        def info(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(explain, "logger", _Catch())
    explain._emit_scan_brief("fu2701", "观望")
    explain._emit_scan_brief("fu2701", "观望")
    explain._emit_scan_brief("fu2701", "做多")
    assert seen == [True, False, True]
