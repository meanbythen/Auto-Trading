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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from loguru import logger

from src.data.bar_store import BarStore, aligned_bars, merge_local_and_download
from src.data.periods import (
    bars_from_minutes,
    cache_tag,
    canonical_period,
    drop_forming_bar,
    minute_base,
    overlay_minute_bars,
    period_label,
    resample_ohlc,
)
from src.data.tq_source import session_open_minutes
from src.utils import load_secrets

# 实盘扫描和回测会同时来拉同一份行情。排队拉，避免两路请求缠在一起后其中一路永远不返回。
_FETCH_LOCK = threading.Lock()
# 连接最多等 5 秒，读数据最多等 20 秒。到点就失败，回测改用本地缓存，不再停在「请稍候」。
_HTTP_TIMEOUT = (5, 20)


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
        data_cfg = cfg.get("data", {})
        self.cache_dir = root / data_cfg.get("cache_dir", "data/klines")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 历次下载都追加进这个库。新浪窗口往前滚动后，更早的K线仍留在这里。
        self.store = BarStore(self.cache_dir / "market.sqlite")
        from src.data.quote_check import normalize_source

        # akshare 是新浪。eastmoney 是东方财富。tq 是天勤。一次只用配置里选中的那一家。
        self.source = normalize_source(data_cfg.get("source", "akshare"))
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
        if self.source == "tq" and period in {"D", "W"}:
            raise RuntimeError("天勤只提供分钟K线；要看日线/周线请把行情来源切回新浪或东方财富。")
        sina_code = self._to_sina_symbol(symbol)
        limit = int(max_bars) if max_bars is not None else self.live_max_bars
        limit = max(limit, self.min_bars)
        logger.info(
            "拉取K线 symbol={} sina={} period={} source={} max_bars={}",
            symbol,
            sina_code,
            period_label(period),
            {"eastmoney": "东方财富", "tq": "天勤"}.get(self.source, "新浪"),
            "全部库存" if keep_history else limit,
        )

        if self.source == "demo":
            return self._clip(self._demo_bars(symbol, period, n=limit), limit)

        local = self._prepare_local(symbol, period)
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
            if self.source == "tq":
                # 天勤的K线时间已经是右标签，不再拿新浪的1分钟来重拼。
                return local
            fixed = self._rebuild_with_minutes(symbol, sina_code, period, local)
            return fixed if fixed is not None else local

        fresh: pd.DataFrame | None = None
        if self.source == "csv":
            fresh = self._load_csv(symbol, period)
        else:
            try:
                if self.source == "eastmoney":
                    fresh = self._fetch_eastmoney(symbol, period, limit=1200 if keep_history else 200)
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
                else:
                    fresh = self._fetch_akshare(sina_code, period, symbol_hint=symbol)
            except Exception as e:  # noqa: BLE001
                if local is not None and len(local) > 0:
                    logger.warning("行情下载失败，先用刚才存下的K线: {}", e)
                elif self.source == "eastmoney":
                    logger.warning("东方财富这次没连上，电脑里还没有它的K线。下一轮再试。")
                else:
                    logger.warning("行情下载失败: {}，改用本地K线库", e)

        if fresh is not None and len(fresh) > 0 and self.source != "csv":
            full = self._archive_bars(symbol, period, fresh, local=local)
            full = self._replace_rebuilt_bars(symbol, period, full, fresh)
        else:
            full = local if local is not None and len(local) > 0 else self._load_history(symbol, period)
            if full is None and fresh is not None and len(fresh) > 0:
                full = fresh
            # 下载失败时，本地库存也尽量用 1 分钟纠偏
            if full is not None and len(full) > 0 and self.source not in {"csv", "tq"}:
                rebuilt = self._rebuild_with_minutes(symbol, sina_code, period, full)
                if rebuilt is not None:
                    full = rebuilt

        if full is not None and len(full) > 0:
            if self.source == "csv" or fresh is not None and len(fresh) > 0:
                return full if keep_history else self._clip(full, limit)
            out = full if keep_history else self._clip(full, limit)
            age = bars_age_minutes(out)
            try:
                out.attrs["from_cache"] = True
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

    @staticmethod
    def _clip(df: pd.DataFrame, max_bars: int) -> pd.DataFrame:
        """只保留最近 max_bars 根，避免实盘扫描太慢。"""
        if df is None or len(df) <= max_bars:
            return df
        return df.tail(max_bars)

    def _prepare_local(self, symbol: str, period: str) -> pd.DataFrame | None:
        """
        读出本地库，并把 CSV 里多出来的更早K线补进去。

        数据库里已经有的时间不改。这样手工留下的完整历史不会被一份较短的文件盖掉。
        """
        stored_name = self._stored_symbol(symbol)
        opens = session_open_minutes(symbol)
        stored = aligned_bars(self.store.load(stored_name, period), period, session_opens=opens)
        raw_count = self.store.count(stored_name, period)
        if stored is not None and len(stored) < raw_count:
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("读本地K线文件失败，只使用数据库: {}", exc)
        merged = merge_local_and_download(stored, csv_bars, refresh_last=False)
        if merged is None or len(merged) == 0:
            return None
        stored_len = 0 if stored is None else len(stored)
        if len(merged) > stored_len:
            self.store.upsert(stored_name, period, merged)
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
    ) -> pd.DataFrame:
        """
        先下载到的新K线接到本地历史上。

        本地已有的时间保持原样；下载多出来的新K线补上；本地最后一根用下载结果刷新。
        网上窗口比回测开始日期短时，更早的本地K线因此还在。
        """
        if local is None:
            local = self._prepare_local(symbol, period)
        before = 0 if local is None else len(local)
        merged = merge_local_and_download(
            local,
            aligned_bars(fresh, period, session_opens=session_open_minutes(symbol)),
            refresh_last=True,
        )
        if merged is None or len(merged) == 0:
            return fresh
        stored_name = self._stored_symbol(symbol)
        # 库里只放已经收盘的K线。正在走的那根只给这一轮判断用（持仓看影线），
        # 不写进去，免得下次回测把它当成收盘价。
        stored = drop_forming_bar(merged)
        self.store.upsert(stored_name, period, stored)
        self._save_cache(stored_name, period, stored)
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

    def _load_history(self, symbol: str, period: str) -> pd.DataFrame | None:
        """下载失败时用。先读数据库，没有再读 CSV。"""
        stored_name = self._stored_symbol(symbol)
        stored = self.store.load(stored_name, period)
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
            return self._normalize_ohlc(self._sina_daily(sina_code))
        if period == "W":
            daily = self._normalize_ohlc(self._sina_daily(sina_code))
            return resample_ohlc(daily, "W")

        base = minute_base(period)
        raw = self._sina_minutes(sina_code, base)
        df = self._normalize_ohlc(raw)
        if base != period:
            df = resample_ohlc(df, period)
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
                # 顺手把最近 1 分钟写入库存，下次回测也能补齐
                try:
                    self.store.upsert(self._stored_symbol(symbol), "1", recent)
                    self._save_cache(self._stored_symbol(symbol), "1", recent)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("最近1分钟暂时拉不到: {}", exc)
        if not pieces:
            return None
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
        try:
            merged.attrs["rebuilt_from_minutes"] = list(complete)
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
        span = list(getattr(fresh, "attrs", {}).get("rebuilt_from_minutes") or [])
        if not span or full is None or len(full) == 0:
            # 若 fresh 已是完整纠偏表且比 full 多出夜盘首根，仍优先返回 fresh
            if fresh is not None and len(fresh) >= len(full):
                return fresh.sort_index() if hasattr(fresh, "sort_index") else fresh
            return full
        cols = [
            name
            for name in ("open", "high", "low", "close", "volume")
            if name in full.columns and name in fresh.columns
        ]
        if not cols:
            return full
        stamps = [pd.Timestamp(ts) for ts in span]
        start = min(stamps)
        end = max(stamps)
        updated = full.copy()
        before = len(updated)
        # 覆盖窗内旧戳整段拿掉，再写入右标签拼图
        updated = updated.loc[~((updated.index >= start) & (updated.index <= end))]
        piece = fresh.loc[fresh.index.isin(stamps), cols]
        updated = pd.concat([updated, piece]).sort_index()
        updated = updated[~updated.index.duplicated(keep="last")].sort_index()
        replaced = max(0, len(piece))
        # 也统计是否有新增时间戳
        added = int(len(updated) - before + (before - len(full.loc[~((full.index >= start) & (full.index <= end))])))
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
        return updated

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
        df = df[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
        return df.sort_index()

    def _cache_path(self, symbol: str, period: str) -> Path:
        safe = symbol.replace("/", "_")
        return self.cache_dir / f"{safe}_{cache_tag(period)}.csv"

    def _save_cache(self, symbol: str, period: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, period)
        df.to_csv(path, encoding="utf-8-sig")

    def _load_cache(self, symbol: str, period: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol, period)
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
                l = min(o, c) - abs(float(rng.normal(0.5, 0.3)))
                price = c
            else:
                # 明显长下影 Pinbar
                o = price
                c = price + 2.0
                h = max(o, c) + 0.5
                l = min(o, c) - 12.0
            rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 100 + i})

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="datetime"))
        self._save_cache(symbol, period, df)
        return df
