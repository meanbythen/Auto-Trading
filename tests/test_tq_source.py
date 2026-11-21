"""天勤行情：合约转换、周期、K 线时间口径、存储分开。不联网、不登录。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.data.kline import KlineProvider
from src.data.tq_source import (
    _clear_history_short,
    _filled_close_count,
    config_symbol_from_tq,
    fetch_frame,
    fetch_frames,
    frame_from_klines,
    main_contract,
    period_seconds,
    tq_symbol,
)


def _raw(*starts: str, close: float = 4700.0) -> pd.DataFrame:
    """按 tqsdk 的样子造 K 线：datetime 是 UTC 起点时间，单位纳秒。"""
    # pandas 3 默认用微秒，这里显式换成纳秒，和 tqsdk 一致。
    stamps = pd.to_datetime(list(starts)).astype("datetime64[ns]").asi8
    return pd.DataFrame(
        {
            "datetime": stamps,
            "open": [close] * len(starts),
            "high": [close + 3] * len(starts),
            "low": [close - 3] * len(starts),
            "close": [close] * len(starts),
            "volume": [100] * len(starts),
        }
    )


def test_tq_symbol_adds_exchange() -> None:
    """配置里只写 fu2701，天勤要的是 SHFE.fu2701。"""
    assert tq_symbol("fu2701") == "SHFE.fu2701"
    assert tq_symbol("c2701") == "DCE.c2701"
    assert tq_symbol("SHFE.fu2701") == "SHFE.fu2701"


def test_tq_symbol_czce_keeps_three_digits() -> None:
    """郑商所只认三位年月，配置里的 SR2701 要转成 SR701。"""
    assert tq_symbol("SR2701") == "CZCE.SR701"
    assert tq_symbol("CF2701") == "CZCE.CF701"


def test_tq_symbol_product_only_is_main_continuous() -> None:
    """只写品种时用主连，开仓仍看具体合约。"""
    assert tq_symbol("bu") == "KQ.m@SHFE.bu"
    assert tq_symbol("sr") == "KQ.m@CZCE.SR"


def test_tq_symbol_unknown_product_says_so() -> None:
    """不认识的品种直接说清楚，不猜交易所。"""
    with pytest.raises(ValueError, match="交易所"):
        tq_symbol("xx2701")


def test_config_symbol_from_tq_keeps_config_style() -> None:
    """天勤的交易所写法换回配置写法；郑商所三位年月补成四位。"""
    assert config_symbol_from_tq("SHFE.fu2701") == "fu2701"
    assert config_symbol_from_tq("DCE.c2701") == "c2701"
    assert config_symbol_from_tq("GFEX.si2601") == "si2601"
    assert config_symbol_from_tq("CZCE.TA701", today=date(2026, 11, 1)) == "TA2701"


class _FakeMainApi:
    """只实现查主连标的要用到的两个接口。"""

    def __init__(self, found: list[str] | None = None, underlying: str = "") -> None:
        self.found = list(found or [])
        self.underlying = underlying
        self.quote_calls: list[str] = []

    def query_cont_quotes(self, exchange_id: str | None = None, product_id: str | None = None):
        return list(self.found)

    def get_quote(self, symbol: str) -> object:
        self.quote_calls.append(symbol)
        return type("Quote", (), {"underlying_symbol": self.underlying})()

    def wait_update(self, deadline: float | None = None) -> None:
        return None


def test_tq_symbol_covers_newer_products() -> None:
    """新上市品种也要认得出交易所：瓶片/丙烯、纯苯/原木、多晶硅、铂钯等。"""
    assert tq_symbol("PR2701") == "CZCE.PR701"
    assert tq_symbol("PL2701") == "CZCE.PL701"
    assert tq_symbol("BZ2701") == "DCE.bz2701"
    assert tq_symbol("LG2701") == "DCE.lg2701"
    assert tq_symbol("PS2701") == "GFEX.ps2701"
    assert tq_symbol("PT2701") == "GFEX.pt2701"
    assert tq_symbol("AD2701") == "SHFE.ad2701"
    assert tq_symbol("OP2701") == "SHFE.op2701"


def test_main_contract_uses_query_cont_quotes() -> None:
    """主连标的有现成的查询接口时，直接用它的结果。"""
    api = _FakeMainApi(["SHFE.fu2701"])
    assert main_contract("fu2701", api=api) == "fu2701"


def test_main_contract_falls_back_to_cont_quote() -> None:
    """查询接口没结果时，退回到主连行情的标的合约。"""
    api = _FakeMainApi([], underlying="SHFE.fu2701")
    assert main_contract("fu2701", api=api) == "fu2701"
    assert api.quote_calls == ["KQ.m@SHFE.fu"]


def test_main_contract_rejects_unknown_product() -> None:
    with pytest.raises(ValueError, match="交易所"):
        main_contract("xx2701", api=_FakeMainApi())


def test_period_seconds() -> None:
    """分钟、小时、日线换成秒；周线不能直接要一周秒数。"""
    assert period_seconds("1") == 60
    assert period_seconds("5") == 300
    assert period_seconds("15分钟") == 900
    assert period_seconds("120") == 7200
    assert period_seconds("D") == 86400
    with pytest.raises(ValueError, match="周线"):
        period_seconds("W")


def test_frame_turns_utc_start_time_into_beijing_right_label() -> None:
    """tqsdk 给 UTC 起点 01:00 / 01:05，北京时间是 09:00 / 09:05，右标签是 09:05 / 09:10。"""
    frame = frame_from_klines(_raw("2026-10-30 01:00", "2026-10-30 01:05"), 300)
    assert list(frame.index) == [
        pd.Timestamp("2026-10-30 09:05"),
        pd.Timestamp("2026-10-30 09:10"),
    ]
    assert frame.index.name == "datetime"
    assert float(frame.iloc[0]["open"]) == 4700.0


def test_frame_keeps_the_unfinished_last_bar() -> None:
    """最后一根可能还在走。照样留着，交给上层：持仓看影线，空仓和回测只认收盘。"""
    frame = frame_from_klines(_raw("2026-10-30 01:00", "2026-10-30 01:05"), 300)
    assert len(frame) == 2
    assert frame.index[-1] == pd.Timestamp("2026-10-30 09:10")


def test_frame_without_bars_says_so() -> None:
    """天勤一根都没给时，报中文错，不返回空表让后面误开仓。"""
    with pytest.raises(RuntimeError, match="没有返回"):
        frame_from_klines(_raw(), 300)


def test_fetch_frame_uses_the_fake_client() -> None:
    """假客户端只验参数：合约转对、周期是秒、拿够根数。"""
    starts = [f"2026-10-30 01:{m:02d}" for m in range(10)]
    fake = _FakeApi(_raw(*starts))
    frame = fetch_frame("fu2701", "5", limit=10, api=fake)
    assert fake.calls == [("SHFE.fu2701", 300, 10)]
    assert len(frame) == 10


def test_fetch_frames_pulls_each_contract_and_reuses_serial() -> None:
    """多合约一次拉取：各自一根序列，共用等待；第二轮不再新建序列。"""
    starts = [f"2026-10-30 01:{m:02d}" for m in range(10)]

    class _MultiApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, int]] = []
            self.waits = 0
            self.raw = {}
            for code in ("SHFE.fu2701", "DCE.c2701"):
                frame = _raw(*starts)
                frame["close"] = float("nan")
                self.raw[code] = frame

        def get_kline_serial(self, code: str, seconds: int, data_length: int = 200) -> pd.DataFrame:
            self.calls.append((code, seconds, data_length))
            return self.raw[code]

        def wait_update(self, deadline: float | None = None) -> None:
            self.waits += 1
            for frame in self.raw.values():
                frame.loc[frame.index[:10], "close"] = 4700.0

    _clear_history_short()
    fake = _MultiApi()
    frames = fetch_frames(["fu2701", "c2701"], "5", limit=10, api=fake)
    assert sorted(frames) == ["c2701", "fu2701"]
    assert all(len(frame) == 10 for frame in frames.values())
    assert fake.calls == [("SHFE.fu2701", 300, 10), ("DCE.c2701", 300, 10)]
    assert fake.waits == 1

    again = fetch_frames(["fu2701", "c2701"], "5", limit=10, api=fake)
    assert len(again) == 2
    assert len(fake.calls) == 2  # 序列复用，没有重复创建


def test_fetch_frame_needs_account() -> None:
    """没填账号密码时不要偷偷去登录。"""
    with pytest.raises(RuntimeError, match="账号和密码"):
        fetch_frame("fu2701", "5", user="", password="")


def test_fetch_frame_drops_a_dead_connection(monkeypatch) -> None:
    """连接断了就丢掉旧连接，下一轮重新登录，不拿死连接一直重试。"""
    from src.data import tq_source

    calls: list[int] = []

    class _BrokenApi:
        def get_kline_serial(self, *_args, **_kwargs):
            raise ConnectionError("connection aborted")

    monkeypatch.setattr(tq_source, "_get_api", lambda _u, _p: _BrokenApi())
    monkeypatch.setattr(tq_source, "reset_api", lambda: calls.append(1))
    with pytest.raises(ConnectionError):
        fetch_frame("fu2701", "5", user="u", password="p")
    assert calls == [1]


def test_provider_writes_tq_bars_to_their_own_name(tmp_path) -> None:
    """选天勤时只写 fu2701@tq，新浪存下的 fu2701 原样不动。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "tq", "cache_dir": "klines", "min_bars": 1, "live_max_bars": 10},
        }
    )
    fresh = pd.DataFrame(
        [{"open": 4700, "high": 4703, "low": 4697, "close": 4700, "volume": 100}],
        index=pd.to_datetime(["2026-10-30 09:05"]),
    )
    provider._fetch_tq = lambda *_args, **_kwargs: fresh  # type: ignore[method-assign]
    provider._fetch_akshare = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("天勤不回头问新浪")
    )  # type: ignore[method-assign]
    provider._rebuild_with_minutes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("天勤的时间已经是右标签，不该重拼")
    )  # type: ignore[method-assign]

    got = provider.fetch("fu2701", period="5", max_bars=10)
    assert float(got.iloc[-1]["close"]) == 4700.0
    assert provider.store.load("fu2701@tq", "5") is not None
    assert provider.store.load("fu2701", "5") is None


def test_frame_daily_is_trading_day_midnight_not_plus_one_day() -> None:
    """
    日线 datetime 是交易日 00:00（北京）。

    UTC 10-29 16:00 换北京是 10-30 00:00。如果再加 86400 秒会错成 10-31，多因子日线会对不上。
    """
    frame = frame_from_klines(_raw("2026-10-29 16:00", "2026-10-30 16:00"), 86400)
    assert list(frame.index) == [
        pd.Timestamp("2026-10-30 00:00"),
        pd.Timestamp("2026-10-31 00:00"),
    ]


def test_fetch_frame_asks_86400_for_daily() -> None:
    """日线问天勤 86400 秒，不换一家。"""
    fake = _FakeApi(_raw("2026-10-29 16:00", "2026-10-30 16:00"))
    frame = fetch_frame("fu2701", "D", limit=2, api=fake)
    assert fake.calls == [("SHFE.fu2701", 86400, 10)]
    assert frame.index[0] == pd.Timestamp("2026-10-30 00:00")


def test_fetch_frame_weekly_aggregates_daily_not_604800() -> None:
    """周线不直接要 604800，只用日线再收到周五。"""
    days = pd.bdate_range("2026-10-12", "2026-10-30")
    starts = [(pd.Timestamp(d) - pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M") for d in days]
    fake = _FakeApi(_raw(*starts, close=2800.0))
    frame = fetch_frame("fu2701", "W", limit=20, api=fake)
    assert fake.calls == [("SHFE.fu2701", 86400, 80)]
    assert all(pd.Timestamp(ts).weekday() == 4 for ts in frame.index)
    assert len(frame) >= 2


def test_provider_fetches_daily_from_tq(tmp_path) -> None:
    """选天勤时日线走天勤，写进 fu2701@tq，不回头问新浪。"""
    provider = KlineProvider(
        {"_project_root": str(tmp_path), "data": {"source": "tq", "cache_dir": "klines", "min_bars": 1}}
    )
    daily = pd.DataFrame(
        [{"open": 2700, "high": 2710, "low": 2690, "close": 2705, "volume": 10}],
        index=pd.to_datetime(["2026-10-30 00:00"]),
    )
    provider._fetch_tq = lambda *_args, **_kwargs: daily  # type: ignore[method-assign]
    provider._fetch_akshare = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("天勤日线不回头问新浪")
    )  # type: ignore[method-assign]
    got = provider.fetch("fu2701", period="D", max_bars=10)
    assert float(got.iloc[-1]["close"]) == 2705.0
    assert provider.store.load("fu2701@tq", "D") is not None


def test_latest_price_asks_tianqin_not_sina(tmp_path) -> None:
    """选天勤时，开仓滑点用的现价也必须问天勤，不能回头拉新浪。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "tq", "cache_dir": "klines"},
        }
    )
    asked: list[tuple[str, str]] = []

    def _fake_tq(symbol: str, period: str, *, limit: int = 200):
        asked.append((symbol, period))
        return pd.DataFrame(
            [{"open": 1, "high": 1, "low": 1, "close": 4712, "volume": 1}],
            index=pd.to_datetime(["2026-11-06 21:01"]),
        )

    provider._fetch_tq = _fake_tq  # type: ignore[method-assign]
    provider._recent_minutes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("选天勤时 latest_price 不该去新浪")
    )  # type: ignore[method-assign]
    assert provider.latest_price("fu2701") == 4712.0
    assert asked == [("fu2701", "1")]


def test_provider_reads_tq_account_from_config(tmp_path) -> None:
    """账号写在 data.tq.user，密码写在 data.tq.password。"""
    provider = KlineProvider(
        {
            "_project_root": str(tmp_path),
            "data": {"source": "tq", "tq": {"user": "u1", "password": "p1"}},
        }
    )
    assert provider.tq_user == "u1"
    assert provider.tq_password == "p1"


class _FakeApi:
    """够用的假 TqApi：记下参数，返回造好的 K 线。"""

    def __init__(self, raw: pd.DataFrame) -> None:
        self.raw = raw
        self.calls: list[tuple[str, int, int]] = []

    def get_kline_serial(self, symbol: str, seconds: int, data_length: int = 200) -> pd.DataFrame:
        self.calls.append((symbol, seconds, data_length))
        return self.raw

    def wait_update(self, deadline: float | None = None) -> None:
        return None


def test_filled_close_count_reads_plain_and_nested() -> None:
    """平表、嵌套结构都能数出 close 填了几行；读不出结构就返回 None。"""
    raw = _raw("2026-10-30 01:00", "2026-10-30 01:05")
    assert _filled_close_count(raw) == 2
    nested = {"klines": {"SHFE.fu2701": {"300": raw}}}
    assert _filled_close_count(nested) == 2
    assert _filled_close_count(object()) is None


def test_fetch_frame_waits_until_history_is_filled() -> None:
    """服务端分批下发时，循环等 close 列填满到请求根数再转换，不拿半截数据。"""
    starts = [f"2026-10-30 01:{m:02d}" for m in range(10)]
    raw = _raw(*starts)
    raw["close"] = float("nan")

    class _SlowApi:
        """每次 wait_update 只多填 4 行，模拟分批下发。"""

        def __init__(self) -> None:
            self.waits = 0

        def get_kline_serial(self, symbol: str, seconds: int, data_length: int = 200) -> pd.DataFrame:
            return raw

        def wait_update(self, deadline: float | None = None) -> None:
            self.waits += 1
            upto = min(len(raw), self.waits * 4)
            raw.loc[raw.index[:upto], "close"] = 4700.0

    _clear_history_short()
    fake = _SlowApi()
    frame = fetch_frame("fu2701", "5", limit=10, api=fake)
    assert fake.waits == 3  # 0 → 4 → 8 → 10 填满就停，不死等
    assert len(frame) == 10


def test_short_history_warns_once_and_does_not_wait_again(monkeypatch) -> None:
    """合约历史不够时：close 不再增加就停；同一条告警只写一次，下一轮不再空等。"""
    from src.data import tq_source

    _clear_history_short()
    raw = _raw(*[f"2026-10-30 01:{m:02d}" for m in range(5)])
    warnings: list[str] = []
    real_warning = tq_source.logger.warning

    def _capture_warning(message, *args, **kwargs):
        warnings.append(str(message).format(*args) if args else str(message))
        return real_warning(message, *args, **kwargs)

    monkeypatch.setattr(tq_source.logger, "warning", _capture_warning)

    class _ShortApi:
        def __init__(self) -> None:
            self.waits = 0

        def get_kline_serial(self, symbol: str, seconds: int, data_length: int = 200) -> pd.DataFrame:
            return raw

        def wait_update(self, deadline: float | None = None) -> None:
            self.waits += 1

    fake = _ShortApi()
    first = fetch_frame("fu2701", "5", limit=10, api=fake)
    second = fetch_frame("fu2701", "5", limit=10, api=fake)
    short_warnings = [text for text in warnings if "只填满" in text]
    assert len(first) == 5
    assert len(second) == 5
    assert fake.waits == 3
    assert len(short_warnings) == 1
    assert "5/10" in short_warnings[0]
