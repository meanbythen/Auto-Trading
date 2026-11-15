"""
K线数据获取
功能：拉取沥青(bu)、玉米(c) 的 5/15 分钟K线，供策略计算 Pinbar/SAR。
原理：优先用 akshare 新浪分钟线；失败则读本地 CSV / 生成演示数据，保证程序能跑通。

注意：你未开通程序化时，这里的数据只用于「算信号」；真正下单仍走同花顺 GUI 或模拟。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from loguru import logger

from src.data.bar_store import (
    BarStore,
    RawBarStore,
    aligned_bars,
    merge_local_and_download,
    rows_needing_write,
)
from src.data.periods import (
    bars_from_minutes,
    cache_tag,
    canonical_period,
    csv_skip_marker,
    drop_forming_bar,
    fix_gap_spanning_bars,
    minute_base,
    overlay_minute_bars,
    period_label,
    resample_ohlc,
    stale_after_minutes,
)
from src.data.tq_source import session_open_minutes
from src.utils import load_secrets

# 实盘扫描和回测会同时来拉同一份行情。排队拉，避免两路请求缠在一起后其中一路永远不返回。
_FETCH_LOCK = threading.Lock()
# 连接最多等 5 秒，读数据最多等 20 秒。到点就失败，回测改用本地缓存，不再停在「请稍候」。
_HTTP_TIMEOUT = (5, 20)


def bars_without_attrs(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """
    拼两张 K 线表之前，先清掉表上的备注。

    用 1 分钟拼 5 分钟时，备注里会记下「改过哪些时间」。那是一整段时间，不是一个是/否。
    pandas 拼表时会拿两段时间去比相不相等，白银夜盘这种很长的一段就会报
    「array is ambiguous」，历史回测直接失败。
    """
    if frame is None:
        return None
    out = frame
    extra = getattr(out, "attrs", None)
    if extra:
        out = out.copy()
        out.attrs = {}
    return out


def rebuilt_stamp_list(raw: Any) -> list:
    """把「1 分钟改过哪些时间」收成普通时间列表，不要留成一整段时间索引。"""
    if raw is None:
        return []
    try:
        return [pd.Timestamp(ts) for ts in raw]
    except (TypeError, ValueError):
        return []


def bars_age_minutes(df: pd.DataFrame | None) -> float | None:
    """最后一根K线距离现在多少分钟；无法判断则返回 None。"""
    if df is None or len(df) == 0:
        return None
    try:
        last = pd.Timestamp(df.index[-1]).to_pydatetime()
        if last.tzinfo is not None:
            last = last.replace(tzinfo=None)
        return max(0.0, (datetime.now() - last).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001
        return None


def is_bars_stale(df: pd.DataFrame | None, max_age_minutes: float) -> bool:
    """K线是否过期（用于禁止用陈旧缓存开仓）。"""
    age = bars_age_minutes(df)
    if age is None:
        return True
    return age > float(max_age_minutes)


# 品种主连在新浪接口里常见写法（akshare futures_zh_minute_sina）
# 沥青=BU0，玉米=C0；也可直接填具体合约如 bu2505
DEFAULT_SYMBOL_MAP = {
    "bu": "BU0",
    "沥青": "BU0",
    "c": "C0",
    "玉米": "C0",
}


class KlineProvider:
    """统一的 K 线提供者。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        root = Path(cfg.get("_project_root", "."))
        self.root_dir = root
        data_cfg = cfg.get("data", {})
        self.cache_dir = root / data_cfg.get("cache_dir", "data/klines")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 历次下载都追加进这个库。新浪窗口往前滚动后，更早的K线仍留在这里。
        self.store = BarStore(self.cache_dir / "market.sqlite")
        # 接口原样另存一份，清洗后的仍在 market.sqlite。出了问题能对照网上原来长什么样。
        self.raw_store = RawBarStore(self.cache_dir / "raw.sqlite")
        from src.data.quote_check import normalize_source

        # akshare 是新浪。eastmoney 是东方财富。tq 是天勤。ths 是同花顺图（只给回测）。
        self.source = normalize_source(data_cfg.get("source", "akshare"))
        self._cfg = cfg
        # 天勤要账号密码。账号写在配置里，密码优先环境变量，其次本地密钥文件。
        tq_cfg = data_cfg.get("tq") or {}
        secrets = load_secrets()
        self.tq_user = str(tq_cfg.get("user") or secrets.get("tq_user") or os.environ.get("TQ_USER") or "")
        self.tq_password = str(
            tq_cfg.get("password")
            or os.environ.get("TQ_PASSWORD")
            or secrets.get("tq_password")
            or ""
        )
        self.symbol_map = {**DEFAULT_SYMBOL_MAP, **(data_cfg.get("symbol_map") or {})}
        self.period = str(cfg.get("strategy", {}).get("timeframe", "5"))
        self.min_bars = int(data_cfg.get("min_bars", 80))
        # 实盘扫描默认只要较近一段；回测应另传更大的 max_bars，否则成交样本会极少
        self.live_max_bars = int(data_cfg.get("live_max_bars", 200))
        # 缓存K线过期阈值（分钟）：超时仍可平仓，但应禁止新开仓
        self.max_stale_minutes = float(data_cfg.get("max_stale_minutes", 90))
        # 同一次扫描里，5 分钟和 15 分钟都要用 1 分钟来拼，短时间只下载一次。
        self._minute_cache: tuple[str, float, pd.DataFrame] | None = None
        # 每个合约/周期保留最近一次下载或失败证据；成功时同一份也挂在 bars.attrs 上。
        self._market_health: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _market_health_key(symbol: str, period: str) -> str:
        return f"{symbol.strip().lower()}|{canonical_period(period)}"

    def get_market_health(self, symbol: str, period: str) -> dict[str, Any]:
        """取最近一次行情健康记录；断流没有 K 线可挂 attrs 时也能查到。"""
        return dict(self._market_health.get(self._market_health_key(symbol, period), {}))

    def fetch(
        self,
        symbol: str,
        period: str | None = None,
        *,
        max_bars: int | None = None,
        keep_history: bool = False,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """获取 K 线，并记录来源、下载耗时、接收时间和最新 K 线年龄。"""
        canonical = canonical_period(period or self.period)
        key = self._market_health_key(symbol, canonical)
        started_at = datetime.now().astimezone()
        started = time.perf_counter()
        try:
            bars = self._fetch_impl(
                symbol,
                canonical,
                max_bars=max_bars,
                keep_history=keep_history,
                start=start,
                end=end,
            )
        except Exception as exc:
            health = {
                "symbol": symbol,
                "period": canonical,
                "source": self.source,
                "status": "error",
                "fetch_started_at": started_at.isoformat(timespec="milliseconds"),
                "received_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "download_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "latest_bar_time": "",
                "latest_bar_age_minutes": None,
                "from_cache": False,
                "stale": True,
                "synthetic": self.source == "demo",
                "error": str(exc),
            }
            self._market_health[key] = health
            raise

        age = bars_age_minutes(bars)
        stale_limit = stale_after_minutes(canonical, self.max_stale_minutes)
        from_cache = bool(bars.attrs.get("from_cache", False))
        error = str(bars.attrs.get("download_error", "") or "")
        stale = bool(age is None or age > stale_limit)
        status = "stale" if stale else ("cache" if from_cache or error else "ok")
        health = {
            "symbol": symbol,
            "period": canonical,
            "source": self.source,
            "status": status,
            "fetch_started_at": started_at.isoformat(timespec="milliseconds"),
            "received_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "download_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "latest_bar_time": str(bars.index[-1]) if len(bars) else "",
            "latest_bar_age_minutes": None if age is None else round(age, 3),
            "stale_limit_minutes": stale_limit,
            "from_cache": from_cache,
            "stale": stale,
            "synthetic": self.source == "demo",
            "error": error,
        }
        # 只写元数据，不换数据源、不改 K 线值。调用方可把这份证据带进信号和委托。
        bars.attrs["source"] = self.source
        bars.attrs["bars_age_minutes"] = age
        bars.attrs["stale"] = stale
        bars.attrs["market_health"] = dict(health)
        self._market_health[key] = dict(health)
        return bars

    def _to_sina_symbol(self, symbol: str) -> str:
        """
        把配置里的品种名转成新浪分钟线代码。

        规则（重要）：
        - 写具体合约 bu2612 / BU2612 → 直接扫该合约（不再偷换成主连 BU0）
        - 只写品种 bu / c / 沥青 → 才用 symbol_map 映射到主连（如 BU0）
        """
        s = symbol.strip()
        key = s.lower()

        # 1) 完整键命中（可在 config 里写 bu2612: BU2612）
        if key in self.symbol_map:
            return str(self.symbol_map[key]).upper()
        if s in self.symbol_map:
            return str(self.symbol_map[s]).upper()

        # 2) 具体合约：字母+数字（如 bu2612、rb2510）→ 原样大写去拉行情
        alpha = "".join(ch for ch in key if ch.isalpha())
        digits = "".join(ch for ch in key if ch.isdigit())
        if alpha and digits:
            return f"{alpha.upper()}{digits}"

        # 3) 仅品种代码：bu / c → 主连
        if alpha in self.symbol_map:
            return str(self.symbol_map[alpha]).upper()

        return s.upper()

    def _stored_symbol(self, symbol: str) -> str:
        """本地库存名。东方财富和新建浪分开，避免两家写进同一根时间。"""
        from src.data.quote_check import storage_symbol

        return storage_symbol(symbol, self.source)

    def latest_price(self, symbol: str) -> float:
        """
        最新成交价（1 分钟粒度）。取不到返回 0。

        必须问当前选中的那一家：选天勤就问天勤，选东方财富就问东方财富。
        否则开仓滑点闸门会拿另一家的价格去比，选天勤时等于没闸门。
        """
        try:
            if self.source == "ths":
                return 0.0
            if self.source == "tq":
                frame = self._fetch_tq(symbol, "1", limit=5)
            elif self.source == "eastmoney":
                frame = self._fetch_eastmoney(symbol, "1", limit=5)
            else:
                frame = self._recent_minutes(self._to_sina_symbol(symbol))
            if frame is None or len(frame) == 0:
                return 0.0
            value = float(frame["close"].iloc[-1])
            return value if value > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _fetch_impl(
        self,
        symbol: str,
        period: str | None = None,
        *,
        max_bars: int | None = None,
        keep_history: bool = False,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """
        获取指定合约的分钟K线。

        参数:
            max_bars: 返回时最多保留多少根（从最新往前截）。
                      None=实盘默认 live_max_bars。
                      本地库里超过这个数量的旧K线不会删，只是这次不返回。
            keep_history: 回测、优化、复盘用。返回本地库和这次下载接好的全部K线，
                      再由调用方按开始和结束日期截取。
            start / end: 回测要的开始、结束日期。本地这段已经齐，并且结束日不是今天，
                      就不再下载，直接用本地。否则先下载新K线，再和本地接上。

        返回列：open, high, low, close, volume；索引为时间。
        """
        period = canonical_period(period or self.period)
        sina_code = self._to_sina_symbol(symbol)
        limit = int(max_bars) if max_bars is not None else self.live_max_bars
        limit = max(limit, self.min_bars)
        if self.source == "ths" and not keep_history:
            raise RuntimeError(
                "同花顺图只能用于历史回测。读图要抢窗口、动键盘，实盘扫描和下单会卡住。"
                "请到历史回测页选「同花顺图」。配置页请继续用新浪、东方财富或天勤。"
            )
        logger.info(
            "拉取K线 symbol={} sina={} period={} source={} max_bars={}",
            symbol,
            sina_code,
            period_label(period),
            {"eastmoney": "东方财富", "tq": "天勤", "ths": "同花顺图"}.get(self.source, "新浪"),
            "全部库存" if keep_history else limit,
        )

        if self.source == "demo":
            return self._clip(self._demo_bars(symbol, period, n=limit), limit)

        local = self._prepare_local(symbol, period, limit=None if keep_history else limit)
        if self.source == "ths":
            # 读图只由「从同花顺图读取K线」按钮触发。回测、优化只用已经存进 @ths 的库。
            # 结束日是今天也不能再去抢鼠标：否则刚读完 800 根，一跑回测又从头读一遍。
            if local is not None and len(local) > 0:
                logger.info(
                    "同花顺图用本地库存 {} 根（{} ~ {}），不再去期货通读图",
                    len(local),
                    local.index[0],
                    local.index[-1],
                )
                out = fix_gap_spanning_bars(local)
                out.attrs["from_cache"] = True
                return out
            raise RuntimeError(
                f"本地还没有 {symbol} 的同花顺图K线。"
                "请到历史回测页点「从同花顺图读取K线」。读完再跑回测。"
            )
        # 回测区间落在过去，而且本地从开始日到结束日都有K线：这段本地数据是齐的，优先用本地。
        # 仍要用 1 分钟把夜盘第一根等对不齐的 K 线补成同花顺时间戳。
        if keep_history and self.source != "csv" and self._local_covers_requested(local, start, end):
            logger.info(
                "本地K线已覆盖 {} ~ {}，优先使用本地 {} 根（{} ~ {}）",
                start or "起",
                end or "止",
                len(local),
                local.index[0],
                local.index[-1],
            )
            if self.source in {"tq", "ths"}:
                # 天勤和同花顺图已经是右标签，不再拿新浪的1分钟来重拼。
                local.attrs["from_cache"] = True
                return local
            fixed = self._rebuild_with_minutes(symbol, sina_code, period, local)
            out = fix_gap_spanning_bars(fixed) if fixed is not None else fix_gap_spanning_bars(local)
            out.attrs["from_cache"] = True
            return out

        fresh: pd.DataFrame | None = None
        download_error = ""
        if self.source == "csv":
            fresh = self._load_csv(symbol, period)
        else:
            try:
                if self.source == "eastmoney":
                    fresh = self._fetch_eastmoney(symbol, period, limit=1200 if keep_history else 200)
                    self._save_vendor_raw(symbol, period, fresh)
                    # 东方财富直接下的 5/15/30/60 时间戳也可能和同花顺不一致；同样用 1 分钟右标签重拼。
                    if period in {"5", "15", "30", "60"} and fresh is not None and len(fresh) > 0:
                        rebuilt = self._rebuild_frame_with_minutes(
                            sina_code, period, fresh, symbol_hint=symbol
                        )
                        if rebuilt is not None:
                            fresh = rebuilt
                elif self.source == "tq":
                    # 回测要长历史：往大要，天勤服务端按它的上限（免费账号实测约 10000 根）
                    # 截断，给多少存多少。这些K线会存进本地库，下次区间落在已有数据里就不再重下。
                    # 实盘仍只要最近一段。
                    fresh = self._fetch_tq(symbol, period, limit=1_000_000 if keep_history else 200)
                    self._save_vendor_raw(symbol, period, fresh)
                else:
                    fresh = self._fetch_akshare(sina_code, period, symbol_hint=symbol)
            except Exception as e:  # noqa: BLE001
                download_error = str(e)
                if local is not None and len(local) > 0:
                    logger.warning("行情下载失败，先用刚才存下的K线: {}", e)
                elif self.source == "eastmoney":
                    logger.warning("东方财富这次没连上，电脑里还没有它的K线。下一轮再试。")
                else:
                    logger.warning("行情下载失败: {}，改用本地K线库", e)

        if fresh is not None and len(fresh) > 0:
            # 跨休市的第一根常被接口拼进上一段收盘价，先修掉再入库，避免把假高低点存下来。
            fresh = fix_gap_spanning_bars(fresh)
        if fresh is not None and len(fresh) > 0 and self.source != "csv":
            full = self._archive_bars(symbol, period, fresh, local=local, full_history=keep_history)
            full = self._replace_rebuilt_bars(symbol, period, full, fresh)
        else:
            full = local if local is not None and len(local) > 0 else self._load_history(
                symbol, period, limit=None if keep_history else limit
            )
            if full is None and fresh is not None and len(fresh) > 0:
                full = fresh
            # 下载失败时，本地库存也尽量用 1 分钟纠偏
            if full is not None and len(full) > 0 and self.source not in {"csv", "tq", "ths"}:
                rebuilt = self._rebuild_with_minutes(symbol, sina_code, period, full)
                if rebuilt is not None:
                    full = rebuilt

        if full is not None and len(full) > 0:
            # 拼图（1分钟合成）也会带上同样的假影线，返回前按上一根收盘修一次。
            full = fix_gap_spanning_bars(full)
            if self.source == "csv" or fresh is not None and len(fresh) > 0:
                return full if keep_history else self._clip(full, limit)
            out = full if keep_history else self._clip(full, limit)
            age = bars_age_minutes(out)
            try:
                out.attrs["from_cache"] = True
                out.attrs["download_error"] = download_error
                out.attrs["bars_age_minutes"] = age
                out.attrs["stale"] = bool(age is not None and age > self.max_stale_minutes)
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "使用本地K线库: {} 根 | 距今约 {} 分钟 | stale={}",
                len(out),
                None if age is None else round(age, 1),
                out.attrs.get("stale", False),
            )
            return out

        if self.source == "eastmoney":
            raise RuntimeError(
                f"东方财富这次没有拿到 {symbol} 的{period_label(period)}K线。"
                "这一轮先不判断开仓。不会拿新浪的K线来顶替，下一轮会再试。"
            )
        if self.source == "tq":
            raise RuntimeError(
                f"天勤这次没有拿到 {symbol} 的{period_label(period)}K线。"
                "这一轮先不判断开仓。不会拿别家的K线来顶替，下一轮会再试。"
            )
        if self.source == "ths":
            raise RuntimeError(
                f"同花顺图这次没有读到 {symbol} 的{period_label(period)}K线。"
                "请确认期货通主图已打开、周期和合约已点好，信息框里能看到开盘和收盘。"
            )
        raise RuntimeError(
            f"无法获取真实K线 symbol={symbol} period={period_label(period)}。"
            "请检查网络；已禁止回退演示数据，以免错误下单。"
        )

    def _local_covers_requested(
        self,
        local: pd.DataFrame | None,
        start: str | None,
        end: str | None,
    ) -> bool:
        """
        本地数据是否已经盖住这次要的整段日期。

        没填开始或结束，说明还想要最新的一根，不能当成已经齐。
        结束日是今天或更晚，盘中还会走出新K线，也要先下载。
        """
        if local is None or len(local) == 0:
            return False
        if start in (None, "") or end in (None, ""):
            return False
        start_ts = pd.Timestamp(start)
        end_day = pd.Timestamp(end).normalize()
        today = pd.Timestamp.now().normalize()
        if end_day >= today:
            return False
        if pd.Timestamp(local.index.min()) > start_ts:
            return False
        # 结束日当天要至少有一根K线，才算这段本地是齐的。
        end_close = end_day + pd.Timedelta(days=1)
        on_end_day = (local.index >= end_day) & (local.index < end_close)
        return bool(on_end_day.any())

    def _save_vendor_raw(self, symbol: str, period: str, frame: pd.DataFrame | None) -> None:
        """
        接口刚下来的K线另存一份。失败不影响开平仓。

        存的是还没对齐时间、还没用 1 分钟重拼、还没修跨休市影线的表。
        """
        if frame is None or len(frame) == 0 or self.source in {"csv", "demo"}:
            return
        try:
            stored = self._stored_symbol(symbol)
            n = self.raw_store.upsert(stored, canonical_period(period), self.source, frame)
            logger.debug("原样K线已存 {} {} {} 根", stored, period_label(period), n)
        except Exception as exc:  # noqa: BLE001
            logger.debug("原样K线没写上（不影响开仓）: {}", exc)

    @staticmethod
    def _clip(df: pd.DataFrame, max_bars: int) -> pd.DataFrame:
        """只保留最近 max_bars 根，避免实盘扫描太慢。"""
        if df is None or len(df) <= max_bars:
            return df
        return df.tail(max_bars)

    def _prepare_local(
        self, symbol: str, period: str, limit: int | None = None
    ) -> pd.DataFrame | None:
        """
        读出本地库，并把 CSV 里多出来的更早K线补进去。

        数据库里已经有的时间不改。这样手工留下的完整历史不会被一份较短的文件盖掉。
        limit 有值时只处理最近这么多根（实盘扫描）；旧K线仍留在库里，不读也不删。
        """
        stored_name = self._stored_symbol(symbol)
        opens = session_open_minutes(symbol)
        stored = aligned_bars(
            self.store.load(stored_name, period, limit=limit), period, session_opens=opens
        )
        raw_count = self.store.count(stored_name, period)
        # 只有把整库都读进来时才做对齐修复；只读尾部时不能拿尾部根数去和全库比，否则会误删历史。
        if (limit is None or raw_count <= limit) and stored is not None and len(stored) < raw_count:
            self.store.replace(stored_name, period, stored)
            self._save_cache(stored_name, period, stored)
            logger.warning(
                "已去掉 {} {} 里 {} 根对不齐周期或价格带小数的K线，剩下 {} 根",
                symbol,
                period_label(period),
                raw_count - len(stored),
                len(stored),
            )
        csv_bars: pd.DataFrame | None = None
        try:
            csv_bars = aligned_bars(
                self._load_cache(stored_name, period), period, session_opens=opens
            )
            if limit is not None and csv_bars is not None and len(csv_bars) > limit:
                csv_bars = csv_bars.tail(limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读本地K线文件失败，只使用数据库: {}", exc)
        merged = merge_local_and_download(stored, csv_bars, refresh_last=False)
        if merged is None or len(merged) == 0:
            return None
        if limit is not None and len(merged) > limit:
            merged = merged.tail(limit)
        stored_len = 0 if stored is None else len(stored)
        if len(merged) > stored_len:
            # 只把 CSV 里多出来、或和库里不一样的K线写进去，不整段重写。
            pending = rows_needing_write(
                merged, stored, columns=("open", "high", "low", "close", "volume", "oi")
            )
            if pending is not None and len(pending) > 0:
                self.store.upsert(stored_name, period, pending)
                # 实盘只读尾部，不能把这段尾部当成全部历史写回 CSV，否则会把更早的K线删掉。
                if limit is None:
                    self._save_cache(stored_name, period, merged)
            logger.info(
                "本地K线文件补进数据库 {} {} {} 根，库存 {} 根",
                symbol,
                period_label(period),
                len(merged) - stored_len,
                len(merged),
            )
        return merged

    def _archive_bars(
        self,
        symbol: str,
        period: str,
        fresh: pd.DataFrame,
        local: pd.DataFrame | None = None,
        *,
        full_history: bool = True,
    ) -> pd.DataFrame:
        """
        先下载到的新K线接到本地历史上。

        本地已有的时间保持原样；下载多出来的新K线补上；本地最后一根用下载结果刷新。
        网上窗口比回测开始日期短时，更早的本地K线因此还在。
        full_history=False（实盘）时只写这次下载的这一段，旧K线留在库里，
        不再每轮把整段历史重写一遍。
        """
        if local is None:
            local = self._prepare_local(symbol, period)
        pieces = aligned_bars(fresh, period, session_opens=session_open_minutes(symbol))
        before = 0 if local is None else len(local)
        merged = merge_local_and_download(local, pieces, refresh_last=True)
        if merged is None or len(merged) == 0:
            return fresh
        stored_name = self._stored_symbol(symbol)
        # 库里只放已经收盘的K线。正在走的那根只给这一轮判断用（持仓看影线），
        # 不写进去，免得下次回测把它当成收盘价。
        stored = drop_forming_bar(merged)
        if full_history:
            # 回测每轮拿到的都是同一段历史。和本地比对后只写新增/变化的那几根，
            # 不整段重写；CSV 也一样，没有新K线就不重写。
            pending = rows_needing_write(
                stored, local, columns=("open", "high", "low", "close", "volume", "oi")
            )
            if pending is not None and len(pending) > 0:
                self.store.upsert(stored_name, period, pending)
                self._save_cache(stored_name, period, stored)
        else:
            # 实盘这段下载窗口每轮都一样。库里已经有且一样的不再重写，只补新K线和变化的那根。
            pending = rows_needing_write(drop_forming_bar(pieces), local)
            if pending is not None and len(pending) > 0:
                self.store.upsert(stored_name, period, pending)
        added = len(merged) - before
        fresh_from = fresh.index.min() if len(fresh) else ""
        local_from = "" if local is None or len(local) == 0 else local.index.min()
        if local_from != "" and fresh_from != "" and pd.Timestamp(local_from) < pd.Timestamp(fresh_from):
            logger.info(
                "下载窗口从 {} 开始，更早的本地K线从 {} 保留，合并后 {} 根",
                fresh_from,
                local_from,
                len(merged),
            )
        elif added > 0:
            logger.info(
                "本地K线库 {} {} 新增 {} 根，库存 {} 根",
                symbol,
                period_label(period),
                added,
                len(merged),
            )
        else:
            logger.info(
                "本地K线库 {} {} 没有更早的新K线，库存 {} 根",
                symbol,
                period_label(period),
                len(merged),
            )
        return merged

    def _load_history(
        self, symbol: str, period: str, limit: int | None = None
    ) -> pd.DataFrame | None:
        """下载失败时用。先读数据库，没有再读 CSV。"""
        stored_name = self._stored_symbol(symbol)
        stored = self.store.load(stored_name, period, limit=limit)
        if stored is not None and len(stored) > 0:
            return stored
        return self._load_cache(stored_name, period)

    def _fetch_akshare(
        self, sina_code: str, period: str, *, symbol_hint: str | None = None
    ) -> pd.DataFrame:
        """
        拉行情。

        1、5、15、30、60 分钟走新浪分钟线。
        日线走新浪日线。周线用日线收到周五。
        自定义分钟，比如 120，先拉能整除它的那一档，再合成。
        """
        period = canonical_period(period)
        if period == "D":
            daily = self._normalize_ohlc(self._sina_daily(sina_code))
            self._save_vendor_raw(symbol_hint or sina_code, period, daily)
            return daily
        if period == "W":
            daily = self._normalize_ohlc(self._sina_daily(sina_code))
            week = resample_ohlc(daily, "W")
            self._save_vendor_raw(symbol_hint or sina_code, period, week)
            return week

        base = minute_base(period)
        raw = self._sina_minutes(sina_code, base)
        df = self._normalize_ohlc(raw)
        if base != period:
            df = resample_ohlc(df, period)
        # 先把接口原样存下来，再拿 1 分钟重拼。开仓仍看拼好的那一份。
        self._save_vendor_raw(symbol_hint or sina_code, period, df)
        # 5、15、30、60 分钟改用 1 分钟来拼。时间戳与同花顺右标签对齐；夜盘第一根一并补上。
        # symbol_hint 要用配置里的合约名（如 fu2701），才能对上本地 1 分钟库存；不要用新浪大写码。
        if period not in {"5", "15", "30", "60"}:
            return df
        rebuilt = self._rebuild_frame_with_minutes(
            sina_code, period, df, symbol_hint=symbol_hint or sina_code
        )
        return rebuilt if rebuilt is not None else df

    def _recent_minutes(self, sina_code: str) -> pd.DataFrame:
        """最近一段 1 分钟。5 秒内同一合约不重复下载（缩短缓存，减轻收盘后仍显示旧K的滞后）。"""
        import time

        now = time.monotonic()
        cached = self._minute_cache
        if cached is not None and cached[0] == sina_code and now - cached[1] < 5:
            return cached[2].copy()
        frame = self._normalize_ohlc(self._sina_minutes(sina_code, "1"))
        self._minute_cache = (sina_code, now, frame)
        return frame.copy()

    def _local_minutes(self, symbol: str) -> pd.DataFrame | None:
        """本地库存里的 1 分钟（回测补夜盘第一根要用历史，不能只靠刚下载的一小段）。"""
        stored_name = self._stored_symbol(symbol)
        stored = self.store.load(stored_name, "1")
        if stored is not None and len(stored) > 0:
            return self._normalize_ohlc(stored)
        cached = self._load_cache(stored_name, "1")
        if cached is not None and len(cached) > 0:
            return self._normalize_ohlc(cached)
        return None

    def _minutes_for_rebuild(self, symbol: str, sina_code: str) -> pd.DataFrame | None:
        """本地 1 分钟 + 刚拉的最近 1 分钟拼在一起。"""
        pieces: list[pd.DataFrame] = []
        local = self._local_minutes(symbol)
        if local is not None and len(local) > 0:
            pieces.append(local)
        try:
            recent = self._recent_minutes(sina_code)
            if recent is not None and len(recent) > 0:
                pieces.append(recent)
                # 顺手把最近 1 分钟写入库存，下次回测也能补齐。库里已经一样的行不再重写。
                try:
                    pending = rows_needing_write(recent, local)
                    if pending is not None and len(pending) > 0:
                        self.store.upsert(self._stored_symbol(symbol), "1", pending)
                    self._save_cache(self._stored_symbol(symbol), "1", recent)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("最近1分钟暂时拉不到: {}", exc)
        if not pieces:
            return None
        pieces = [bars_without_attrs(part) for part in pieces]
        out = pd.concat(pieces).sort_index()
        out = out[~out.index.duplicated(keep="last")]
        return out

    def _rebuild_frame_with_minutes(
        self,
        sina_code: str,
        period: str,
        frame: pd.DataFrame | None,
        *,
        symbol_hint: str | None = None,
    ) -> pd.DataFrame | None:
        """把 frame 里能被 1 分钟盖住的时段，换成右标签、可含夜盘首根的版本。"""
        if frame is None or len(frame) == 0:
            return frame
        if period not in {"5", "15", "30", "60"}:
            return frame
        symbol = symbol_hint or sina_code
        try:
            minutes = self._minutes_for_rebuild(symbol, sina_code)
            if minutes is None or len(minutes) < 3:
                return frame
            built = bars_from_minutes(minutes, period)
            merged, complete = overlay_minute_bars(frame, built, int(period))
        except Exception as exc:  # noqa: BLE001
            logger.warning("1分钟没能拼成{}，这一轮仍用原K线: {}", period_label(period), exc)
            return frame
        if merged is None:
            return frame
        # 1分钟拼出来的第一根同样会带上上一段收盘价，这里也修一次再写回库存。
        merged = fix_gap_spanning_bars(merged)
        try:
            # 只记时间字符串。不要塞 DatetimeIndex：拼表时 pandas 会拿两段时间比相不相等，直接报错。
            merged.attrs["rebuilt_from_minutes"] = tuple(
                pd.DatetimeIndex(complete).strftime("%Y-%m-%d %H:%M:%S").tolist()
            )
        except Exception:  # noqa: BLE001
            pass
        return merged

    def _rebuild_with_minutes(
        self,
        symbol: str,
        sina_code: str,
        period: str,
        frame: pd.DataFrame | None,
    ) -> pd.DataFrame | None:
        """回测/缓存路径：用本地+最近 1 分钟纠偏，并写回改过的那几根。"""
        rebuilt = self._rebuild_frame_with_minutes(
            sina_code, period, frame, symbol_hint=symbol
        )
        if rebuilt is None:
            return frame
        if frame is None:
            return rebuilt
        # rebuilt 已是覆盖时段替换后的完整表；写回库存后直接用它
        return self._replace_rebuilt_bars(symbol, period, frame, rebuilt)

    def _replace_rebuilt_bars(
        self,
        symbol: str,
        period: str,
        full: pd.DataFrame,
        fresh: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        把 1 分钟拼齐的那几根写回本地。

        覆盖时段内以拼图为准（含夜盘第一根），清掉同时间错位的旧戳，避免左标签残留。
        """
        raw_span = getattr(fresh, "attrs", {}).get("rebuilt_from_minutes")
        stamps = rebuilt_stamp_list(raw_span)
        if not stamps or full is None or len(full) == 0:
            # 若 fresh 已是完整纠偏表且比 full 多出夜盘首根，仍优先返回 fresh
            if fresh is not None and len(fresh) >= len(full):
                return bars_without_attrs(
                    fresh.sort_index() if hasattr(fresh, "sort_index") else fresh
                )
            return bars_without_attrs(full)
        cols = [
            name
            for name in ("open", "high", "low", "close", "volume")
            if name in full.columns and name in fresh.columns
        ]
        if not cols:
            return bars_without_attrs(full)
        updated = bars_without_attrs(full.copy())
        before = len(updated)
        # 按时间戳逐根替换，不整段删除：缺 1 分钟数据的那天原生 K 线保留
        updated = updated.loc[~updated.index.isin(stamps)]
        piece = bars_without_attrs(fresh.loc[fresh.index.isin(stamps), cols])
        updated = pd.concat([updated, piece]).sort_index()
        updated = updated[~updated.index.duplicated(keep="last")].sort_index()
        replaced = max(0, len(piece))
        # 也统计是否有新增时间戳
        added = int(len(updated) - before + (before - len(full.loc[~full.index.isin(stamps)])))
        if replaced <= 0 and len(updated) == len(full):
            return updated
        logger.info(
            "{} {} 有 {} 根改用 1 分钟拼出来的K线（右标签，对齐同花顺；含夜盘首根补齐）。开仓看这一版。",
            symbol,
            period_label(period),
            replaced,
        )
        stored_name = self._stored_symbol(symbol)
        self.store.upsert(stored_name, period, piece)
        self._save_cache(stored_name, period, updated)
        _ = added
        return bars_without_attrs(updated)

    @staticmethod
    def _sina_json_table(url: str, params: dict[str, str]) -> pd.DataFrame:
        """
        自己向新浪要 K 线。

        原来交给 akshare 去下，它的下载没有等待上限。
        实盘正在每半分钟拉一次时，回测再去拉，这一路可能永远不回来，页面就停在「请稍候」。
        """
        with _FETCH_LOCK:
            response = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        text = response.text
        start = text.find("=(")
        end = text.rfind(");")
        if start < 0 or end <= start:
            raise RuntimeError("新浪返回的不是K线数据")
        table = pd.DataFrame(json.loads(text[start + 2 : end]))
        # 新浪现在用单字母列名。收成和原来一样的开高低收，后面的整理才认得出收盘价。
        short = {"d": "datetime", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "p": "hold", "s": "settle"}
        return table.rename(columns={key: name for key, name in short.items() if key in table.columns})

    def _fetch_ths(self, symbol: str, period: str, *, limit: int = 200) -> pd.DataFrame:
        """从期货通主图信息框读 K 线。只给历史回测。"""
        from src.data.ths_chart import scrape_ths_klines

        want = min(max(int(limit), 10), 5000)
        return scrape_ths_klines(self._cfg, symbol, period, limit=want)

    def import_ths_chart(self, symbol: str, period: str, *, limit: int = 800) -> pd.DataFrame:
        """
        按按钮「从同花顺图读取K线」时走这里。

        读失败就原样报错，不用新浪库存顶替，也不假装已经读到。
        读成功才写入 @ths 原样库和清洗库。
        """
        period = canonical_period(period or self.period)
        fresh = self._fetch_ths(symbol, period, limit=limit)
        if fresh is None or len(fresh) == 0:
            raise RuntimeError(
                f"同花顺图这次没有读到 {symbol} 的{period_label(period)}K线。"
                "请确认期货通主图已打开、周期和合约已点好，信息框里能看到开盘和收盘。"
            )
        return self._store_ths_batch(symbol, period, fresh, start=None, end=None)["frame"]

    def import_ths_chart_batch(
        self,
        symbol: str,
        period: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """
        按「时段 + 周期」读一批同花顺图并入库。

        读完做三件事：算这段的覆盖/缺口，记一行读图批次，重写库存 Excel。
        旧批次和新批次按 (库存名, 周期, 时间戳) 接续，不会重复。
        progress 每读到一根回调一次（已读, 计划, 这根的时间），给界面刷进度。
        """
        from src.data.ths_chart import scrape_ths_klines_range

        period = canonical_period(period or self.period)
        ths_cfg = (self._cfg.get("data") or {}).get("ths_chart") or {}
        cap = int(limit) if limit else int(ths_cfg.get("max_bars", 5000) or 5000)
        fresh = scrape_ths_klines_range(
            self._cfg, symbol, period, start, end, max_bars=cap, progress=progress
        )
        return self._store_ths_batch(symbol, period, fresh, start=start, end=end)

    def _store_ths_batch(
        self,
        symbol: str,
        period: str,
        fresh: pd.DataFrame,
        *,
        start: str | None,
        end: str | None,
    ) -> dict[str, Any]:
        """把一批读到的图存进原样库和清洗库，再记批次、刷新库存表。"""
        from src.data.coverage import coverage_report, require_timestamps
        from src.data.inventory import write_kline_inventory
        from src.data.quote_check import storage_symbol

        require_timestamps(fresh)
        fresh = fix_gap_spanning_bars(fresh)
        self._save_vendor_raw(symbol, period, fresh)
        local = self._prepare_local(symbol, period, limit=None)
        before = 0 if local is None else len(local)
        merged = self._archive_bars(symbol, period, fresh, local=local, full_history=True)
        added = max(0, len(merged) - before)

        stored_name = storage_symbol(symbol, "ths")
        stored = self.store.load(stored_name, period)
        report = None
        if start is not None and end is not None:
            report = coverage_report(stored, start, end, period)
        notes: list[str] = []
        if report is not None and report.missing_n > 0:
            notes.append(f"时段内仍缺 {report.missing_n} 根")
        if report is None:
            notes.append("未指定时段，按根数读")
        try:
            self.store.record_ingest(
                source="ths",
                symbol=stored_name,
                period=period,
                requested_from=start or "",
                requested_to=end or "",
                read_from=str(fresh.index[0]) if len(fresh) else "",
                read_to=str(fresh.index[-1]) if len(fresh) else "",
                read_n=len(fresh),
                added_n=added,
                expected_n=0 if report is None else report.expected_n,
                missing_n=0 if report is None else report.missing_n,
                note="；".join(notes),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读图批次没记上（不影响K线入库）: {}", exc)
        try:
            inventory_path = str(write_kline_inventory(self.root_dir))
        except Exception as exc:  # noqa: BLE001
            logger.warning("库存 Excel 没更新（不影响K线入库）: {}", exc)
            inventory_path = ""
        return {
            "added": added,
            "read_n": len(fresh),
            "bar_from": str(fresh.index[0]) if len(fresh) else "",
            "bar_to": str(fresh.index[-1]) if len(fresh) else "",
            "coverage": {} if report is None else report.as_dict(),
            "inventory_path": inventory_path,
            "frame": merged,
        }

    def _fetch_tq(self, symbol: str, period: str, *, limit: int = 200) -> pd.DataFrame:
        """向天勤要 K 线。账号密码来自配置和本地密钥，不写进代码。"""
        from src.data import tq_source

        return tq_source.fetch_frame(
            symbol,
            period,
            limit=limit,
            user=getattr(self, "tq_user", ""),
            password=getattr(self, "tq_password", ""),
        )

    def _fetch_eastmoney(self, symbol: str, period: str, *, limit: int = 200) -> pd.DataFrame:
        """
        拉东方财富这一家。

        5、15、30、60 分钟直接用它给出的 K 线，不再用 1 分钟重拼。
        自定义分钟先拉能整除的那一档，再合成。和新建浪分开存。
        实盘只要最近一段。回测才多要一些，要太多时对方更容易把连接掐断。
        """
        from src.data.quote_check import fetch_eastmoney_frame

        period = canonical_period(period)
        with _FETCH_LOCK:
            if period in {"1", "5", "15", "30", "60", "D", "W"}:
                return self._normalize_ohlc(fetch_eastmoney_frame(symbol, period, limit=limit))
            base = minute_base(period)
            raw = self._normalize_ohlc(fetch_eastmoney_frame(symbol, base, limit=limit))
        return resample_ohlc(raw, period)

    def _sina_minutes(self, sina_code: str, period: str) -> pd.DataFrame:
        """1、5、15、30、60 分钟。period 用新浪要的数字，例如 5。"""
        url = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getFewMinLine"
        return self._sina_json_table(url, {"symbol": sina_code, "type": str(period)})

    def _sina_daily(self, sina_code: str) -> pd.DataFrame:
        """日线。周线再由日线合成。"""
        url = (
            "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_V21052021_4_12="
            "/InnerFuturesNewService.getDailyKLine"
        )
        return self._sina_json_table(url, {"symbol": sina_code, "type": "2021_04_12"})

    @staticmethod
    def _normalize_ohlc(raw: Any) -> pd.DataFrame:
        """日线和分钟线的列名不一样，这里收成同一张表：时间索引 + 开高低收量。"""
        df = pd.DataFrame(raw)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime")
        elif "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        cols = {c.lower(): c for c in df.columns}
        rename = {}
        for need in ("open", "high", "low", "close", "volume"):
            if need in cols:
                rename[cols[need]] = need
        df = df.rename(columns=rename)
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep]
        # 单字母列和英文列同时存在时，rename 后会留下两列 close，后面 if 收盘价会报错。
        df = df.loc[:, ~df.columns.duplicated(keep="last")]
        df = df.apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
        return df.sort_index()

    def _cache_path(self, symbol: str, period: str) -> Path:
        safe = symbol.replace("/", "_")
        return self.cache_dir / f"{safe}_{cache_tag(period)}.csv"

    def _save_cache(self, symbol: str, period: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, period)
        try:
            df.to_csv(path, encoding="utf-8-sig")
        except OSError as exc:
            # Excel 开着这份表时写不进去。不要在这里写 .skip：
            # 实盘新浪/天勤也会走这里，误打标记会让扫描再也读不到 CSV。
            logger.warning("写本地K线 CSV 失败（请先关掉 Excel）: {} — {}", path, exc)
            return
        marker = csv_skip_marker(path)
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_cache(self, symbol: str, period: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol, period)
        marker = csv_skip_marker(path)
        if marker.exists():
            logger.info("已跳过旧 CSV（刚才删库时文件被占用）: {}", path.name)
            return None
        if not path.exists():
            return None
        return self._read_ohlc_csv(path)

    def _load_csv(self, symbol: str, period: str) -> pd.DataFrame:
        path = self._cache_path(symbol, period)
        if not path.exists():
            raise FileNotFoundError(f"找不到 CSV: {path}")
        return self._read_ohlc_csv(path)

    @staticmethod
    def _read_ohlc_csv(path: Path) -> pd.DataFrame:
        df = pd.read_csv(path)
        time_col = "datetime" if "datetime" in df.columns else df.columns[0]
        df[time_col] = pd.to_datetime(df[time_col])
        df = df.set_index(time_col)
        df.columns = [c.lower() for c in df.columns]
        return df.sort_index()

    def _demo_bars(self, symbol: str, period: str, n: int | None = None) -> pd.DataFrame:
        """
        生成带「下跌后长下影 Pinbar」的演示数据，方便你马上看到买入信号。
        """
        period = canonical_period(period)
        if period == "D":
            step = timedelta(days=1)
        elif period == "W":
            step = timedelta(weeks=1)
        else:
            step = timedelta(minutes=int(period))
        n = int(n) if n is not None else max(self.min_bars, 60)
        n = max(n, self.min_bars, 60)
        end = datetime.now().replace(second=0, microsecond=0)
        idx = [end - step * (n - i) for i in range(n)]

        # 前半段下跌，末尾一根锤子线
        rng = np.random.default_rng(abs(hash(symbol)) % 10000)
        price = 3500.0 if "c" in symbol.lower() or "玉米" in symbol else 3800.0
        rows = []
        for i in range(n):
            if i < n - 1:
                drift = -1.5 if i > n // 3 else 0.3
                o = price
                c = price + drift + float(rng.normal(0, 0.8))
                h = max(o, c) + abs(float(rng.normal(0.5, 0.3)))
                low = min(o, c) - abs(float(rng.normal(0.5, 0.3)))
                price = c
            else:
                # 明显长下影 Pinbar
                o = price
                c = price + 2.0
                h = max(o, c) + 0.5
                low = min(o, c) - 12.0
            rows.append({"open": o, "high": h, "low": low, "close": c, "volume": 100 + i})

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="datetime"))
        self._save_cache(symbol, period, df)
        return df
