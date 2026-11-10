"""
Auto-Trading 桌面控制台
功能：运行控制 + 配置选项卡（策略/账号/密码/软件路径等）。
原理：CustomTkinter Tabview；BotWorker 后台跑策略；密码存 secrets.local.yaml。

启动（任选其一）:
    python -m src.ui
    python -m src.main --gui
    双击项目根目录「启动界面.bat」
    在 Cursor 中运行 src/ui/__main__.py（已兼容）
"""

from __future__ import annotations

import copy
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from datetime import date, datetime
from pathlib import Path
from typing import Any

# 允许直接 python src/ui/app.py 启动：先把项目根加入路径
_FALLBACK_ROOT = Path(__file__).resolve().parents[2]
if str(_FALLBACK_ROOT) not in sys.path:
    sys.path.insert(0, str(_FALLBACK_ROOT))
from src.utils.paths import project_root

_ROOT = project_root()

from loguru import logger

from src.data.periods import PERIOD_MENU, period_label, ui_parts
from src.trader.factory import list_executors
from src.ui import theme as T
from src.ui.bot_worker import BotWorker
from src.utils import load_config, load_secrets, save_config, save_secrets, setup_logger
from src.utils.trading_day import futures_trading_day
from src.version import __version__

try:
    import customtkinter as ctk
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "缺少 customtkinter，请先执行:\n  pip install customtkinter\n"
        f"原始错误: {e}"
    ) from e


ENGINE_LABELS = {
    "daily_body": "日内实体（主推）",
    "morning_box_a": "早盘箱体突破A",
    "morning_a": "早盘箱体突破A",
    "trend_follow": "趋势跟随",
    "supertrend": "SuperTrend趋势",
    "st": "SuperTrend趋势",
    "dual_ma": "双均线趋势",
    "ma_cross": "双均线趋势",
    "bollinger": "布林带突破",
    "boll": "布林带突破",
    "sar_trend": "SAR波段趋势",
    "parabolic_sar": "SAR波段趋势",
    "multi_factor": "多因子CTA（加权打分）",
    "cta": "多因子CTA（加权打分）",
    "multi_factor_adapt": "多因子CTA（自适应权重·对照）",
    "multi_vote": "多因子投票",
    "vote": "多因子投票",
    "multi_hybrid": "多因子混合",
    "hybrid": "多因子混合",
    "swing_trend": "长线趋势加减仓",
    "jesse_casper": "Jesse Casper（简化SMC）",
    "casper": "Jesse Casper（简化SMC）",
    "jesse": "Jesse Casper（简化SMC）",
}

# 界面下拉统一用这一份（主推在前）。
ENGINE_CHOICES = [
    "multi_factor",
    "multi_factor_adapt",
    "multi_vote",
    "multi_hybrid",
    "daily_body",
    "morning_box_a",
    "trend_follow",
    "supertrend",
    "dual_ma",
    "bollinger",
    "sar_trend",
    "swing_trend",
    "jesse_casper",
]

# 旧配置里的短名字。下拉框用完整名字，打开时换成同一套策略。
_ENGINE_ALIASES = {
    "st": "supertrend",
    "ma_cross": "dual_ma",
    "morning_a": "morning_box_a",
    "boll": "bollinger",
    "bollinger_break": "bollinger",
    "sar": "sar_trend",
    "parabolic_sar": "sar_trend",
    "multi": "multi_factor",
    "cta": "multi_factor",
    "vote": "multi_vote",
    "hybrid": "multi_hybrid",
    "casper": "jesse_casper",
    "jesse": "jesse_casper",
}

# 精简时删掉的策略；旧配置读到这些名字时回到日内实体。
_REMOVED_ENGINES: set[str] = {
    "liquidity_exhaustion",
    "liquidity_signs",
    "macd_divergence",
    "macd_top_bottom",
    "macd_ths",
    "macd_zero",
    "macd",
    "reversal_pivot",
    "fvg",
    "intraday_breakout",
    "intraday",
    "pinbar_sar",
    "ema_pin_momentum",
    "ema_pin",
    "price_action",
    "pa_liquidity",
    "custom",
    "morning_gap_c",
    "morning_c",
    "sar_flip",
    "sar",
    "mean_reversion",
    "single_ma",
    "Single_ma",
    "ma20_trend",
    "ma20",
    "morning_structure_b",
    "morning_b",
}


def _ui_engine(name: object) -> str:
    """下拉框只保留还在的策略。删掉的名字回到日内实体，避免界面打不开。"""
    text = str(name or "daily_body")
    text = _ENGINE_ALIASES.get(text, text)
    if text in _REMOVED_ENGINES:
        return "daily_body"
    if text not in ENGINE_LABELS and text not in ENGINE_CHOICES:
        return "daily_body"
    return text


def _eth_minutes_from_strategy(st: dict) -> int:
    """ETH 分钟数：优先 daily_body；旧配置若还写在 liquidity_exhaustion 里也能读到。默认与策略代码一致为 20。"""
    body = st.get("daily_body") or {}
    if "eth_minutes" in body:
        try:
            return max(0, int(body.get("eth_minutes", 20)))
        except (TypeError, ValueError):
            return 20
    le = st.get("liquidity_exhaustion") or {}
    try:
        return max(0, int(le.get("eth_minutes", 20)))
    except (TypeError, ValueError):
        return 20


EXEC_LABELS = {
    "sim": "模拟盘 sim",
    "gui": "同花顺点击 gui",
    "ctp": "CTP 柜台 ctp",
}


def _clock_text(value: object, default: str) -> str:
    """保存钟点参数：能读成 HH:MM 就规整成两位，读不出来就用默认值。"""
    try:
        hour, minute = str(value or "").strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (TypeError, ValueError):
        return default
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return default
    return f"{hour_i:02d}:{minute_i:02d}"


def _same_contract(left: object, right: object) -> bool:
    """两个写法是不是同一个合约。郑商所三位和四位年月写法算同一个。"""
    a = str(left or "").strip().lower()
    b = str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    product_a = "".join(ch for ch in a if ch.isalpha())
    product_b = "".join(ch for ch in b if ch.isalpha())
    return product_a == product_b and a[-3:] == b[-3:]


def _duration_text(seconds: float) -> str:
    """秒数写成「12秒」「3分05秒」「1小时20分」，给进度提示和预计剩余时间用。"""
    sec = int(max(0.0, seconds))
    if sec < 60:
        return f"{sec}秒"
    minutes, sec = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes:02d}分"


# 周期下拉。分钟、日线、周线都在这里。选「自定义」后，右边填分钟数，例如 120 就是 2 小时。
# 1 分钟用来测试更细的信号；公开分钟线大约只有最近一千根，1 分钟只能覆盖最近几天。


class QueueLogHandler:
    """把 loguru 日志放进线程安全队列；界面在主线程里取，避免跨线程调用 Tk。"""

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def write(self, message: str) -> None:
        text = message.rstrip()
        if text:
            self._emit(text)


class AutoTradingApp(ctk.CTk):
    """主窗口：运行台 + 配置选项卡。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"Auto-Trading {__version__}  ·  期货自动交易台")
        self.geometry("1240x820")
        self.minsize(1020, 700)

        # 深蓝主题：系统用 dark，具体色值看 theme.py（保证下拉/输入框字色够亮）
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.configure(fg_color=T.BG)

        self.root_dir = project_root()
        from src.utils.config_loader import ensure_runtime_layout

        ensure_runtime_layout()
        self.cfg = load_config()
        setup_logger(
            log_dir=str(self.root_dir / self.cfg.get("logging", {}).get("dir", "logs")),
            level="INFO",
        )
        self.worker = BotWorker()
        self._status = "待命"
        self._show_password = ctk.BooleanVar(value=False)
        self._show_tq_password = ctk.BooleanVar(value=False)
        self._prep_busy = False
        self._login_fired: set[str] = set()
        # 准备按钮已经登录成功时，下一次启动常驻不再登录。
        self._skip_next_bot_login = False
        # 正在点击同花顺时先攒着的日志。点击结束前不写进窗口，避免把本程序拉到前面。
        self._held_logs: list[str] = []
        # 后台线程写日志只进队列，主线程定时取出，避免跨线程调用 Tk。
        self._log_queue: queue.Queue[str] = queue.Queue()
        # 定时刷新的 after id。关窗口时先 after_cancel，避免窗口销毁后回调还在跑。
        self._closing = False
        self._tick_after_id: str | None = None
        self._log_after_id: str | None = None
        self._login_tick_after_id: str | None = None
        # 运行日志最多留这么多行，长时间挂机不会把文本框越撑越大。
        self._LOG_MAX_LINES = 2000

        self._init_vars()
        self._build()
        self._hook_logger()
        self._refresh_status_cards()
        self._tick_after_id = self.after(800, self._tick)
        self._login_tick_after_id = self.after(5000, self._tick_login_schedule)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._ths_scraping = False
        # 读同花顺图时焦点在期货通上，窗口快捷键可能收不到；工作线程还会另外查 ESC。
        self.bind_all("<Escape>", self._on_escape_key)

    # ---------- 变量 ----------
    def _init_vars(self) -> None:
        st = self.cfg.get("strategy", {})
        acc = self.cfg.get("account", {})
        app = self.cfg.get("app", {})
        trade = self.cfg.get("trade", {})
        risk = self.cfg.get("risk", {})
        sch = self.cfg.get("schedule", {})
        secrets = load_secrets()

        self.engine_var = ctk.StringVar(value=_ui_engine(st.get("engine", "daily_body")))
        self.exec_var = ctk.StringVar(value=str(trade.get("executor", "sim")))
        from src.trader.ctp_logic import CTP_KIND_LABELS, ctp_kind_label

        ctp_box = trade.get("ctp") or {}
        live_box = ctp_box.get("live") if isinstance(ctp_box.get("live"), dict) else {}
        self.ctp_kind_var = ctk.StringVar(value=ctp_kind_label(ctp_box.get("account_kind")))
        self.ctp_kind_choices = list(CTP_KIND_LABELS.values())
        self.ctp_live_td_var = ctk.StringVar(value=str(live_box.get("td_front") or ""))
        self.ctp_live_md_var = ctk.StringVar(value=str(live_box.get("md_front") or ""))
        from src.trader.go_live import STAGE_LABELS, current_stage_from_cfg, load_state as load_go_live
        from src.trader.go_live import stage_hint, stage_label

        go_state = load_go_live(self.root_dir)
        yaml_stage = ((trade.get("go_live") or {}) if isinstance(trade.get("go_live"), dict) else {}).get("stage")
        stage_now = go_state.get("stage") or yaml_stage or current_stage_from_cfg(self.cfg)
        self.go_live_choices = list(STAGE_LABELS.values())
        self.go_live_var = ctk.StringVar(value=stage_label(stage_now))
        self.go_live_hint_var = ctk.StringVar(value=stage_hint(stage_now))
        from src.trader.go_live import allows_strategy_orders, apply_stage_to_cfg, account_kind_for_stage

        apply_stage_to_cfg(self.cfg, stage_now)
        if allows_strategy_orders(stage_now):
            self.exec_var.set("ctp")
        self.ctp_kind_var.set(ctp_kind_label(account_kind_for_stage(stage_now)))
        from src.data.quote_check import source_label

        quote_label = source_label((self.cfg.get("data") or {}).get("source"))
        self.source_var = ctk.StringVar(value=quote_label)
        self.dry_var = ctk.BooleanVar(value=bool(self.cfg.get("dry_run", True)))
        tf_label, tf_custom = ui_parts(str(st.get("timeframe", "15")))
        trail_label, trail_custom = ui_parts(str(st.get("trail_timeframe", "30")))
        self.tf_var = ctk.StringVar(value=tf_label)
        self.tf_custom_var = ctk.StringVar(value=tf_custom)
        self.trail_var = ctk.StringVar(value=trail_label)
        self.trail_custom_var = ctk.StringVar(value=trail_custom)
        self.symbols_var = ctk.StringVar(value=",".join(str(x) for x in st.get("symbols", ["bu", "c"])))
        # 关注列表。下拉框选中的那一个，就是运行页顶部显示的当前合约。
        self._watch_symbols = self._parse_list(self.symbols_var.get())
        self.watch_choice_var = ctk.StringVar(
            value=self._watch_symbols[0] if self._watch_symbols else "（还没有）"
        )
        self.watch_add_var = ctk.StringVar(value="")
        self._pick_baseline: dict[str, Any] | None = None
        self._pick_running = False
        self._pick_cancel = threading.Event()
        self.volume_var = ctk.StringVar(value=str(trade.get("volume", 1)))
        self.max_slip_var = ctk.StringVar(value=str(trade.get("max_entry_slip_atr", 0.5)))
        self.max_slip_points_var = ctk.StringVar(value=str(trade.get("max_entry_slip_points", 0)))
        self.sizing_mode_var = ctk.StringVar(value=str(risk.get("sizing_mode", "risk_pct")))
        self.capital_var = ctk.StringVar(value=str(risk.get("capital", 1000000)))
        self.risk_pct_var = ctk.StringVar(value=str(risk.get("risk_per_trade_pct", 0.5)))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

        self.margin_usage_pct_var = ctk.StringVar(
            value=str(risk.get("margin_usage_pct", DEFAULT_MARGIN_USAGE_PCT))
        )
        self.margin_var = ctk.StringVar(value=str(risk.get("margin_per_lot", 0) or 0))
        from src.risk.dynamic import DynamicExposure

        _dyn = DynamicExposure.from_config(self.cfg)
        self.dyn_enabled_var = ctk.BooleanVar(value=bool(_dyn.enabled))
        self.dyn_dd_half_var = ctk.StringVar(value=str(_dyn.dd_half_pct))
        self.dyn_dd_pause_var = ctk.StringVar(value=str(_dyn.dd_pause_pct))
        self.dyn_atr_half_var = ctk.StringVar(value=str(_dyn.atr_half_mult))
        self.dyn_atr_pause_var = ctk.StringVar(value=str(_dyn.atr_pause_mult))
        # 关注合约+保证金只认总表；margin_map_var 给回测页抄「当前品种保证金」用。
        from src.risk.universe_table import DEFAULT_REL_PATH, summarize_universe

        self.margin_table_path_var = ctk.StringVar(
            value=str(risk.get("universe_table") or risk.get("margin_table") or DEFAULT_REL_PATH)
        )
        self.margin_map_var = ctk.StringVar(
            value=summarize_universe(
                list(st.get("symbols") or []),
                risk.get("contracts") or {},
            )
        )
        self.scan_var = ctk.StringVar(value=str(sch.get("scan_interval_sec", 30)))
        # 公共交易时段：界面显示中文，保存时换回 all/day/night。只拦开新仓。
        from src.strategy.session_scope import scope_from_cfg, scope_label

        self.session_scope_var = ctk.StringVar(value=scope_label(scope_from_cfg(self.cfg)))
        self.eth_min_var = ctk.StringVar(value=str(_eth_minutes_from_strategy(st)))
        # 各策略必要旋钮。只在选对应策略时真正用到，保存进各自小节。
        body_box = st.get("daily_body") or {}
        trend_box = st.get("trend_follow") or {}
        st_box = st.get("supertrend") or {}
        dual_box = st.get("dual_ma") or {}
        # 箱沿外再让几跳：多单箱底外、空单箱顶外。默认 1。
        self.body_stop_ticks_var = ctk.StringVar(value=str(body_box.get("stop_extra_ticks", 1)))
        # 这一段开盘跳空超过几跳就不开；0=关闭。燃油每跳 1 点，默认 50 就是 50 点。
        self.body_skip_gap_var = ctk.StringVar(value=str(body_box.get("skip_gap_ticks", 50)))
        # 开盘箱体最高-最低超过几跳就不开；0=关闭。箱体越宽，箱沿止损离进场价越远。
        self.body_max_box_var = ctk.StringVar(value=str(body_box.get("max_box_ticks", 100)))
        # 急破箱体不追：收盘离破位超过几跳就先等回抽；0=关闭。
        self.body_max_chase_var = ctk.StringVar(value=str(body_box.get("max_chase_ticks", 20)))
        # 回抽时看哪条 EMA；以及收盘超过它几跳才算有效打穿。
        self.body_ema_period_var = ctk.StringVar(value=str(body_box.get("ema_period", 20)))
        self.body_ema_buffer_var = ctk.StringVar(value=str(body_box.get("ema_buffer_ticks", 2)))
        # 开仓截止钟点：过了这个钟点，日盘/夜盘这一段不再开新仓。默认沿用旧版 10:30 / 22:15。
        self.body_day_cutoff_var = ctk.StringVar(value=str(body_box.get("day_cutoff", "10:30")))
        self.body_night_cutoff_var = ctk.StringVar(value=str(body_box.get("night_cutoff", "22:15")))
        self.body_eth2_var = ctk.StringVar(value=str(body_box.get("eth_minutes2", 30)))
        from src.strategy.market_state import range_params_for_daily_body

        body_rp = range_params_for_daily_body(self.cfg)
        self.body_filter_enabled_var = ctk.BooleanVar(value=bool(body_rp.enabled))
        self.body_filter_adx_period_var = ctk.StringVar(value=str(body_rp.adx_period))
        self.body_filter_atr_period_var = ctk.StringVar(value=str(body_rp.atr_period))
        self.body_filter_lookback_var = ctk.StringVar(value=str(body_rp.lookback))
        self.body_filter_adx_upper_var = ctk.StringVar(value=f"{body_rp.adx_upper:g}")
        body_atr_high_ui = 0 if body_rp.atr_high_pct >= 0.999 else body_rp.atr_high_pct * 100
        self.body_filter_atr_high_var = ctk.StringVar(value=f"{body_atr_high_ui:g}")
        # 止损触发方式（全策略共用）：触及即止损（实时看影线）/ 收盘价确认（等这根K线收完）。
        from src.strategy.types import (
            entry_mode_from_cfg,
            entry_mode_label,
            stop_mode_from_cfg,
            stop_mode_label,
        )

        self.stop_mode_var = ctk.StringVar(value=stop_mode_label(stop_mode_from_cfg(self.cfg)))
        self.entry_mode_var = ctk.StringVar(value=entry_mode_label(entry_mode_from_cfg(self.cfg)))
        # 通用止损（独立于策略自身止损）：关闭 / SAR / 前高低点 / ATR 吊灯。
        from src.strategy.generic_stop import generic_stop_from_cfg, generic_stop_label

        gs_cfg = generic_stop_from_cfg(self.cfg)
        self.generic_stop_var = ctk.StringVar(value=generic_stop_label(gs_cfg.kind))
        self.generic_stop_lookback_var = ctk.StringVar(value=str(gs_cfg.lookback))
        self.generic_stop_sar_start_var = ctk.StringVar(value=f"{gs_cfg.sar_af_start:g}")
        self.generic_stop_sar_step_var = ctk.StringVar(value=f"{gs_cfg.sar_af_step:g}")
        self.generic_stop_sar_max_var = ctk.StringVar(value=f"{gs_cfg.sar_af_max:g}")
        self.generic_stop_atr_period_var = ctk.StringVar(value=str(gs_cfg.atr_period))
        self.generic_stop_atr_mult_var = ctk.StringVar(value=f"{gs_cfg.atr_mult:g}")
        self.generic_stop_be_atr_var = ctk.StringVar(value=f"{gs_cfg.be_atr_mult:g}")
        # 趋势策略各自的行情过滤（ADX 没趋势或 ATR 波动压缩时不开新仓）。
        self._init_trend_filter_vars()
        self.trend_entry_bars_var = ctk.StringVar(value=str(trend_box.get("entry_bars", 36)))
        self.trend_atr_mult_var = ctk.StringVar(value=str(trend_box.get("atr_mult", 2.0)))
        self.st_atr_period_var = ctk.StringVar(value=str(st_box.get("atr_period", 10)))
        self.st_multiplier_var = ctk.StringVar(value=str(st_box.get("multiplier", 3.0)))
        self.dual_short_var = ctk.StringVar(value=str(dual_box.get("short_period", 20)))
        self.dual_long_var = ctk.StringVar(value=str(dual_box.get("long_period", 60)))
        self.dual_reverse_var = ctk.BooleanVar(value=bool(dual_box.get("reverse_on_cross", True)))
        boll_box = st.get("bollinger") or {}
        self.boll_window_var = ctk.StringVar(value=str(boll_box.get("boll_window", 20)))
        self.boll_dev_var = ctk.StringVar(value=str(boll_box.get("boll_dev", 2.0)))
        self.boll_atr_mult_var = ctk.StringVar(value=str(boll_box.get("atr_stop_mult", 1.5)))
        sar_box = st.get("sar_trend") or {}
        self.sar_ma_filter_var = ctk.StringVar(value=str(sar_box.get("ma_filter", 20)))
        self.sar_af_step_var = ctk.StringVar(value=str(sar_box.get("af_step", 0.02)))
        multi_box = st.get("multi_factor") or {}
        self.multi_threshold_var = ctk.StringVar(value=str(multi_box.get("threshold", 0.6)))
        self.multi_ma_short_var = ctk.StringVar(value=str(multi_box.get("ma_short", 20)))
        self.multi_ma_long_var = ctk.StringVar(value=str(multi_box.get("ma_long", 60)))
        self.multi_channel_var = ctk.StringVar(value=str(multi_box.get("channel", 20)))
        self.multi_atr_mult_var = ctk.StringVar(value=str(multi_box.get("atr_stop_mult", 2.0)))
        self.multi_disagree_var = ctk.StringVar(value=str(multi_box.get("disagree_max", 0.7)))
        self.multi_use_sar_var = ctk.BooleanVar(value=bool(multi_box.get("use_sar", True)))
        vote_box = st.get("multi_vote") or {}
        hybrid_box = st.get("multi_hybrid") or {}
        adapt_box = st.get("multi_factor_adapt") or {}
        self.adapt_enabled_var = ctk.BooleanVar(value=bool(adapt_box.get("adapt_enabled", True)))
        self.adapt_lookback_var = ctk.StringVar(value=str(adapt_box.get("lookback_trades", 20)))
        self.adapt_min_w_var = ctk.StringVar(value=str(adapt_box.get("min_weight", 0.05)))
        self.adapt_max_w_var = ctk.StringVar(value=str(adapt_box.get("max_weight", 0.40)))
        self.adapt_mix_var = ctk.StringVar(value=str(adapt_box.get("recent_mix", 0.30)))
        self.adapt_step_var = ctk.StringVar(value=str(adapt_box.get("max_step", 0.03)))
        self.multi_min_votes_var = ctk.StringVar(
            value=str(vote_box.get("min_votes", hybrid_box.get("min_votes", 2)))
        )
        self.multi_vote_diff_var = ctk.StringVar(
            value=str(vote_box.get("min_vote_diff", hybrid_box.get("min_vote_diff", 2)))
        )
        self.hybrid_min_ratio_var = ctk.StringVar(
            value=str(hybrid_box.get("min_position_ratio", 0.5))
        )
        self.hybrid_max_ratio_var = ctk.StringVar(
            value=str(hybrid_box.get("max_position_ratio", 1.0))
        )
        self._init_cta_filter_vars(multi_box, vote_box, hybrid_box, adapt_box)
        swing_box = st.get("swing_trend") or {}
        from src.strategy.swing_trend import DEFAULT_SCALE, SCALE_LABELS, resolve_scale_pair, scale_label

        scale_code, _trend_tf, _sig_tf = resolve_scale_pair(swing_box if isinstance(swing_box, dict) else {})
        self._swing_scale_by_label = dict(SCALE_LABELS)
        self._swing_label_by_scale = {code: label for code, label in SCALE_LABELS.items()}
        self.swing_scale_var = ctk.StringVar(
            value=self._swing_label_by_scale.get(scale_code, scale_label(DEFAULT_SCALE))
        )
        self.swing_daily_ma_var = ctk.StringVar(value=str(swing_box.get("daily_ma", 20)))
        self.swing_unit_var = ctk.StringVar(value=str(swing_box.get("unit_volume", 1)))
        self.swing_max_adds_var = ctk.StringVar(value=str(swing_box.get("max_adds", 2)))
        self.swing_add_atr_var = ctk.StringVar(value=str(swing_box.get("add_min_atr", 1.0)))
        self.swing_reduce_atr_var = ctk.StringVar(value=str(swing_box.get("reduce_extend_atr", 2.5)))
        jesse_box = st.get("jesse_casper") or {}
        self.jesse_lookback_var = ctk.StringVar(value=str(jesse_box.get("lookback_liq", 20)))
        self.jesse_vol_mult_var = ctk.StringVar(value=str(jesse_box.get("vol_mult", 1.2)))
        self.jesse_min_rr_var = ctk.StringVar(value=str(jesse_box.get("min_rr", 3.0)))
        self.jesse_tick_var = ctk.StringVar(value=str(jesse_box.get("price_tick", 1.0)))
        self.jesse_use_va_var = ctk.BooleanVar(value=bool(jesse_box.get("use_va", True)))
        self.jesse_use_kill_var = ctk.BooleanVar(value=bool(jesse_box.get("use_kill_zone", True)))
        self.jesse_use_halt_var = ctk.BooleanVar(value=bool(jesse_box.get("use_daily_halt", True)))
        self.flatten_var = ctk.StringVar(
            value=",".join(str(x) for x in st.get("flatten_times", ["14:55", "22:55"]))
        )
        # 默认开：到点强平。关掉后仓可以留过收盘，用来对比隔夜表现。
        self.flatten_at_close_var = ctk.BooleanVar(value=bool(st.get("flatten_at_close", True)))
        # 收盘后关机（默认关；有夜盘会等到夜盘结束）
        rt = self.cfg.get("runtime") or {}
        self.auto_shutdown_var = ctk.BooleanVar(value=bool(rt.get("auto_shutdown_after_close", False)))

        self.broker_var = ctk.StringVar(value=str(acc.get("broker", "")))
        self.channel_var = ctk.StringVar(value=str(acc.get("channel", "")))
        self.user_var = ctk.StringVar(value=str(acc.get("user_id", "")))
        self.pwd_env_var = ctk.StringVar(value=str(acc.get("password_env", "FUTURES_PASSWORD")))
        self.password_var = ctk.StringVar(value=str(secrets.get("password", "") or acc.get("resolved_password", "")))
        # 天勤行情要单独登录。账号写进 config.yaml 的 data.tq.user，密码写进 secrets.local.yaml。
        tq_cfg = (self.cfg.get("data") or {}).get("tq") or {}
        self.tq_user_var = ctk.StringVar(value=str(tq_cfg.get("user") or secrets.get("tq_user", "") or ""))
        self.tq_password_var = ctk.StringVar(
            value=str(secrets.get("tq_password", "") or tq_cfg.get("password", "") or "")
        )
        self.captcha_var = ctk.StringVar(value=str(acc.get("captcha_mode", "pause")))
        # 配置里 skip_auto_login=true 表示不自动填密码。界面用「自动登录」开关，打开才去点登录。
        self.auto_login_var = ctk.BooleanVar(value=not bool(acc.get("skip_auto_login", True)))

        self.app_path_var = ctk.StringVar(value=str(app.get("path", "")))
        self.app_title_var = ctk.StringVar(value=str(app.get("window_title", "期货通")))
        self.startup_wait_var = ctk.StringVar(value=str(app.get("startup_wait_sec", 15)))

        self.max_loss_var = ctk.StringVar(value=str(risk.get("max_daily_loss", 1000)))
        self.min_interval_var = ctk.StringVar(value=str(risk.get("min_order_interval_sec", 60)))
        self.max_orders_var = ctk.StringVar(value=str(risk.get("max_orders_per_day", 20)))

        # —— 回测 / 优化页专用（与实盘配置分开选，避免改乱挂机参数）——
        bt = self.cfg.get("backtest") or {}
        default_sym = (st.get("symbols") or ["bu2612"])[0]
        self.bt_engine_var = ctk.StringVar(value=_ui_engine(st.get("engine", "daily_body")))
        self.bt_source_var = ctk.StringVar(value=quote_label)
        self.bt_symbol_var = ctk.StringVar(value=str(default_sym))
        self.bt_tf_var = ctk.StringVar(value=tf_label)
        self.bt_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.bt_trail_var = ctk.StringVar(value=trail_label)
        self.bt_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.bt_range_var = ctk.StringVar(value=str(bt.get("range_preset", "全部可用")))
        # 配置里没写开始日期时，默认当年1月1日。今年打开就是 2026-01-01。
        default_start = self._initial_start_date(bt.get("start_date", ""))
        self.bt_start_var = ctk.StringVar(value=default_start)
        self.bt_end_var = ctk.StringVar(value=str(bt.get("end_date", "")))
        ths_chart = ((self.cfg.get("data") or {}).get("ths_chart") or {})
        self.bt_ths_bars_var = ctk.StringVar(value=str(ths_chart.get("max_bars", 800)))
        # 成交近似：下一根开盘成交与滑点跳数，只影响回测。配置页和历史回测页共用这一组。
        self.bt_fill_next_open_var = ctk.BooleanVar(value=bool(bt.get("fill_on_next_open", True)))
        self.bt_slippage_var = ctk.StringVar(value=str(bt.get("slippage_ticks", 1)))
        # 回测页自己的资金限制，默认抄配置页，改这里不会写回实盘配置。
        self.bt_sizing_var = ctk.StringVar(value=str(risk.get("sizing_mode", "risk_pct")))
        self.bt_capital_var = ctk.StringVar(value=str(risk.get("capital", 14000)))
        self.bt_risk_pct_var = ctk.StringVar(value=str(risk.get("risk_per_trade_pct", 0.5)))
        self.bt_volume_var = ctk.StringVar(value=str(trade.get("volume", 1)))
        self.bt_min_vol_var = ctk.StringVar(value=str(risk.get("min_volume", 1)))
        self.bt_max_loss_var = ctk.StringVar(value=str(risk.get("max_daily_loss", 500)))
        self.bt_interval_var = ctk.StringVar(value=str(risk.get("min_order_interval_sec", 60)))
        self.bt_max_orders_var = ctk.StringVar(value=str(risk.get("max_orders_per_day", 20)))
        self.bt_margin_var = ctk.StringVar(value=str(risk.get("margin_per_lot", 0) or 0))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT as _DEF_MGN_PCT

        self.bt_margin_usage_pct_var = ctk.StringVar(
            value=str(risk.get("margin_usage_pct", _DEF_MGN_PCT))
        )
        # 默认开着，和实盘一致。关掉后固定 1 手，用来看策略本身。配置页没有这个开关。
        self.bt_limits_on_var = ctk.BooleanVar(value=True)
        # 打开程序时先跟配置页一致。之后可以在回测页单独改，点「按配置页重新填入」再抄回来。
        self.bt_flatten_var = ctk.BooleanVar(value=bool(st.get("flatten_at_close", True)))
        # 按主力合约表的时段自动换月。默认开，让历史回测跟实盘一样换月日仍用旧合约、次日切新主力。
        self.bt_rollover_var = ctk.BooleanVar(value=bool(bt.get("rollover_main", True)))
        # 换月分段的资金口径。默认每段同一份起始资金，便于和单合约回测对照；
        # 打开后上一段结束的权益接到下一段（真实账户口径）。
        self.bt_rollover_chain_var = ctk.BooleanVar(value=False)
        # 交叉测试不再跟某一次回测比。周期和手数用本页自己的数字，策略会把每一套都测。
        self.pk_engine_var = ctk.StringVar(value=self.bt_engine_var.get())
        # 交叉测试自己的行情来源。打开时跟配置页一样，之后可以单独改。
        self.pk_source_var = ctk.StringVar(value=quote_label)
        self.pk_tf_var = ctk.StringVar(value=tf_label)
        self.pk_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.pk_trail_var = ctk.StringVar(value=trail_label)
        self.pk_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.pk_range_var = ctk.StringVar(value=self.bt_range_var.get())
        self.pk_start_var = ctk.StringVar(value=default_start)
        self.pk_end_var = ctk.StringVar(value=self.bt_end_var.get())
        self.pk_flatten_var = ctk.BooleanVar(value=bool(self.bt_flatten_var.get()))
        self.pk_limits_on_var = ctk.BooleanVar(value=True)
        self.pk_sizing_var = ctk.StringVar(value="fixed")
        self.pk_capital_var = ctk.StringVar(value=self.bt_capital_var.get())
        self.pk_risk_pct_var = ctk.StringVar(value=self.bt_risk_pct_var.get())
        self.pk_volume_var = ctk.StringVar(value=self.bt_volume_var.get())
        self.pk_min_vol_var = ctk.StringVar(value=self.bt_min_vol_var.get())
        self.pk_max_loss_var = ctk.StringVar(value=self.bt_max_loss_var.get())
        self.pk_interval_var = ctk.StringVar(value=self.bt_interval_var.get())
        self.pk_max_orders_var = ctk.StringVar(value=self.bt_max_orders_var.get())
        self.pk_margin_var = ctk.StringVar(value="0")

        self.opt_engine_var = ctk.StringVar(value=_ui_engine(st.get("engine", "daily_body")))
        self.opt_source_var = ctk.StringVar(value=quote_label)
        self.opt_symbol_var = ctk.StringVar(value=str(default_sym))
        self.opt_tf_var = ctk.StringVar(value=tf_label)
        self.opt_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.opt_trail_var = ctk.StringVar(value=trail_label)
        self.opt_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.opt_range_var = ctk.StringVar(value=str(bt.get("range_preset", "全部可用")))
        self.opt_start_var = ctk.StringVar(value=default_start)
        self.opt_end_var = ctk.StringVar(value=str(bt.get("end_date", "")))
        self.opt_trials_var = ctk.StringVar(value=str(bt.get("max_trials", 80)))
        self.opt_depth_var = ctk.StringVar(value=str(bt.get("optimize_depth", "标准")))
        # 检验比例默认 30%，和优化器里的默认切断一致。可在 10～50 之间手调。
        self.opt_holdout_pct_var = ctk.StringVar(value=str(bt.get("optimize_holdout_pct", 30)))
        self.opt_mode_var = ctk.StringVar(value=str(bt.get("optimize_mode", "一次切断")))
        self.opt_walk_steps_var = ctk.StringVar(value=str(bt.get("optimize_walk_steps", 3)))
        # 写入第几组：1=训练最优；填其它编号会先按检验段重新复核再写。
        self.opt_write_index_var = ctk.StringVar(value="1")
        # 打开＝衰减重跑：搜完对照正在用的参数，默认不写配置。
        self.opt_compare_var = ctk.BooleanVar(value=False)
        # 写入时额外留一份整页配置存档（配置页可一键应用）。和下面参数版本不是同一份。
        self.opt_save_preset_var = ctk.BooleanVar(value=True)
        self._last_opt_params: dict = {}
        self._last_opt_engine: str = ""
        # 后一段验收过了才是 True。没过时参数仍显示在结果里，但不能写入配置。
        self._last_opt_accepted: bool = False
        self._last_opt_reason: str = ""
        self._last_opt_start: str = ""
        self._last_opt_end: str = ""
        # 这次优化的全部打分行和运行设置，供「写入第几组」复核用。
        self._last_opt_trials: list[dict] = []
        self._last_opt_ctx: dict = {}
        # 对照结果：打开「对照正在用的参数」跑完后才有。全面更好时写入按钮改成切实盘。
        self._last_evolve_better: bool = False
        self._last_evolve_reason: str = ""
        self._last_opt_compared: bool = False

        # 复盘页：日期默认今天；策略对照默认跟配置里正在用的那一套，也可以改成「只看盘面」
        from datetime import date as _date

        from src.review.replay import label_for_engine

        self.rv_date_var = ctk.StringVar(value=_date.today().isoformat())
        self.rv_symbol_var = ctk.StringVar(value=str(default_sym))
        self.rv_tf_var = ctk.StringVar(value=tf_label)
        self.rv_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.rv_trail_var = ctk.StringVar(value=trail_label)
        self.rv_trail_custom_var = ctk.StringVar(value=trail_custom)
        self.rv_scope_var = ctk.StringVar(value="前夜盘+日盘")
        self.rv_engine_var = ctk.StringVar(
            value=label_for_engine(_ui_engine(st.get("engine", "daily_body")))
        )

        # 账户分析：初始资金默认跟配置里的账户权益一样。出入金和结算是你手工填的金额。
        self.ac_capital_var = ctk.StringVar(value=str(risk.get("capital", 100000)))
        self.ac_cash_var = ctk.StringVar(value="")
        self.ac_settle_var = ctk.StringVar(value="")
        self.ac_span_var = ctk.StringVar(value="全部")
        self._ac_chart = "权益"
        self._ac_cal = futures_trading_day().replace(day=1)
        self._ac_report = None
        self._signal_book_mtime = 0.0
        # 资金表控件还没建好时，刷新说明不能去读这个字典（Tk 会当成窗口属性报错）。
        self._ac_fund_values: dict[str, Any] = {}
        # 账户分析换账时，下拉框改回去不要再弹一次确认。
        self._account_switch_busy = False
        from src.account.workspace import display_label, load_active_id, profile_of

        self.ac_account_var = ctk.StringVar(value=display_label(load_active_id(self.root_dir), self.root_dir))

        # 规律候选：只扫统计关系，不下单。日期默认跟回测页同一套，改这里不会改回测。
        self.rs_source_var = ctk.StringVar(value=quote_label)
        self.rs_symbol_var = ctk.StringVar(value=str(default_sym))
        self.rs_tf_var = ctk.StringVar(value=tf_label)
        self.rs_tf_custom_var = ctk.StringVar(value=tf_custom)
        self.rs_range_var = ctk.StringVar(value=str(bt.get("range_preset", "全部可用")))
        self.rs_start_var = ctk.StringVar(value=default_start)
        self.rs_end_var = ctk.StringVar(value=str(bt.get("end_date", "")))
        self.rs_scope_var = ctk.StringVar(value="扫关注名单全部")
        self.rs_cross_var = ctk.BooleanVar(value=True)
        self._rs_busy = False
        self._rs_check_vars: dict[str, ctk.BooleanVar] = {}

    # ---------- 搭建 ----------
    def _build(self) -> None:
        hero = ctk.CTkFrame(self, fg_color=T.BG)
        hero.pack(fill="x", padx=28, pady=(20, 4))
        ctk.CTkLabel(
            hero, text=f"Auto-Trading {__version__}", font=T.FONT_DISPLAY, text_color=T.ACCENT, anchor="w"
        ).pack(anchor="w")
        # 副标题按当前券商、关注品种和模拟开关生成，不再写死某几个品种。
        self.hero_sub = ctk.CTkLabel(
            hero,
            text="-",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        )
        self.hero_sub.pack(anchor="w", pady=(2, 0))

        # 状态卡。五列按固定比例分同一行：窗口变宽时一起变，不会再被某一句长文字单独撑开。
        self.status_row = ctk.CTkFrame(self, fg_color=T.BG)
        self.status_row.pack(fill="x", padx=28, pady=(10, 6))
        self._status_card_frames: list[ctk.CTkFrame] = []
        # 执行通道那句最长，只比旁边宽一档。其余四张一样宽，避免品种和持仓缩成一条。
        status_weights = (5, 5, 6, 5, 5)
        for i, weight in enumerate(status_weights):
            self.status_row.grid_columnconfigure(i, weight=weight, uniform="status_cards", minsize=140)
        self.card_mode = self._make_stat_card(self.status_row, 0, "运行状态", "待命")
        self.card_engine = self._make_stat_card(self.status_row, 1, "策略引擎", "-")
        self.card_exec = self._make_stat_card(self.status_row, 2, "执行通道", "-")
        self.card_symbols = self._make_stat_card(self.status_row, 3, "关注品种", "-")
        self.card_position = self._make_stat_card(self.status_row, 4, "程序持仓", "空仓")
        # 持仓句子可能写成「bu2612 空1手 @4662 止损4700」，用小一号字，避免一有仓就把整排撑高。
        self.card_position.configure(font=T.FONT_BODY)
        self.status_row.bind("<Configure>", self._fit_status_card_text)
        # 告警 / 健康 / 动态减仓：三句经常很长，不挤成一行。行距压紧，没字的行收起来。
        self.status_notes = ctk.CTkFrame(self, fg_color="transparent")
        self.status_notes.pack(fill="x", padx=28, pady=(0, 4))
        note_kw = {
            "font": T.FONT_SMALL,
            "anchor": "w",
            "justify": "left",
            "height": 18,
        }
        self.alert_line = ctk.CTkLabel(self.status_notes, text="", text_color=T.DANGER, **note_kw)
        self.health_line = ctk.CTkLabel(self.status_notes, text="", text_color=T.TEXT_DIM, **note_kw)
        self.dyn_line = ctk.CTkLabel(self.status_notes, text="", text_color=T.TEXT_DIM, **note_kw)
        self.status_notes.bind("<Configure>", self._fit_status_note_wrap)

        self.tabs = ctk.CTkTabview(
            self,
            fg_color=T.BG_PANEL,
            segmented_button_fg_color=T.BG_ELEVATED,
            segmented_button_selected_color=T.ACCENT,
            segmented_button_selected_hover_color=T.ACCENT_HOVER,
            segmented_button_unselected_color=T.BG_ELEVATED,
            segmented_button_unselected_hover_color=T.BORDER,
            text_color=T.TEXT,
            corner_radius=16,
        )
        self.tabs.pack(fill="both", expand=True, padx=28, pady=(6, 22))
        self.tabs.add("运行")
        self.tabs.add("配置")
        self.tabs.add("历史回测")
        self.tabs.add("交叉测试")
        self.tabs.add("策略优化")
        self.tabs.add("复盘")
        self.tabs.add("账户分析")
        self.tabs.add("规律候选")

        self._build_run_tab(self.tabs.tab("运行"))
        self._build_config_tab(self.tabs.tab("配置"))
        self._build_backtest_tab(self.tabs.tab("历史回测"))
        self._build_picker_tab(self.tabs.tab("交叉测试"))
        self._build_optimize_tab(self.tabs.tab("策略优化"))
        self._build_review_tab(self.tabs.tab("复盘"))
        self._build_account_tab(self.tabs.tab("账户分析"))
        self._build_research_tab(self.tabs.tab("规律候选"))
        self._load_saved_picks()
        # 打开程序先看一眼当前关注合约是不是主力，非主力时在配置页亮提示。
        self.after(300, self._refresh_main_contract_hint)

    def _build_research_tab(self, tab: ctk.CTkFrame) -> None:
        """
        规律候选：在历史 K 线上扫因子和后面涨跌的关系，过关的写进候选池。

        这里不下单。勾选只表示你觉得这条逻辑说得通，不会改现在哪根 K 线开仓。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="规律候选", font=T.FONT_TITLE, text_color=T.TEXT).pack(
            anchor="w", padx=4, pady=(4, 2)
        )
        ctk.CTkLabel(
            left,
            text="程序在一份写清经济学逻辑的因子清单上，看它们和后面几根涨跌是否稳。两段时间都显著才进池，再看分段里大部分时间是否同向。说明书开头会写更像趋势、突破、震荡还是波段，只是倾向，不会自动换策略，也不会下单。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        from src.data.quote_check import SOURCE_LABELS

        self._option_row(box, "行情数据源", self.rs_source_var, list(SOURCE_LABELS), width=160)
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.rs_symbol_var.get() not in symbol_values:
            self.rs_symbol_var.set(symbol_values[0])
        self.rs_symbol_menu = self._option_row(box, "品种", self.rs_symbol_var, symbol_values, width=160)
        self._option_row(
            box,
            "扫哪些",
            self.rs_scope_var,
            ["只扫当前这个", "扫关注名单全部"],
            width=180,
        )
        self._timeframe_row(box, "K线周期", self.rs_tf_var, self.rs_tf_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.rs_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.rs_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.rs_end_var, hint="点日历；清空=到最新")
        ctk.CTkCheckBox(
            box,
            text="多个品种时，至少两个合约都过关才进池",
            variable=self.rs_cross_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(6, 4))

        ctk.CTkButton(
            left,
            text="开始扫描规律",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_research_scan,
        ).pack(fill="x", padx=4, pady=(12, 6))
        ctk.CTkButton(
            left,
            text="保存勾选（仍不下单）",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_save_research_picks,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="过关条目（勾选=我认可这条逻辑，还要另写成开平规则才能当策略）",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        self.rs_pick_frame = ctk.CTkScrollableFrame(left, fg_color=T.BG_ELEVATED, height=220)
        self.rs_pick_frame.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_PANEL, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="规律说明书", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.rs_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.rs_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._refresh_research_picks()

    def _make_stat_card(self, parent: ctk.CTkFrame, col: int, title: str, value: str) -> ctk.CTkLabel:
        """做顶部的一张状态卡。宽度由列的比例决定，文字在卡片里面换行，不把旁边的卡片挤窄。"""
        # 宽高只给一个很小的起点。真正的宽由上面的列比例拉开，高由里面的字撑开。
        # 不写的话，卡片会按默认 200 像素去抢宽度，窗口一窄就会被挤变形。
        card = ctk.CTkFrame(
            parent,
            fg_color=T.BG_PANEL,
            corner_radius=14,
            border_width=1,
            border_color=T.BORDER,
            width=40,
            height=36,
        )
        # nsew：五张卡同一行里一样高，某一张换成两行字时，旁边的卡不会显得矮一截。
        card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0 if col == 4 else 6))
        # 宽高写小。不写的话这一行会按默认 200 像素把整张状态卡撑高。
        title_row = ctk.CTkFrame(card, fg_color="transparent", width=20, height=16)
        title_row.pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(title_row, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(side="left")
        # 日期放在「运行状态」这张卡的右上角。这张卡本来就在说现在，标题又短，不会把心跳时间挤成两行。
        if col == 0:
            self.card_date = ctk.CTkLabel(
                title_row, text=date.today().isoformat(), font=T.FONT_SMALL, text_color=T.TEXT_DIM,
            )
            self.card_date.pack(side="right")
        val = ctk.CTkLabel(
            card,
            text=value,
            font=T.FONT_TITLE,
            text_color=T.TEXT,
            anchor="w",
            justify="left",
            width=40,
            wraplength=160,
        )
        val.pack(anchor="w", fill="x", padx=14, pady=(4, 14))
        self._status_card_frames.append(card)
        return val

    def _fit_status_card_text(self, _event: object = None) -> None:
        """窗口变宽或变窄时，让每张卡里的字按卡片实际宽度换行。"""
        labels = (
            self.card_mode,
            self.card_engine,
            self.card_exec,
            self.card_symbols,
            self.card_position,
        )
        for card, label in zip(self._status_card_frames, labels):
            width = card.winfo_width()
            if width < 80:
                continue
            # 左右各留 14 像素。界面有屏幕放大时，换行宽度要先换回未放大的像素，否则字会画出边框。
            inner = max(96, width - 28)
            wrap = int(round(card._reverse_widget_scaling(inner)))
            current = int(label.cget("wraplength") or 0)
            if abs(current - wrap) <= 4:
                continue
            label.configure(wraplength=wrap)

    def _build_run_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=2)
        tab.grid_columnconfigure(1, weight=3)
        tab.grid_rowconfigure(0, weight=1)

        # 可滚动：窗口较矮时，手工单录入下面的导出和保存仍能滚出来，不会被裁掉。
        left = ctk.CTkScrollableFrame(
            tab,
            fg_color="transparent",
            border_width=0,
            scrollbar_fg_color=T.BG,
            scrollbar_button_color=T.BORDER,
            scrollbar_button_hover_color=T.TEXT_DIM,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="控制台", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            left,
            text="当前关注的品种看顶部。要换合约请到「配置」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=8, pady=(0, 8))

        # 只留顶部状态栏没有的内容：信号周期、止损周期。
        route = ctk.CTkFrame(left, fg_color=T.BG_ELEVATED, corner_radius=10, border_width=1, border_color=T.BORDER)
        route.pack(fill="x", padx=8, pady=(0, 8))
        self.route_summary = ctk.CTkLabel(
            route,
            text="-",
            font=T.FONT_SMALL,
            text_color=T.TEXT,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self.route_summary.pack(anchor="w", fill="x", padx=12, pady=(4, 4))

        form = ctk.CTkFrame(left, fg_color="transparent")
        form.pack(fill="x", padx=8)
        # 模拟开关保留在运行页：启动前最容易忘记，单独放这里更安全
        dry = ctk.CTkFrame(form, fg_color="transparent")
        dry.pack(fill="x", pady=(4, 6))
        ctk.CTkLabel(dry, text="模拟模式 dry_run", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w").pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkSwitch(dry, text="", variable=self.dry_var, progress_color=T.OK, fg_color=T.BORDER).pack(side="left")

        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(6, 8))
        # 启动常驻 / 跑着时变成停止常驻（结束扫描循环）。暂停报单不停扫描。
        self.btn_start = ctk.CTkButton(
            btn_row,
            text="启动常驻",
            height=38,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            text_color_disabled=T.ACCENT_TEXT_DISABLED,
            command=self._on_start,
        )
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.btn_stop = ctk.CTkButton(
            btn_row,
            text="暂停报单",
            height=38,
            fg_color=T.DANGER_MUTED,
            hover_color=T.DANGER_HOVER,
            text_color=T.DANGER_TEXT,
            text_color_disabled=T.DANGER_TEXT_DISABLED,
            command=self._on_pause_orders,
            state="disabled",
        )
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(6, 0))
        # 对账确认：程序若在「点击下单」和「记账」之间崩溃，下次启动会熔断等人工核对。
        self.btn_confirm = ctk.CTkButton(
            btn_row,
            text="对账确认",
            width=86,
            height=38,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.TEXT_DIM,
            command=self._on_confirm_positions,
        )
        self.btn_confirm.pack(side="left", padx=(10, 0))


        ctk.CTkLabel(
            left,
            text="「暂停报单」后扫描还在跑，开平仓、止损、清仓都不会发出去。要结束常驻请点「停止常驻」。交易台只给人测开平撤；③④再启动常驻由策略接管。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            left,
            text="CTP 上线四步（手工和策略同一套接口）",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        ).pack(anchor="w", padx=8, pady=(4, 0))
        self.go_live_menu = ctk.CTkOptionMenu(
            left,
            variable=self.go_live_var,
            values=self.go_live_choices,
            command=lambda _v: self._on_go_live_change(),
            width=360,
            height=32,
        )
        self.go_live_menu.pack(fill="x", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            left,
            textvariable=self.go_live_hint_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(4, 4))
        stage_btns = ctk.CTkFrame(left, fg_color="transparent")
        stage_btns.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(
            stage_btns,
            text="打开交易台",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_ctp_probe,
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            stage_btns,
            text="本阶段完成",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_go_live_done,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        # 试跑一轮 / 点击下单 / 完整自检收进一个入口，避免三个按钮挤在一起。
        self.btn_test_tools = ctk.CTkButton(
            left,
            text="测试工具",
            height=36,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            text_color_disabled=T.TEXT_DIM,
            command=self._on_test_tools,
        )
        self.btn_test_tools.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="日常先「试跑一轮」；要验坐标用「点击下单」；怀疑程序坏了再「完整自检」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        ctk.CTkButton(
            left,
            text="手工单录入",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_manual_trade,
        ).pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="手动在同花顺上开仓/平仓后，在这里录入。品种、价格、手数 + 买多/卖空/平仓。只改程序的账，不会去点同花顺。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        ctk.CTkButton(
            left,
            text="导出全部到一个 Excel",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_all,
        ).pack(fill="x", padx=8, pady=(0, 8))

        self.btn_save_dry = ctk.CTkButton(
            left,
            text="保存全部配置到文件",
            height=34,
            fg_color="transparent",
            hover_color=T.BG_ELEVATED,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_save,
        )
        self.btn_save_dry.pack(fill="x", padx=8, pady=(0, 8))

        # 日志
        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        ctk.CTkLabel(head, text="运行日志", font=T.FONT_TITLE, text_color=T.TEXT).pack(side="left")
        ctk.CTkButton(
            head, text="清空", width=64, height=28, fg_color=T.BG_PANEL, hover_color=T.BORDER,
            command=lambda: self.log_box.delete("1.0", "end"),
        ).pack(side="right")
        self.log_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word",
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self._append_log("控制台就绪。点「启动常驻」或到了登录时间，都会先准备同花顺，完成后再开始扫描。")

    def _build_config_tab(self, tab: ctk.CTkFrame) -> None:
        """配置页已拆到 src/ui/config_tab.py，这里只保留入口。"""
        from src.ui.config_tab import build_config_tab

        build_config_tab(self, tab, ENGINE_CHOICES)

    def _build_backtest_tab(self, tab: ctk.CTkFrame) -> None:
        """历史回测：可选策略、品种、周期、时间段，结果写在本页。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="历史回测", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="用历史K线模拟开平仓。分钟、日线、周线都可以选。开始日期比网上行情更早时，会先下载新K线，再接到电脑里已经存好的K线上；这段本地已经齐，就直接用本地的。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        engines = list(ENGINE_CHOICES)
        self._option_row(box, "策略引擎", self.bt_engine_var, engines, width=240)
        from src.data.quote_check import BACKTEST_SOURCE_LABELS

        self._option_row(box, "行情数据源", self.bt_source_var, list(BACKTEST_SOURCE_LABELS), width=160)
        self.bt_source_hint = ctk.CTkLabel(
            box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        )
        self.bt_source_hint.pack(anchor="w", pady=(0, 4))
        try:
            self.bt_source_var.trace_add("write", lambda *_: self._refresh_bt_source_hint())
        except Exception:  # noqa: BLE001
            pass
        self._refresh_bt_source_hint()
        # 和配置页关注品种用同一份合约。先放进下拉，避免打开时选中的合约不在列表里。
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.bt_symbol_var.get() not in symbol_values:
            self.bt_symbol_var.set(symbol_values[0])
        self.bt_symbol_menu = self._option_row(box, "历史回测品种", self.bt_symbol_var, symbol_values, width=160)
        try:
            self.bt_symbol_var.trace_add("write", lambda *_: self._sync_bt_margin_from_table())
        except Exception:  # noqa: BLE001
            pass
        self._sync_bt_margin_from_table()
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。在那边添加或移出，这里的列表一起变。要改成配置页当前选中的合约，点下面「按配置页重新填入」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._timeframe_row(box, "信号周期", self.bt_tf_var, self.bt_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.bt_trail_var, self.bt_trail_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.bt_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.bt_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.bt_end_var, hint="点日历；清空=到最新")
        flat_row = ctk.CTkFrame(box, fg_color="transparent")
        flat_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            flat_row,
            text="收盘清仓",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            flat_row,
            text="",
            variable=self.bt_flatten_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=到点强平。关=仓可以留过收盘。趋势跟随不听这个开关，仍用它自己的设置。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        roll_row = ctk.CTkFrame(box, fg_color="transparent")
        roll_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            roll_row,
            text="主力合约换月",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            roll_row,
            text="",
            variable=self.bt_rollover_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=按「主力时段」分段回测，换月日当天仍用旧合约、下一交易日起切新主力，旧仓段末按收盘价结算；关=只测当前选的合约。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        chain_row = ctk.CTkFrame(box, fg_color="transparent")
        chain_row.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            chain_row,
            text="真实账户口径",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            chain_row,
            text="",
            variable=self.bt_rollover_chain_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=上一段剩余的权益接到下一段，前面亏多了后面可能开不了仓；关=每段同一份起始资金，便于和单合约对照。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        table_row = ctk.CTkFrame(box, fg_color="transparent")
        table_row.pack(fill="x", pady=(0, 4))
        self.bt_table_btn = ctk.CTkButton(
            table_row,
            text="更新主力合约表",
            width=150,
            height=30,
            command=self._on_update_main_table,
        )
        self.bt_table_btn.pack(side="left", padx=(148, 8))
        self.bt_table_open_btn = ctk.CTkButton(
            table_row,
            text="打开表格",
            width=84,
            height=30,
            fg_color=T.BG_ELEVATED,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
            border_width=1,
            border_color=T.BORDER,
            command=self._on_open_main_table,
        )
        self.bt_table_open_btn.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            box,
            text="表只在点「更新」时重算。要改某品种的换月：点「打开表格」，在「手动修正」页加行，更新时该行保留。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 2))
        self.bt_table_state = ctk.CTkLabel(
            box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        )
        self.bt_table_state.pack(anchor="w", padx=(148, 0), pady=(0, 6))
        self._refresh_main_table_state()

        fill_row = ctk.CTkFrame(box, fg_color="transparent")
        fill_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            fill_row,
            text="下一根开盘成交",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            fill_row,
            text="",
            variable=self.bt_fill_next_open_var,
            progress_color=T.OK,
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text="开=信号这根收盘确认后，按下一根开盘价成交，和实盘一致；关=按当根收盘价。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._entry_row(
            box,
            "滑点(跳)",
            self.bt_slippage_var,
            hint="0=不加滑点；每跳价格优先用品种表的「每跳价格」",
            width=160,
        )
        ctk.CTkButton(
            box,
            text="用实盘滑点建议填入",
            height=28,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_apply_slippage_hint,
        ).pack(anchor="w", padx=(148, 0), pady=(0, 6))
        ctk.CTkLabel(
            box,
            text="从成交滑点记录算出建议跳数，点一下才填进上面的框，不会自动改。没有真实成交时请不要用。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", padx=(148, 0), pady=(0, 6))

        self._pack_open_limits(left)

        ctk.CTkButton(
            left,
            text="开始历史回测",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_backtest,
        ).pack(fill="x", padx=4, pady=(12, 6))
        ctk.CTkButton(
            left,
            text="滑点灵敏度",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_slippage_scan,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="同一份参数，只换滑点跳数各跑一遍：看利润边缘有多厚、滑点加到几跳由盈转亏。K线只拉一次。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="导出本次历史回测",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_backtest,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="导出追加到「回测摘要」「回测成交」「回测信号」。第一列都是回测时间，同一次的行时间相同，用来分清是哪一次。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="画出因子衰减图",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_plot_factor_decay,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="用最近一次回测和模拟盘平仓记录，画出四个因子最近 20 笔滚动盈亏。只画图，不会改开平仓。没有多因子分数的成交不会进图。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        inv_row = ctk.CTkFrame(left, fg_color="transparent")
        inv_row.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            inv_row,
            text="查看本地K线库存",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_show_kline_inventory,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            inv_row,
            text="删除本地K线",
            height=34,
            width=120,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_purge_local_klines,
        ).pack(side="left")
        ctk.CTkButton(
            left,
            text="查看这根K线",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_inspect_local_kline,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="库存表直接打开 Excel。抽查某一根：选数据源、合约、周期、日期和时间，对照同花顺开高低收。对应 CSV 可用 Excel 打开；sqlite 请用文件夹入口，不要拿 Excel 硬开。某一家质量差时，可按数据源、合约、时间段删掉再重新拉。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="检查行情质量",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_check_quote_quality,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="先换新浪、东方财富、天勤各拉一次；同花顺图请点下面「从同花顺图读取K线」。再点这里生成 Excel：总览、问题K线、各家开高低收可筛选。只读本地、不下单。同花顺图不会进实盘。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        self._entry_row(
            left,
            "同花顺图读几根",
            self.bt_ths_bars_var,
            hint="默认 800，最多 5000。点按钮后先等 3 秒。",
            width=80,
        )
        ths_row = ctk.CTkFrame(left, fg_color="transparent")
        ths_row.pack(fill="x", padx=4, pady=(0, 4))
        self.bt_ths_scrape_btn = ctk.CTkButton(
            ths_row,
            text="从同花顺图读取K线",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_scrape_ths_chart,
        )
        self.bt_ths_scrape_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.bt_ths_stop_btn = ctk.CTkButton(
            ths_row,
            text="停止读图",
            height=34,
            width=96,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_stop_ths_chart,
            state="disabled",
        )
        self.bt_ths_stop_btn.pack(side="left")
        ctk.CTkLabel(
            left,
            text="打开期货通，主图选好合约和周期。点按钮后先等几秒再接管鼠标。读的时候按 ESC 或点「停止读图」可停，已读到的会先存下来。存到 @ths，只给回测。不会点买卖。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 6))
        ctk.CTkButton(
            left,
            text="把条件同步到「策略优化」页",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._sync_bt_to_opt,
        ).pack(fill="x", padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="历史回测结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.bt_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.bt_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.bt_result_box.insert("end", "点左侧「开始历史回测」后，这里显示胜率、盈亏和成交明细摘要。\n")

    def _pack_watch_editor(self, parent: ctk.CTkFrame) -> None:
        """
        配置页的关注品种。

        下拉框是已经加入的具体合约，选中的那一个会显示在运行页顶部。
        右边的文本框用来手工填写品种加合约，例如 bu2612，点添加后才进入下拉框。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text="关注品种", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        values = self._watch_symbols or ["（还没有）"]
        self.watch_menu = self._option(row, self.watch_choice_var, values, width=140)
        self.watch_menu.configure(command=self._on_watch_picked)
        self.watch_menu.pack(side="left")
        ctk.CTkEntry(
            row,
            textvariable=self.watch_add_var,
            width=110,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
            placeholder_text="如 bu2612",
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="添加",
            width=64,
            height=32,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_watch_add,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="移出",
            width=64,
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_watch_remove,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            parent,
            text="下拉是当前启用的合约。添加/移出会同步进「品种与合约」总表。保证金、乘数、手续费、交易时段、清仓时刻请在总表里改；同品种必须填成一样。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        # 当前合约不是主力时，在这里亮一行黄底提示。平时高度压到 1 像素，不占地方。
        self.main_contract_hint = ctk.CTkLabel(
            parent,
            text="",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            fg_color="transparent",
            corner_radius=8,
            height=1,
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.main_contract_hint.pack(fill="x", pady=(0, 4))

    def _pick_switch_row(self, parent: ctk.CTkFrame, label: str, var: ctk.BooleanVar) -> None:
        """一行说明加一个开关。交叉测试页的基准开关、清仓和针确认都用这一行。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(row, text="", variable=var, progress_color=T.OK, fg_color=T.BORDER).pack(side="left")

    def _pack_pick_limits(self, parent: ctk.CTkFrame) -> None:
        """
        交叉测试留下的开仓限制。

        持仓手数决定开几手，不看账户权益。开仓间隔和每天最多开仓会限制开仓次数。
        每笔风险记在这一轮里，不拿它重算手数。不写配置文件，也不改回测页和实盘。
        """
        box = self._card(parent)
        ctk.CTkLabel(box, text="开仓和止损限制", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            box,
            text="手数、间隔、每天次数、每笔风险、收盘清仓开跑前跟实盘配置对齐。交叉测试仍按固定手数比组合（不看权益），保证各品种同一套开仓纪律。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._entry_row(
            box, "每笔风险%", self.pk_risk_pct_var, hint="跟配置页一致，开跑前对齐", width=160, readonly=True
        )
        self._entry_row(
            box, "持仓手数", self.pk_volume_var, hint="跟配置页一致；各组合同手数", width=160, readonly=True
        )
        self._entry_row(box, "开仓最小间隔秒", self.pk_interval_var, hint="跟配置页一致", width=160, readonly=True)
        self._entry_row(box, "每天最多开仓", self.pk_max_orders_var, hint="跟配置页一致", width=160, readonly=True)
        ctk.CTkButton(
            box,
            text="按配置页重新填入",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._fill_picker_from_config,
        ).pack(fill="x", pady=(8, 0))

    def _build_picker_tab(self, tab: ctk.CTkFrame) -> None:
        """
        交叉测试：配置里的每个合约，配配置里的每套参与策略测一遍。

        全品种主力连续只写在备查文件里，这一轮不用那份。
        周期、日期和手数都用本页上的数字。不改正在用的策略。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)
        ctk.CTkLabel(left, text="交叉测试", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="这是「策略×合约」对照筛选：用配置里的固定参数，每个合约配每套参与策略测一遍，标出相对较好的组合。它还不是多品种交叉验证（不会按优化出的参数做样本外验收）。要验优化参数稳不稳，用下面「用最近优化参数做多品种验收」，或直接在策略优化里跑（优化结束会自动验其它关注品种）。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        # 两份名单都从配置来。合约跟关注品种是同一份，策略看 cross_engines。
        lists = self._card(left)
        ctk.CTkLabel(lists, text="这一轮测这些", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w")
        ctk.CTkLabel(
            lists,
            text="策略和合约都跟配置走。全品种主力连续写在 config/全品种备查.txt，只供查看。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(4, 6))
        ctk.CTkLabel(lists, text="参与策略", font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(anchor="w")
        self.cross_engine_box = ctk.CTkTextbox(lists, height=96, fg_color=T.BG, text_color=T.TEXT)
        self.cross_engine_box.pack(fill="x", pady=(2, 6))
        ctk.CTkLabel(lists, text="品种合约", font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(anchor="w")
        self.cross_symbol_box = ctk.CTkTextbox(lists, height=96, fg_color=T.BG, text_color=T.TEXT)
        self.cross_symbol_box.pack(fill="x", pady=(2, 4))
        self._refresh_cross_lists()

        box = self._card(left)
        from src.data.quote_check import SOURCE_LABELS

        # 这一轮只下载这里选中的那一家，不混用另一家的 K 线。
        self._option_row(box, "行情数据源", self.pk_source_var, list(SOURCE_LABELS), width=160)
        ctk.CTkLabel(
            box,
            text="K线按这里选的来源下载。同一个品种下载一次后，后面的策略接着用。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        self._timeframe_row(box, "信号周期", self.pk_tf_var, self.pk_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.pk_trail_var, self.pk_trail_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.pk_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.pk_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.pk_end_var, hint="点日历；清空=到最新")
        self._pick_switch_row(box, "收盘清仓", self.pk_flatten_var)
        ctk.CTkLabel(
            box,
            text="每个组合都用这些周期，以及下面的开仓限制。趋势跟随不听收盘清仓。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        self._pack_pick_limits(left)
        ctk.CTkButton(
            left,
            text="开始交叉测试",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_pick_start,
        ).pack(fill="x", padx=4, pady=(8, 8))
        ctk.CTkButton(
            left,
            text="用最近优化参数做多品种验收",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_pick_opt_cross,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="先在策略优化页跑完一轮。这里把当时挑出的参数，原样拿到关注品种上再跑一遍，不再重新搜参数。这才是多品种样本外验收。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="停止测试",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_pick_stop,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="导出本次交叉测试",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_cross,
        ).pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="导出追加到「交叉测试」表。第一列是这一轮的完成时间，同一轮再导出不会重复。品种和策略各占一列，较好的排在前面。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))
        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="测试结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.pick_summary = ctk.CTkLabel(
            right,
            text="点「开始交叉测试」后，每个品种和策略的组合占一行。较好的排在前面。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            justify="left",
            anchor="w",
            wraplength=640,
        )
        self.pick_summary.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        # 表头固定，下面的品种行可以上下滚动。列多了以后，这一块还可以左右拖，避免「结果」被挤出窗口。
        # 宽度和表头、数据行共用。实际测试时段要放下「2026-10-01 ~ 2026-10-22」。
        # 列顺序和导出「交叉测试」表一致：净盈亏后是盈利覆盖；交易频率后是笔数、每笔；时段后是起止、天数、日均。
        # 「当下行情」只在这里显示（这一轮跑的时候现算），不写进导出表。
        self._pick_col_widths = (
            108, 132, 48, 64, 72, 72, 72, 64, 120, 40, 72, 176, 88, 88, 40, 72, 72, 48, 96, 52, 64,
        )
        self._pick_headers = [
            "品种",
            "策略",
            "胜率",
            "盈利因子",
            "最大回撤",
            "净盈亏",
            "盈利覆盖",
            "收益率",
            "交易频率",
            "笔数",
            "每笔收益",
            "实际测试时段",
            "开始时间",
            "结束时间",
            "天数",
            "日均",
            "K线根数",
            "得分",
            "当下行情",
            "流动性",
            "结果",
        ]
        right.grid_rowconfigure(1, weight=0)
        right.grid_rowconfigure(2, weight=1)
        table_width = sum(self._pick_col_widths) + 8 + 4 * (len(self._pick_col_widths) - 1) + 8
        xscroll = ctk.CTkScrollableFrame(
            right,
            orientation="horizontal",
            fg_color=T.BG,
            corner_radius=8,
            scrollbar_fg_color=T.BG,
            scrollbar_button_color=T.BORDER,
            scrollbar_button_hover_color=T.TEXT_DIM,
        )
        xscroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        header = ctk.CTkFrame(xscroll, fg_color=T.BG, corner_radius=8, width=table_width)
        header.pack(fill="x", pady=(4, 4))
        self._pick_header_labels = []
        self._pack_pick_metric_line(header, self._pick_headers, header=True)
        self.pick_table_body = ctk.CTkScrollableFrame(
            xscroll,
            fg_color=T.BG,
            corner_radius=8,
            scrollbar_fg_color=T.BG,
            scrollbar_button_color=T.BORDER,
            scrollbar_button_hover_color=T.TEXT_DIM,
            width=table_width,
        )
        self.pick_table_body.pack(fill="both", expand=True)

    def _build_review_tab(self, tab: ctk.CTkFrame) -> None:
        """
        收盘复盘：用当天真实行情检验策略有效性、稳定性、可信度，并给出缺陷与改进建议。
        原理：和回测页一样左选条件、右看长文；生成放后台，避免拉行情时界面卡住。
        """
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="收盘复盘", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="用当天真实行情给策略打分：有效性、稳定性、可信度。会算 MAE/MFE/R、对照挂机记录，标出缺陷，并和近几日复盘、最近回测对照。用来补充历史回测与策略优化。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        self._date_row(box, "复盘日期", self.rv_date_var, hint="点日历选择，默认今天", allow_empty=False)
        # 和配置页关注品种用同一份合约，不用再手填代码。
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.rv_symbol_var.get() not in symbol_values:
            self.rv_symbol_var.set(symbol_values[0])
        self.rv_symbol_menu = self._option_row(box, "品种", self.rv_symbol_var, symbol_values, width=160)
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。要改成配置页当前选中的合约，点下面「按配置页重新填入」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._option_row(box, "看哪段", self.rv_scope_var, ["日盘", "夜盘", "前夜盘+日盘"], width=180)
        self._timeframe_row(box, "信号周期", self.rv_tf_var, self.rv_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.rv_trail_var, self.rv_trail_custom_var, width=140)
        from src.review.replay import ENGINE_BY_LABEL

        self._option_row(
            box,
            "策略对照",
            self.rv_engine_var,
            list(ENGINE_BY_LABEL.keys()),
            width=200,
        )

        ctk.CTkLabel(
            left,
            text="当日基本面补充（可选）",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=4, pady=(8, 2))
        ctk.CTkLabel(
            left,
            text="自动拉的仓单/基差/快讯经常不全。按下面清单打开网站核对后，把一两句贴进框里。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))
        self.rv_note_box = ctk.CTkTextbox(
            left, height=90, font=T.FONT_BODY, fg_color=T.BG_ELEVATED, text_color=T.TEXT, corner_radius=8
        )
        self.rv_note_box.pack(fill="x", padx=4, pady=(0, 4))
        # 随品种变化的推荐来源；换合约时刷新，避免一直显示沥青说明。
        self.rv_sources_label = ctk.CTkLabel(
            left,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
            anchor="w",
        )
        self.rv_sources_label.pack(anchor="w", fill="x", padx=4, pady=(0, 8))
        self.rv_symbol_var.trace_add("write", lambda *_: self._refresh_review_sources_hint())
        self._refresh_review_sources_hint()

        ctk.CTkButton(
            left,
            text="生成复盘",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_review,
        ).pack(fill="x", padx=4, pady=(4, 8))
        ctk.CTkButton(
            left,
            text="按配置页重新填入",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._fill_review_from_config,
        ).pack(fill="x", padx=4, pady=(0, 8))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="复盘报告", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.rv_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.rv_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.rv_result_box.insert(
            "end",
            "选好日期和品种后点「生成复盘」。\n"
            "报告开头是成绩单（有效性/稳定性/可信度），后面依次是：\n"
            "盘面 → 技术 → 基本面 → 策略对照 → 信号质量(MAE/MFE/R) →\n"
            "真实记录 → 信号与实盘对照 → 缺陷与改进 → 近几日/回测基准 → 综合。\n",
        )

    def _build_account_tab(self, tab: ctk.CTkFrame) -> None:
        """
        账户分析：左边改本金和出入金，右边看程序信号算出来的成绩。

        原理：开平仓由程序在真正记账时自动写入。这里只允许改初始资金、入金、出金和每日结算。
        """
        tab.grid_columnconfigure(0, weight=0, minsize=430)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent", width=420)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        ctk.CTkLabel(left, text="账户分析", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        self.ac_balance_label = ctk.CTkLabel(
            left, text="当前余额  —", font=T.FONT_TITLE, text_color=T.ACCENT, anchor="w"
        )
        self.ac_balance_label.pack(anchor="w", padx=4, pady=(0, 6))
        switch_box = self._card(left)
        switch_row = ctk.CTkFrame(switch_box, fg_color="transparent")
        switch_row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            switch_row, text="当前账", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=80, anchor="w"
        ).pack(side="left", padx=(0, 8))
        from src.account.workspace import labels as account_labels

        account_menu = self._option(switch_row, self.ac_account_var, account_labels(self.root_dir), width=280)
        account_menu.pack(side="left")
        account_menu.configure(command=self._on_account_switch)
        self.ac_account_hint = ctk.CTkLabel(
            switch_box,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        )
        self.ac_account_hint.pack(anchor="w", pady=(4, 0))

        self.ac_ths_panel = self._card(left)
        self._account_action_row(
            self.ac_ths_panel, "初始资金", self.ac_capital_var, [("初始化", self._on_signal_initial, True)]
        )
        self._account_action_row(
            self.ac_ths_panel,
            "金额",
            self.ac_cash_var,
            [("入金", self._on_signal_deposit, False), ("出金", self._on_signal_withdraw, False)],
        )
        self._account_action_row(
            self.ac_ths_panel, "实际余额", self.ac_settle_var, [("每日结算", self._on_signal_settle, True)]
        )
        ctk.CTkLabel(
            self.ac_ths_panel,
            text="同花顺点单账可以手改。初始化会先把旧账存档，再从现在另记一本。入金、出金填正数。红字是赚，绿字是亏。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self.ac_ctp_panel = self._card(left)
        ctk.CTkLabel(
            self.ac_ctp_panel,
            text="CTP 柜台资金（与交易台「资金」页同一份回报）",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.ac_ctp_user_label = ctk.CTkLabel(
            self.ac_ctp_panel, text="资金账号  —", font=T.FONT_BODY, text_color=T.ACCENT, anchor="w"
        )
        self.ac_ctp_user_label.pack(anchor="w")
        self.ac_ctp_time_label = ctk.CTkLabel(
            self.ac_ctp_panel, text="还没有查询记录", font=T.FONT_SMALL, text_color=T.TEXT_DIM, anchor="w"
        )
        self.ac_ctp_time_label.pack(anchor="w", pady=(0, 6))
        fund_grid = ctk.CTkFrame(self.ac_ctp_panel, fg_color="transparent")
        fund_grid.pack(fill="x")
        self._ac_fund_values: dict[str, Any] = {}
        from src.trader.ctp_display import account_pairs

        for index, (title, _value) in enumerate(account_pairs({})):
            row, col = divmod(index, 2)
            ctk.CTkLabel(
                fund_grid, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM, width=88, anchor="e"
            ).grid(row=row, column=col * 2, sticky="e", padx=(0, 6), pady=2)
            value_label = ctk.CTkLabel(
                fund_grid, text="—", font=T.FONT_SMALL, text_color=T.TEXT, width=110, anchor="w"
            )
            value_label.grid(row=row, column=col * 2 + 1, sticky="w", pady=2)
            self._ac_fund_values[title] = value_label
        ctk.CTkButton(
            self.ac_ctp_panel,
            text="从柜台刷新资金",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_ctp_refresh_funds,
        ).pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(
            self.ac_ctp_panel,
            text="数字原样来自期货公司，本地只保存、不改。同花顺点单请换到上面的同花顺账。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self.ac_common_refresh = ctk.CTkButton(
            left, text="刷新", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT, command=self._refresh_signal_view,
        )
        self.ac_common_refresh.pack(fill="x", padx=4, pady=(8, 4))
        # 这两张表和「导出全部」里的「交易记录」「资金变动记录」是同一份内容，只单独写出一张。
        ctk.CTkButton(
            left, text="导出交易记录", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT,
            command=lambda: self._on_export_account_part("trades"),
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkButton(
            left, text="导出资金变动记录", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT,
            command=lambda: self._on_export_account_part("changes"),
        ).pack(fill="x", padx=4, pady=(0, 8))
        self.ac_ths_manual = ctk.CTkButton(
            left,
            text="手工单录入",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_manual_trade,
        )
        self.ac_ths_manual.pack(fill="x", padx=4, pady=(0, 4))
        self.ac_ths_manual_note = ctk.CTkLabel(
            left,
            text="手动在同花顺上开仓/平仓后，在这里录入。品种、价格、手数 + 买多/卖空/平仓。只改程序的账，不会去点同花顺。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        )
        self.ac_ths_manual_note.pack(anchor="w", padx=4, pady=(0, 8))
        self._sync_account_mode()

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(right, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        head = ctk.CTkFrame(scroll, fg_color="transparent")
        head.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(head, text="账户统计", font=T.FONT_TITLE, text_color=T.TEXT).pack(side="left")
        spans = ctk.CTkFrame(head, fg_color="transparent")
        spans.pack(side="right")
        self._ac_span_buttons = {}
        for name in ("近1周", "近1月", "近3月", "近6月", "全部"):
            button = ctk.CTkButton(
                spans,
                text=name,
                width=58,
                height=26,
                fg_color=T.ACCENT if name == "全部" else T.BG,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON if name == "全部" else T.TEXT,
                command=lambda choice=name: self._on_account_span(choice),
            )
            button.pack(side="left", padx=2)
            self._ac_span_buttons[name] = button

        stats = ctk.CTkFrame(scroll, fg_color="transparent")
        stats.pack(fill="x", pady=(0, 6))
        self.ac_win_label = self._account_stat_card(stats, "胜率")
        self.ac_factor_label = self._account_stat_card(stats, "盈利因子")
        self.ac_dd_label = self._account_stat_card(stats, "最大回撤")
        self.ac_use_label = self._account_stat_card(stats, "资金使用率")

        chart_head = ctk.CTkFrame(scroll, fg_color="transparent")
        chart_head.pack(fill="x")
        self._ac_chart_buttons = {}
        for name in ("权益", "每日盈亏"):
            button = ctk.CTkButton(
                chart_head,
                text=name,
                width=72,
                height=26,
                fg_color=T.ACCENT if name == "权益" else T.BG,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON if name == "权益" else T.TEXT,
                command=lambda choice=name: self._on_account_chart(choice),
            )
            button.pack(side="left", padx=(0, 6))
            self._ac_chart_buttons[name] = button
        import tkinter as tk

        self.ac_chart = tk.Canvas(
            scroll, height=150, bg=T.BG, highlightthickness=0, bd=0
        )
        self.ac_chart.pack(fill="x", pady=(4, 8))

        ctk.CTkLabel(scroll, text="分品种", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w")
        self.ac_symbol_label = ctk.CTkLabel(
            scroll,
            text="还没有平仓。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            justify="left",
            anchor="w",
        )
        self.ac_symbol_label.pack(anchor="w", fill="x", pady=(2, 8))

        cal_head = ctk.CTkFrame(scroll, fg_color="transparent")
        cal_head.pack(fill="x")
        ctk.CTkButton(
            cal_head, text="上月", width=52, height=26, fg_color=T.BG, hover_color=T.BORDER,
            text_color=T.TEXT, command=lambda: self._shift_account_month(-1),
        ).pack(side="left")
        self.ac_cal_label = ctk.CTkLabel(cal_head, text="", font=T.FONT_BODY, text_color=T.TEXT)
        self.ac_cal_label.pack(side="left", padx=8)
        ctk.CTkButton(
            cal_head, text="下月", width=52, height=26, fg_color=T.BG, hover_color=T.BORDER,
            text_color=T.TEXT, command=lambda: self._shift_account_month(1),
        ).pack(side="left")
        self.ac_month_pnl = ctk.CTkLabel(cal_head, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM)
        self.ac_month_pnl.pack(side="right")
        self.ac_calendar = tk.Canvas(scroll, height=210, bg=T.BG, highlightthickness=0, bd=0)
        self.ac_calendar.pack(fill="x", pady=(4, 8))

        ctk.CTkLabel(scroll, text="资金变动记录", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w")
        self.ac_result_box = ctk.CTkTextbox(
            scroll, height=220, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.ac_result_box.pack(fill="x", pady=(4, 8))
        self._refresh_signal_view()

    def _build_optimize_tab(self, tab: ctk.CTkFrame) -> None:
        """策略优化：在选定时间段上对策略参数做网格搜索，可写回配置。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 8), pady=8)

        ctk.CTkLabel(left, text="策略优化", font=T.FONT_TITLE, text_color=T.TEXT).pack(anchor="w", padx=4, pady=(4, 2))
        ctk.CTkLabel(
            left,
            text="训练段挑参数，检验段只验收。第一次选旋钮：直接开始优化。已经在用、后来衰减了：打开「对照正在用的参数」再搜，默认不改实盘。写入前会备份这一套策略的参数版本；需要整页还原时再留配置存档。两份不是同一本账。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        box = self._card(left)
        engines = list(ENGINE_CHOICES)
        opt_engine_menu = self._option_row(box, "策略引擎", self.opt_engine_var, engines, width=240)
        # 换了策略，下面的版本列表只显示这一套，出厂默认也只重置这一套。
        opt_engine_menu.configure(command=lambda _choice: self._refresh_opt_history())
        from src.data.quote_check import SOURCE_LABELS

        self._option_row(box, "行情数据源", self.opt_source_var, list(SOURCE_LABELS), width=160)
        symbol_values = list(self._watch_symbols) or ["（还没有）"]
        if self.opt_symbol_var.get() not in symbol_values:
            self.opt_symbol_var.set(symbol_values[0])
        self.opt_symbol_menu = self._option_row(box, "策略优化品种", self.opt_symbol_var, symbol_values, width=160)
        ctk.CTkLabel(
            box,
            text="和配置页的关注品种是同一份。要改成配置页当前选中的合约，点下面「按配置页重新填入」，或在历史回测页点「把条件同步到策略优化页」。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._timeframe_row(box, "信号周期", self.opt_tf_var, self.opt_tf_custom_var, width=140)
        self._timeframe_row(box, "止损周期", self.opt_trail_var, self.opt_trail_custom_var, width=140)
        self._option_row(
            box,
            "时间段",
            self.opt_range_var,
            ["全部可用", "最近3天", "最近7天", "最近14天", "自定义"],
            width=160,
        )
        self._date_row(box, "开始日期", self.opt_start_var, hint="点日历选择，默认当年1月1日", allow_empty=False)
        self._date_row(box, "结束日期", self.opt_end_var, hint="点日历；清空=到最新")
        self._option_row(
            box,
            "搜索深度",
            self.opt_depth_var,
            ["精简", "标准", "完整"],
            width=160,
        )
        file_row = ctk.CTkFrame(box, fg_color="transparent")
        file_row.pack(fill="x", padx=4, pady=(0, 8))
        ctk.CTkButton(
            file_row,
            text="打开优化参数表",
            width=140,
            height=28,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT_DIM,
            command=self._open_optimizer_file,
        ).pack(side="left")
        ctk.CTkLabel(
            file_row,
            text="config/优化参数.xlsx：改网格档位和中文名，保存后下次优化生效",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left", padx=(8, 0))
        self._entry_row(
            box,
            "最多试几组",
            self.opt_trials_var,
            hint="理论组合超过这个数就随机抽；没超过就全跑",
            width=100,
        )
        self._entry_row(
            box,
            "检验比例%",
            self.opt_holdout_pct_var,
            hint="默认30，可填10～50。剩下的才用来挑参数",
            width=100,
        )
        self._option_row(
            box,
            "检验方式",
            self.opt_mode_var,
            ["一次切断", "滚动前进"],
            width=160,
        )
        self._entry_row(
            box,
            "滚动段数",
            self.opt_walk_steps_var,
            hint="仅滚动前进用，默认3。段数越多越久",
            width=100,
        )
        ctk.CTkLabel(
            box,
            text="一次切断：按比例切一刀。滚动前进：把检验段拆成多段，走一步再验一步。优化结束后，其它关注品种会用同一组参数再跑一遍（不再重搜），成绩会列在结果里。开仓限制与保证金开跑前跟实盘配置/品种表对齐。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self._pack_open_limits(left)

        run = self._card(left)
        ctk.CTkLabel(run, text="开始优化", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w", pady=(0, 4))
        self._pick_switch_row(run, "对照正在用的参数", self.opt_compare_var)
        ctk.CTkLabel(
            run,
            text="关掉：第一次选旋钮，只出搜索成绩。打开：衰减后重跑，多和现在这套对照，跑完不自动写。两种都用同一套搜索。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(
            run,
            text="开始策略优化",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_optimize,
        ).pack(fill="x")

        write = self._card(left)
        ctk.CTkLabel(write, text="写入配置", font=T.FONT_BODY, text_color=T.TEXT).pack(anchor="w", pady=(0, 4))
        pick = ctk.CTkFrame(write, fg_color="transparent")
        pick.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(pick, text="写入第几组", font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(side="left")
        ctk.CTkEntry(
            pick,
            textvariable=self.opt_write_index_var,
            width=54,
            height=28,
            justify="center",
        ).pack(side="left", padx=(6, 6))
        ctk.CTkLabel(
            pick,
            text="1=训练最优；其它组会先复核检验",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left")
        self._pick_switch_row(write, "同时留下整页配置存档", self.opt_save_preset_var)
        ctk.CTkLabel(
            write,
            text="整页存档在配置页「配置存档」里，可一键换回策略、品种、模拟开关。对照全面更好时，按钮会写成「写入并切实盘」，并且一定会留这份存档。没更好仍可写入，会多问一次。常驻在跑时不能写。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        self.opt_write_btn = ctk.CTkButton(
            write,
            text="把所选参数写入配置",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_apply_opt_params,
        )
        self.opt_write_btn.pack(fill="x")

        # —— 参数版本回溯：只还原这一套策略的旋钮，不是整页配置存档 ——
        hist = self._card(left)
        ctk.CTkLabel(
            hist,
            text="参数版本历史（只还原这一套旋钮）",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            hist,
            text="和上面的整页配置存档不是同一份。这里只改当前选中的这一套策略参数，不改正在用的策略名、品种、周期，也不改另外几套。写入、切实盘、回溯前都会自动加一条。出厂默认不能删，用来重置初始。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        self._opt_hist_id_by_label: dict[str, str] = {}
        self.opt_hist_var = ctk.StringVar(value="★ 出厂默认（只重置当前选中的策略）")
        self.opt_hist_menu = ctk.CTkOptionMenu(
            hist,
            variable=self.opt_hist_var,
            values=["★ 出厂默认（只重置当前选中的策略）"],
            width=300,
            fg_color=T.BG_ELEVATED,
            button_color=T.ACCENT,
            button_hover_color=T.ACCENT_HOVER,
            dropdown_fg_color=T.BG_PANEL,
            text_color=T.TEXT,
            dropdown_text_color=T.TEXT,
        )
        self.opt_hist_menu.pack(anchor="w", pady=(0, 8))
        self.opt_hist_menu.configure(command=self._sync_hist_name_box)
        name_row = ctk.CTkFrame(hist, fg_color="transparent")
        name_row.pack(fill="x", pady=(0, 8))
        self.opt_hist_name_var = ctk.StringVar(value="出厂默认")
        self.opt_hist_name_entry = ctk.CTkEntry(
            name_row,
            textvariable=self.opt_hist_name_var,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
        )
        self.opt_hist_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.opt_hist_rename_btn = ctk.CTkButton(
            name_row,
            text="改名",
            height=32,
            width=64,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_rename_opt_history,
        )
        self.opt_hist_rename_btn.pack(side="left", padx=(0, 6))
        self.opt_hist_delete_btn = ctk.CTkButton(
            name_row,
            text="删除",
            height=32,
            width=64,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_delete_opt_history,
        )
        self.opt_hist_delete_btn.pack(side="left")
        hist_btns = ctk.CTkFrame(hist, fg_color="transparent")
        hist_btns.pack(fill="x")
        ctk.CTkButton(
            hist_btns,
            text="刷新列表",
            height=32,
            width=90,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._refresh_opt_history,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            hist_btns,
            text="回溯到选中版本",
            height=32,
            width=120,
            fg_color=T.DANGER_MUTED,
            hover_color=T.DANGER,
            text_color=T.DANGER_TEXT,
            command=self._on_restore_opt_history,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            hist,
            text="重置当前策略为出厂默认",
            height=34,
            fg_color=T.BG,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_reset_factory_params,
        ).pack(fill="x", pady=(8, 4))
        excel_row = ctk.CTkFrame(hist, fg_color="transparent")
        excel_row.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(
            excel_row,
            text="导出追加到Excel",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_export_params_excel,
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            excel_row,
            text="从Excel导入",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_import_params_excel,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))
        ctk.CTkLabel(
            hist,
            text="程序内只留最近30条。导出会同时写「优化参数」表（参数版本）和「优化结果」表（最近一次的完整成绩，第一列测试时间分清是哪一次），都在 data/Auto-Trading导出.xlsx。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="策略优化结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        # 进度条 + 一行文字：第几组/共几组、已用时间、预计还要多久。
        self.opt_progress_label = ctk.CTkLabel(
            right,
            text="尚未开始",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
            justify="left",
        )
        self.opt_progress_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.opt_progress = ctk.CTkProgressBar(right, height=10, progress_color=T.ACCENT)
        self.opt_progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.opt_progress.set(0)
        self._opt_started_at: float | None = None
        self._opt_progress_at = 0.0
        self.opt_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.opt_result_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.opt_result_box.insert(
            "end",
            "点左侧「开始策略优化」。第一次选旋钮：开关关掉即可。\n"
            "衰减后打开「对照正在用的参数」再搜，默认不改实盘。\n"
            "全面更好时，写入按钮会变成「写入并切实盘」。切坏了用下方回溯或重置出厂。\n",
        )
        # 打开界面时刷新一次历史下拉
        self.after(200, self._refresh_opt_history)

    # ---------- 小部件工厂 ----------
    def _section(self, parent: ctk.CTkFrame, title: str) -> None:
        ctk.CTkLabel(parent, text=title, font=T.FONT_TITLE, text_color=T.ACCENT).pack(anchor="w", pady=(14, 6))

    def _card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=T.BG_ELEVATED, corner_radius=12, border_width=1, border_color=T.BORDER)
        card.pack(fill="x", pady=(0, 4))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)
        return inner

    def _refresh_strategy_param_panel(self, *_args: object) -> None:
        """
        右边只显示当前策略的参数面板。

        原理：每套策略的输入控件一开始就建好并绑在 StringVar 上；
        换引擎时只 pack / pack_forget，不销毁控件，所以没展示的策略数值也不会丢。
        换完后按内容重算右侧高度，避免「少参数策略」还留着一大块空白。
        """
        panels = getattr(self, "_strategy_detail_panels", None) or {}
        if not panels:
            return
        current = _ui_engine(self.engine_var.get())
        # 别名都指到同一块面板（如 morning_a / morning_box_a）。
        show = panels.get(current)
        if show is None:
            show = panels.get("daily_body")
            current = "daily_body"
        meta = getattr(self, "_strategy_detail_meta", {}) or {}
        title, note = meta.get(current, ("当前策略参数", ""))
        try:
            self._strategy_detail_title.configure(text=title)
            self._strategy_detail_note.configure(text=note)
        except Exception:  # noqa: BLE001
            pass
        for panel in panels.values():
            try:
                panel.pack_forget()
            except Exception:  # noqa: BLE001
                pass
        if show is not None:
            show.pack(fill="x", anchor="n")
        # 换策略后说明文字换行宽度跟着右侧实际宽度走。
        try:
            self.after_idle(self._sync_strategy_param_wrap)
        except Exception:  # noqa: BLE001
            self._sync_strategy_param_wrap()

    def _refresh_generic_stop_param_panel(self, *_args: object) -> None:
        """
        通用止损：左边选方式，右边只显示这种方式要填的参数。

        和策略引擎一样，各方式的面板一开始就建好并绑在 StringVar 上；
        切换时只 pack / pack_forget，不销毁控件，其它方式的数值也不会丢。
        """
        panels = getattr(self, "_generic_stop_detail_panels", None) or {}
        if not panels:
            return
        from src.strategy.generic_stop import generic_stop_from_label

        kind = generic_stop_from_label(self.generic_stop_var.get())
        show = panels.get(kind) or panels.get("off")
        meta = getattr(self, "_generic_stop_detail_meta", {}) or {}
        title, note = meta.get(kind, ("通用止损参数", ""))
        try:
            self._generic_stop_detail_title.configure(text=title)
            self._generic_stop_detail_note.configure(text=note)
        except Exception:  # noqa: BLE001
            pass
        for panel in panels.values():
            try:
                panel.pack_forget()
            except Exception:  # noqa: BLE001
                pass
        if show is not None:
            show.pack(fill="x", anchor="n")

    def _sync_strategy_param_wrap(self) -> None:
        """
        按右侧参数框当前实际宽度，刷新整段说明的换行。

        只改 wraplength，不改控件宽高，避免 Tk 用非法尺寸画背景图时报 CreateDIBSection。
        """
        shell = getattr(self, "_strategy_detail_shell", None)
        if shell is None:
            return
        try:
            shell.update_idletasks()
            w = int(shell.winfo_width() or 0)
            min_w = int(getattr(self, "_strategy_param_min_width", 360) or 360)
            # 尚未完成布局时宽度可能是 1，先用最小宽，等 Configure 再校正。
            if w < min_w:
                w = min_w
            # 防止异常超大宽度把 wraplength 撑爆。
            w = min(w, 1600)
            # 说明文字固定不超过 420，避免窗口一拉宽就把字画出蓝框。
            wrap = max(200, min(420, w - 40))
            self._strategy_param_hint_wrap = 200
            try:
                self._strategy_detail_note.configure(wraplength=wrap)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _on_strategy_split_configure(self, event: object | None = None) -> None:
        """
        分栏宽度变化时，只同步右侧说明换行。

        横向铺满由 grid weight=1 完成；这里不再手写 width/height，避免启动时尺寸非法。
        """
        split = getattr(self, "_strategy_split", None)
        if split is None:
            return
        try:
            split_w = int(split.winfo_width() or 0)
            if split_w <= 1:
                return
            last_w = int(getattr(self, "_strategy_split_last_w", 0) or 0)
            if abs(split_w - last_w) < 4:
                return
            self._strategy_split_last_w = split_w
            self._sync_strategy_param_wrap()
        except Exception:  # noqa: BLE001
            pass

    def _option(
        self,
        parent: ctk.CTkFrame,
        var: ctk.StringVar,
        values: list[str],
        width: int = 220,
    ) -> ctk.CTkOptionMenu:
        """创建下拉菜单；调用方必须把 parent 设为「当前这一行」的 Frame，再 pack 进去。"""
        return ctk.CTkOptionMenu(
            parent,
            values=values,
            variable=var,
            width=width,
            fg_color=T.BG_ELEVATED,
            button_color=T.ACCENT,
            button_hover_color=T.ACCENT_HOVER,
            dropdown_fg_color=T.BG_PANEL,
            text_color=T.TEXT,                 # 框内已选文字：浅色，深蓝底上清晰
            dropdown_text_color=T.TEXT,        # 展开列表里的文字
        )

    def _option_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        values: list[str],
        width: int = 220,
    ) -> None:
        """
        一行：左侧固定宽标签 + 右侧下拉框。
        原理：控件必须挂在同一 row 上再 pack(side=left)，
        若挂在外层 parent 再 side=right，会挤乱整列（楼梯错位、输入框变细条）。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        menu = self._option(row, var, values, width=width)
        menu.pack(side="left")
        return menu

    def _timeframe_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        choice_var: ctk.StringVar,
        custom_var: ctk.StringVar,
        width: int = 160,
    ) -> None:
        """
        周期一行：下拉选常用档，右边小框留给「自定义」。

        下拉里有 1 到 60 分钟、日线、周线。选自定义时，小框里填分钟数，120 就是 2 小时。
        没选自定义时，小框里的字不会被用上。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        menu = self._option(row, choice_var, PERIOD_MENU, width=width)
        menu.pack(side="left")
        ent = ctk.CTkEntry(
            row,
            textvariable=custom_var,
            width=70,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
            placeholder_text="120",
        )
        ent.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            row, text="自定义填分钟", font=T.FONT_SMALL, text_color=T.TEXT_DIM
        ).pack(side="left", padx=6)

    def _period_code(self, choice_var: ctk.StringVar, custom_var: ctk.StringVar) -> str:
        """下拉框收成配置里的周期代码。日线是 D，周线是 W，分钟是数字。"""
        from src.data.periods import canonical_period

        return canonical_period(choice_var.get(), custom_var.get())

    def _set_period_vars(self, code: str, choice_var: ctk.StringVar, custom_var: ctk.StringVar) -> None:
        """把配置里的 15 或 D 填回下拉框。自定义分钟会选中「自定义」并写进小框。"""
        label, custom = ui_parts(str(code))
        choice_var.set(label)
        custom_var.set(custom)

    def _entry_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        hint: str = "",
        width: int = 280,
        readonly: bool = False,
        hint_wrap: int | None = None,
    ) -> None:
        """一行：标签 + 输入框 + 可选灰色提示；三者同属一行 Frame，宽度固定不被挤扁。

        hint_wrap：说明文字的换行宽度。策略参数框较窄时传入，避免一行把右侧撑出大片空白。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        if readonly:
            # 只显示数字。余额由账户分析改，配置页不能在这里填另一个数。
            ctk.CTkLabel(
                row, textvariable=var, font=T.FONT_BODY, text_color=T.TEXT, anchor="w", width=width
            ).pack(side="left")
        else:
            # 可编辑时才放输入框。只读行上面已经用标签显示，不能再排一个能打字的框。
            ent = ctk.CTkEntry(
                row,
                textvariable=var,
                width=width,
                fg_color=T.BG_ELEVATED,
                border_color=T.BORDER,
                text_color=T.TEXT,  # 输入内容用浅色字，避免看不清
            )
            ent.pack(side="left")
        if hint:
            hint_lbl = ctk.CTkLabel(
                row,
                text=hint,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                justify="left",
                anchor="w",
            )
            if hint_wrap and hint_wrap > 0:
                hint_lbl.configure(wraplength=int(hint_wrap))
            hint_lbl.pack(side="left", padx=8)

    def _date_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        hint: str = "",
        allow_empty: bool = True,
    ) -> None:
        """一行：标签 + 日历选日期。选完仍是 2026-01-01 这种字，后面逻辑不用改。"""
        from src.ui.datetime_pickers import DateField

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        DateField(row, var, allow_empty=allow_empty).frame.pack(side="left")
        if hint:
            ctk.CTkLabel(
                row,
                text=hint,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                justify="left",
                anchor="w",
            ).pack(side="left", padx=8)

    def _toggle_password(self) -> None:
        self.pwd_entry.configure(show="" if self._show_password.get() else "*")

    def _toggle_tq_password(self) -> None:
        self.tq_pwd_entry.configure(show="" if self._show_tq_password.get() else "*")

    def _refresh_bt_source_hint(self) -> None:
        """选同花顺图时说明怎么读、为什么不能实盘用。"""
        label = getattr(self, "bt_source_hint", None)
        if label is None:
            return
        from src.data.quote_check import is_ths_source

        if is_ths_source(self.bt_source_var.get()):
            label.configure(
                text="同花顺图：回测只用已经点「从同花顺图读取K线」存进本地的那一份，"
                "不会再去期货通抢鼠标。没有库存时请先读图。只给历史回测，不会拿去扫描下单。"
            )
        else:
            label.configure(text="")

    def _refresh_tq_hint(self) -> None:
        """
        选天勤时说明能拉什么周期。日线原生可用；周线用日线合成，不是直接要一周。
        """
        label = getattr(self, "tq_hint_label", None)
        if label is None:
            return
        from src.data.quote_check import normalize_source

        if normalize_source(self.source_var.get()) != "tq":
            label.configure(text="", text_color=T.TEXT_DIM)
            return
        label.configure(
            text="天勤免费接口：分钟、小时、日线可直接拉；周线用日线合成。多因子的日线方向也能用。账号密码填在上面「账号与登录」。",
            text_color=T.TEXT_DIM,
        )

    # ---------- 日志 ----------
    def _hook_logger(self) -> None:
        def emit(text: str) -> None:
            # 后台线程只入队，不碰 Tk。
            self._log_queue.put_nowait(text)

        logger.add(QueueLogHandler(emit).write, format="{time:HH:mm:ss} | {level} | {message}", level="INFO")
        self._log_after_id = self.after(100, self._drain_logs)

    def _drain_logs(self) -> None:
        """主线程定时把队列里的日志写进运行页。内部任何一步异常都不能让日志泵断掉。"""
        if self._closing:
            return
        try:
            try:
                while True:
                    text = self._log_queue.get_nowait()
                    self._append_log(text)
            except queue.Empty:
                pass
            from src.trader.gui_trader import order_click_in_progress

            if not order_click_in_progress():
                self._flush_held_logs()
        except Exception as exc:  # noqa: BLE001
            logger.warning("日志刷新跳过一轮: {}", exc)
        finally:
            if not self._closing:
                try:
                    self._log_after_id = self.after(100, self._drain_logs)
                except Exception:  # noqa: BLE001
                    pass

    def _flush_held_logs(self) -> None:
        """把点击期间攒下的日志一次性补上。"""
        pending = self._held_logs
        self._held_logs = []
        for line in pending:
            self.log_box.insert("end", line + "\n")
        if pending:
            self.log_box.see("end")
            self._trim_log_box()

    def _trim_log_box(self) -> None:
        """运行日志只留最近若干行，避免长时间挂机把文本框和内存越撑越大。"""
        try:
            total = int(self.log_box.index("end-1c").split(".")[0])
            if total <= self._LOG_MAX_LINES:
                return
            self.log_box.delete("1.0", f"{total - self._LOG_MAX_LINES}.0")
        except Exception as exc:  # noqa: BLE001
            logger.debug("运行日志裁剪跳过: {}", exc)

    def _append_log(self, text: str) -> None:
        """
        把一行日志写进运行页。

        正在点击同花顺时先攒着，不插入文本框。文本框一滚动，窗口就会抢到最前，
        后面的价格和买卖按钮就点到本程序上。点击结束后再一次性补上。
        """
        from src.trader.gui_trader import order_click_in_progress

        if order_click_in_progress():
            self._held_logs.append(text)
            return
        self._flush_held_logs()
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self._trim_log_box()

    # ---------- 配置读写 ----------
    def _init_cta_filter_vars(
        self, multi_box: dict, vote_box: dict, hybrid_box: dict, adapt_box: dict | None = None
    ) -> None:
        """多因子各套各自一份行情闸门控件，互不影响。"""
        from src.strategy.cta_filter import CtaFilterParams

        self._cta_filter_vars = {}
        for kind, box in (
            ("multi_factor", multi_box),
            ("multi_factor_adapt", adapt_box or {}),
            ("multi_vote", vote_box),
            ("multi_hybrid", hybrid_box),
        ):
            p = CtaFilterParams.from_box(box if isinstance(box, dict) else {}, kind=kind)
            self._cta_filter_vars[kind] = {
                "enabled": ctk.BooleanVar(value=p.enabled),
                "use_adx": ctk.BooleanVar(value=p.use_adx),
                "use_atr": ctk.BooleanVar(value=p.use_atr),
                "use_disagree": ctk.BooleanVar(value=p.use_disagree),
                "adx_flat": ctk.StringVar(value=f"{p.adx_flat:g}"),
                "adx_trend": ctk.StringVar(value=f"{p.adx_trend:g}"),
                "atr_lookback": ctk.StringVar(value=str(p.atr_lookback)),
                "atr_low_pct": ctk.StringVar(value=f"{p.atr_low_pct * 100:g}"),
                "weak_score": ctk.StringVar(value=f"{p.weak_score:g}"),
                "weak_size": ctk.StringVar(value=f"{p.weak_size:g}"),
                "weak_votes": ctk.StringVar(value=str(p.weak_votes)),
            }

    def _fill_cta_filter_vars(
        self, multi_box: dict, vote_box: dict, hybrid_box: dict, adapt_box: dict | None = None
    ) -> None:
        """从配置重新灌进各套闸门控件。"""
        from src.strategy.cta_filter import CtaFilterParams

        boxes = {
            "multi_factor": multi_box,
            "multi_factor_adapt": adapt_box or {},
            "multi_vote": vote_box,
            "multi_hybrid": hybrid_box,
        }
        for kind, box in boxes.items():
            p = CtaFilterParams.from_box(box if isinstance(box, dict) else {}, kind=kind)
            vars_ = self._cta_filter_vars[kind]
            vars_["enabled"].set(p.enabled)
            vars_["use_adx"].set(p.use_adx)
            vars_["use_atr"].set(p.use_atr)
            vars_["use_disagree"].set(p.use_disagree)
            vars_["adx_flat"].set(f"{p.adx_flat:g}")
            vars_["adx_trend"].set(f"{p.adx_trend:g}")
            vars_["atr_lookback"].set(str(p.atr_lookback))
            vars_["atr_low_pct"].set(f"{p.atr_low_pct * 100:g}")
            vars_["weak_score"].set(f"{p.weak_score:g}")
            vars_["weak_size"].set(f"{p.weak_size:g}")
            vars_["weak_votes"].set(str(p.weak_votes))

    def _write_cta_filter_vars(self, box: dict, kind: str) -> None:
        """把界面上的闸门写回这一套策略小节。"""
        from src.strategy.cta_filter import default_cta_filter

        vars_ = self._cta_filter_vars[kind]
        seed = default_cta_filter(kind)
        try:
            adx_flat = min(90.0, max(5.0, float(vars_["adx_flat"].get())))
        except ValueError:
            adx_flat = float(seed["adx_flat"])
        try:
            adx_trend = min(90.0, max(adx_flat, float(vars_["adx_trend"].get())))
        except ValueError:
            adx_trend = float(seed["adx_trend"])
        try:
            lookback = max(10, int(vars_["atr_lookback"].get()))
        except ValueError:
            lookback = int(seed["atr_lookback"])
        try:
            atr_low = min(90.0, max(1.0, float(vars_["atr_low_pct"].get()))) / 100.0
        except ValueError:
            atr_low = float(seed["atr_low_pct"])
        try:
            weak_score = min(0.95, max(0.0, float(vars_["weak_score"].get())))
        except ValueError:
            weak_score = float(seed["weak_score"])
        try:
            weak_size = min(1.0, max(0.1, float(vars_["weak_size"].get())))
        except ValueError:
            weak_size = float(seed["weak_size"])
        try:
            weak_votes = max(0, int(vars_["weak_votes"].get()))
        except ValueError:
            weak_votes = int(seed["weak_votes"])
        box["cta_filter"] = {
            "enabled": bool(vars_["enabled"].get()),
            "use_adx": bool(vars_["use_adx"].get()),
            "use_atr": bool(vars_["use_atr"].get()),
            "use_disagree": bool(vars_["use_disagree"].get()),
            "adx_period": 14,
            "adx_flat": adx_flat,
            "adx_trend": adx_trend,
            "atr_lookback": lookback,
            "atr_low_pct": atr_low,
            "weak_score": weak_score,
            "weak_size": weak_size,
            "weak_votes": weak_votes,
        }

    def _init_trend_filter_vars(self) -> None:
        """海龟、SuperTrend、双均线、布林、SAR、长线各自一份趋势过滤控件。"""
        from src.strategy.market_state import TREND_FILTER_SECTIONS, trend_params_for_section

        self._trend_filter_vars = {}
        for section in TREND_FILTER_SECTIONS:
            p = trend_params_for_section(self.cfg, section)
            self._trend_filter_vars[section] = {
                "enabled": ctk.BooleanVar(value=bool(p.enabled)),
                "adx_period": ctk.StringVar(value=str(p.adx_period)),
                "atr_period": ctk.StringVar(value=str(p.atr_period)),
                "lookback": ctk.StringVar(value=str(p.lookback)),
                "adx_flat": ctk.StringVar(value=f"{p.adx_flat:g}"),
                "atr_low_pct": ctk.StringVar(value=f"{p.atr_low_pct * 100:g}"),
            }

    def _fill_trend_filter_vars(self) -> None:
        """从配置重新灌进趋势策略各自的过滤控件。"""
        from src.strategy.market_state import TREND_FILTER_SECTIONS, trend_params_for_section

        for section in TREND_FILTER_SECTIONS:
            p = trend_params_for_section(self.cfg, section)
            vars_ = self._trend_filter_vars[section]
            vars_["enabled"].set(bool(p.enabled))
            vars_["adx_period"].set(str(p.adx_period))
            vars_["atr_period"].set(str(p.atr_period))
            vars_["lookback"].set(str(p.lookback))
            vars_["adx_flat"].set(f"{p.adx_flat:g}")
            vars_["atr_low_pct"].set(f"{p.atr_low_pct * 100:g}")

    def _write_trend_filter_vars(self, box: dict, section: str) -> None:
        """把界面上的趋势过滤写回这一套策略小节。"""
        vars_ = self._trend_filter_vars[section]
        box["filter_enabled"] = bool(vars_["enabled"].get())
        try:
            box["filter_adx_period"] = max(2, min(200, int(vars_["adx_period"].get())))
        except ValueError:
            box["filter_adx_period"] = 14
        try:
            box["filter_atr_period"] = max(2, min(200, int(vars_["atr_period"].get())))
        except ValueError:
            box["filter_atr_period"] = 14
        try:
            box["filter_lookback"] = max(10, min(500, int(vars_["lookback"].get())))
        except ValueError:
            box["filter_lookback"] = 60
        try:
            box["filter_adx_flat"] = min(60.0, max(5.0, float(vars_["adx_flat"].get())))
        except ValueError:
            box["filter_adx_flat"] = 20.0
        try:
            atr_ui = min(90.0, max(5.0, float(vars_["atr_low_pct"].get())))
        except ValueError:
            atr_ui = 20.0
        box["filter_atr_low_pct"] = atr_ui / 100.0

    def _parse_list(self, raw: str) -> list[str]:
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def _reload_values_only(self) -> None:
        self.cfg = load_config()
        st = self.cfg.get("strategy", {})
        acc = self.cfg.get("account", {})
        app = self.cfg.get("app", {})
        trade = self.cfg.get("trade", {})
        risk = self.cfg.get("risk", {})
        sch = self.cfg.get("schedule", {})
        secrets = load_secrets()

        self.engine_var.set(_ui_engine(st.get("engine", "daily_body")))
        self.exec_var.set(str(trade.get("executor", "sim")))
        from src.trader.ctp_logic import ctp_kind_label

        ctp_box = trade.get("ctp") or {}
        live_box = ctp_box.get("live") if isinstance(ctp_box.get("live"), dict) else {}
        self.ctp_kind_var.set(ctp_kind_label(ctp_box.get("account_kind")))
        self.ctp_live_td_var.set(str(live_box.get("td_front") or ""))
        self.ctp_live_md_var.set(str(live_box.get("md_front") or ""))
        from src.trader.go_live import load_state as load_go_live, stage_hint, stage_label

        go_state = load_go_live(self.root_dir)
        yaml_stage = ((trade.get("go_live") or {}) if isinstance(trade.get("go_live"), dict) else {}).get("stage")
        stage_now = go_state.get("stage") or yaml_stage
        if stage_now:
            self.go_live_var.set(stage_label(stage_now))
            self.go_live_hint_var.set(stage_hint(stage_now))
        from src.data.quote_check import source_label

        self.source_var.set(source_label((self.cfg.get("data") or {}).get("source")))
        tq_cfg = (self.cfg.get("data") or {}).get("tq") or {}
        self.tq_user_var.set(str(tq_cfg.get("user") or secrets.get("tq_user", "") or ""))
        self.tq_password_var.set(
            str(secrets.get("tq_password", "") or tq_cfg.get("password", "") or "")
        )
        self._refresh_tq_hint()
        self.dry_var.set(bool(self.cfg.get("dry_run", True)))
        self._set_period_vars(str(st.get("timeframe", "15")), self.tf_var, self.tf_custom_var)
        self._set_period_vars(str(st.get("trail_timeframe", "30")), self.trail_var, self.trail_custom_var)
        self.symbols_var.set(",".join(str(x) for x in st.get("symbols", ["bu", "c"])))
        # 这是从文件读回来，不要再写一遍，免得把刚读到的名单覆盖掉。
        self._set_watch_codes(self._parse_list(self.symbols_var.get()), save=False)
        self.volume_var.set(str(trade.get("volume", 1)))
        self.max_slip_var.set(str(trade.get("max_entry_slip_atr", 0.5)))
        self.max_slip_points_var.set(str(trade.get("max_entry_slip_points", 0)))
        self.sizing_mode_var.set(str(risk.get("sizing_mode", "risk_pct")))
        self.capital_var.set(str(risk.get("capital", 1000000)))
        self.risk_pct_var.set(str(risk.get("risk_per_trade_pct", 0.5)))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

        self.margin_usage_pct_var.set(str(risk.get("margin_usage_pct", DEFAULT_MARGIN_USAGE_PCT)))
        self.margin_var.set(str(risk.get("margin_per_lot", 0) or 0))
        if hasattr(self, "bt_margin_usage_pct_var"):
            self.bt_margin_usage_pct_var.set(self.margin_usage_pct_var.get())
        from src.risk.universe_table import DEFAULT_REL_PATH, summarize_universe

        self.margin_table_path_var.set(
            str(risk.get("universe_table") or risk.get("margin_table") or DEFAULT_REL_PATH)
        )
        self.margin_map_var.set(
            summarize_universe(list(st.get("symbols") or []), risk.get("contracts") or {})
        )
        try:
            self._refresh_margin_table_summary()
        except Exception:  # noqa: BLE001
            pass
        self.scan_var.set(str(sch.get("scan_interval_sec", 30)))
        from src.strategy.session_scope import scope_from_cfg, scope_label

        self.session_scope_var.set(scope_label(scope_from_cfg(self.cfg)))
        self.eth_min_var.set(str(_eth_minutes_from_strategy(st)))
        body_box = st.get("daily_body") or {}
        trend_box = st.get("trend_follow") or {}
        st_box = st.get("supertrend") or {}
        dual_box = st.get("dual_ma") or {}
        self.body_stop_ticks_var.set(str(body_box.get("stop_extra_ticks", 1)))
        self.body_skip_gap_var.set(str(body_box.get("skip_gap_ticks", 50)))
        self.body_max_box_var.set(str(body_box.get("max_box_ticks", 100)))
        self.body_max_chase_var.set(str(body_box.get("max_chase_ticks", 20)))
        self.body_ema_period_var.set(str(body_box.get("ema_period", 20)))
        self.body_ema_buffer_var.set(str(body_box.get("ema_buffer_ticks", 2)))
        self.body_day_cutoff_var.set(str(body_box.get("day_cutoff", "10:30")))
        self.body_night_cutoff_var.set(str(body_box.get("night_cutoff", "22:15")))
        self.body_eth2_var.set(str(body_box.get("eth_minutes2", 30)))
        from src.strategy.market_state import range_params_for_daily_body

        body_rp = range_params_for_daily_body(self.cfg)
        self.body_filter_enabled_var.set(bool(body_rp.enabled))
        self.body_filter_adx_period_var.set(str(body_rp.adx_period))
        self.body_filter_atr_period_var.set(str(body_rp.atr_period))
        self.body_filter_lookback_var.set(str(body_rp.lookback))
        self.body_filter_adx_upper_var.set(f"{body_rp.adx_upper:g}")
        body_atr_high_ui = 0 if body_rp.atr_high_pct >= 0.999 else body_rp.atr_high_pct * 100
        self.body_filter_atr_high_var.set(f"{body_atr_high_ui:g}")
        from src.strategy.types import (
            entry_mode_from_cfg,
            entry_mode_label,
            stop_mode_from_cfg,
            stop_mode_label,
        )

        self.stop_mode_var.set(stop_mode_label(stop_mode_from_cfg(self.cfg)))
        self.entry_mode_var.set(entry_mode_label(entry_mode_from_cfg(self.cfg)))
        from src.strategy.generic_stop import generic_stop_from_cfg, generic_stop_label

        gs_cfg = generic_stop_from_cfg(self.cfg)
        self.generic_stop_var.set(generic_stop_label(gs_cfg.kind))
        self.generic_stop_lookback_var.set(str(gs_cfg.lookback))
        self.generic_stop_sar_start_var.set(f"{gs_cfg.sar_af_start:g}")
        self.generic_stop_sar_step_var.set(f"{gs_cfg.sar_af_step:g}")
        self.generic_stop_sar_max_var.set(f"{gs_cfg.sar_af_max:g}")
        self.generic_stop_atr_period_var.set(str(gs_cfg.atr_period))
        self.generic_stop_atr_mult_var.set(f"{gs_cfg.atr_mult:g}")
        self.generic_stop_be_atr_var.set(f"{gs_cfg.be_atr_mult:g}")
        self._fill_trend_filter_vars()
        self.trend_entry_bars_var.set(str(trend_box.get("entry_bars", 36)))
        self.trend_atr_mult_var.set(str(trend_box.get("atr_mult", 2.0)))
        self.st_atr_period_var.set(str(st_box.get("atr_period", 10)))
        self.st_multiplier_var.set(str(st_box.get("multiplier", 3.0)))
        self.dual_short_var.set(str(dual_box.get("short_period", 20)))
        self.dual_long_var.set(str(dual_box.get("long_period", 60)))
        self.dual_reverse_var.set(bool(dual_box.get("reverse_on_cross", True)))
        boll_box = st.get("bollinger") or {}
        self.boll_window_var.set(str(boll_box.get("boll_window", 20)))
        self.boll_dev_var.set(str(boll_box.get("boll_dev", 2.0)))
        self.boll_atr_mult_var.set(str(boll_box.get("atr_stop_mult", 1.5)))
        sar_box = st.get("sar_trend") or {}
        self.sar_ma_filter_var.set(str(sar_box.get("ma_filter", 20)))
        self.sar_af_step_var.set(str(sar_box.get("af_step", 0.02)))
        multi_box = st.get("multi_factor") or {}
        self.multi_threshold_var.set(str(multi_box.get("threshold", 0.6)))
        self.multi_ma_short_var.set(str(multi_box.get("ma_short", 20)))
        self.multi_ma_long_var.set(str(multi_box.get("ma_long", 60)))
        self.multi_channel_var.set(str(multi_box.get("channel", 20)))
        self.multi_atr_mult_var.set(str(multi_box.get("atr_stop_mult", 2.0)))
        self.multi_disagree_var.set(str(multi_box.get("disagree_max", 0.7)))
        self.multi_use_sar_var.set(bool(multi_box.get("use_sar", True)))
        vote_box = st.get("multi_vote") or {}
        hybrid_box = st.get("multi_hybrid") or {}
        adapt_box = st.get("multi_factor_adapt") or {}
        self.adapt_enabled_var.set(bool(adapt_box.get("adapt_enabled", True)))
        self.adapt_lookback_var.set(str(adapt_box.get("lookback_trades", 20)))
        self.adapt_min_w_var.set(str(adapt_box.get("min_weight", 0.05)))
        self.adapt_max_w_var.set(str(adapt_box.get("max_weight", 0.40)))
        self.adapt_mix_var.set(str(adapt_box.get("recent_mix", 0.30)))
        self.adapt_step_var.set(str(adapt_box.get("max_step", 0.03)))
        self.multi_min_votes_var.set(
            str(vote_box.get("min_votes", hybrid_box.get("min_votes", 2)))
        )
        self.multi_vote_diff_var.set(
            str(vote_box.get("min_vote_diff", hybrid_box.get("min_vote_diff", 2)))
        )
        self.hybrid_min_ratio_var.set(str(hybrid_box.get("min_position_ratio", 0.5)))
        self.hybrid_max_ratio_var.set(str(hybrid_box.get("max_position_ratio", 1.0)))
        self._fill_cta_filter_vars(multi_box, vote_box, hybrid_box, adapt_box)
        swing_box = st.get("swing_trend") or {}
        from src.strategy.swing_trend import DEFAULT_SCALE, resolve_scale_pair, scale_label

        scale_code, _t, _s = resolve_scale_pair(swing_box if isinstance(swing_box, dict) else {})
        self.swing_scale_var.set(self._swing_label_by_scale.get(scale_code, scale_label(DEFAULT_SCALE)))
        self.swing_daily_ma_var.set(str(swing_box.get("daily_ma", 20)))
        self.swing_unit_var.set(str(swing_box.get("unit_volume", 1)))
        self.swing_max_adds_var.set(str(swing_box.get("max_adds", 2)))
        self.swing_add_atr_var.set(str(swing_box.get("add_min_atr", 1.0)))
        self.swing_reduce_atr_var.set(str(swing_box.get("reduce_extend_atr", 2.5)))
        jesse_box = st.get("jesse_casper") or {}
        self.jesse_lookback_var.set(str(jesse_box.get("lookback_liq", 20)))
        self.jesse_vol_mult_var.set(str(jesse_box.get("vol_mult", 1.2)))
        self.jesse_min_rr_var.set(str(jesse_box.get("min_rr", 3.0)))
        self.jesse_tick_var.set(str(jesse_box.get("price_tick", 1.0)))
        self.jesse_use_va_var.set(bool(jesse_box.get("use_va", True)))
        self.jesse_use_kill_var.set(bool(jesse_box.get("use_kill_zone", True)))
        self.jesse_use_halt_var.set(bool(jesse_box.get("use_daily_halt", True)))
        self.flatten_var.set(",".join(str(x) for x in st.get("flatten_times", ["14:55", "22:55"])))
        self.flatten_at_close_var.set(bool(st.get("flatten_at_close", True)))
        rt = self.cfg.get("runtime") or {}
        self.auto_shutdown_var.set(bool(rt.get("auto_shutdown_after_close", False)))
        self.broker_var.set(str(acc.get("broker", "")))
        self.channel_var.set(str(acc.get("channel", "")))
        self.user_var.set(str(acc.get("user_id", "")))
        self.pwd_env_var.set(str(acc.get("password_env", "FUTURES_PASSWORD")))
        self.password_var.set(str(secrets.get("password", "") or ""))
        self.captcha_var.set(str(acc.get("captcha_mode", "pause")))
        self.auto_login_var.set(not bool(acc.get("skip_auto_login", True)))
        self.app_path_var.set(str(app.get("path", "")))
        self.app_title_var.set(str(app.get("window_title", "期货通")))
        self.startup_wait_var.set(str(app.get("startup_wait_sec", 15)))
        self.max_loss_var.set(str(risk.get("max_daily_loss", 1000)))
        self.min_interval_var.set(str(risk.get("min_order_interval_sec", 60)))
        self.max_orders_var.set(str(risk.get("max_orders_per_day", 20)))
        from src.risk.dynamic import DynamicExposure

        _dyn = DynamicExposure.from_config(self.cfg)
        self.dyn_enabled_var.set(bool(_dyn.enabled))
        self.dyn_dd_half_var.set(str(_dyn.dd_half_pct))
        self.dyn_dd_pause_var.set(str(_dyn.dd_pause_pct))
        self.dyn_atr_half_var.set(str(_dyn.atr_half_mult))
        self.dyn_atr_pause_var.set(str(_dyn.atr_pause_mult))
        self._copy_risk_to_backtest()

    def _dynamic_cfg_from_ui(self) -> dict[str, Any]:
        """配置页动态减仓几个框收成一段配置。数字填错就用默认。"""
        from src.risk.dynamic import DynamicExposure

        fallback = DynamicExposure()
        try:
            dd_half = float(self.dyn_dd_half_var.get() or fallback.dd_half_pct)
        except ValueError:
            dd_half = fallback.dd_half_pct
        try:
            dd_pause = float(self.dyn_dd_pause_var.get() or fallback.dd_pause_pct)
        except ValueError:
            dd_pause = fallback.dd_pause_pct
        try:
            atr_half = max(1.01, float(self.dyn_atr_half_var.get() or fallback.atr_half_mult))
        except ValueError:
            atr_half = fallback.atr_half_mult
        try:
            atr_pause = max(1.01, float(self.dyn_atr_pause_var.get() or fallback.atr_pause_mult))
        except ValueError:
            atr_pause = fallback.atr_pause_mult
        return {
            "enabled": bool(self.dyn_enabled_var.get()),
            "dd_half_pct": dd_half,
            "dd_pause_pct": dd_pause,
            "atr_half_mult": atr_half,
            "atr_pause_mult": atr_pause,
            "atr_period": fallback.atr_period,
            "atr_lookback": fallback.atr_lookback,
            "reduce_held": True,
        }

    def _scan_interval_value(self) -> int:
        """
        扫描间隔秒。

        读界面上的值并夹在 5~600 秒：太小会把行情源打到限流（下载失败会退化成收盘判止损），
        太大则止损发现明显滞后。空着或写错按 30 秒。
        """
        try:
            value = int(str(self.scan_var.get()).strip())
        except (TypeError, ValueError):
            value = 30
        return max(5, min(600, value))

    def _apply_ui_to_cfg(self) -> dict[str, Any]:
        """把表单写回内存 cfg，并处理密码。"""
        self.cfg["dry_run"] = bool(self.dry_var.get())
        from src.data.quote_check import normalize_source

        data = self.cfg.setdefault("data", {})
        data["source"] = normalize_source(self.source_var.get())
        if data["source"] == "ths":
            # 同花顺图不能当实盘来源。误选时退回新浪，避免扫描去抢鼠标。
            data["source"] = "akshare"
        data.pop("quote_check", None)
        # 天勤账号进主配置；密码不写这里，保存时进 secrets.local.yaml。
        tq_box = data.get("tq") if isinstance(data.get("tq"), dict) else {}
        tq_box["user"] = self.tq_user_var.get().strip()
        data["tq"] = tq_box

        st = self.cfg.setdefault("strategy", {})
        st["engine"] = self.engine_var.get()
        st["timeframe"] = self._period_code(self.tf_var, self.tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.trail_var, self.trail_custom_var)
        # 以关注下拉框里的整份名单为准。选中的排在第一个，其余合约也要留下来。
        st["symbols"] = list(self._watch_symbols)
        st["flatten_times"] = self._parse_list(self.flatten_var.get())
        st["flatten_at_close"] = bool(self.flatten_at_close_var.get())

        body = st.setdefault("daily_body", {})
        try:
            body["eth_minutes"] = max(0, int(self.eth_min_var.get()))
        except ValueError:
            body["eth_minutes"] = 20
        try:
            body["stop_extra_ticks"] = max(0, int(self.body_stop_ticks_var.get()))
        except ValueError:
            body["stop_extra_ticks"] = 1
        try:
            body["skip_gap_ticks"] = max(0, int(self.body_skip_gap_var.get()))
        except ValueError:
            body["skip_gap_ticks"] = 50
        try:
            body["max_box_ticks"] = max(0, int(self.body_max_box_var.get()))
        except ValueError:
            body["max_box_ticks"] = 100
        try:
            body["max_chase_ticks"] = max(0, int(self.body_max_chase_var.get()))
        except ValueError:
            body["max_chase_ticks"] = 20
        try:
            body["ema_period"] = max(0, int(self.body_ema_period_var.get()))
        except ValueError:
            body["ema_period"] = 20
        try:
            body["ema_buffer_ticks"] = max(0, int(self.body_ema_buffer_var.get()))
        except ValueError:
            body["ema_buffer_ticks"] = 2
        body["day_cutoff"] = _clock_text(self.body_day_cutoff_var.get(), "10:30")
        body["night_cutoff"] = _clock_text(self.body_night_cutoff_var.get(), "22:15")
        try:
            body["eth_minutes2"] = max(0, int(self.body_eth2_var.get()))
        except ValueError:
            body["eth_minutes2"] = 30
        from src.strategy.types import entry_mode_from_label, stop_mode_from_label

        # 止损/开仓触发方式都是全策略公共设置，写在 strategy 根。
        st["stop_mode"] = stop_mode_from_label(self.stop_mode_var.get())
        st["entry_mode"] = entry_mode_from_label(self.entry_mode_var.get())
        # 通用止损：独立于策略自身止损，写在 strategy.generic_stop。
        from src.strategy.generic_stop import generic_stop_from_label

        gs = st.setdefault("generic_stop", {})
        if not isinstance(gs, dict):
            gs = {}
            st["generic_stop"] = gs
        gs["kind"] = generic_stop_from_label(self.generic_stop_var.get())

        def _num(var, default: float, low: float, high: float) -> float:
            """界面参数统一收口：填错就用默认值，超范围夹到边界。"""
            try:
                value = float(var.get())
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        gs["lookback"] = int(_num(self.generic_stop_lookback_var, 20.0, 2.0, 200.0))
        gs["sar_af_start"] = _num(self.generic_stop_sar_start_var, 0.02, 0.001, 0.5)
        gs["sar_af_step"] = _num(self.generic_stop_sar_step_var, 0.02, 0.001, 0.5)
        gs["sar_af_max"] = _num(self.generic_stop_sar_max_var, 0.2, 0.05, 0.9)
        gs["atr_period"] = int(_num(self.generic_stop_atr_period_var, 14.0, 2.0, 200.0))
        gs["atr_mult"] = _num(self.generic_stop_atr_mult_var, 3.0, 0.1, 10.0)
        gs["be_atr_mult"] = _num(self.generic_stop_be_atr_var, 1.0, 0.1, 10.0)
        body["filter_enabled"] = bool(self.body_filter_enabled_var.get())
        body["filter_adx_period"] = int(_num(self.body_filter_adx_period_var, 14.0, 2.0, 200.0))
        body["filter_atr_period"] = int(_num(self.body_filter_atr_period_var, 14.0, 2.0, 200.0))
        body["filter_lookback"] = int(_num(self.body_filter_lookback_var, 60.0, 10.0, 500.0))
        body["filter_adx_upper"] = _num(self.body_filter_adx_upper_var, 30.0, 0.0, 90.0)
        body_high_raw = _num(self.body_filter_atr_high_var, 80.0, 0.0, 100.0)
        body["filter_atr_high_pct"] = 0.0 if body_high_raw <= 1 else body_high_raw / 100.0
        st.pop("market_filter", None)
        # 旧版 ATR 底线 / 回撤止损已取消，保存时清掉，避免配置里还留着误导人。
        body.pop("initial_atr_mult", None)
        body.pop("reverse_atr_mult", None)
        # 止损触发方式已挪到 strategy 根，旧键顺手清掉。
        body.pop("stop_mode", None)

        trend_box = st.setdefault("trend_follow", {})
        try:
            trend_box["entry_bars"] = max(5, int(self.trend_entry_bars_var.get()))
        except ValueError:
            trend_box["entry_bars"] = 36
        try:
            trend_box["atr_mult"] = max(0.1, float(self.trend_atr_mult_var.get()))
        except ValueError:
            trend_box["atr_mult"] = 2.0

        st_box = st.setdefault("supertrend", {})
        try:
            st_box["atr_period"] = max(2, int(self.st_atr_period_var.get()))
        except ValueError:
            st_box["atr_period"] = 10
        try:
            st_box["multiplier"] = max(0.1, float(self.st_multiplier_var.get()))
        except ValueError:
            st_box["multiplier"] = 3.0

        dual_box = st.setdefault("dual_ma", {})
        try:
            dual_box["short_period"] = max(2, int(self.dual_short_var.get()))
        except ValueError:
            dual_box["short_period"] = 20
        try:
            dual_box["long_period"] = max(3, int(self.dual_long_var.get()))
        except ValueError:
            dual_box["long_period"] = 60
        dual_box["reverse_on_cross"] = bool(self.dual_reverse_var.get())
        boll_box = st.setdefault("bollinger", {})
        try:
            boll_box["boll_window"] = max(10, int(self.boll_window_var.get()))
        except ValueError:
            boll_box["boll_window"] = 20
        try:
            boll_box["boll_dev"] = max(0.5, float(self.boll_dev_var.get()))
        except ValueError:
            boll_box["boll_dev"] = 2.0
        try:
            boll_box["atr_stop_mult"] = max(0.1, float(self.boll_atr_mult_var.get()))
        except ValueError:
            boll_box["atr_stop_mult"] = 1.5
        sar_box = st.setdefault("sar_trend", {})
        try:
            sar_box["ma_filter"] = max(2, int(self.sar_ma_filter_var.get()))
        except ValueError:
            sar_box["ma_filter"] = 20
        try:
            sar_box["af_step"] = max(0.005, float(self.sar_af_step_var.get()))
        except ValueError:
            sar_box["af_step"] = 0.02
        multi_box = st.setdefault("multi_factor", {})
        try:
            multi_box["threshold"] = min(0.95, max(0.1, float(self.multi_threshold_var.get())))
        except ValueError:
            multi_box["threshold"] = 0.6
        try:
            multi_box["ma_short"] = max(2, int(self.multi_ma_short_var.get()))
        except ValueError:
            multi_box["ma_short"] = 20
        try:
            multi_box["ma_long"] = max(3, int(self.multi_ma_long_var.get()))
        except ValueError:
            multi_box["ma_long"] = 60
        if multi_box["ma_long"] <= multi_box["ma_short"]:
            multi_box["ma_long"] = multi_box["ma_short"] * 2
        try:
            multi_box["channel"] = max(5, int(self.multi_channel_var.get()))
        except ValueError:
            multi_box["channel"] = 20
        try:
            multi_box["atr_stop_mult"] = max(0.1, float(self.multi_atr_mult_var.get()))
        except ValueError:
            multi_box["atr_stop_mult"] = 2.0
        try:
            multi_box["disagree_max"] = max(0.1, float(self.multi_disagree_var.get()))
        except ValueError:
            multi_box["disagree_max"] = 0.7
        multi_box["use_sar"] = bool(self.multi_use_sar_var.get())
        vote_box = st.setdefault("multi_vote", {})
        hybrid_box = st.setdefault("multi_hybrid", {})
        try:
            vote_box["min_votes"] = max(1, int(self.multi_min_votes_var.get()))
        except ValueError:
            vote_box["min_votes"] = 2
        try:
            vote_box["min_vote_diff"] = max(0, int(self.multi_vote_diff_var.get()))
        except ValueError:
            vote_box["min_vote_diff"] = 2
        hybrid_box["min_votes"] = vote_box["min_votes"]
        hybrid_box["min_vote_diff"] = vote_box["min_vote_diff"]
        hybrid_box["threshold"] = multi_box["threshold"]
        hybrid_box["use_sar"] = multi_box["use_sar"]
        try:
            hybrid_box["min_position_ratio"] = min(
                1.0, max(0.1, float(self.hybrid_min_ratio_var.get()))
            )
        except ValueError:
            hybrid_box["min_position_ratio"] = 0.5
        try:
            hybrid_box["max_position_ratio"] = min(
                1.0, max(hybrid_box["min_position_ratio"], float(self.hybrid_max_ratio_var.get()))
            )
        except ValueError:
            hybrid_box["max_position_ratio"] = 1.0
        self._write_cta_filter_vars(multi_box, "multi_factor")
        self._write_cta_filter_vars(vote_box, "multi_vote")
        self._write_cta_filter_vars(hybrid_box, "multi_hybrid")
        adapt_box = st.setdefault("multi_factor_adapt", {})
        for key in (
            "ma_short",
            "ma_long",
            "channel",
            "boll_window",
            "boll_dev",
            "af_start",
            "af_step",
            "af_max",
            "threshold",
            "atr_period",
            "atr_stop_mult",
            "use_sar",
            "disagree_max",
            "bias_ma",
            "trend_timeframe",
            "weights",
            "flatten_at_close",
        ):
            if key in multi_box:
                adapt_box[key] = copy.deepcopy(multi_box[key]) if key == "weights" else multi_box[key]
        adapt_box["adapt_enabled"] = bool(self.adapt_enabled_var.get())
        try:
            adapt_box["lookback_trades"] = max(5, int(float(self.adapt_lookback_var.get())))
        except ValueError:
            adapt_box["lookback_trades"] = 20
        try:
            adapt_box["min_weight"] = min(0.2, max(0.0, float(self.adapt_min_w_var.get())))
        except ValueError:
            adapt_box["min_weight"] = 0.05
        try:
            adapt_box["max_weight"] = min(0.8, max(adapt_box["min_weight"] + 0.01, float(self.adapt_max_w_var.get())))
        except ValueError:
            adapt_box["max_weight"] = 0.40
        try:
            adapt_box["recent_mix"] = min(0.5, max(0.0, float(self.adapt_mix_var.get())))
        except ValueError:
            adapt_box["recent_mix"] = 0.30
        try:
            adapt_box["max_step"] = min(0.1, max(0.0, float(self.adapt_step_var.get())))
        except ValueError:
            adapt_box["max_step"] = 0.03
        self._write_cta_filter_vars(adapt_box, "multi_factor_adapt")
        swing_box = st.setdefault("swing_trend", {})
        from src.strategy.swing_trend import DEFAULT_SCALE, SCALE_PAIRS

        label_to_code = {label: code for code, label in self._swing_label_by_scale.items()}
        scale_code = label_to_code.get(str(self.swing_scale_var.get() or "").strip(), DEFAULT_SCALE)
        if scale_code not in SCALE_PAIRS:
            scale_code = DEFAULT_SCALE
        trend_tf, signal_tf = SCALE_PAIRS[scale_code]
        swing_box["scale"] = scale_code
        swing_box["trend_timeframe"] = trend_tf
        swing_box["signal_timeframe"] = signal_tf
        try:
            swing_box["daily_ma"] = max(2, int(self.swing_daily_ma_var.get()))
        except ValueError:
            swing_box["daily_ma"] = 20
        try:
            swing_box["unit_volume"] = max(1, int(self.swing_unit_var.get()))
        except ValueError:
            swing_box["unit_volume"] = 1
        try:
            swing_box["max_adds"] = max(0, int(self.swing_max_adds_var.get()))
        except ValueError:
            swing_box["max_adds"] = 2
        try:
            swing_box["add_min_atr"] = max(0.0, float(self.swing_add_atr_var.get()))
        except ValueError:
            swing_box["add_min_atr"] = 1.0
        try:
            swing_box["reduce_extend_atr"] = max(0.5, float(self.swing_reduce_atr_var.get()))
        except ValueError:
            swing_box["reduce_extend_atr"] = 2.5
        swing_box.setdefault("flatten_at_close", False)
        self._write_trend_filter_vars(trend_box, "trend_follow")
        self._write_trend_filter_vars(st_box, "supertrend")
        self._write_trend_filter_vars(dual_box, "dual_ma")
        self._write_trend_filter_vars(boll_box, "bollinger")
        self._write_trend_filter_vars(sar_box, "sar_trend")
        self._write_trend_filter_vars(swing_box, "swing_trend")
        jesse_box = st.setdefault("jesse_casper", {})
        try:
            jesse_box["lookback_liq"] = max(5, int(self.jesse_lookback_var.get()))
        except ValueError:
            jesse_box["lookback_liq"] = 20
        try:
            jesse_box["vol_mult"] = max(0.1, float(self.jesse_vol_mult_var.get()))
        except ValueError:
            jesse_box["vol_mult"] = 1.3
        try:
            jesse_box["min_rr"] = max(1.0, float(self.jesse_min_rr_var.get()))
        except ValueError:
            jesse_box["min_rr"] = 3.0
        try:
            jesse_box["price_tick"] = max(0.01, float(self.jesse_tick_var.get()))
        except ValueError:
            jesse_box["price_tick"] = 1.0
        jesse_box["use_va"] = bool(self.jesse_use_va_var.get())
        jesse_box["use_kill_zone"] = bool(self.jesse_use_kill_var.get())
        jesse_box["use_daily_halt"] = bool(self.jesse_use_halt_var.get())
        jesse_box.setdefault("flatten_at_close", True)
        jesse_box.setdefault("trend_timeframe", "15")
        # 保存前清掉已删策略残留，并补齐现有策略缺的默认键。
        from src.strategy.factory import sanitize_strategy_config

        sanitize_strategy_config(self.cfg)

        # 收盘后关机
        rt = self.cfg.setdefault("runtime", {})
        rt["auto_shutdown_after_close"] = bool(self.auto_shutdown_var.get())

        # 回测成交模拟：下一根开盘成交、滑点跳数。只影响回测/优化，配置页和历史回测页共用同一组。
        bt = self.cfg.setdefault("backtest", {})
        bt["fill_on_next_open"] = bool(self.bt_fill_next_open_var.get())
        try:
            bt["slippage_ticks"] = max(0.0, float(self.bt_slippage_var.get() or 0))
        except (TypeError, ValueError):
            bt["slippage_ticks"] = 1.0
        # 策略优化页的搜索设置：写进配置，下次打开还是上次的样子，不再关窗口就回默认。
        depth = self.opt_depth_var.get().strip()
        bt["optimize_depth"] = depth if depth in ("精简", "标准", "完整") else "标准"
        mode = self.opt_mode_var.get().strip()
        bt["optimize_mode"] = mode if mode in ("一次切断", "滚动前进") else "一次切断"
        try:
            bt["optimize_holdout_pct"] = min(80, max(0, int(float(self.opt_holdout_pct_var.get() or 30))))
        except (TypeError, ValueError):
            bt["optimize_holdout_pct"] = 30
        try:
            bt["optimize_walk_steps"] = min(10, max(1, int(float(self.opt_walk_steps_var.get() or 3))))
        except (TypeError, ValueError):
            bt["optimize_walk_steps"] = 3
        try:
            bt["max_trials"] = max(5, int(float(self.opt_trials_var.get() or 80)))
        except (TypeError, ValueError):
            bt["max_trials"] = 80

        trade = self.cfg.setdefault("trade", {})
        trade["executor"] = self.exec_var.get()
        from src.trader.go_live import apply_stage_to_cfg, normalize_stage

        apply_stage_to_cfg(self.cfg, normalize_stage(self.go_live_var.get()))

        ctp_box = trade.setdefault("ctp", {})
        if not isinstance(ctp_box, dict):
            ctp_box = {}
            trade["ctp"] = ctp_box
        from src.trader.go_live import account_kind_for_stage
        from src.trader.ctp_logic import ctp_kind_label

        # 账号种类以上线阶段为准，手工台和策略始终对同一本账。
        ctp_box["account_kind"] = account_kind_for_stage(self.go_live_var.get())
        self.ctp_kind_var.set(ctp_kind_label(ctp_box["account_kind"]))
        live_box = ctp_box.setdefault("live", {})
        if not isinstance(live_box, dict):
            live_box = {}
            ctp_box["live"] = live_box
        # 真实资金账号跟「资金账号」同一格，保存时抄一份到 live，界面不再单独填。
        live_box["user_id"] = str(self.user_var.get() or "").strip()
        live_box["td_front"] = str(self.ctp_live_td_var.get() or "").strip()
        live_box["md_front"] = str(self.ctp_live_md_var.get() or "").strip()
        if not str(live_box.get("broker_id") or "").strip():
            live_box["broker_id"] = str(ctp_box.get("broker_id") or "8080")
        sim_box = ctp_box.setdefault("sim", {})
        if not isinstance(sim_box, dict):
            sim_box = {}
            ctp_box["sim"] = sim_box
        # 顶层仍留给旧配置和仿真窗口。有值时同步进 sim 小节，避免两处对不上。
        for key in ("td_front", "md_front", "user_id", "broker_id"):
            top = str(ctp_box.get(key) or "").strip()
            if top and not str(sim_box.get(key) or "").strip():
                sim_box[key] = top
        try:
            trade["volume"] = int(self.volume_var.get())
        except ValueError:
            trade["volume"] = 1
        try:
            trade["max_entry_slip_atr"] = max(0.0, float(self.max_slip_var.get()))
        except ValueError:
            trade["max_entry_slip_atr"] = 0.5
        try:
            trade["max_entry_slip_points"] = max(0.0, float(self.max_slip_points_var.get()))
        except ValueError:
            trade["max_entry_slip_points"] = 0.0
        if st["symbols"]:
            trade["symbol"] = st["symbols"][0]

        risk_pre = self.cfg.setdefault("risk", {})
        risk_pre["sizing_mode"] = self.sizing_mode_var.get() or "risk_pct"
        try:
            risk_pre["capital"] = float(self.capital_var.get())
        except ValueError:
            risk_pre["capital"] = 1_000_000.0
        try:
            risk_pre["risk_per_trade_pct"] = float(self.risk_pct_var.get())
        except ValueError:
            risk_pre["risk_per_trade_pct"] = 0.5
        try:
            from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

            risk_pre["margin_usage_pct"] = max(
                0.0, float(self.margin_usage_pct_var.get() or DEFAULT_MARGIN_USAGE_PCT)
            )
        except ValueError:
            from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

            risk_pre["margin_usage_pct"] = DEFAULT_MARGIN_USAGE_PCT
        try:
            risk_pre["margin_per_lot"] = max(0.0, float(self.margin_var.get() or 0))
        except ValueError:
            risk_pre["margin_per_lot"] = 0.0
        # 关注合约+保证金只从总表读。
        table_rel = (
            str(self.margin_table_path_var.get() or "").strip() or "config/品种与合约.xlsx"
        )
        risk_pre["universe_table"] = table_rel
        risk_pre["margin_table"] = table_rel
        self._reload_margin_table(silent=True)
        # 「启用限制」只属于回测和优化这一次。留在这里的话，下次保存会把回测开关写进实盘配置。
        risk_pre.pop("apply_limits", None)

        sch = self.cfg.setdefault("schedule", {})
        sch["scan_interval_sec"] = self._scan_interval_value()
        from src.strategy.session_scope import scope_code

        sch["session_scope"] = scope_code(self.session_scope_var.get())

        acc = self.cfg.setdefault("account", {})
        acc["broker"] = self.broker_var.get().strip()
        acc["channel"] = self.channel_var.get().strip()
        acc["user_id"] = self.user_var.get().strip()
        acc["password_env"] = self.pwd_env_var.get().strip() or "FUTURES_PASSWORD"
        acc["captcha_mode"] = self.captcha_var.get()
        acc["skip_auto_login"] = not bool(self.auto_login_var.get())

        app = self.cfg.setdefault("app", {})
        app["path"] = self.app_path_var.get().strip()
        app["window_title"] = self.app_title_var.get().strip() or "期货通"
        try:
            app["startup_wait_sec"] = int(self.startup_wait_var.get())
        except ValueError:
            app["startup_wait_sec"] = 15

        risk = self.cfg.setdefault("risk", {})
        for key, var in (
            ("max_daily_loss", self.max_loss_var),
            ("min_order_interval_sec", self.min_interval_var),
            ("max_orders_per_day", self.max_orders_var),
        ):
            try:
                risk[key] = int(float(var.get()))
            except ValueError:
                pass
        # 已废弃「最大持仓手数」：保存时从配置里删掉，仓位只靠仓位上限%和日最大亏损。
        risk.pop("max_position", None)
        risk["dynamic"] = self._dynamic_cfg_from_ui()

        # 密码：写入本地密钥 + 当前进程环境
        pwd = self.password_var.get()
        pwd_env = acc["password_env"]
        if pwd:
            save_secrets(password=pwd)
            os.environ[pwd_env] = pwd
            acc["resolved_password"] = pwd
        else:
            # 保留已有 secrets，不覆盖为空
            existing = str(load_secrets().get("password", "") or "")
            acc["resolved_password"] = existing or os.environ.get(pwd_env, "")

        # 天勤密码同样只放本地密钥文件。留空表示不改已有密码。
        tq_pwd = self.tq_password_var.get()
        if tq_pwd:
            save_secrets(extra={"tq_password": tq_pwd})

        return self.cfg

    def _refresh_status_cards(self) -> None:
        running = self.worker.running
        snap = self.worker.status_snapshot() if running else {}
        # 过了零点也要换成新的一天。这里用的就是换日清零所用的日历日期。
        if hasattr(self, "card_date"):
            self.card_date.configure(text=date.today().isoformat())
        if running and snap.get("heartbeat_at"):
            mode_txt = f"运行中 · 心跳{str(snap['heartbeat_at'])[-8:]}"
            if snap.get("halted"):
                mode_txt = "已熔断"
            self.card_mode.configure(text=mode_txt, text_color=T.DANGER if snap.get("halted") else T.OK)
        else:
            self.card_mode.configure(
                text="运行中" if running else self._status,
                text_color=T.OK if running else T.TEXT,
            )
        eng = self.engine_var.get()
        self.card_engine.configure(text=ENGINE_LABELS.get(eng, eng))
        ex = self.exec_var.get()
        pnl = snap.get("daily_pnl")
        if running and pnl is not None:
            self.card_exec.configure(text=f"{EXEC_LABELS.get(ex, ex)} · 今日{pnl:+.0f}元")
        else:
            self.card_exec.configure(text=EXEC_LABELS.get(ex, ex))
        current = self.watch_choice_var.get().strip()
        if current in ("", "（还没有）"):
            current = self._watch_symbols[0] if self._watch_symbols else "-"
        self.card_symbols.configure(text=current)
        if hasattr(self, "hero_sub"):
            broker = str((self.cfg.get("account") or {}).get("broker") or "期货公司").strip()
            syms = self._watch_symbols
            sym_txt = " / ".join(syms[:3]) + (f" 等{len(syms)}个" if len(syms) > 3 else "")
            dry_txt = "模拟" if self.dry_var.get() else "实盘"
            from src.account.workspace import profile_of

            book_txt = profile_of(root=self.root_dir)["banner"]
            self.hero_sub.configure(
                text=f"{broker} · {sym_txt or '未选品种'} · 当前{dry_txt}（开关在下方控制台） · {book_txt}"
            )
        position_text = self._position_card_text(snap if running else None)
        self.card_position.configure(
            text=position_text,
            text_color=T.TEXT if position_text == "空仓" else T.OK,
        )
        if hasattr(self, "alert_line"):
            alert = snap.get("last_alert") if running else None
            if not isinstance(alert, dict):
                try:
                    from src.ops.alerts import last_alert

                    alert = last_alert(self.root_dir)
                except Exception:  # noqa: BLE001
                    alert = None
            if isinstance(alert, dict) and alert.get("message"):
                text = f"最近告警 {alert.get('ts', '')}  {alert.get('message')}"
                # 告警是当时写下的字，不会跟着配置页改。日亏限额已经调大时，避免误以为现在还是 50。
                if str(alert.get("code") or "") == "halt":
                    try:
                        now_limit = float(self.max_loss_var.get())
                    except (TypeError, ValueError):
                        now_limit = None
                    if now_limit is not None and f"{now_limit:.0f}" not in str(alert.get("message") or ""):
                        text += f"（旧记录，现限额 {now_limit:.0f} 元）"
                alert_text = text
            else:
                alert_text = ""
        else:
            alert_text = ""
        health_text = ""
        health_tone = T.TEXT_DIM
        if hasattr(self, "health_line"):
            health = snap.get("health") if running else None
            if not isinstance(health, dict):
                try:
                    from src.ops.health import evaluate_health

                    health = evaluate_health(self.root_dir).as_dict()
                except Exception:  # noqa: BLE001
                    health = None
            if isinstance(health, dict) and health.get("message"):
                health_text = str(health.get("message") or "")
                health_tone = T.DANGER if health.get("status") == "watch" else T.TEXT_DIM
        dyn_text = ""
        if hasattr(self, "dyn_line"):
            note = snap.get("dynamic") if running else None
            if not note:
                try:
                    from src.risk.dynamic import describe_status, live_equity_and_peak

                    preview = dict(self.cfg)
                    preview_risk = dict(preview.get("risk") or {})
                    preview_risk["dynamic"] = self._dynamic_cfg_from_ui()
                    preview["risk"] = preview_risk
                    equity, peak = live_equity_and_peak(self.root_dir)
                    note = describe_status(preview, equity=equity, peak=peak)
                except Exception:  # noqa: BLE001
                    note = ""
            dyn_text = str(note or "")
        flow = str(snap.get("ctp_flow") or "") if running else ""
        if flow:
            dyn_text = f"{flow}\n{dyn_text}" if dyn_text else flow
        if running and snap.get("orders_paused"):
            alert_text = (alert_text + "  " if alert_text else "") + "已暂停报单，开平仓不会发出去"
        self._show_status_notes(alert_text, health_text, health_tone, dyn_text)
        # 跑着：左边停止常驻，右边暂停/恢复报单。没跑：只能点启动。
        if running:
            paused = bool(snap.get("orders_paused"))
            self.btn_start.configure(
                state="normal",
                text="停止常驻",
                fg_color=T.DANGER,
                hover_color=T.DANGER_HOVER,
                text_color=T.DANGER_TEXT,
                command=self._on_halt_resident,
            )
            self.btn_stop.configure(
                state="normal",
                text="恢复报单" if paused else "暂停报单",
                fg_color=T.ACCENT if paused else T.DANGER,
                hover_color=T.ACCENT_HOVER if paused else T.DANGER_HOVER,
                text_color=T.ACCENT_ON if paused else T.DANGER_TEXT,
            )
        else:
            self.btn_start.configure(
                state="normal",
                text="启动常驻",
                fg_color=T.ACCENT,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON,
                command=self._on_start,
            )
            self.btn_stop.configure(
                state="disabled",
                text="暂停报单",
                fg_color=T.DANGER_MUTED,
                text_color_disabled=T.DANGER_TEXT_DISABLED,
            )
        # 运行页只补顶部没有的周期和开关。策略、通道、品种已经在状态栏。
        if hasattr(self, "route_summary"):
            bits = [
                f"信号{period_label(self._period_code(self.tf_var, self.tf_custom_var))}",
                f"止损{period_label(self._period_code(self.trail_var, self.trail_custom_var))}",
            ]
            self.route_summary.configure(text=" · ".join(bits))

    def _fit_status_note_wrap(self, _event: Any = None) -> None:
        """窗口拉宽时，三行提示跟着折行，避免字跑出右边缘。"""
        box = getattr(self, "status_notes", None)
        if box is None:
            return
        width = max(240, int(box.winfo_width() or 0) - 4)
        for name in ("alert_line", "health_line", "dyn_line"):
            label = getattr(self, name, None)
            if label is not None:
                label.configure(wraplength=width)

    def _show_status_notes(
        self,
        alert_text: str,
        health_text: str,
        health_tone: str,
        dyn_text: str,
    ) -> None:
        """
        有字才占一行。三句都可能很长，不并成一行，免得挤成看不清的一段。
        """
        if not hasattr(self, "alert_line"):
            return
        width = 900
        box = getattr(self, "status_notes", None)
        if box is not None:
            width = max(240, int(box.winfo_width() or 0) - 4) or 900
        rows = (
            (self.alert_line, (alert_text or "").strip(), T.DANGER),
            (self.health_line, (health_text or "").strip(), health_tone or T.TEXT_DIM),
            (self.dyn_line, (dyn_text or "").strip(), T.TEXT_DIM),
        )
        for label, _, _ in rows:
            label.pack_forget()
        for label, text, color in rows:
            if not text:
                label.configure(text="")
                continue
            label.configure(text=text, text_color=color, wraplength=width, height=18)
            label.pack(fill="x", pady=0)

    def _position_card_text(self, snap: dict[str, Any] | None) -> str:
        """
        顶部「程序持仓」的字。

        常驻开着时用内存里的仓，这样刚下完单马上能看见。
        没开常驻时读 data/bot_state.json，关掉再打开也能看到上次记下的仓。
        """
        from src.utils.state_store import format_position_text, load_state

        positions = (snap or {}).get("positions") if snap else None
        if positions is None:
            try:
                from src.utils.state_store import default_state_path

                positions = load_state(default_state_path(self.root_dir)).get("positions") or {}
            except Exception:  # noqa: BLE001
                positions = {}
        return format_position_text(positions)

    def _tick(self) -> None:
        """每 800ms 刷状态卡和账户余额。内部任何一步异常都不能让心跳链断掉。"""
        if self._closing:
            return
        try:
            self._refresh_status_cards()
        except Exception as exc:  # noqa: BLE001
            logger.warning("状态卡刷新跳过一轮: {}", exc)
        try:
            self._pull_signal_balance()
        except Exception as exc:  # noqa: BLE001
            logger.warning("账户余额刷新跳过一轮: {}", exc)
        finally:
            if not self._closing:
                try:
                    self._tick_after_id = self.after(800, self._tick)
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 按钮 ----------
    def _merge_disk_gui_coords(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """
        保存前合并磁盘上的最新 trade.gui，避免界面保存用旧坐标盖掉标定结果。
        """
        try:
            disk = load_config()
            disk_gui = ((disk.get("trade") or {}).get("gui") or {})
            if disk_gui:
                cfg.setdefault("trade", {}).setdefault("gui", {}).update(disk_gui)
        except Exception as e:  # noqa: BLE001
            logger.warning("合并磁盘坐标失败: {}", e)
        return cfg

    def _clone_cfg(self) -> dict[str, Any]:
        """
        拿出一份和配置页当前内容一样的配置副本。

        启动、单次扫描、回测、优化、复盘都用这份副本。
        副本上改策略或开仓限制，不会改到界面上的配置，也不会改到已经在跑的程序。
        """
        return copy.deepcopy(self._merge_disk_gui_coords(self._apply_ui_to_cfg()))

    def _cfg_for_live_scan(self) -> dict[str, Any]:
        """
        给常驻和单次扫描用的配置。

        关注列表可以有好几个具体合约，但同花顺一次只盯下拉框里选中的那一个。
        其余合约仍保存在配置里，下次可以再选，这一次不会一起扫描。
        """
        cfg = self._clone_cfg()
        current = self.watch_choice_var.get().strip()
        symbols = list((cfg.get("strategy") or {}).get("symbols") or [])
        if not current or current == "（还没有）":
            return cfg
        st = cfg.setdefault("strategy", {})
        st["symbols"] = [current]
        cfg.setdefault("trade", {})["symbol"] = current
        extras = [item for item in symbols if item.lower() != current.lower()]
        if extras:
            self._append_log(
                f"本次只盯 {current}。列表里还有 {'、'.join(extras)}，不会一起挂机。"
            )
        return cfg

    def _sync_bt_periods_after_save(
        self, old_tf: str, old_trail: str, cur_tf: str, cur_trail: str, cfg: dict[str, Any]
    ) -> None:
        """
        配置页保存后，把回测页还跟着旧配置的信号/止损周期一起换过来。

        回测页有自己的周期选择器（故意和实盘分开）。只有它当前值还是配置页上一次
        的值——也就是没在回测页单独改过——才跟着换，免得把特意选的周期冲掉。
        """
        st = cfg.get("strategy") or {}
        pairs = (
            ("信号周期", old_tf, cur_tf, str(st.get("timeframe") or ""),
             self.bt_tf_var, self.bt_tf_custom_var),
            ("止损周期", old_trail, cur_trail, str(st.get("trail_timeframe") or ""),
             self.bt_trail_var, self.bt_trail_custom_var),
        )
        for name, old, cur, new, var, custom in pairs:
            if new and old and cur == old and new != old:
                self._set_period_vars(new, var, custom)
                self._append_log(f"回测页{name}跟着配置页改成 {period_label(new)}。")

    def _on_save(self) -> None:
        # 记下配置页和回测页各自的信号/止损周期，保存后把「还跟着配置页」的那项一起换掉。
        old_st = self.cfg.get("strategy") or {}
        old_tf = str(old_st.get("timeframe") or "")
        old_trail = str(old_st.get("trail_timeframe") or "")
        cur_tf = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        cur_trail = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)
        # 保存前用最新余额填上账户权益。这个数不能在配置页手改。
        self._pull_signal_balance()
        # 扫描间隔夹到 5~600 秒，并把夹过的值显示回输入框，避免界面和配置对不上。
        self.scan_var.set(str(self._scan_interval_value()))
        cfg = self._merge_disk_gui_coords(self._apply_ui_to_cfg())
        path = save_config(cfg)
        self._sync_bt_periods_after_save(old_tf, old_trail, cur_tf, cur_trail, cfg)
        self._status = "已保存"
        self._append_log(f"配置已保存: {path}")
        # 常驻正在跑时，把新间隔推给它：主循环每轮都读这个属性，不用重启就能生效。
        bot = getattr(self.worker, "bot", None)
        if bot is not None and bool(getattr(bot, "is_running", False)):
            bot.scan_interval = int((cfg.get("schedule") or {}).get("scan_interval_sec") or 30)
            if hasattr(bot, "cfg"):
                bot.cfg.setdefault("schedule", {})["trading_sessions"] = list(
                    (cfg.get("schedule") or {}).get("trading_sessions") or []
                )
                risk_box = (cfg.get("risk") or {}).get("contracts")
                if isinstance(risk_box, dict):
                    bot.cfg.setdefault("risk", {})["contracts"] = risk_box
            if hasattr(bot, "_refresh_sessions"):
                bot._refresh_sessions()
            self._append_log(
                f"扫描间隔已改为 {bot.scan_interval} 秒；当前合约时段已同步，下一轮扫描起生效。"
            )
        if self.password_var.get():
            self._append_log("密码已写入 config/secrets.local.yaml（本地私密）")
        try:
            from src.ui.config_presets import add_preset

            entry = add_preset(self.root_dir, cfg)
            self._refresh_config_presets(select_id=str(entry.get("id") or ""))
            self._append_log(f"配置存档已留下: {entry.get('name')}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("留下配置存档失败")
            self._append_log(f"配置文件已保存，但存档没有留下: {exc}")
        self._refresh_status_cards()

    def _refresh_config_presets(self, select_id: str | None = None) -> None:
        """刷新配置页的存档下拉。select_id 有值时停在刚保存的那一份。"""
        if not hasattr(self, "cfg_preset_menu"):
            return
        from src.ui.config_presets import load_presets

        items = load_presets(self.root_dir)
        self._cfg_preset_ids = {}
        labels: list[str] = []
        chosen = "（还没有保存过配置）"
        for item in items:
            label = str(item.get("name") or item.get("id") or "")
            vid = str(item.get("id") or "")
            if not label or not vid:
                continue
            if label in self._cfg_preset_ids:
                label = f"{label} ({vid})"
            self._cfg_preset_ids[label] = vid
            labels.append(label)
            if select_id and vid == select_id:
                chosen = label
        if not labels:
            labels = ["（还没有保存过配置）"]
        elif select_id is None:
            chosen = labels[0]
        self.cfg_preset_menu.configure(values=labels)
        self.cfg_preset_var.set(chosen if chosen in labels else labels[0])

    def _on_apply_config_preset(self) -> None:
        """把选中的存档写回配置页和配置文件。常驻正在跑时不应用。"""
        from tkinter import messagebox

        from src.ui.config_presets import apply_preset_to_config, get_preset, switch_summary
        from src.utils.config_loader import load_config

        label = self.cfg_preset_var.get()
        vid = (self._cfg_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("还没有存档", "请先点「保存全部配置」，再来一键应用。")
            return
        if self.worker.running:
            messagebox.showwarning(
                "请先停止常驻",
                "程序正在跑，不会自己换成新开关。\n请先点「停止常驻」，再一键应用，然后重新启动。",
            )
            return
        entry = get_preset(self.root_dir, vid)
        saved = (entry or {}).get("config")
        if not isinstance(saved, dict):
            messagebox.showerror("找不到存档", "这一份配置读不出来，请重新保存一次。")
            return
        if not messagebox.askyesno(
            "一键应用配置",
            f"将用这一份盖住当前配置页，并写入配置文件：\n\n{label}\n\n{switch_summary(saved)}\n\n确定？",
        ):
            return
        merged = apply_preset_to_config(load_config(), saved)
        save_config(self._merge_disk_gui_coords(merged))
        self._reload_values_only()
        self._refresh_status_cards()
        self._append_log(f"已一键应用配置: {label}")
        messagebox.showinfo("已应用", "配置页已经换成这一份。请再看一眼开关，然后启动常驻。")

    def _on_delete_config_preset(self) -> None:
        """删掉下拉框里选中的那一份配置存档。不改当前正在用的配置。"""
        from tkinter import messagebox

        from src.ui.config_presets import delete_preset

        label = self.cfg_preset_var.get()
        vid = (self._cfg_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("没有可删的存档", "请先在下拉框里选一份已经保存的配置。")
            return
        if not messagebox.askyesno("删除配置存档", f"只删除这一份备份，当前配置不会变：\n\n{label}\n\n确定删除？"):
            return
        try:
            removed = delete_preset(self.root_dir, vid)
        except ValueError as exc:
            messagebox.showwarning("没有删掉", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除配置存档失败")
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_config_presets()
        self._append_log(f"已删除配置存档: {removed}")

    def _refresh_coord_presets(self, select_id: str | None = None) -> None:
        """刷新下单坐标列表。select_id 有值时停在刚保存或刚改名的那一份。"""
        if not hasattr(self, "coord_preset_menu"):
            return
        from src.ui.gui_coord_presets import load_coord_presets

        items = load_coord_presets(self.root_dir)
        self._coord_preset_ids = {}
        labels: list[str] = []
        chosen = "（还没有保存过坐标）"
        for item in items:
            label = str(item.get("name") or item.get("id") or "")
            vid = str(item.get("id") or "")
            if not label or not vid:
                continue
            if label in self._coord_preset_ids:
                label = f"{label} ({vid})"
            self._coord_preset_ids[label] = vid
            labels.append(label)
            if select_id and vid == select_id:
                chosen = label
        if not labels:
            labels = ["（还没有保存过坐标）"]
        elif select_id is None:
            chosen = labels[0]
        self.coord_preset_menu.configure(values=labels)
        self.coord_preset_var.set(chosen if chosen in labels else labels[0])
        self._sync_coord_name_box()

    def _sync_coord_name_box(self, _label: str | None = None) -> None:
        """下拉换了一套时，名字框跟着换成这一套现在的名字。"""
        if not hasattr(self, "coord_name_var"):
            return
        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label, "")
        if not vid:
            self.coord_name_var.set("")
            return
        from src.ui.gui_coord_presets import get_coord_preset

        entry = get_coord_preset(self.root_dir, vid) or {}
        self.coord_name_var.set(str(entry.get("name") or label))

    def _on_save_coord_preset(self) -> None:
        """把配置文件里现在的下单点存成一份。名字空着就用时间。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import add_coord_preset, get_coord_preset
        from src.utils.config_loader import load_config

        gui = ((load_config().get("trade") or {}).get("gui") or {})
        # 名字框里如果还是上一套的名字，就按时间新存一份，避免盖成「家里显示器-2」。
        typed = self.coord_name_var.get().strip()
        selected = (self._coord_preset_ids or {}).get(self.coord_preset_var.get(), "")
        current_name = str((get_coord_preset(self.root_dir, selected) or {}).get("name") or "") if selected else ""
        name = "" if typed == current_name else typed
        try:
            entry = add_coord_preset(self.root_dir, gui, name=name)
        except ValueError as exc:
            messagebox.showwarning("还不能保存", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存下单坐标失败")
            messagebox.showerror("保存失败", str(exc))
            return
        self._refresh_coord_presets(select_id=str(entry.get("id") or ""))
        self._append_log(f"已保存下单坐标: {entry.get('name')}")

    def _on_apply_coord_preset(self) -> None:
        """把选中的一套点写成当前下单坐标。常驻正在跑时不换。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import apply_coords_to_config, get_coord_preset, write_gui_coords_file
        from src.utils.config_loader import load_config

        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("还没有坐标", "请先标定并点「保存当前坐标」。")
            return
        if self.worker.running:
            messagebox.showwarning("请先停止常驻", "程序正在跑，不会中途换点击位置。\n请先点「停止常驻」，再选用坐标，然后重新启动。")
            return
        entry = get_coord_preset(self.root_dir, vid) or {}
        coords = entry.get("coords")
        if not isinstance(coords, dict):
            messagebox.showerror("找不到坐标", "这一份读不出来，请重新保存一次。")
            return
        if not messagebox.askyesno("使用这套坐标", f"接下来下单会点这一套位置：\n\n{label}\n\n确定？"):
            return
        merged = apply_coords_to_config(load_config(), coords)
        save_config(merged)
        write_gui_coords_file(self.root_dir, coords)
        self.cfg = load_config()
        self._append_log(f"已改用下单坐标: {label}")
        messagebox.showinfo("已换上", "当前下单坐标已经换成这一套。请再启动常驻。")

    def _on_rename_coord_preset(self) -> None:
        """把选中的坐标改成输入框里的名字。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import rename_coord_preset

        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("没有可改的坐标", "请先保存一套坐标。")
            return
        try:
            new_name = rename_coord_preset(self.root_dir, vid, self.coord_name_var.get())
        except ValueError as exc:
            messagebox.showwarning("改名没有成功", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("下单坐标改名失败")
            messagebox.showerror("改名失败", str(exc))
            return
        self._refresh_coord_presets(select_id=vid)
        self._append_log(f"下单坐标已改名: {new_name}")

    def _on_delete_coord_preset(self) -> None:
        """删掉选中的坐标备份。正在用的点不受影响。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import delete_coord_preset

        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("没有可删的坐标", "请先在列表里选一份已经保存的坐标。")
            return
        if not messagebox.askyesno("删除坐标", f"只删除这一份备份，当前下单位置不会变：\n\n{label}\n\n确定删除？"):
            return
        try:
            removed = delete_coord_preset(self.root_dir, vid)
        except ValueError as exc:
            messagebox.showwarning("没有删掉", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除下单坐标失败")
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_coord_presets()
        self._append_log(f"已删除下单坐标: {removed}")

    def _on_reload(self) -> None:
        self._reload_values_only()
        self._status = "已重载"
        self._append_log("已从磁盘重新加载配置与本地密码。")
        self._refresh_status_cards()

    def _on_ctp_probe(self) -> None:
        """打开 CTP 交易窗口。不改当前执行通道，也不碰同花顺。"""
        from src.ui.ctp_probe import open_ctp_probe

        open_ctp_probe(self)

    def _on_go_live_change(self) -> None:
        """换上线阶段。没走完前一步时退回原来的选择。"""
        from tkinter import messagebox

        from src.trader.go_live import (
            apply_stage_to_cfg,
            load_state as load_go_live,
            normalize_stage,
            save_state,
            stage_hint,
            stage_label,
            why_cannot_enter,
        )

        target = normalize_stage(self.go_live_var.get())
        state = load_go_live(self.root_dir)
        if target == normalize_stage(state.get("stage")):
            apply_stage_to_cfg(self.cfg, target)
            self.go_live_hint_var.set(stage_hint(target))
            return
        blocked = why_cannot_enter(state, target)
        if blocked:
            messagebox.showwarning("还不能进入这一步", blocked)
            self.go_live_var.set(stage_label(state.get("stage")))
            self.go_live_hint_var.set(stage_hint(state.get("stage")))
            return
        state["stage"] = target
        save_state(self.root_dir, state)
        apply_stage_to_cfg(self.cfg, target)
        self.exec_var.set(str((self.cfg.get("trade") or {}).get("executor", self.exec_var.get())))
        from src.trader.ctp_logic import ctp_kind_label

        self.ctp_kind_var.set(ctp_kind_label(((self.cfg.get("trade") or {}).get("ctp") or {}).get("account_kind")))
        self.go_live_hint_var.set(stage_hint(target))
        self._append_log(f"上线阶段已改为{stage_label(target)}")

    def _on_go_live_done(self) -> None:
        """勾选当前阶段完成，能进下一步就问要不要换过去。"""
        from tkinter import messagebox

        from src.trader.go_live import (
            apply_stage_to_cfg,
            load_state as load_go_live,
            mark_stage_passed,
            next_stage,
            normalize_stage,
            save_state,
            stage_hint,
            stage_label,
            why_cannot_enter,
        )

        current = normalize_stage(self.go_live_var.get())
        state = load_go_live(self.root_dir)
        ok, message = mark_stage_passed(state, current)
        if not ok:
            messagebox.showwarning("还不能勾完成", message)
            return
        state["stage"] = current
        save_state(self.root_dir, state)
        self._append_log(message)
        nxt = next_stage(current)
        if not nxt:
            messagebox.showinfo("上线流程", "四步都已走到策略自动真实账户。请确认真实前置和资金账号后再启动常驻。")
            return
        if why_cannot_enter(state, nxt):
            messagebox.showinfo("本阶段已勾完成", message)
            return
        if messagebox.askyesno("进入下一步？", f"{message}\n\n要进入{stage_label(nxt)}吗？"):
            state["stage"] = nxt
            save_state(self.root_dir, state)
            apply_stage_to_cfg(self.cfg, nxt)
            self.go_live_var.set(stage_label(nxt))
            self.go_live_hint_var.set(stage_hint(nxt))
            self.exec_var.set(str((self.cfg.get("trade") or {}).get("executor", self.exec_var.get())))
            from src.trader.ctp_logic import ctp_kind_label

            self.ctp_kind_var.set(
                ctp_kind_label(((self.cfg.get("trade") or {}).get("ctp") or {}).get("account_kind"))
            )
            self._append_log(f"已进入{stage_label(nxt)}")

    def _on_start(self) -> None:
        from tkinter import messagebox

        if self._prep_busy:
            messagebox.showwarning("正在准备", "同花顺还在登录或改价格。这几步做完之前不会启动常驻。")
            return
        if self.worker.running:
            self._on_halt_resident()
            return

        from src.trader.go_live import (
            allows_strategy_orders,
            apply_stage_to_cfg,
            load_state as load_go_live,
            normalize_stage,
            stage_label,
            why_cannot_enter,
        )

        stage_now = normalize_stage(self.go_live_var.get())
        apply_stage_to_cfg(self.cfg, stage_now)
        if allows_strategy_orders(stage_now):
            self.exec_var.set("ctp")
        if self.exec_var.get() == "ctp" and not allows_strategy_orders(stage_now):
            messagebox.showinfo(
                "这一步请用交易台",
                f"当前是{stage_label(stage_now)}。请点「打开交易台」手工下单。\n"
                "策略自动下单要等到③策略自动仿真或④策略自动真实账户。",
            )
            return
        if allows_strategy_orders(stage_now):
            blocked = why_cannot_enter(load_go_live(self.root_dir), stage_now)
            if blocked:
                messagebox.showerror("还不能让策略自动下单", blocked)
                return
        if self.exec_var.get() == "ctp":
            from src.account.ctp_sync import resident_book_guard

            book_block = resident_book_guard(self.root_dir, self.cfg)
            if book_block:
                messagebox.showerror("先核对账户", book_block)
                self._append_log("未启动常驻：" + book_block)
                return
            from src.trader.go_live import STAGE_AUTO_LIVE

            if stage_now == STAGE_AUTO_LIVE:
                if not messagebox.askyesno(
                    "真实资金",
                    "策略将用和交易台同一套接口，向真实资金账户报单。\n确认前置、资金账号都填对了吗？",
                ):
                    return
        if self.exec_var.get() == "ctp":
            from src.trader.ctp_logic import ctp_kind_label

            kind_txt = ctp_kind_label(self.ctp_kind_var.get())
            self._append_log(
                f"上线阶段{stage_label(stage_now)}。策略走和交易台同一套 CTP 接口（{kind_txt}）。"
            )
            if self.dry_var.get():
                choice = messagebox.askyesnocancel(
                    "模拟模式还开着",
                    "执行通道已经是 CTP，但「模拟模式」还开着：策略不会向柜台发单。\n\n"
                    "是 = 关掉模拟，允许策略用当前操作账报单\n"
                    "否 = 继续只监测、不报单\n"
                    "取消 = 不启动",
                )
                if choice is None:
                    self._skip_next_bot_login = False
                    self._append_log("已取消启动。")
                    return
                if choice:
                    self.dry_var.set(False)
                    self._append_log("已关闭模拟模式，策略将向 CTP 报单。")
                else:
                    self._append_log("保持模拟模式：只监测，不向柜台发单。")

        # 启动时若仍开着模拟，而通道是 gui：明确问要不要真下单
        # 原理：点启动会把界面开关写回 config；开关忘关就会又变成 dry_run=True
        if self.exec_var.get() == "gui" and self.dry_var.get():
            choice = messagebox.askyesnocancel(
                "模拟模式还开着",
                "当前「模拟模式」是开的：会先打开同花顺、登录并改成对手价，但不会点买开、卖开、平仓。\n\n"
                "是 = 关掉模拟，允许自动点单（模拟账户也可）\n"
                "否 = 继续只监测、不点买卖\n"
                "取消 = 不启动",
            )
            if choice is None:
                self._skip_next_bot_login = False
                self._append_log("已取消启动。")
                return
            if choice:
                self.dry_var.set(False)
                self._append_log("已关闭模拟模式，将真实点击同花顺下单。")
            else:
                self._append_log("保持模拟模式：只监测，不点击。")
        elif self.exec_var.get() == "gui" and not self.dry_var.get():
            self._append_log("警告：gui + 关闭模拟 会真实点击同花顺。")

        # 同花顺通道先做完打开、登录、确认结算单和对手价。失败就不启动，避免没准备好就去扫描。
        if self.exec_var.get() == "gui":
            self._begin_prepare(automatic=True)
            return
        self._launch_resident()

    def _launch_resident(self) -> None:
        """真正启动常驻扫描。调用前，准备动作应该已经做完。"""
        try:
            self._launch_resident_inner()
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动常驻失败")
            from tkinter import messagebox

            messagebox.showerror(
                "启动常驻失败",
                str(exc) + "\n\n请检查配置（保证金表、品种、风险参数等），或看日志定位。",
            )
            self._status = "待命"
            try:
                self._refresh_status_cards()
            except Exception:  # noqa: BLE001
                pass

    def _launch_resident_inner(self) -> None:
        cfg = self._cfg_for_live_scan()
        if self._skip_next_bot_login:
            # 准备阶段已经登录过。这个标记只留在内存副本里，不写进配置文件。
            cfg["_startup_login_done"] = True
            self._skip_next_bot_login = False
        # 单品种挂机更安全：多品种 GUI 不会自动改同花顺合约框
        syms = (cfg.get("strategy") or {}).get("symbols") or []
        if cfg.get("trade", {}).get("executor") == "gui" and len(syms) > 1:
            self._append_log(
                f"警告：GUI 模式关注了多个品种 {syms}，程序不会自动切换合约，"
                "请只保留一个（如 bu2612）或确保盘面合约正确。"
            )
        # 磁盘上保存的是配置页本身。交给机器人的是副本，后面回测改副本不会牵动正在跑的程序。
        save_config(self.cfg)
        self._status = "启动中"
        self._refresh_status_cards()

        def on_stopped() -> None:
            self._post_to_main(self._after_stopped)

        self.worker.start(cfg, on_stopped=on_stopped)
        self._refresh_status_cards()
        mode = "模拟(不点单)" if cfg.get("dry_run") else "实盘点击"
        from src.account.workspace import profile_of

        book_name = profile_of(root=self.root_dir)["label"]
        self._append_log(f"已启动常驻扫描 | dry_run={cfg.get('dry_run')} | {mode} | {book_name}")
        if cfg.get("_startup_login_done"):
            self._append_log("刚才已经登录过，常驻不再登录第二次。只再看一眼对手价。")
        else:
            self._append_log("常驻会先登录并核对对手价，这两步不点买卖。价格没改成功时，后面也不会点买卖。")
        self.tabs.set("运行")
        # 不再把自己拉到最前。一拉到前面，后面要点同花顺的那一下会点到本程序的按钮上，
        # 比如下单模拟器。常驻只该操作同花顺，下单模拟器留给你自己点。
        try:
            self.log_box.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _on_prepare_then_start(self, *, automatic: bool = False) -> None:
        """
        先打开同花顺、登录、改对手价，全部成功后再启动常驻。

        准备过程不点买开、卖开、平仓。失败就停在准备，不启动扫描。
        """
        from tkinter import messagebox

        if self._prep_busy:
            if not automatic:
                messagebox.showwarning("正在准备", "上一次准备还没做完。")
            return
        if self.worker.running:
            if not automatic:
                messagebox.showinfo("已经在跑", "常驻已经启动，不用再准备一次。")
            return
        if (
            not automatic
            and self.exec_var.get() == "gui"
            and not self.dry_var.get()
        ):
            if not messagebox.askyesno(
                "准备后会启动常驻",
                "接下来只做三件事：打开同花顺、登录、把价格改成对手价。\n"
                "这三步不点买开、卖开、平仓。\n\n"
                "都成功以后才会启动常驻。当前模拟是关的，"
                "启动后如果策略给出信号，就会真实点击下单。\n\n"
                "确定开始？",
            ):
                return
        self._begin_prepare(automatic=automatic)

    def _begin_prepare(self, *, automatic: bool) -> None:
        """在后台做准备。界面保持可点，但常驻要等准备结果回来才启动。"""
        self._prep_busy = True
        self._append_log("开始准备同花顺：打开、登录、改对手价。这几步不下单。")
        cfg = self._clone_cfg()

        def _work() -> None:
            from src.login.prepare import prepare_trading_desk

            try:
                ok, lines = prepare_trading_desk(cfg)
            except Exception as exc:  # noqa: BLE001
                logger.exception("准备同花顺失败")
                ok, lines = False, [f"准备失败：{exc}"]

            def _done() -> None:
                self._prep_busy = False
                for line in lines:
                    self._append_log(line)
                if not ok:
                    from tkinter import messagebox

                    messagebox.showwarning(
                        "还不能启动",
                        "准备没有完成，常驻没有启动，所以不会下单。\n\n" + "\n".join(lines),
                    )
                    return
                self._append_log("准备完成，现在启动常驻。这次不再登录第二遍。")
                self._skip_next_bot_login = True
                if automatic:
                    self._launch_resident()
                else:
                    self._on_start()

            self._post_to_main(_done)

        threading.Thread(target=_work, name="ths-prepare", daemon=True).start()

    def _tick_login_schedule(self) -> None:
        """
        到了配置里的登录时间，先做准备，完成后再启动常驻。

        常驻已经在跑时，不在这里再开一次。正在跑的程序会在下一轮扫描前自己登录，
        登录过程中不会同时点买卖。
        """
        if self._closing:
            return
        try:
            self._maybe_start_at_login_time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查定时登录失败: {}", exc)
        if not self._closing:
            self._login_tick_after_id = self.after(20000, self._tick_login_schedule)

    def _maybe_start_at_login_time(self) -> None:
        """当前这一分钟如果写在 login_times 里，而且今天还没触发过，就去准备并启动。"""
        from datetime import datetime

        times = [str(item).strip() for item in ((self.cfg.get("schedule") or {}).get("login_times") or [])]
        now = datetime.now()
        current = now.strftime("%H:%M")
        if current not in times:
            return
        key = now.strftime("%Y-%m-%d ") + current
        if key in self._login_fired:
            return
        self._login_fired.add(key)
        if self.worker.running or self._prep_busy:
            self._append_log(f"到了登录时间 {current}，常驻已在运行，不重复启动。")
            return
        self._append_log(f"到了登录时间 {current}。先准备同花顺，完成后再开始扫描。")
        self._begin_prepare(automatic=True)

    def _bring_self_front(self) -> None:
        """把本程序窗口拉回最前（启动后避免被同花顺/命令行盖住）。下单点击还没结束时不拉。"""
        from src.trader.gui_trader import order_click_in_progress

        if order_click_in_progress():
            return
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def _after_stopped(self) -> None:
        self._status = "已停止"
        self._refresh_status_cards()

    def _on_confirm_positions(self) -> None:
        """人工核对同花顺持仓后，清掉遗留委托意图并解除对应熔断。"""
        from tkinter import messagebox

        bot = getattr(self.worker, "bot", None)
        if bot is None:
            messagebox.showinfo("对账确认", "常驻没有在跑，没有待核对的委托记录。")
            return
        if not messagebox.askyesno(
            "对账确认",
            "请先在同花顺里核对实际持仓与委托。\n"
            "如果这笔其实已成交，先用「手工单录入」把仓位补进程序账，再回来点这里。\n\n"
            "确认无误后解除熔断并继续扫描？",
        ):
            return
        ok, msg = bot.confirm_positions_reviewed()
        if ok:
            messagebox.showinfo("对账确认", msg)
        else:
            messagebox.showwarning("对账确认", msg)


    def _maybe_release_intent_halt(
        self, bot: object, symbol: str, side: str, volume: int
    ) -> None:
        """
        手工录入的这笔如果正好是上次没核对完的委托，顺手问一句要不要解除熔断。

        只在品种、方向、手数都对得上时提示；用户点「否」就保持熔断。
        """
        from tkinter import messagebox

        if bot is None:
            return
        matcher = getattr(bot, "pending_intent_matches", None)
        if matcher is None:
            return
        intent = matcher(symbol, side, volume)
        if not intent:
            return
        side_cn = {"buy_open": "买开", "sell_open": "卖开", "close": "平仓"}.get(side, side)
        if not messagebox.askyesno(
            "顺带解除熔断",
            "这笔和上次没核对完的委托一致：\n"
            f"{intent.get('symbol')} {side_cn} {intent.get('volume')} 手（{intent.get('ts')}）\n\n"
            "现在把它标记为已核对，并解除熔断吗？",
        ):
            return
        ok, msg = bot.confirm_positions_reviewed()
        self._append_log(("已解除熔断：" if ok else "解除熔断失败：") + msg)
        self._refresh_status_cards()
        if ok:
            messagebox.showinfo("已解除熔断", msg)

    def _on_halt_resident(self) -> None:
        """结束常驻主循环。暂停报单不够时用这个：扫描、清仓钟点、防休眠都会停。"""
        self.worker.stop()
        self._status = "正在停止常驻…"
        self._append_log("已发送停止常驻请求…")
        self._refresh_status_cards()

    def _on_pause_orders(self) -> None:
        """拨交易台同一套暂停标志。扫描不停。"""
        if not self.worker.running:
            return
        paused = not self.worker.orders_paused()
        self.worker.set_orders_paused(paused)
        self._append_log("已暂停报单，开平仓不会发出去。" if paused else "已恢复报单。")
        self._refresh_status_cards()

    def _on_test_tools(self) -> None:
        """打开测试工具：试跑一轮 / 点击下单 / 完整自检，三项分选。"""
        from src.ui.test_tools import open_test_tools

        open_test_tools(
            self,
            on_once=self._on_once,
            on_order=self._on_order_simulator,
            on_full=self._on_sim_test,
        )

    def _on_once(self) -> None:
        self.worker.run_once(self._cfg_for_live_scan())
        self._append_log("已触发试跑一轮信号（单次扫描）。")

    def _on_sim_test(self) -> None:
        script = self.root_dir / "tools" / "sim_full_test.py"
        self._append_log("开始完整自检…")

        def _run() -> None:
            code = subprocess.call([sys.executable, str(script)])
            self._post_to_main(lambda: self._append_log(f"完整自检结束，exit={code}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_manual_trade(self) -> None:
        """
        统一的手工单录入：弹窗里填品种、价格、手数，按「买多」「卖空」或「平仓」。

        完全仿照真实下单，只是把动作记进程序的账，不会去点同花顺。
        用于你手动在同花顺上开仓/平仓后，把这笔回填进程序的持仓和信号账户。
        """
        from tkinter import messagebox

        import customtkinter as ctk

        from src.account.signal_book import write_capital_to_config
        from src.risk.sizing import PositionSizer
        from src.utils.state_store import (
            default_state_path,
            format_position_text,
            load_state,
            manual_close_without_bot,
            manual_open_without_bot,
            open_position_rows,
        )

        bot = getattr(self.worker, "bot", None)

        # 先读程序当前账上的仓位，弹窗里会显示"程序已记的仓位：xxx"
        if bot is not None and getattr(bot, "strategy", None) is not None:
            pos_rows = open_position_rows(
                {
                    sym: {
                        "side": getattr(pos, "side", "flat"),
                        "volume": getattr(pos, "volume", 0),
                        "entry_price": getattr(pos, "entry_price", 0),
                    }
                    for sym, pos in (getattr(bot.strategy, "positions", {}) or {}).items()
                }
            )
        else:
            state = load_state(default_state_path(self.root_dir))
            pos_rows = open_position_rows(state.get("positions") or {})

        pos_text = (
            "程序账上："
            + "、".join(
                f"{r['symbol']} {'多' if r['side']=='long' else '空'}{r['volume']}手@{r['entry_price']:g}"
                for r in pos_rows
            )
            if pos_rows
            else "程序账上目前是空仓"
        )

        # 弹窗：品种、价格、手数 + 买多/卖空/平仓三个按钮
        dialog = ctk.CTkToplevel(self)
        dialog.title("手工单录入")
        dialog.geometry("420x520")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="手动在同花顺上开/平仓后，在这里录入。只改程序的账，不会去点同花顺。",
            justify="left",
            text_color=T.TEXT_DIM,
        ).pack(padx=16, pady=(16, 4))
        ctk.CTkLabel(
            dialog,
            text=pos_text,
            text_color=T.ACCENT,
            font=T.FONT_SMALL,
        ).pack(padx=16, pady=(0, 12))

        ctk.CTkLabel(dialog, text="品种（如 fu2701）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 6))
        symbol_entry.focus_set()

        ctk.CTkLabel(dialog, text="价格（成交价）").pack(anchor="w", padx=16)
        price_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="4960")
        price_entry.pack(fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(dialog, text="手数").pack(anchor="w", padx=16)
        volume_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="1")
        volume_entry.pack(fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(
            dialog,
            text="止损价（可选，开仓后策略按此自动止损；留空只靠反向信号和收盘清仓）",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=16)
        stop_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="留空=只靠反向信号和收盘清仓")
        stop_entry.pack(fill="x", padx=16, pady=(0, 2))

        rec_label = ctk.CTkLabel(
            dialog, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM,
        )
        rec_label.pack(anchor="w", padx=16, pady=(0, 16))

        # --- 止损价推荐：品种填完后自动算一次 ---
        rec_stops: dict[str, float] = {}  # {"long": X, "short": Y}

        def _refresh_stop_rec(*_args: object) -> None:
            sym = symbol_entry.get().strip().lower()
            if not sym or len(sym) < 4:
                rec_label.configure(text="")
                rec_stops.clear()
                return

            def _worker() -> None:
                try:
                    from src.strategy.daily_body import suggest_stop_both
                    bot_now = bot if bot is not None and getattr(bot, "kline", None) is not None else None
                    if bot_now is None:
                        # 常驻没跑，临时建一个 kline provider
                        from src.data import KlineProvider
                        kp = KlineProvider(self.cfg)
                        bars = kp.fetch(sym, period="5")
                        price_tick = float(
                            ((self.cfg.get("trade") or {}).get("ctp") or {}).get("price_tick", 1)
                            or (self.cfg.get("trade") or {}).get("price_tick", 1)
                            or 1
                        )
                        stop_extra_ticks = 1
                        eth_minutes = 30
                        session_open_h = 9
                    else:
                        bars = bot_now.kline.fetch(sym, period=str(bot_now.strategy.timeframe))
                        price_tick = float(getattr(bot_now.strategy, "price_tick", 1) or 1)
                        stop_extra_ticks = int(getattr(bot_now.strategy, "stop_extra_ticks", 1) or 1)
                        eth_minutes = int(getattr(bot_now.strategy, "eth_minutes", 30) or 30)
                        from src.strategy.daily_body import _parse_hhmm
                        so_raw = getattr(bot_now.strategy, "session_open", None)
                        session_open_h = _parse_hhmm(str(so_raw)).hour if so_raw else 9

                    from datetime import time as _dtime
                    long_stop, short_stop = suggest_stop_both(
                        bars or (),
                        price_tick=price_tick,
                        stop_extra_ticks=stop_extra_ticks,
                        eth_minutes=eth_minutes,
                        timeframe=str(getattr(bot_now.strategy, "timeframe", "5") if bot_now else "5"),
                        session_open=_dtime(session_open_h, 0),
                    )
                    rec_stops.clear()
                    parts = []
                    if long_stop is not None:
                        rec_stops["long"] = float(long_stop)
                        parts.append(f"买多建议 {long_stop:g}")
                    if short_stop is not None:
                        rec_stops["short"] = float(short_stop)
                        parts.append(f"卖空建议 {short_stop:g}")
                    hint = "  |  ".join(parts) if parts else "（暂无推荐，可留空）"
                    self._post_to_main(lambda: rec_label.configure(text=hint))
                except Exception:
                    self._post_to_main(lambda: rec_label.configure(text="（暂无推荐，可留空）"))

            import threading
            threading.Thread(target=_worker, daemon=True).start()

        symbol_entry.bind("<FocusOut>", _refresh_stop_rec)
        symbol_entry.bind("<Return>", _refresh_stop_rec)
        price_entry.bind("<FocusOut>", _refresh_stop_rec)
        price_entry.bind("<Return>", _refresh_stop_rec)

        # --- 点击买多/卖空时，如果止损空着，自动填入推荐值 ---
        def _fill_stop_if_empty(side_key: str) -> None:
            if stop_entry.get().strip():
                return
            rec = rec_stops.get(side_key)
            if rec is not None:
                stop_entry.insert(0, f"{rec:g}")

        ctk.CTkLabel(
            dialog,
            text="选择操作：",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(anchor="w", padx=16, pady=(0, 4))

        action_taken: dict[str, str] = {}

        def _take(action: str) -> None:
            action_taken["action"] = action
            action_taken["symbol"] = symbol_entry.get().strip()
            action_taken["price"] = price_entry.get().strip()
            action_taken["volume"] = volume_entry.get().strip()
            action_taken["stop"] = stop_entry.get().strip()
            dialog.destroy()

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        btn_w = 110
        ctk.CTkButton(
            btn_frame, text="买多", width=btn_w, height=44,
            fg_color="#E53935", hover_color="#F44336", text_color="#FFFFFF",
            command=lambda: (_fill_stop_if_empty("long"), _take("买多")),
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            btn_frame, text="卖空", width=btn_w, height=44,
            fg_color="#2ECC8A", hover_color="#3ED99A", text_color="#FFFFFF",
            command=lambda: (_fill_stop_if_empty("short"), _take("卖空")),
        ).pack(side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(
            btn_frame, text="平仓", width=btn_w, height=44,
            fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER, text_color=T.ACCENT_ON,
            command=lambda: _take("平仓"),
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        ctk.CTkButton(dialog, text="取消", command=dialog.destroy, height=32).pack(padx=16, pady=(0, 16))

        # 按内容实际需要的高度定窗口。写死 520 在 125%/150% 缩放下控件被放大，取消按钮会被压扁。
        dialog.update_idletasks()
        dlg_w = max(420, dialog.winfo_reqwidth())
        dlg_h = max(520, dialog.winfo_reqheight())
        dialog.geometry(f"{dlg_w}x{dlg_h}")

        self.wait_window(dialog)
        if not action_taken:
            return

        symbol = action_taken["symbol"]
        if not symbol:
            messagebox.showerror("品种不对", "请填写品种代码，例如 fu2701。")
            return

        try:
            price = float(action_taken["price"])
        except ValueError:
            messagebox.showerror("价格不对", "请填写数字，例如 4960。")
            return
        if price <= 0:
            messagebox.showerror("价格不对", "价格要大于 0。")
            return

        try:
            volume = int(action_taken["volume"])
        except ValueError:
            messagebox.showerror("手数不对", "请填写整数，例如 1。")
            return
        if volume <= 0:
            messagebox.showerror("手数不对", "手数要大于 0。")
            return

        action = action_taken["action"]
        sizer = PositionSizer(self.cfg)
        multiplier = float(sizer.multiplier_for(symbol))

        stop_price = 0.0
        stop_raw = action_taken.get("stop", "").strip()
        if stop_raw:
            try:
                stop_price = float(stop_raw)
            except ValueError:
                messagebox.showerror("止损价不对", "请填写数字，或留空。")
                return
            if stop_price < 0:
                messagebox.showerror("止损价不对", "止损价不能是负数。")
                return

        if action in ("买多", "卖空"):
            side = "long" if action == "买多" else "short"
            side_word = "多" if side == "long" else "空"

            stop_hint = f"，策略止损 {stop_price:g}" if stop_price > 0 else "（不填止损，只靠反向信号和收盘清仓）"
            if not messagebox.askyesno(
                "确认手工开仓",
                f"手工录入：{symbol} {side_word}{volume}手，开仓价 {price:g}{stop_hint}。\n不会去点同花顺。\n\n确定？",
            ):
                return

            if bot is not None and getattr(bot, "strategy", None) is not None:
                opened, balance = bot.manual_open(
                    symbol, side, volume, price, initial_stop=stop_price,
                )
            else:
                opened, balance = manual_open_without_bot(
                    self.root_dir,
                    symbol=symbol,
                    side=side,
                    volume=volume,
                    entry_price=price,
                    multiplier=multiplier,
                    initial_capital=float(self.cfg.get("risk", {}).get("capital") or 0),
                    initial_stop=stop_price,
                )

            if opened is None:
                messagebox.showerror(
                    "录入失败",
                    f"{symbol} 不在程序关注列表里，或已有同方向仓位。\n请先清掉程序账上的旧仓再录入。",
                )
                return

            if balance is not None:
                if bot is None or getattr(bot, "strategy", None) is None:
                    write_capital_to_config(balance)
                self._apply_book_balance(balance)

            self._refresh_signal_view()
            self._refresh_status_cards()
            shown = self._position_card_text(None if bot is None else bot.status_snapshot())
            stop_note = f"，策略止损 {stop_price:g}" if stop_price > 0 else ""
            self._append_log(
                f"手工录入开仓：{symbol} {side_word}{volume}手，开仓价 {price:g}{stop_note}。当前持仓：{shown}"
            )
            messagebox.showinfo(
                "已录入",
                f"已把 {symbol} {side_word} {volume} 手记成开仓，开仓价 {price:g}{stop_note}。\n没有去点同花顺。",
            )
            self._maybe_release_intent_halt(
                bot, symbol, "buy_open" if side == "long" else "sell_open", volume
            )

        elif action == "平仓":
            if not messagebox.askyesno(
                "确认手工平仓",
                f"手工录入平仓：{symbol}，平仓价 {price:g}。\n"
                f"会按程序账上 {symbol} 的持仓手数平仓，盈亏按开仓价算。\n不会去点同花顺。\n\n确定？",
            ):
                return

            if bot is not None and getattr(bot, "strategy", None) is not None:
                closed, balance = bot.manual_close(symbol, price)
            else:
                closed, balance = manual_close_without_bot(
                    self.root_dir,
                    symbol=symbol,
                    exit_price=price,
                    multiplier=multiplier,
                    initial_capital=float(self.cfg.get("risk", {}).get("capital") or 0),
                )

            if closed is None:
                messagebox.showinfo("已经是空仓", f"{symbol} 在程序账上已经没有仓了。")
                return

            direction = "多" if closed["side"] == "long" else "空"
            sign = 1.0 if closed["side"] == "long" else -1.0
            pnl = (price - float(closed["entry_price"])) * sign * multiplier * int(closed["volume"])

            if balance is not None:
                if bot is None or getattr(bot, "strategy", None) is None:
                    write_capital_to_config(balance)
                self._apply_book_balance(balance)

            self._refresh_signal_view()
            self._refresh_status_cards()
            shown = self._position_card_text(None if bot is None else bot.status_snapshot())
            self._append_log(
                f"手工录入平仓：{symbol} {direction}{closed['volume']}手，平仓价 {price:g}，"
                f"盈亏 {pnl:+.0f} 元。当前持仓：{shown or format_position_text({})}"
            )
            messagebox.showinfo(
                "已录入",
                f"已按 {price:g} 把 {symbol} {direction} {closed['volume']} 手记成平仓。\n"
                f"这笔盈亏 {pnl:+.0f} 元。没有去点同花顺。",
            )
            self._maybe_release_intent_halt(bot, symbol, "close", int(closed["volume"]))

    def _resolve_date_range(self, preset: str, start_raw: str, end_raw: str) -> tuple[str | None, str | None]:
        """
        把界面上的「时间段」选项换成具体起止日期。
        返回 (start, end)，None 表示不限制（用公开源能给的全部）。
        """
        from datetime import timedelta

        preset = (preset or "全部可用").strip()
        # 最近N天以期货交易日为准：夜盘 21:00 之后已经算今天。
        today = futures_trading_day()
        # 「最近N天」以今天为准，不看下面两个日期框
        if preset == "最近3天":
            return (today - timedelta(days=3)).isoformat(), today.isoformat()
        if preset == "最近7天":
            return (today - timedelta(days=7)).isoformat(), today.isoformat()
        if preset == "最近14天":
            return (today - timedelta(days=14)).isoformat(), today.isoformat()
        # 「全部可用」只有两个日期都空着才不截。
        # 只要填了开始或结束日期，就按填写的截，避免改了日期却仍跑完全部K线。
        s = self._normalize_date(start_raw)
        e = self._normalize_date(end_raw)
        if preset == "全部可用" and not s and not e:
            return None, None
        return s, e

    @staticmethod
    def _initial_start_date(saved: object) -> str:
        """
        开始日期的初始值。

        配置里已经写了日期就用那个。空着时填当年1月1日，例如今年是 2026-01-01。
        这样每次打开回测，不用再手填从哪一天开始。
        """
        text = str(saved or "").strip()
        if text:
            return text
        from datetime import date

        return f"{date.today().year}-01-01"

    @staticmethod
    def _normalize_date(raw: str | None) -> str | None:
        """把 2026-1-1、2026/1/1 收成 2026-01-01。空字符串表示不限制。"""
        text = str(raw or "").strip().replace("/", "-").replace(".", "-")
        if not text:
            return None
        try:
            import pandas as pd

            return pd.Timestamp(text).strftime("%Y-%m-%d")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"日期「{raw}」看不懂，请写成 2026-10-01") from e

    def _sync_bt_to_opt(self) -> None:
        """把历史回测页的条件抄到策略优化页，保证两边看的是同一段行情。"""
        self.opt_engine_var.set(self.bt_engine_var.get())
        symbol = self.bt_symbol_var.get().strip()
        # 优化页品种是下拉，先保证名单和配置一致，再改成回测页当前这一项。
        self._sync_symbol_choice("opt_symbol_menu", self.opt_symbol_var, prefer=symbol, force=True)
        self.opt_tf_var.set(self.bt_tf_var.get())
        self.opt_tf_custom_var.set(self.bt_tf_custom_var.get())
        self.opt_trail_var.set(self.bt_trail_var.get())
        self.opt_trail_custom_var.set(self.bt_trail_custom_var.get())
        self.opt_range_var.set(self.bt_range_var.get())
        self.opt_start_var.set(self.bt_start_var.get())
        self.opt_end_var.set(self.bt_end_var.get())
        from src.data.quote_check import is_ths_source

        if is_ths_source(self.bt_source_var.get()):
            self.opt_source_var.set(self.source_var.get())
        else:
            self.opt_source_var.set(self.bt_source_var.get())
        # 下拉框用代码改策略名时，不会自己刷新历史列表，这里补一次。
        if hasattr(self, "opt_hist_menu"):
            self._refresh_opt_history()
        self._append_log("已把历史回测条件同步到「策略优化」页。")
        from tkinter import messagebox

        messagebox.showinfo(
            "已同步",
            "策略、品种、周期、行情来源和时间段已复制到「策略优化」页。\n"
            "收盘清仓以及开仓限制，两边本来就是同一组。",
        )

    def _set_result_text(self, box: ctk.CTkTextbox, text: str) -> None:
        box.delete("1.0", "end")
        box.insert("end", text)

    def _show_opt_progress(self, done: int, total: int, phase: str = "", *, force: bool = False) -> None:
        """
        策略优化进度：第几组 / 共几组、百分比、已用时间、预计还要多久。

        进度只是提示，不能影响优化。默认 0.4 秒最多刷一次，避免几百组时把界面刷爆。
        """
        if not hasattr(self, "opt_progress"):
            return
        total = max(1, int(total))
        done = max(0, min(int(done), total))
        now = time.monotonic()
        if not force and now - self._opt_progress_at < 0.4:
            return
        self._opt_progress_at = now
        started = self._opt_started_at or now
        elapsed = max(0.0, now - started)
        self.opt_progress.set(done / total)
        if done <= 0:
            self.opt_progress_label.configure(text=f"{phase}… 已用 {_duration_text(elapsed)}")
            return
        percent = done / total * 100.0
        eta = ""
        if done < total:
            eta = f"，预计还要 {_duration_text(elapsed / done * (total - done))}"
        self.opt_progress_label.configure(
            text=f"{phase} {done}/{total}（{percent:.0f}%），已用 {_duration_text(elapsed)}{eta}"
        )

    def _finish_opt_progress(self, *, ok: bool) -> None:
        """优化结束：进度条收尾，并写一句总用时。"""
        if not hasattr(self, "opt_progress"):
            return
        elapsed = time.monotonic() - (self._opt_started_at or time.monotonic())
        self.opt_progress.set(1.0 if ok else 0.0)
        self.opt_progress_label.configure(
            text=(f"完成，用时 {_duration_text(elapsed)}" if ok else f"失败，用时 {_duration_text(elapsed)}")
        )
        self._opt_started_at = None
        self._opt_progress_at = 0.0

    def _pack_open_limits(self, parent) -> None:
        """
        历史回测页和策略优化页共用同一组开仓限制。

        两边绑的是同一批输入框变量，所以在策略优化页改权益或关掉开关，历史回测页会一起变。
        策略优化的每一组参数都按这套限制打分，避免选出实盘根本下不出去的参数。
        """
        box_risk = self._card(parent)
        ctk.CTkLabel(
            box_risk,
            text="开仓和止损限制",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            box_risk,
            text="开仓限制、保证金、收盘清仓与实盘配置一致：开跑前自动按配置页和品种表对齐。本页可改日期和「启用限制」；关掉限制后固定 1 手。点「按配置页重新填入」可连策略品种一起抄过来。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        limit_row = ctk.CTkFrame(box_risk, fg_color="transparent")
        limit_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            limit_row,
            text="启用限制",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkSwitch(
            limit_row,
            text="",
            variable=self.bt_limits_on_var,
            progress_color=T.OK,
        ).pack(side="left")
        menu = self._option_row(box_risk, "仓位模式", self.bt_sizing_var, ["risk_pct", "fixed"], width=160)
        # 开跑前会按配置页覆盖，这里不让改，避免白填一遍。
        menu.configure(state="disabled")
        self._entry_row(
            box_risk,
            "账户权益(元)",
            self.bt_capital_var,
            hint="跟配置页一致，开跑前自动对齐",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk,
            "每手保证金(元)",
            self.bt_margin_var,
            hint="跟品种与合约表一致，开跑前自动对齐。0=不限制",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk,
            "每笔风险%",
            self.bt_risk_pct_var,
            hint="跟配置页一致，2 表示权益的 2%",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk,
            "仓位上限%",
            self.bt_margin_usage_pct_var,
            hint="跟配置页一致；权益×此比例内交保证金",
            width=160,
            readonly=True,
        )
        self._entry_row(
            box_risk, "固定手数", self.bt_volume_var, hint="跟配置页一致", width=160, readonly=True
        )
        self._entry_row(
            box_risk, "最少手数", self.bt_min_vol_var, hint="跟配置文件 risk.min_volume 一致", width=160, readonly=True
        )
        self._entry_row(
            box_risk, "日最大亏损(元)", self.bt_max_loss_var, hint="跟配置页一致，当天亏到这里就停开", width=160, readonly=True
        )
        self._entry_row(box_risk, "开仓最小间隔秒", self.bt_interval_var, hint="跟配置页一致", width=160, readonly=True)
        self._entry_row(
            box_risk, "每天最多开仓", self.bt_max_orders_var, hint="跟配置页一致，次数换日清零", width=160, readonly=True
        )
        ctk.CTkButton(
            box_risk,
            text="按配置页重新填入",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=lambda: self._copy_risk_to_backtest(full=True),
        ).pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(
            box_risk,
            text="从配置页抄到本页：策略、品种、周期、行情来源、余额与开仓数字，以及收盘清仓。优化/复盘的品种也会改成这个合约。启用限制仍由本页开关决定。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkButton(
            box_risk,
            text="把本页参数写入配置页",
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._write_backtest_to_config_page,
        ).pack(fill="x", pady=(0, 0))
        ctk.CTkLabel(
            box_risk,
            text="把本页测完的内容写回配置并保存：策略、品种、信号/止损周期、行情来源、三个开关，以及开仓手数等限制。不会改实盘余额，也不会写回测日期。启用限制开关只影响回测，不写进配置。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _format_limit_lines(self, snap: dict) -> str:
        """结果里写明这次有没有套用开仓限制，方便和上一把对比。"""
        if not snap.get("apply_limits", True):
            return "开仓限制  关（固定1手，不看止损距离、日亏损和开仓次数）"
        skipped = self._format_skipped(snap.get("skipped_opens") or {})
        return (
            f"开仓限制  开\n"
            f"仓位      {snap.get('sizing_mode')}  权益{float(snap.get('capital') or 0):.0f}元"
            f"  每笔{snap.get('risk_per_trade_pct')}%"
            f"  仓位上限{snap.get('margin_usage_pct', '—')}%"
            f"  固定{snap.get('fixed_volume')}手\n"
            f"限制      最少{snap.get('min_volume')}手"
            f"  日亏停{float(snap.get('max_daily_loss') or 0):.0f}元"
            f"  间隔{snap.get('min_order_interval_sec')}秒"
            f"  每天最多开{snap.get('max_orders_per_day')}次"
            f"  每手保证金{float(snap.get('margin_per_lot') or 0):.0f}元\n"
            f"{skipped}"
        )

    def _format_skipped(self, skipped: dict) -> str:
        """把没开成的原因收成一行。没有跳过就写明这次限制没有挡掉信号。"""
        if not skipped:
            return "跳过开仓  无"
        parts = [f"{name}{count}次" for name, count in skipped.items()]
        total = sum(int(v) for v in skipped.values())
        return f"跳过开仓  {total} 次（" + "，".join(parts) + "）"

    def _pack_margin_table_row(self, parent: ctk.CTkFrame) -> None:
        """
        品种与合约总表：路径 + 打开 + 重新读取 + 摘要。

        一张表管关注合约、保证金/乘数，以及每个品种自己的交易时段和清仓时刻。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row,
            text="品种与合约表",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(
            row,
            textvariable=self.margin_table_path_var,
            width=220,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left")
        ctk.CTkButton(
            row,
            text="打开表格",
            width=84,
            height=32,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._open_margin_table,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="重新读取",
            width=84,
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
            border_width=1,
            border_color=T.BORDER,
            command=lambda: self._reload_margin_table(silent=False),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="刷新全市场主力",
            width=132,
            height=32,
            fg_color=T.OK,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._refresh_universe_from_tq,
        ).pack(side="left", padx=(8, 0))
        self.margin_table_summary = ctk.CTkLabel(
            parent,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
            anchor="w",
        )
        self.margin_table_summary.pack(anchor="w", pady=(0, 4))
        self._refresh_margin_table_summary()

    def _margin_table_abs_path(self) -> Path:
        """按配置页路径算出总表绝对路径。"""
        from src.risk.universe_table import DEFAULT_REL_PATH, resolve_universe_path

        rel = str(self.margin_table_path_var.get() or "").strip() or DEFAULT_REL_PATH
        risk = self.cfg.setdefault("risk", {})
        risk["universe_table"] = rel
        risk["margin_table"] = rel
        return resolve_universe_path(self.cfg)

    def _refresh_universe_from_tq(self) -> None:
        """从天勤刷新全市场 81 个主力合约；独立线程跑，不卡界面。"""
        import threading

        def _thread() -> None:
            try:
                from tools.refresh_universe import refresh_universe_table

                rows = refresh_universe_table(dry_run=False)

                # 主线程弹结果
                def _on_done() -> None:
                    total = len(rows)
                    enabled = sum(1 for r in rows if r.get("enabled", True))
                    if hasattr(self, "margin_table_summary"):
                        self._reload_margin_table(silent=True)
                    ctk.CTkMessagebox.show_info(
                        master=self,
                        title="主力合约刷新完成",
                        message=(
                            f"天勤主力查询 {total} 个品种全部成功。\n"
                            f"启用={enabled}（沿用原有关注品种），停用={total - enabled}。\n\n"
                            f"如果某个月分主力变了，品种代码已自动更新。\n"
                            f"想启用新品种，在 Excel 里把「启用」改成「是」即可。"
                        ),
                    )

                self.after(0, _on_done)
            except Exception as exc:  # noqa: BLE001
                logger.error("刷新全市场主力失败: {}", exc)

                def _on_fail() -> None:
                    ctk.CTkMessagebox.show_error(
                        master=self,
                        title="刷新失败",
                        message=(
                            f"{exc}\n\n"
                            f"可能原因：天勤 tqsdk 未安装（pip install tqsdk），"
                            f"或账号密码没填（配置页 → 账号与登录 → 天勤行情账号/密码）。"
                        ),
                    )

                self.after(0, _on_fail)

        threading.Thread(target=_thread, name="UniverseRefresh", daemon=True).start()
        ctk.CTkMessagebox.show_info(
            master=self,
            title="正在刷新",
            message="正在问天勤查询 81 个品种的当前主力合约，约 10-20 秒…\n"
            "窗口卡住是正常的，完成后会自动弹结果。",
        )

    def _refresh_margin_table_summary(self) -> None:
        """刷新总表说明文字；并刷新上方关注下拉（不罗列全部合约）。"""
        from src.risk.margin_table import summarize_contracts

        symbols = list((self.cfg.get("strategy") or {}).get("symbols") or [])
        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        # 回测页抄「当前品种保证金」仍用摘要字符串，只是界面下方不再展示长名单。
        self.margin_map_var.set(summarize_contracts(contracts))
        if symbols and symbols != list(self._watch_symbols):
            try:
                self._set_watch_codes(symbols, select=symbols[0], save=False)
            except Exception:  # noqa: BLE001
                pass
        text = (
            "改表后请先关掉 Excel，再点「重新读取」。同品种保证金/乘数/手续费必须相同。"
            "每手手续费=开仓与平昨各扣一次；平今手续费可另填（空=相同，0=平今免费）。"
        )
        try:
            self.margin_table_summary.configure(text=text)
        except Exception:  # noqa: BLE001
            pass

    def _reload_margin_table(self, *, silent: bool = False) -> bool:
        """
        从品种与合约总表载入关注名单和保证金，并写回配置文件。

        silent=True 用于保存流程；False 时弹窗提示。
        """
        from tkinter import messagebox

        from src.risk.universe_table import apply_universe_table_to_cfg

        try:
            path = self._margin_table_abs_path()
            symbols, contracts = apply_universe_table_to_cfg(
                self.cfg, path=path, create_if_missing=True
            )
            if symbols:
                self._set_watch_codes(symbols, select=symbols[0], save=False)
            self._refresh_margin_table_summary()
            self._sync_bt_margin_from_table()
            if not silent:
                try:
                    save_config(self.cfg)
                except Exception as save_exc:  # noqa: BLE001
                    logger.warning("总表已读入内存，但写回配置文件失败: {}", save_exc)
                    messagebox.showwarning(
                        "已读取，未写回文件",
                        "表格已读进程序，但 config.yaml 没写成功。\n"
                        "请再点一次「保存全部配置」。\n\n"
                        f"{save_exc}",
                    )
                    return True
                messagebox.showinfo(
                    "品种与合约",
                    f"已读取关注 {len(symbols)} 个合约、{len(contracts)} 个品种，并已写进配置。\n{path}",
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取品种与合约表失败: {}", exc)
            if not silent:
                messagebox.showerror(
                    "读取失败",
                    f"读不到品种与合约表。\n请先关掉 Excel 再试。\n同品种保证金必须相同。\n\n{exc}",
                )
            return False

    def _open_margin_table(self) -> None:
        """打开品种与合约总表；没有文件时先按当前配置生成。"""
        from tkinter import messagebox

        from src.risk.universe_table import ensure_universe_table

        try:
            path = self._margin_table_abs_path()
            ensure_universe_table(path, self.cfg)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开", f"打不开品种与合约表。\n\n{exc}")

    def _copy_risk_to_backtest(self, full: bool = False) -> None:
        """
        把配置页上会影响回测的内容抄到回测页。不改磁盘。

        full=True 是「按配置页重新填入」：连策略、品种、周期和行情来源一起抄，优化页同一组条件也跟着变。
        平时重新加载只抄资金数字和三个开关，不改回测页正在看的策略和品种。
        「启用限制」不抄。配置页没有这个开关，实盘一直按这些数字算手数。
        清仓时刻、ETH 分钟、实体 ATR 倍数、趋势进场根数、SuperTrend 倍数等没有单独的回测框，开始回测时直接用配置页当时的值。
        """
        from src.ui.backtest_fill import first_symbol

        if full:
            engine = _ui_engine(self.engine_var.get())
            # 用配置页下拉框里当前选中的合约，不是列表里随便第一个旧名字。
            symbol = self._config_symbol_now() or first_symbol(self.symbols_var.get())
            tf_code = self._period_code(self.tf_var, self.tf_custom_var)
            trail_code = self._period_code(self.trail_var, self.trail_custom_var)
            self.bt_engine_var.set(engine)
            self.opt_engine_var.set(engine)
            self._sync_bt_symbol_menu(prefer=symbol, force=True)
            symbol = self.bt_symbol_var.get().strip()
            if symbol and symbol != "（还没有）":
                self._sync_symbol_choice("opt_symbol_menu", self.opt_symbol_var, prefer=symbol, force=True)
                self._sync_symbol_choice("rv_symbol_menu", self.rv_symbol_var, prefer=symbol, force=True)
            self._set_period_vars(tf_code, self.bt_tf_var, self.bt_tf_custom_var)
            self._set_period_vars(tf_code, self.opt_tf_var, self.opt_tf_custom_var)
            self._set_period_vars(trail_code, self.bt_trail_var, self.bt_trail_custom_var)
            self._set_period_vars(trail_code, self.opt_trail_var, self.opt_trail_custom_var)
            self.bt_source_var.set(self.source_var.get())
            self.opt_source_var.set(self.source_var.get())
        else:
            symbol = self.bt_symbol_var.get().strip()
            if symbol == "（还没有）":
                symbol = ""
            symbol = symbol or self._config_symbol_now() or first_symbol(self.symbols_var.get())
            engine = self.bt_engine_var.get()
            tf_code = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
            trail_code = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)

        # 收盘清仓要一起抄。以前只抄数字，回测仍用页面上旧的关。
        self.bt_flatten_var.set(bool(self.flatten_at_close_var.get()))

        self.bt_sizing_var.set(self.sizing_mode_var.get())
        self.bt_capital_var.set(self.capital_var.get())
        self.bt_risk_pct_var.set(self.risk_pct_var.get())
        self.bt_margin_usage_pct_var.set(self.margin_usage_pct_var.get())
        self.bt_volume_var.set(self.volume_var.get())
        self.bt_max_loss_var.set(self.max_loss_var.get())
        self.bt_interval_var.set(self.min_interval_var.get())
        self.bt_max_orders_var.set(self.max_orders_var.get())
        risk = self.cfg.get("risk", {}) if isinstance(self.cfg, dict) else {}
        self.bt_min_vol_var.set(str(risk.get("min_volume", self.bt_min_vol_var.get() or 1)))
        self._sync_bt_margin_from_table(symbol)
        if not full:
            return
        if hasattr(self, "opt_hist_menu"):
            self._refresh_opt_history()
        on_off = lambda flag: "开" if flag else "关"
        self._append_log(
            "已按配置页填入历史回测："
            f"策略 {ENGINE_LABELS.get(engine, engine)}，品种 {symbol}，"
            f"周期 {period_label(tf_code)}/{period_label(trail_code)}，"
            f"行情 {self.bt_source_var.get()}，"
            f"收盘清仓{on_off(self.bt_flatten_var.get())}"
        )

    def _write_backtest_to_config_page(self) -> None:
        """
        把历史回测页测完的参数完整写进配置页并保存。

        包括：策略、品种、信号/止损周期、行情来源、收盘清仓，以及开仓手数等限制。
        不写：账户权益（仍是实盘余额）、回测日期、「启用限制」开关（配置页没有这个开关）。
        """
        from tkinter import messagebox

        from src.data.quote_check import normalize_source

        symbol = (self.bt_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在历史回测页选一个合约，再写入配置页。")
            return
        engine = _ui_engine(self.bt_engine_var.get())
        tf_code = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        trail_code = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)
        from src.data.quote_check import is_ths_source

        source_label = (self.bt_source_var.get() or "").strip()
        live_keeps_source = is_ths_source(source_label)
        flatten_on = bool(self.bt_flatten_var.get())

        try:
            sizing = self.bt_sizing_var.get() or "risk_pct"
            float(self.bt_capital_var.get())
            risk_pct = float(self.bt_risk_pct_var.get())
            margin_usage = max(0.0, float(self.bt_margin_usage_pct_var.get() or 0))
            volume = int(float(self.bt_volume_var.get()))
            min_vol = max(1, int(float(self.bt_min_vol_var.get())))
            max_loss = float(self.bt_max_loss_var.get())
            interval = max(0, int(float(self.bt_interval_var.get())))
            max_orders = max(1, int(float(self.bt_max_orders_var.get())))
            margin = max(0.0, float(self.bt_margin_var.get() or 0))
        except ValueError:
            messagebox.showerror("数字不对", "开仓限制里的权益、风险和手数要填数字")
            return

        on_off = lambda flag: "开" if flag else "关"
        if not messagebox.askyesno(
            "写入配置页",
            "下面内容会写到配置页，并保存到配置文件。实盘以后按这套策略和手数跑。\n"
            "账户权益仍是当前余额，不会改成历史回测里手填的数。回测日期不写入。\n\n"
            f"策略  {ENGINE_LABELS.get(engine, engine)}\n"
            f"品种  {symbol}\n"
            f"信号周期  {period_label(tf_code)}    止损周期  {period_label(trail_code)}\n"
            f"行情来源  {'不同花顺图写入实盘（配置页仍用现在的那一家）' if live_keeps_source else (source_label or '（空）')}\n"
            f"收盘清仓  {on_off(flatten_on)}\n\n"
            f"仓位模式  {sizing}\n"
            f"每笔风险  {risk_pct}%\n"
            f"仓位上限  {margin_usage:g}%\n"
            f"固定手数  {volume}\n"
            f"最少手数  {min_vol}\n"
            f"日最大亏损  {max_loss:.0f} 元\n"
            f"开仓间隔  {interval} 秒\n"
            f"每天最多开仓  {max_orders} 次\n"
            f"每手保证金  {margin:.0f} 元\n\n"
            "确定写入？",
        ):
            return

        # —— 策略 / 周期 / 来源 / 开关 ——
        self.engine_var.set(engine)
        self._set_period_vars(tf_code, self.tf_var, self.tf_custom_var)
        self._set_period_vars(trail_code, self.trail_var, self.trail_custom_var)
        if source_label and not live_keeps_source:
            self.source_var.set(source_label)
        self.flatten_at_close_var.set(flatten_on)

        # 关注名单：没有这个合约就加上，并选为当前合约（排到最前）
        codes = list(self._watch_symbols)
        if not any(c.lower() == symbol.lower() for c in codes):
            codes.append(symbol)
        self._set_watch_codes(codes, select=symbol, save=False)

        # —— 开仓限制（不含权益）——
        self.sizing_mode_var.set(sizing)
        self.risk_pct_var.set(str(risk_pct))
        self.margin_usage_pct_var.set(str(margin_usage))
        self.volume_var.set(str(volume))
        self.max_loss_var.set(str(max_loss))
        self.min_interval_var.set(str(interval))
        self.max_orders_var.set(str(max_orders))
        self.margin_var.set(str(margin))
        self.cfg.setdefault("risk", {})["min_volume"] = min_vol
        self.cfg.setdefault("risk", {})["margin_per_lot"] = margin
        self.cfg.setdefault("risk", {})["margin_usage_pct"] = margin_usage
        self.cfg.setdefault("risk", {}).pop("max_position", None)

        cfg = self._merge_disk_gui_coords(self._apply_ui_to_cfg())
        cfg.setdefault("risk", {})["min_volume"] = min_vol
        cfg.setdefault("risk", {})["margin_per_lot"] = margin
        cfg.setdefault("risk", {})["margin_usage_pct"] = margin_usage
        cfg.setdefault("risk", {}).pop("max_position", None)
        # 回测页的新浪/东财/天勤可以写入实盘。同花顺图不能写进 data.source，
        # 否则下次启动扫描会去抢鼠标，整轮拉K失败，一单也开不出。
        if source_label and not live_keeps_source:
            cfg.setdefault("data", {})["source"] = normalize_source(source_label)
        path = save_config(cfg)
        self.cfg = cfg
        self._status = "已保存"
        self._append_log(
            "历史回测本页参数已写入配置页并保存："
            f"策略 {ENGINE_LABELS.get(engine, engine)}，品种 {symbol}，"
            f"周期 {period_label(tf_code)}/{period_label(trail_code)}，"
            f"来源 {'仍用配置页当前这家（同花顺图不写实盘）' if live_keeps_source else (source_label or '-')}，"
            f"清仓{on_off(flatten_on)} | {path}"
        )
        self._refresh_status_cards()
        self.tabs.set("配置")
        messagebox.showinfo(
            "已写入",
            "配置页已换成历史回测本页的策略、品种、周期、开关和开仓限制，并保存到配置文件。",
        )

    def _write_limits_to_config_page(self) -> None:
        """旧名字保留：别处若还调用，转到完整写入。"""
        self._write_backtest_to_config_page()

    def _apply_backtest_switches(self, cfg: dict) -> None:
        """
        收盘清仓跟实盘配置页总开关一致，写进这一次回测副本。

        趋势跟随看的是自己那一节里的开关，这里不动。
        """
        st = cfg.setdefault("strategy", {})
        live_flat = bool(self.flatten_at_close_var.get())
        st["flatten_at_close"] = live_flat
        if hasattr(self, "bt_flatten_var"):
            self.bt_flatten_var.set(live_flat)
        # 成交近似：下一根开盘 + 滑点跳数，只写进这一次回测副本。
        bt = cfg.setdefault("backtest", {})
        if hasattr(self, "bt_fill_next_open_var"):
            bt["fill_on_next_open"] = bool(self.bt_fill_next_open_var.get())
        if hasattr(self, "bt_slippage_var"):
            try:
                bt["slippage_ticks"] = max(0.0, float(self.bt_slippage_var.get() or 0))
            except (TypeError, ValueError):
                logger.warning("滑点跳数不是数字，沿用配置里的 {}", bt.get("slippage_ticks", 1))

    def _table_margin_for_symbol(self, symbol: str | None = None) -> float:
        """从品种与合约表（已载入的 risk.contracts）读该品种每手保证金。没有或为 0 则返回 0。"""
        from src.risk.universe_table import product_key

        code = (symbol or self.bt_symbol_var.get() or "").strip()
        key = product_key(code)
        if not key:
            return 0.0
        risk = self.cfg.get("risk") or {}
        box = (risk.get("contracts") or {}).get(key)
        if not isinstance(box, dict):
            return 0.0
        try:
            return max(0.0, float(box.get("margin") or 0))
        except (TypeError, ValueError):
            return 0.0

    def _sync_bt_margin_from_table(self, symbol: str | None = None) -> None:
        """
        把回测页「每手保证金」改成品种表里的数。

        表里没有或为 0 时，改用配置页统一的每手保证金。
        """
        if not hasattr(self, "bt_margin_var"):
            return
        margin = self._table_margin_for_symbol(symbol)
        if margin <= 0:
            try:
                margin = max(0.0, float(self.margin_var.get() or 0))
            except (TypeError, ValueError):
                margin = 0.0
        text = str(int(margin)) if margin == int(margin) else str(margin)
        try:
            if str(self.bt_margin_var.get() or "").strip() == text:
                return
        except Exception:  # noqa: BLE001
            pass
        self.bt_margin_var.set(text)

    def _align_backtest_trading_params_with_live(self) -> None:
        """
        仓位、风险、保证金、收盘清仓与实盘配置对齐，并刷新回测页显示。

        策略引擎 / 周期 / 品种仍可由回测页单独选，方便对比；真正影响手数和保证金的数字跟实盘走。
        """
        if not hasattr(self, "bt_capital_var"):
            return
        risk = self.cfg.get("risk") or {}
        self.bt_sizing_var.set(self.sizing_mode_var.get() or "risk_pct")
        self.bt_capital_var.set(self.capital_var.get())
        self.bt_risk_pct_var.set(self.risk_pct_var.get())
        self.bt_margin_usage_pct_var.set(self.margin_usage_pct_var.get())
        self.bt_volume_var.set(self.volume_var.get())
        self.bt_max_loss_var.set(self.max_loss_var.get())
        self.bt_interval_var.set(self.min_interval_var.get())
        self.bt_max_orders_var.set(self.max_orders_var.get())
        self.bt_min_vol_var.set(str(risk.get("min_volume", 1)))
        if hasattr(self, "bt_flatten_var") and hasattr(self, "flatten_at_close_var"):
            self.bt_flatten_var.set(bool(self.flatten_at_close_var.get()))
        self._sync_bt_margin_from_table()

    def _apply_backtest_limits(self, cfg: dict, *, symbol: str | None = None) -> None:
        """
        把实盘配置里的开仓限制写进这一次回测/优化副本（保证金优先品种表）。

        不写磁盘。回测页「启用限制」关掉时仍固定 1 手，便于只看信号。
        symbol：算哪一个合约的表内保证金；空则用历史回测页当前品种。
        """
        self._align_backtest_trading_params_with_live()
        risk = cfg.setdefault("risk", {})
        trade = cfg.setdefault("trade", {})
        # 开关关掉：固定 1 手，后面的数字先留着，下次打开还在。
        risk["apply_limits"] = bool(self.bt_limits_on_var.get())
        if not risk["apply_limits"]:
            trade["volume"] = 1
            risk["sizing_mode"] = "fixed"
            return
        try:
            # 一律读配置页（实盘）数字，避免回测/优化页旧值跟实盘不一致。
            risk["sizing_mode"] = self.sizing_mode_var.get() or "risk_pct"
            risk["capital"] = float(self.capital_var.get())
            risk["risk_per_trade_pct"] = float(self.risk_pct_var.get())
            risk["margin_usage_pct"] = max(0.0, float(self.margin_usage_pct_var.get() or 0))
            trade["volume"] = int(float(self.volume_var.get()))
            risk["min_volume"] = max(1, int(float(risk.get("min_volume", 1) or 1)))
            risk.pop("max_position", None)
            risk["max_daily_loss"] = float(self.max_loss_var.get())
            risk["min_order_interval_sec"] = max(0, int(float(self.min_interval_var.get())))
            risk["max_orders_per_day"] = max(1, int(float(self.max_orders_var.get())))
            generic_margin = max(0.0, float(self.margin_var.get() or 0))
        except ValueError as e:
            raise ValueError("配置页的权益、风险和手数要填数字") from e
        code = (symbol or self.bt_symbol_var.get() or "").strip()
        table_margin = self._table_margin_for_symbol(code)
        margin = table_margin if table_margin > 0 else generic_margin
        from src.risk.sizing import bind_run_margin

        bind_run_margin(cfg, margin)
        logger.info(
            "开仓限制已按实盘对齐 | 权益={:g} 风险%={} 模式={} 保证金={}→{:g}",
            risk["capital"],
            risk["risk_per_trade_pct"],
            risk["sizing_mode"],
            code or "?",
            margin,
        )

    def _prepare_backtest_run(self) -> tuple[dict, str, str | None, str | None] | None:
        """把回测页的选项收成一份可跑的回测配置。参数不对时弹窗并返回 None。"""
        from tkinter import messagebox

        # 先对齐开仓限制，再克隆，保证副本里就是实盘数字。
        self._align_backtest_trading_params_with_live()
        cfg = self._clone_cfg()
        try:
            self._apply_backtest_limits(cfg)
        except ValueError as e:
            messagebox.showerror("数字不对", str(e))
            return None
        # 用回测页选择的策略/周期，只改这份副本
        st = cfg.setdefault("strategy", {})
        st["engine"] = self.bt_engine_var.get()
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.bt_source_var.get())
        st["timeframe"] = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)
        symbol = self.bt_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约，例如 bu2612。")
            return None
        st["symbols"] = [symbol]
        # 收盘清仓跟实盘配置页总开关一致。
        self._apply_backtest_switches(cfg)
        try:
            preset = self.bt_range_var.get()
            # 日期解析和上面的下拉必须在同一层 try 里，填错才能弹出「日期不对」。
            start, end = self._resolve_date_range(
                preset, self.bt_start_var.get(), self.bt_end_var.get()
            )
        except ValueError as e:
            messagebox.showerror("日期不对", str(e))
            return None
        # 填了日期就按日期走，并把下拉改成「自定义」，免得下次又被「全部可用」忽略
        if preset == "全部可用" and (start or end):
            self.bt_range_var.set("自定义")
        if start:
            self.bt_start_var.set(start)
        if end:
            self.bt_end_var.set(end)
        return cfg, symbol, start, end

    def _ths_bar_limit(self) -> int:
        """读图根数。写错数字时用 800，最少 10，最多 5000。"""
        raw = (self.bt_ths_bars_var.get() or "").strip() or "800"
        try:
            n = int(float(raw))
        except ValueError:
            n = 800
        return max(10, min(5000, n))

    def _set_ths_scraping(self, busy: bool) -> None:
        """读图进行中：禁止再点读取，允许点停止。"""
        self._ths_scraping = bool(busy)
        scrape = getattr(self, "bt_ths_scrape_btn", None)
        stop = getattr(self, "bt_ths_stop_btn", None)
        if scrape is not None:
            scrape.configure(state="disabled" if busy else "normal")
        if stop is not None:
            stop.configure(state="normal" if busy else "disabled")

    def _on_escape_key(self, _event=None):
        """读同花顺图时 ESC 当停止。其它时候不拦。"""
        if self._ths_scraping:
            self._on_stop_ths_chart()
            return "break"
        return None

    def _on_stop_ths_chart(self) -> None:
        """点停止或按 ESC：读图循环下一拍就停，已读到的会存盘。"""
        from src.data.ths_chart import request_ths_stop

        if not self._ths_scraping:
            return
        request_ths_stop()
        self._append_log("已请求停止读图。按 ESC 也可以。已读到的会先存下来。")

    def _confirm_ths_chart(self) -> bool:
        """读图前请用户确认期货通已经打开。取消就不读。"""
        from tkinter import messagebox

        n = self._ths_bar_limit()
        return bool(
            messagebox.askokcancel(
                "从同花顺图读K线",
                "请先打开期货通，主图选好这个合约和这个周期。\n"
                f"点确定后先等 3 秒，再开始读，最多 {n} 根。\n"
                "读的时候请不要动鼠标键盘。按 ESC 或点「停止读图」可停。\n"
                "信息框顶部是这根K线的结束时间。不会点买卖。\n\n继续？",
            )
        )

    def _on_scrape_ths_chart(self) -> None:
        """只把同花顺图读进本地库，不跑策略。方便再点检查行情质量。"""
        import threading
        from tkinter import messagebox

        from src.data.kline import KlineProvider
        from src.data.ths_chart import ThsScrapeStopped

        symbol = (self.bt_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
            return
        if self._ths_scraping:
            messagebox.showinfo("正在读图", "正在读同花顺图。要停请按 ESC 或点「停止读图」。")
            return
        period = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        limit = self._ths_bar_limit()
        if not self._confirm_ths_chart():
            return
        self._set_ths_scraping(True)
        self._append_log(
            f"准备从同花顺图读取 {symbol} {period}，最多 {limit} 根。"
            "3 秒后开始，请把主图放到前面。按 ESC 可取消。"
        )

        def work() -> None:
            try:
                cfg = self._clone_cfg()
                data = cfg.setdefault("data", {})
                data["source"] = "ths"
                ths_cfg = dict(data.get("ths_chart") or {})
                ths_cfg["max_bars"] = limit
                ths_cfg.setdefault("ready_sec", 3)
                data["ths_chart"] = ths_cfg
                provider = KlineProvider(cfg)
                # 直接读图再入库。回测 fetch 只用本地库，不会再抢鼠标。
                frame = provider.import_ths_chart(symbol, period, limit=limit)
                n = 0 if frame is None else len(frame)
                last = "" if frame is None or n == 0 else str(frame.index[-1])
                self.after(
                    0,
                    lambda n=n, last=last: self._append_log(
                        f"同花顺图已存 {n} 根，最新 {last or '无'}（@ths）"
                    ),
                )
            except ThsScrapeStopped as exc:
                err = str(exc)
                self.after(0, lambda m=err: self._append_log(m))
                self.after(0, lambda m=err: messagebox.showinfo("已停止", m))
            except Exception as exc:  # noqa: BLE001
                # Python 3.12 结束后会清掉 except 里的 exc，after 里再 str(exc) 会变成 NameError。
                err = str(exc)
                logger.exception("从同花顺图读取失败")
                self.after(0, lambda m=err: messagebox.showerror("没读到", m))
            finally:
                self.after(0, lambda: self._set_ths_scraping(False))

        threading.Thread(target=work, daemon=True).start()

    def _on_backtest(self) -> None:
        """后台跑历史回测，结果写本页 + data/backtest_last.json。"""
        prepared = self._prepare_backtest_run()
        if prepared is None:
            return
        cfg, symbol, start, end = prepared
        st = cfg["strategy"]

        self._append_log(
            f"开始历史回测… engine={st['engine']} symbol={symbol} "
            f"行情={self.bt_source_var.get()} "
            f"区间={start or '起'}~{end or '止'} "
            f"换月={'按主力时段' if self.bt_rollover_var.get() else '否'}"
        )
        self.tabs.set("历史回测")
        if hasattr(self, "bt_result_box"):
            self._set_result_text(
                self.bt_result_box,
                "历史回测进行中，请稍候…\n正在下载行情。网络慢时最多大约 20 秒，然后改用电脑里已经有的K线。\n",
            )

        def _run() -> None:
            try:
                from src.backtest.engine import run_backtest, save_backtest_report
                from src.backtest.rollover import run_backtest_rollover, format_rollover_lines

                if self.bt_rollover_var.get():
                    result = run_backtest_rollover(
                        cfg,
                        symbol=symbol,
                        start_date=start,
                        end_date=end,
                        engine=st["engine"],
                        chain_capital=bool(self.bt_rollover_chain_var.get()),
                    )
                else:
                    result = run_backtest(
                        cfg, symbol=symbol, start_date=start, end_date=end, engine=st["engine"]
                    )
                path = self.root_dir / "data" / "backtest_last.json"
                save_backtest_report(result, path)
                p = result.performance
                snap = result.params_snapshot or {}
                rollover_text = format_rollover_lines(snap)
                note = ""
                if p.trades < 3:
                    note = (
                        f"\n注意：K线 {result.bars} 根、成交仅 {p.trades} 笔，"
                        "胜率参考价值低（可改成「全部可用」再试）。"
                    )
                from src.screen.picker import format_cover_lines

                # 摘要 + 最近几笔成交。盈利覆盖等和交叉测试同一套算法，方便对照。
                lines = [
                    f"历史回测完成  {result.symbol}",
                    f"跑完时间  {result.ran_at}",
                    f"策略      {snap.get('engine', st['engine'])}",
                    "收盘清仓  开（到点强平）"
                    if snap.get("flatten_at_close", True)
                    else "收盘清仓  关（可留过收盘；区间最后一根仍会按收盘价结算未平的仓）",
                    f"周期      信号{period_label(result.timeframe)} / 止损{period_label(result.trail_timeframe)}",
                    self._format_limit_lines(snap),
                    f"你选的区间  {snap.get('start_date') or '不限'} ~ {snap.get('end_date') or '不限'}",
                    f"行情能给的  {str(snap.get('available_from', ''))[:16]}  →  {str(snap.get('available_to', ''))[:16]}",
                    f"实际区间    {str(snap.get('data_from', ''))[:16]}  →  {str(snap.get('data_to', ''))[:16]}",
                    str(snap.get("coverage_note") or "").strip(),
                    *([rollover_text] if rollover_text else []),
                    str(snap.get("fill_note") or "").strip(),
                    f"K线根数   {result.bars}",
                    f"成交      {p.trades} 笔（赢{p.wins} / 亏{p.losses}）",
                    f"没开成    {len(result.rejected)} 条开仓信号（被策略过滤或风控挡住，明细在下面）"
                    if result.rejected
                    else "没开成    无（每个开仓信号都成交了）",
                    f"胜率      {p.win_rate:.0%}",
                    f"净盈亏    {p.net_pnl:.0f} 元",
                    f"盈利因子  {p.profit_factor:.2f}",
                    f"最大回撤  {p.max_drawdown:.0f} 元（{p.max_drawdown_pct:.2%}）",
                    *format_cover_lines(
                        p.net_pnl,
                        p.max_drawdown,
                        p.trades,
                        str(snap.get("data_from") or ""),
                        str(snap.get("data_to") or ""),
                    ),
                    f"报告      {path}",
                    note.strip(),
                    "",
                    "—— 成交明细（最多显示最近 15 笔）——",
                ]
                for t in result.trades[-15:]:
                    lines.append(
                        f"{t.entry_time[:16]}→{t.exit_time[:16]} {t.side}{t.volume}手 "
                        f"{t.entry_price:.1f}->{t.exit_price:.1f} "
                        f"pnl={t.pnl:.0f} | {t.reason_exit[:40]}"
                    )
                if result.rejected:
                    lines.append("")
                    lines.append(
                        f"—— 有开仓信号但没做（同一交易时段同类原因只记一次，共 "
                        f"{len(result.rejected)} 条，最多显示最近 20 条）——"
                    )
                    for item in result.rejected[-20:]:
                        price = f"{item.price:.2f}" if item.price else "—"
                        stop = f"{item.stop:.2f}" if item.stop else "—"
                        lines.append(
                            f"{item.time[:16]} {item.side} 价{price} 止损{stop} "
                            f"| {item.stage}：{item.reason[:80]}"
                        )
                text = "\n".join(x for x in lines if x is not None)
                self._post_to_main(lambda: self._append_log(
                    f"历史回测完成 {result.symbol} | {result.bars}根 | "
                    f"{p.trades}笔 胜率{p.win_rate:.0%} 净盈亏{p.net_pnl:.0f}"
                ))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("历史回测失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"历史回测失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.bt_result_box, f"历史回测失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("历史回测失败", m))

        threading.Thread(target=_run, daemon=True).start()

    def _main_table_path(self) -> Path:
        """当前回测会用的主力合约表路径。"""
        from src.backtest.rollover import resolve_main_table

        found = resolve_main_table({"_project_root": str(self.root_dir)})
        return found if found else (self.root_dir / "config" / "主力合约表.xlsx")

    def _refresh_main_table_state(self) -> None:
        """按钮旁显示表的最后更新时间，方便确认表是不是旧的。"""
        if not hasattr(self, "bt_table_state"):
            return
        path = self._main_table_path()
        if path.exists():
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self.bt_table_state.configure(text=f"更新于 {stamp}")
        else:
            self.bt_table_state.configure(text="还没有表，点「更新」生成")

    def _on_open_main_table(self) -> None:
        """用系统默认程序打开主力合约表，方便直接改「手动修正」页。"""
        from tkinter import messagebox

        path = self._main_table_path()
        if not path.exists():
            messagebox.showinfo(
                "还没有主力合约表",
                "先点「更新主力合约表」生成，再打开。\n生成后请在「手动修正」页里加行。",
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开", f"打不开主力合约表。\n\n{exc}")

    def _on_update_main_table(self) -> None:
        """手动更新主力合约表：只有点这个按钮才会重新拉行情，平时不会自动更新。"""
        from tkinter import messagebox

        if getattr(self, "_main_table_busy", False):
            return
        path = self._main_table_path()
        if not messagebox.askyesno(
            "更新主力合约表",
            f"现在重新下载日行情并重算主力时段？\n\n输出：{path}\n\n"
            "「手动修正」页会原样保留，不会被覆盖。\n"
            "大商所品种要逐个合约拉日线，会慢一些，期间可以切到其它页面。",
        ):
            return
        self._main_table_busy = True
        if hasattr(self, "bt_table_btn"):
            self.bt_table_btn.configure(state="disabled", text="更新中…")
        if hasattr(self, "bt_table_state"):
            self.bt_table_state.configure(text="正在更新，请稍候…")
        self._append_log("开始更新主力合约表（手动触发）…")
        start = f"{date.today().year}-01-01"

        def _progress(msg: str) -> None:
            self._post_to_main(lambda m=msg: self._append_log(m))

        def _run() -> None:
            ok = False
            try:
                from src.backtest.main_table import update_table

                summary = update_table(self.root_dir, start=start, out=path, progress=_progress)
                self._post_to_main(lambda s=summary: self._append_log(
                    f"主力合约表已更新：{s['products']} 个品种、{s['periods']} 段 | "
                    f"区间 {s['start']}~{s['end']}"
                ))
                ok = True
            except Exception as e:  # noqa: BLE001
                logger.exception("主力合约表更新失败")
                err = str(e)
                if isinstance(e, PermissionError):
                    err = f"写不进表，文件正被占用。请先关闭 Excel 里打开的{path.name}再更新。\n（{e}）"
                self._post_to_main(lambda m=err: self._append_log(f"主力合约表更新失败: {m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("主力合约表更新失败", m))
            finally:
                self._post_to_main(lambda good=ok: self._finish_main_table_update(good))

        threading.Thread(target=_run, daemon=True).start()

    def _finish_main_table_update(self, ok: bool) -> None:
        """恢复按钮，并刷新表的时间戳。"""
        self._main_table_busy = False
        if hasattr(self, "bt_table_btn"):
            self.bt_table_btn.configure(state="normal", text="更新主力合约表")
        if not ok and hasattr(self, "bt_table_state"):
            self.bt_table_state.configure(text="更新失败，表没有改动")
            return
        self._refresh_main_table_state()

    def _on_apply_slippage_hint(self) -> None:
        """把实盘滑点 70 分位建议填进回测滑点框。要点确认，不会自己改。"""
        from tkinter import messagebox

        from src.account.slippage_log import summarize

        info = summarize(self.root_dir)
        if not info["n"]:
            messagebox.showinfo(
                "实盘滑点",
                "还没有滑点记录。有真实成交（尤其是 CTP 回报）之后才会给出建议。",
            )
            return
        suggested = float(info["suggested"] or 0)
        scope = "真实成交" if info["used_real"] else "全部记录（含点击假定成交，仅供参考）"
        if not messagebox.askyesno(
            "填入回测滑点",
            f"样本 {info['n']} 笔（{scope}）。\n"
            f"中位 {info['median']:+.2f} 跳，70分位 {info['p70']:+.2f} 跳。\n"
            f"建议回测填 {suggested:g} 跳。\n\n"
            "只改历史回测页这个数字，不改开平仓规则。要填入吗？",
        ):
            return
        self.bt_slippage_var.set(str(suggested))
        self.cfg.setdefault("backtest", {})["slippage_ticks"] = suggested
        self._append_log(f"回测滑点已改成建议值 {suggested:g} 跳（未自动保存配置文件）")

    def _on_slippage_scan(self) -> None:
        """滑点灵敏度：同一份K线换几档滑点重跑，看利润边缘和盈亏分水岭。"""
        from tkinter import messagebox

        prepared = self._prepare_backtest_run()
        if prepared is None:
            return
        cfg, symbol, start, end = prepared
        st = cfg["strategy"]

        self._append_log(f"开始滑点灵敏度… {symbol} 区间={start or '起'}~{end or '止'}")
        self.tabs.set("历史回测")
        if hasattr(self, "bt_result_box"):
            self._set_result_text(
                self.bt_result_box,
                "滑点灵敏度进行中…\nK线只拉一次，之后每档滑点各跑一遍，大约比单次回测多几倍时间。\n",
            )

        def _run() -> None:
            try:
                from src.backtest.engine import FillModel
                from src.backtest.sensitivity import format_slip_scan, run_slippage_scan
                from src.risk.sizing import PositionSizer

                scan = run_slippage_scan(
                    cfg,
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                    engine=st.get("engine"),
                )
                try:
                    current = float(self.bt_slippage_var.get() or 0)
                except (TypeError, ValueError):
                    current = 1.0
                text = format_slip_scan(
                    scan,
                    symbol=symbol,
                    current_slip=current,
                    tick=FillModel.from_cfg(cfg, symbol).price_tick,
                    mult=PositionSizer(cfg).multiplier_for(symbol),
                )
                self._post_to_main(lambda: self._append_log(f"滑点灵敏度完成 {symbol}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("滑点灵敏度失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"滑点灵敏度失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.bt_result_box, f"滑点灵敏度失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("滑点灵敏度失败", m))

        threading.Thread(target=_run, daemon=True).start()

    def _picks_path(self) -> Path:
        """交叉测试自己的记录。和回测报告分开，避免优选把回测结果盖掉。"""
        return self.root_dir / "data" / "symbol_picks.json"

    def _pack_pick_metric_line(
        self,
        parent: ctk.CTkFrame,
        texts: list[str],
        *,
        header: bool = False,
        picked: str = "",
    ) -> None:
        """
        结果表的一行。

        各列用固定宽度，表头和每一行同一套数字，品种再多也不会左右错开。
        结果这一列按文字上色：入选和第 1 名用绿色，没测成用黄色，基准用蓝色。
        """
        line = ctk.CTkFrame(parent, fg_color="transparent")
        line.pack(fill="x")
        if str(picked).startswith("第"):
            picked_color = T.OK if picked == "第1名" else T.TEXT
        else:
            picked_color = {
                "入选": T.OK,
                "较好": T.OK,
                "没测成": T.WARN,
                "基准": T.ACCENT,
            }.get(picked, T.TEXT)
        for index, text in enumerate(texts):
            color = T.TEXT_DIM if header else T.TEXT
            if not header and text == "偏少":
                color = T.WARN
            elif not header and "不适配" in text:
                color = T.WARN
            elif not header and index == len(texts) - 1:
                color = picked_color if picked != "未入选" else T.TEXT_DIM
            label = ctk.CTkLabel(
                line,
                text=text,
                width=self._pick_col_widths[index],
                anchor="w",
                font=T.FONT_SMALL,
                text_color=color,
            )
            label.pack(side="left", padx=(8 if index == 0 else 4, 0), pady=5)
            if header:
                self._pick_header_labels.append(label)

    def _render_pick_board(self, summary: str, rows: list[dict[str, str]], *, empty_text: str = "") -> None:
        """刷新右上角说明，并按行重画全部品种。旧行先清掉，避免上一轮的成绩留在表里。"""
        if hasattr(self, "pick_summary"):
            self.pick_summary.configure(text=summary)
        body = getattr(self, "pick_table_body", None)
        if body is None:
            return
        for child in body.winfo_children():
            child.destroy()
        if not rows:
            ctk.CTkLabel(
                body,
                text=empty_text or "测完后，每个品种和策略的组合占一行。较好的排在前面。",
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                anchor="w",
            ).pack(anchor="w", padx=8, pady=8)
            return
        # 上次如果测了上千个组合，一次画出来会让窗口打不开。这里只画前面几十行。
        from src.screen.picker import window_rows

        visible, extra = window_rows(rows)
        for item in visible:
            self._pack_pick_metric_line(
                body,
                [
                    item.get("symbol") or "-",
                    item.get("strategy") or "—",
                    item.get("win_rate") or "—",
                    item.get("profit_factor") or "—",
                    item.get("max_drawdown") or "—",
                    item.get("net_pnl") or "—",
                    item.get("profit_cover") or "—",
                    item.get("return_rate") or "—",
                    item.get("frequency") or "—",
                    item.get("trade_count") or "—",
                    item.get("pnl_per_trade") or "—",
                    item.get("span") or "—",
                    item.get("span_start") or "—",
                    item.get("span_end") or "—",
                    item.get("span_days") or "—",
                    item.get("daily_avg") or "—",
                    item.get("bars") or "—",
                    item.get("score") or "—",
                    item.get("market") or "—",
                    item.get("liquidity") or "—",
                    item.get("picked") or "—",
                ],
                picked=str(item.get("picked") or ""),
            )
        if extra:
            ctk.CTkLabel(
                body,
                text=extra,
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                anchor="w",
                justify="left",
                wraplength=640,
            ).pack(anchor="w", padx=8, pady=8)

    def _show_pick_result(self, result: dict[str, Any]) -> None:
        """用这一轮的组合画结果表。较好的排在前面。"""
        from src.screen.picker import board_summary, list_rows

        self._render_pick_board(board_summary(result), list_rows(result))

    def _load_saved_picks(self) -> None:
        """打开窗口时恢复上一次的基准和备选，免得每次都要重测。"""
        from src.screen.picker import liquidity_text, load_picks

        def _drop_thin(rows: list) -> list:
            # 上次结果里如果还留着流动性偏少的品种，打开窗口时不再显示。
            kept = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("liquidity") or "") == "偏少":
                    continue
                if liquidity_text(str(row.get("symbol") or "")) == "偏少":
                    continue
                kept.append(row)
            return kept

        try:
            data = load_picks(self._picks_path())
        except Exception as exc:  # noqa: BLE001
            # 上次的结果读不出来时，窗口仍要能打开，不能卡在这里。
            logger.warning("上次交叉测试结果读不出来，先打开空表: {}", exc)
            return
        baseline = data.get("baseline") if isinstance(data.get("baseline"), dict) else None
        self._pick_baseline = baseline or None
        rows = _drop_thin(data.get("rows") or [])
        if baseline or data.get("rows"):
            shown = {
                "baseline": baseline or {},
                "rows": rows,
                "volume": data.get("volume"),
                "mode": data.get("mode") or "baseline",
                "settings": data.get("settings") or {},
                "stopped": False,
            }
            if "skipped_thin" in data:
                shown["skipped_thin"] = data.get("skipped_thin")
            self._show_pick_result(shown)

    def _align_picker_trading_params_with_live(self) -> None:
        """交叉测试：手数、风险、间隔、次数、收盘清仓跟实盘配置对齐，并刷新本页显示。"""
        if not hasattr(self, "pk_volume_var"):
            return
        self.pk_flatten_var.set(bool(self.flatten_at_close_var.get()))
        self.pk_risk_pct_var.set(self.risk_pct_var.get())
        self.pk_volume_var.set(self.volume_var.get())
        self.pk_interval_var.set(self.min_interval_var.get())
        self.pk_max_orders_var.set(self.max_orders_var.get())

    def _fill_picker_from_config(self) -> None:
        """把配置页当前的周期、行情、持仓手数、每笔风险和开仓次数抄到交叉测试页。不抄账户权益，也不写文件。"""
        self.pk_source_var.set(self.source_var.get())
        self._set_period_vars(self._period_code(self.tf_var, self.tf_custom_var), self.pk_tf_var, self.pk_tf_custom_var)
        self._set_period_vars(
            self._period_code(self.trail_var, self.trail_custom_var),
            self.pk_trail_var,
            self.pk_trail_custom_var,
        )
        self._align_picker_trading_params_with_live()
        self._refresh_cross_lists()
        self._append_log(f"已按配置页填好交叉测试的周期、行情和开仓限制，行情 {self.pk_source_var.get()}")

    def _picker_settings(self) -> dict[str, Any]:
        """
        读交叉测试用的周期和开仓限制。

        手数/风险/间隔/次数/收盘清仓强制跟实盘配置；日期仍用本页。数字填错就停下来。
        """
        self._align_picker_trading_params_with_live()
        preset = self.pk_range_var.get()
        start, end = self._resolve_date_range(preset, self.pk_start_var.get(), self.pk_end_var.get())
        if preset == "全部可用" and (start or end):
            self.pk_range_var.set("自定义")
        if start:
            self.pk_start_var.set(start)
        if end:
            self.pk_end_var.set(end)
        try:
            # 与实盘配置页同一套数字，避免交叉测试页旧值各跑各的。
            volume = int(float(self.volume_var.get()))
            risk_pct = float(self.risk_pct_var.get())
            interval = int(float(self.min_interval_var.get()))
            max_orders = int(float(self.max_orders_var.get()))
        except ValueError as exc:
            raise ValueError("配置页的每笔风险、持仓手数、开仓间隔和每天最多开仓要填数字") from exc
        if volume < 1:
            raise ValueError("持仓手数至少是 1")
        if risk_pct < 0:
            raise ValueError("每笔风险不能是负数")
        if interval < 0:
            raise ValueError("开仓最小间隔不能是负数")
        if max_orders < 1:
            raise ValueError("每天最多开仓至少是 1")
        return {
            "timeframe": self._period_code(self.pk_tf_var, self.pk_tf_custom_var),
            "trail_timeframe": self._period_code(self.pk_trail_var, self.pk_trail_custom_var),
            "start_date": start or "",
            "end_date": end or "",
            "flatten_at_close": bool(self.flatten_at_close_var.get()),
            "has_flatten": True,
            "fixed_volume": volume,
            "risk_per_trade_pct": risk_pct,
            "min_order_interval_sec": interval,
            "max_orders_per_day": max_orders,
            "data_source": self.pk_source_var.get(),
        }

    def _on_pick_opt_cross(self) -> None:
        """
        用策略优化刚选出的参数，在关注品种上做多品种样本外验收。

        不再重新搜参数。这和「开始交叉测试」不同：交叉测试是配置固定参数的对照筛选。
        """
        from tkinter import messagebox

        if self._pick_running:
            messagebox.showinfo("正在测试", "上一轮还没结束。可以点「停止测试」。")
            return
        params = dict(getattr(self, "_last_opt_params", None) or {})
        engine = str(getattr(self, "_last_opt_engine", "") or "")
        if not params or not engine:
            messagebox.showwarning(
                "还没有优化结果",
                "请先到「策略优化」页跑完一轮，再回到这里做多品种验收。",
            )
            return
        symbols = [str(x).strip() for x in self._watch_symbols if str(x).strip()]
        if not symbols:
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加合约。")
            return
        try:
            settings = self._picker_settings()
        except ValueError as exc:
            messagebox.showerror("数字不对", str(exc))
            return
        cfg = self._clone_cfg()
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.pk_source_var.get())
        st = cfg.setdefault("strategy", {})
        st["timeframe"] = str(settings.get("timeframe") or self._period_code(self.pk_tf_var, self.pk_tf_custom_var))
        st["trail_timeframe"] = str(
            settings.get("trail_timeframe") or self._period_code(self.pk_trail_var, self.pk_trail_custom_var)
        )
        if "flatten_at_close" in settings:
            st["flatten_at_close"] = bool(settings.get("flatten_at_close"))
        start = str(settings.get("start_date") or getattr(self, "_last_opt_start", "") or "")
        end = str(settings.get("end_date") or getattr(self, "_last_opt_end", "") or "")
        self._append_log(
            f"开始多品种验收：参数来自最近一次策略优化（{engine}），共 {len(symbols)} 个关注合约"
        )
        self._pick_running = True
        self._pick_cancel.clear()
        self._render_pick_board("正在用最近优化参数做多品种验收…", [])

        def _run() -> None:
            from src.backtest.optimizer import describe_params, format_perf_block, validate_params_on_symbols
            from src.screen.picker import save_picks

            try:
                rows = validate_params_on_symbols(
                    cfg,
                    params=params,
                    engine=engine,
                    symbols=symbols,
                    skip_symbol="",
                    start_date=start or None,
                    end_date=end or None,
                )
                if self._pick_cancel.is_set():

                    def _stopped() -> None:
                        self._pick_running = False
                        self._render_pick_board("多品种验收已停止。表里是已经测完的部分。", [])

                    self._post_to_main(_stopped)
                    return
                # 这里只展示各品种成绩，不替用户决定能不能写入配置，所以不拿训练净盈亏去卡。
                reason = "各品种成绩已列出。要不要写入配置，仍以策略优化页的检验结论为准。"
                from src.screen.picker import span_cover_fields

                board_rows: list[dict[str, str]] = []
                for row in rows:
                    err = str(row.get("error") or "")
                    code = str(row.get("symbol") or "")
                    cover = span_cover_fields(
                        row.get("net_pnl"),
                        row.get("max_drawdown"),
                        row.get("trades"),
                        start or "",
                        end or "",
                    )
                    if err:
                        cover_cells = {
                            "profit_cover": "—",
                            "trade_count": "—",
                            "pnl_per_trade": "—",
                            "span_start": "—",
                            "span_end": "—",
                            "span_days": "—",
                            "daily_avg": "—",
                        }
                    else:
                        per = cover["pnl_per_trade"]
                        daily = cover["daily_avg"]
                        days = cover["span_days"]
                        cover_cells = {
                            "profit_cover": f"{cover['profit_cover']:.0f}",
                            "trade_count": str(int(cover["trade_count"])),
                            "pnl_per_trade": f"{per:.2f}" if per is not None else "—",
                            "span_start": cover["span_start"] or "—",
                            "span_end": cover["span_end"] or "—",
                            "span_days": str(int(days)) if days is not None else "—",
                            "daily_avg": f"{daily:.2f}" if daily is not None else "—",
                        }
                    board_rows.append(
                        {
                            "symbol": code,
                            "strategy": engine,
                            "win_rate": "-" if err else f"{float(row.get('win_rate') or 0):.0%}",
                            "profit_factor": "-" if err else f"{float(row.get('profit_factor') or 0):.2f}",
                            "max_drawdown": "-" if err else f"{float(row.get('max_drawdown') or 0):.0f}",
                            "net_pnl": err if err else f"{float(row.get('net_pnl') or 0):.0f}",
                            "profit_cover": cover_cells["profit_cover"],
                            "return_rate": "—",
                            "frequency": f"{int(row.get('trades') or 0)}笔" if not err else "—",
                            "trade_count": cover_cells["trade_count"],
                            "pnl_per_trade": cover_cells["pnl_per_trade"],
                            "span": f"{start or '不限'}~{end or '不限'}",
                            "span_start": cover_cells["span_start"],
                            "span_end": cover_cells["span_end"],
                            "span_days": cover_cells["span_days"],
                            "daily_avg": cover_cells["daily_avg"],
                            "bars": str(int(row.get("bars") or 0)),
                            "score": "-" if err else f"{float(row.get('score') or 0):.2f}",
                            "liquidity": "优化参数验收",
                            "picked": "没测成" if err else "一般",
                        }
                    )
                lines = [
                    f"多品种验收完成（策略 {engine}）",
                    f"参数  {describe_params(params)}",
                    f"区间  {start or '不限'} ~ {end or '不限'}",
                    "说明  参数不再重搜，这是样本外多品种验收，不是交叉测试对照筛选。",
                    "",
                ]
                for row in rows:
                    if row.get("error"):
                        lines.append(f"{row.get('symbol')}  失败：{row.get('error')}")
                    else:
                        lines.extend(
                            format_perf_block(
                                str(row.get("symbol") or ""),
                                row,
                                bars=int(row.get("bars") or 0),
                            )
                        )
                        lines.append("")
                lines.append(reason or "各品种成绩已列出，请自己对照。")
                text = "\n".join(lines)
                save_picks(
                    self._picks_path(),
                    {
                        "baseline": {},
                        "candidates": [],
                        "rows": board_rows,
                        "report": text,
                        "ran_at": "",
                        "volume": settings.get("fixed_volume") or 1,
                        "mode": "opt_cross",
                        "settings": settings,
                        "skipped_thin": 0,
                    },
                )

                def _done() -> None:
                    self._pick_running = False
                    self._render_pick_board(
                        f"多品种验收完成：{len(rows)} 个合约。这是优化参数的样本外成绩，不是交叉测试的「较好」名单。",
                        board_rows,
                    )
                    self._append_log(f"多品种验收完成，测了 {len(rows)} 个合约")

                self._post_to_main(_done)
            except Exception as exc:  # noqa: BLE001
                logger.exception("多品种验收失败")
                err = str(exc)

                def _fail() -> None:
                    self._pick_running = False
                    self._render_pick_board(f"多品种验收失败：{err}", [])
                    self._append_log(f"多品种验收失败: {err}")
                    messagebox.showerror("多品种验收失败", err)

                self._post_to_main(_fail)

        threading.Thread(target=_run, daemon=True).start()

    def _on_pick_start(self) -> None:
        """后台把配置里的合约和参与策略配着回测。界面只负责显示进度，不自己算指标。"""
        from tkinter import messagebox

        from src.screen.picker import contracts_for_cross, engines_for_cross

        if self._pick_running:
            messagebox.showinfo("正在测试", "上一轮还没结束。可以点「停止测试」。")
            return
        contracts = contracts_for_cross(self._watch_symbols)
        if not contracts:
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加要测的合约，例如 rb2701。")
            return
        try:
            settings = self._picker_settings()
        except ValueError as exc:
            messagebox.showerror("数字不对", str(exc))
            return
        cfg = self._clone_cfg()
        engines = engines_for_cross(cfg)
        self._append_log(
            f"开始交叉测试：{len(contracts)} 个合约，{len(engines)} 套策略，"
            f"每个组合 {settings['fixed_volume']} 手（跟实盘手数），"
            f"间隔 {settings['min_order_interval_sec']} 秒，每天最多 {settings['max_orders_per_day']} 次，"
            f"收盘清仓={'开' if settings.get('flatten_at_close') else '关'}，"
            f"不看账户权益，行情 {self.pk_source_var.get()}"
        )
        # 只用交叉测试页选中的这一家。配置页当时存的来源不参与这一轮。
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.pk_source_var.get())
        self._pick_running = True
        self._pick_cancel.clear()
        self._render_pick_board("正在把每个品种和每一套策略配着测，请稍候…", [])

        def _run() -> None:
            from src.screen.picker import format_report, save_picks, screen_cross

            def on_progress(index: int, total: int, label: str) -> None:
                # 每测一个组合刷新一次，让人知道没有卡住。
                self._post_to_main(lambda i=index, n=total, name=label: self._show_pick_progress(i, n, name))

            try:
                result = screen_cross(
                    cfg,
                    settings,
                    universe=contracts,
                    engines=engines,
                    on_progress=on_progress,
                    cancel=self._pick_cancel.is_set,
                )
                text = format_report(result)
                save_picks(
                    self._picks_path(),
                    {
                        "baseline": {},
                        "candidates": result["candidates"],
                        "rows": result["rows"],
                        "report": text,
                        "ran_at": result["ran_at"],
                        "volume": result["volume"],
                        "mode": result.get("mode") or "baseline",
                        "settings": result.get("settings") or {},
                        "skipped_thin": result.get("skipped_thin") or 0,
                    },
                )
                def _done() -> None:
                    self._pick_running = False
                    self._show_pick_result(result)
                    better = sum(1 for row in result.get("rows") or [] if row.get("picked") == "较好")
                    self._append_log(
                        f"交叉测试完成，测了 {len(result.get('rows') or [])} 个组合，较好 {better} 个"
                    )

                self._post_to_main(_done)
            except Exception as exc:  # noqa: BLE001
                logger.exception("交叉测试失败")
                err = str(exc)

                def _fail() -> None:
                    self._pick_running = False
                    self._append_log(f"交叉测试失败: {err}")
                    self._render_pick_board(f"交叉测试失败：{err}", [])
                    messagebox.showerror("交叉测试失败", err)

                self._post_to_main(_fail)

        threading.Thread(target=_run, daemon=True).start()

    def _show_pick_progress(self, index: int, total: int, label: str) -> None:
        """优选进行中时，右边只留当前进度，测完再换成完整成绩。"""
        if not self._pick_running:
            return
        self._append_log(f"交叉测试 {index}/{total} {label}")
        self._render_pick_board(
            f"正在测 {index}/{total}：{label}\n拉过的K线会留在电脑里，下一次会快一些。",
            [],
        )

    def _on_pick_stop(self) -> None:
        """停在两个组合之间。已经测完的仍会标出较好的。"""
        if not self._pick_running:
            self._append_log("现在没有在做交叉测试")
            return
        self._pick_cancel.set()
        self._append_log("将在当前组合测完后停止")

    def _set_watch_codes(self, codes: list[str], select: str | None = None, save: bool = True) -> None:
        """
        用一份合约列表刷新关注下拉框。

        选中的那一项排到最前。添加、移出或换当前合约时马上写入配置文件，
        重新打开程序时整份名单还在。save=False 只用在从文件读回来的时候。
        """
        if getattr(self, "_watch_busy", False):
            return
        self._watch_busy = True
        try:
            self._apply_watch_codes(codes, select)
        finally:
            self._watch_busy = False
        if save:
            self._persist_watch_symbols()

    def _apply_watch_codes(self, codes: list[str], select: str | None = None) -> None:
        """真正改下拉框。外面的 _set_watch_codes 负责挡住重复刷新。"""
        cleaned: list[str] = []
        seen: set[str] = set()
        for code in codes:
            text = str(code).strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        if not cleaned:
            self._watch_symbols = []
            self.symbols_var.set("")
            self.watch_choice_var.set("（还没有）")
            if hasattr(self, "watch_menu"):
                self.watch_menu.configure(values=["（还没有）"])
            if hasattr(self, "card_symbols"):
                self.card_symbols.configure(text="-")
            self._sync_bt_symbol_menu()
            self._sync_extra_symbol_menus()
            self._refresh_cross_lists()
            self._refresh_main_contract_hint("")
            return
        chosen = select or cleaned[0]
        match = next((item for item in cleaned if item.lower() == chosen.lower()), cleaned[0])
        ordered = [match] + [item for item in cleaned if item.lower() != match.lower()]
        self._watch_symbols = ordered
        self.symbols_var.set(",".join(ordered))
        self.watch_choice_var.set(match)
        if hasattr(self, "watch_menu"):
            self.watch_menu.configure(values=ordered)
        if hasattr(self, "card_symbols"):
            self.card_symbols.configure(text=match)
        # 列表变了就让回测页的下拉一起变。回测页已经选中的合约还在，就先不改它。
        self._sync_bt_symbol_menu()
        self._sync_extra_symbol_menus()
        self._refresh_cross_lists()
        self._refresh_main_contract_hint(match)

    def _refresh_cross_lists(self) -> None:
        """
        刷新交叉测试左边的两份名单。

        策略读配置里的 cross_engines，合约读当前关注品种。
        程序新加的策略若还没写进配置，这里会补上并写回磁盘，避免名单一直落后。
        页还没建好时直接跳过，避免启动顺序出错。
        """
        engine_box = getattr(self, "cross_engine_box", None)
        symbol_box = getattr(self, "cross_symbol_box", None)
        if engine_box is None or symbol_box is None:
            return
        from src.screen.picker import (
            contracts_for_cross,
            engines_for_cross,
            sync_cross_engines_in_cfg,
        )

        before = list(((self.cfg.get("strategy") or {}).get("cross_engines") or []))
        codes = sync_cross_engines_in_cfg(self.cfg)
        if codes != before:
            try:
                disk = load_config()
                sync_cross_engines_in_cfg(disk)
                save_config(disk)
                self._append_log("交叉测试参与策略已补上程序新加的几套，并写入配置")
            except Exception as exc:  # noqa: BLE001
                logger.warning("补写 cross_engines 失败: {}", exc)

        engines = engines_for_cross(self.cfg)
        contracts = contracts_for_cross(self._watch_symbols)
        engine_text = "\n".join(f"{label}    {code}" for code, label in engines) or "配置里还没有参与策略"
        symbol_text = "\n".join(f"{name}    {code}" for code, name, _mult, _thin in contracts) or "配置里还没有合约"
        self._set_readonly_box(engine_box, engine_text)
        self._set_readonly_box(symbol_box, symbol_text)

    def _set_readonly_box(self, box: ctk.CTkTextbox, text: str) -> None:
        """先打开再写入，写完锁住，避免人在这里改了却没存进配置。"""
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _persist_watch_symbols(self) -> None:
        """
        把关注名单写进总表和配置文件。

        添加/移出后立刻改 Excel「启用」列，再写 yaml，保证只维护一处源头。
        """
        from src.risk.universe_table import sync_watch_symbols_to_table

        symbols = list(self._watch_symbols)
        self.cfg.setdefault("strategy", {})["symbols"] = symbols
        if symbols:
            self.cfg.setdefault("trade", {})["symbol"] = symbols[0]
        try:
            path = self._margin_table_abs_path()
            sync_watch_symbols_to_table(
                path,
                symbols,
                contracts=(self.cfg.get("risk") or {}).get("contracts") or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("关注名单没有写入品种与合约表: {}", exc)
        try:
            disk = load_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取配置失败，关注名单先留在窗口里: {}", exc)
            return
        disk.setdefault("strategy", {})["symbols"] = symbols
        if symbols:
            disk.setdefault("trade", {})["symbol"] = symbols[0]
        try:
            save_config(self._merge_disk_gui_coords(disk))
        except Exception as exc:  # noqa: BLE001
            logger.warning("关注名单没有写入配置文件: {}", exc)

    def _config_symbol_now(self) -> str:
        """配置页关注品种下拉框里，现在选中的那一个合约。"""
        current = self.watch_choice_var.get().strip()
        if current and current != "（还没有）":
            return current
        if self._watch_symbols:
            return self._watch_symbols[0]
        return ""

    def _set_main_contract_hint(self, text: str, *, warn: bool = False) -> None:
        """写关注品种下面那行提示。空文本时把高度压到 1 像素。"""
        label = getattr(self, "main_contract_hint", None)
        if label is None:
            return
        if not text:
            label.configure(
                text="", fg_color="transparent", text_color=T.TEXT_DIM, height=1, border_width=0
            )
        elif warn:
            label.configure(
                text=text,
                fg_color="#3A2A12",
                text_color=T.WARN,
                height=30,
                corner_radius=8,
                border_width=1,
                border_color=T.WARN,
            )
        else:
            label.configure(
                text=text, fg_color="transparent", text_color=T.TEXT_DIM, height=22, border_width=0
            )

    def _refresh_main_contract_hint(self, symbol: str | None = None) -> None:
        """
        当前合约不是主力时，在关注品种下面亮出主力合约代码。

        主力合约向天勤的主连要（按成交量和持仓量挑），不在本地猜月份。
        查询放在后台线程，慢也不会卡住界面；换合约后旧结果直接丢掉。
        只有行情来源选「天勤」才登录天勤；选新浪/东方财富时不连。
        """
        label = getattr(self, "main_contract_hint", None)
        if label is None:
            return
        from src.data.quote_check import is_tq_source

        code = (symbol or self._config_symbol_now()).strip()
        self._main_contract_seq = getattr(self, "_main_contract_seq", 0) + 1
        seq = self._main_contract_seq
        if not is_tq_source(self.source_var.get()):
            self._set_main_contract_hint("")
            return
        if not code or code == "（还没有）" or not any(ch.isdigit() for ch in code):
            self._set_main_contract_hint("")
            return
        tq_user = self.tq_user_var.get().strip()
        tq_password = self.tq_password_var.get().strip()
        if not tq_user or not tq_password:
            self._set_main_contract_hint("")
            return
        self._set_main_contract_hint("正在查询主力合约…")

        def run() -> None:
            from src.data.tq_source import main_contract

            try:
                found = main_contract(code, user=tq_user, password=tq_password)
            except Exception as exc:  # noqa: BLE001
                logger.debug("查询主力合约失败 {}: {}", code, exc)
                found = ""
            try:
                self._post_to_main(lambda: self._apply_main_contract_result(seq, code, found))
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=run, name="main-contract", daemon=True).start()

    def _apply_main_contract_result(self, seq: int, code: str, found: str) -> None:
        """后台查到主力后回到界面。慢一步的旧查询结果不再显示。"""
        if seq != getattr(self, "_main_contract_seq", 0):
            return
        if self._config_symbol_now().lower() != code.lower():
            return
        if not found:
            self._set_main_contract_hint("暂时查不到主力合约（天勤未登录或网络不可用）")
            return
        if _same_contract(code, found):
            self._set_main_contract_hint("")
            return
        self._set_main_contract_hint(f"当前合约非主力合约，主力合约为：{found}", warn=True)

    def _sync_bt_symbol_menu(self, prefer: str | None = None, force: bool = False) -> None:
        """
        把历史回测品种下拉刷成配置页现在的关注列表。

        force 为真时，选中项改成 prefer，也就是配置页当前合约。
        平时只更新列表，回测页自己选过的合约还在列表里就留着。
        """
        from src.ui.backtest_fill import choose_backtest_symbol

        values = list(self._watch_symbols) or ["（还没有）"]
        chosen = choose_backtest_symbol(
            values,
            self.bt_symbol_var.get(),
            prefer=prefer,
            force=force,
        )
        if hasattr(self, "bt_symbol_menu"):
            self.bt_symbol_menu.configure(values=values)
            self.bt_symbol_menu.set(chosen)
        else:
            self.bt_symbol_var.set(chosen)

    def _sync_symbol_choice(self, menu_name: str, var, prefer: str | None = None, force: bool = False) -> None:
        """把某一个下拉刷成关注名单。force 为真时改成 prefer，否则已选中的还在就留着。"""
        from src.ui.backtest_fill import choose_backtest_symbol

        values = list(self._watch_symbols) or ["（还没有）"]
        chosen = choose_backtest_symbol(values, var.get(), prefer=prefer, force=force)
        menu = getattr(self, menu_name, None)
        if menu is not None:
            menu.configure(values=values)
            menu.set(chosen)
        else:
            var.set(chosen)

    def _sync_extra_symbol_menus(self, prefer: str | None = None, force: bool = False) -> None:
        """
        策略优化和复盘的品种下拉跟关注名单一起变。

        平时不改已经选中的合约。force 为真时改成 prefer，也就是配置页或回测页当前那一个。
        """
        self._sync_symbol_choice("opt_symbol_menu", self.opt_symbol_var, prefer=prefer, force=force)
        self._sync_symbol_choice("rv_symbol_menu", self.rv_symbol_var, prefer=prefer, force=force)
        self._sync_symbol_choice("rs_symbol_menu", self.rs_symbol_var, prefer=prefer, force=force)

    def _fill_review_from_config(self) -> None:
        """把配置页当前合约和周期抄到复盘页。不改复盘日期，也不写文件。"""
        symbol = self._config_symbol_now()
        self._sync_symbol_choice("rv_symbol_menu", self.rv_symbol_var, prefer=symbol, force=True)
        self._set_period_vars(self._period_code(self.tf_var, self.tf_custom_var), self.rv_tf_var, self.rv_tf_custom_var)
        self._set_period_vars(
            self._period_code(self.trail_var, self.trail_custom_var),
            self.rv_trail_var,
            self.rv_trail_custom_var,
        )
        shown = self.rv_symbol_var.get().strip() or "（还没有）"
        self._refresh_review_sources_hint()
        self._append_log(f"已按配置页填好复盘的品种和周期，品种 {shown}")

    def _research_symbols(self) -> list[str]:
        """这次扫描要看哪些合约：当前一个，或关注名单全部。"""
        if self.rs_scope_var.get().strip() == "只扫当前这个":
            one = (self.rs_symbol_var.get() or "").strip()
            if not one or one == "（还没有）":
                return []
            return [one]
        return [item for item in self._watch_symbols if item and item != "（还没有）"]

    def _refresh_research_picks(self) -> None:
        """按候选池文件画出勾选框。文件没有时给一句说明。"""
        frame = getattr(self, "rs_pick_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        from src.research.pool import load_pool

        pool = load_pool(self.root_dir)
        items = pool.get("items") or []
        self._rs_check_vars = {}
        if not items:
            ctk.CTkLabel(
                frame,
                text="还没有过关的规律。点上面「开始扫描规律」。",
                font=T.FONT_SMALL,
                text_color=T.TEXT_DIM,
                wraplength=320,
                justify="left",
            ).pack(anchor="w", padx=6, pady=8)
            return
        for item in items:
            key = str(item.get("id") or "")
            if not key:
                continue
            var = ctk.BooleanVar(value=bool(item.get("selected")))
            self._rs_check_vars[key] = var
            title = str(item.get("title") or key)
            horizon = item.get("horizon")
            label = f"{title} · 往后{horizon}根"
            ctk.CTkCheckBox(
                frame,
                text=label,
                variable=var,
                font=T.FONT_SMALL,
                text_color=T.TEXT,
            ).pack(anchor="w", padx=6, pady=3)

    def _on_save_research_picks(self) -> None:
        """只把勾选写回候选池，不启动扫描，也不下单。"""
        from tkinter import messagebox

        from src.research.pool import mark_selected

        chosen = {key for key, var in self._rs_check_vars.items() if var.get()}
        mark_selected(self.root_dir, chosen)
        self._append_log(f"已保存规律勾选 {len(chosen)} 条（仍不下单）")
        messagebox.showinfo("已保存勾选", "勾选已写入候选池。这不会改开平仓，也不会下单。")

    def _on_research_scan(self) -> None:
        """后台拉 K 线、扫规律、写入候选池。主线程只更新说明和勾选框。"""
        from tkinter import messagebox

        if self._rs_busy:
            messagebox.showinfo("正在扫描", "上一轮规律扫描还没结束。")
            return
        symbols = self._research_symbols()
        if not symbols:
            messagebox.showwarning("还没有品种", "请先在配置页的关注品种里添加合约。")
            return
        try:
            start, end = self._resolve_date_range(
                self.rs_range_var.get(), self.rs_start_var.get(), self.rs_end_var.get()
            )
        except ValueError as exc:
            messagebox.showerror("日期不对", str(exc))
            return
        if self.rs_range_var.get() == "全部可用" and (start or end):
            self.rs_range_var.set("自定义")
        period = self._period_code(self.rs_tf_var, self.rs_tf_custom_var)
        from src.data.quote_check import normalize_source

        source = normalize_source(self.rs_source_var.get())
        require_cross = bool(self.rs_cross_var.get())
        cfg = self._clone_cfg()
        cfg.setdefault("data", {})["source"] = source
        self._rs_busy = True
        self._set_result_text(self.rs_result_box, "正在扫描规律，请稍候。这不会下单。")
        self._append_log(f"开始扫描规律 品种={','.join(symbols)} 周期={period}")

        def _run() -> None:
            import pandas as pd

            from src.data import KlineProvider
            from src.data.periods import drop_forming_bar
            from src.research.pool import load_pool, save_pool
            from src.research.scan import format_scan_report, scan_regularities
            from src.research.styles import advise_styles, format_style_section

            try:
                skipped: list[str] = []
                bars_by_symbol: dict[str, Any] = {}
                counts: dict[str, int] = {}
                kp = KlineProvider(cfg)
                for symbol in symbols:
                    try:
                        df = kp.fetch(symbol, period, keep_history=True, start=start, end=end)
                        df = drop_forming_bar(df)
                        if df is None or len(df) == 0:
                            skipped.append(f"{symbol}：没有 K 线")
                            continue
                        if start:
                            df = df[df.index >= pd.Timestamp(start)]
                        if end:
                            df = df[df.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
                        if df is None or len(df) == 0:
                            skipped.append(f"{symbol}：这段日期没有 K 线")
                            continue
                        bars_by_symbol[symbol] = df
                        counts[symbol] = int(len(df))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("规律扫描拉行情失败 {}: {}", symbol, exc)
                        skipped.append(f"{symbol}：{exc}")
                previous = load_pool(self.root_dir).get("items") or []
                items = scan_regularities(
                    bars_by_symbol,
                    require_cross_symbol=require_cross,
                    previous=previous,
                )
                # 只给倾向，不改开平仓。分段不稳的条不会拿来投票。
                advice = advise_styles(items)
                path = save_pool(self.root_dir, items, extra={"style_advice": advice.to_dict()})
                text = format_scan_report(
                    items,
                    symbols=symbols,
                    bars=counts,
                    skipped=skipped,
                    style_text=format_style_section(advice),
                )
                text += f"\n候选池  {path}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("规律扫描失败")
                err = str(exc)

                def _fail() -> None:
                    self._rs_busy = False
                    self._set_result_text(self.rs_result_box, f"规律扫描失败:\n{err}")
                    self._append_log(f"规律扫描失败: {err}")

                self._post_to_main(_fail)
                return

            def _done() -> None:
                self._rs_busy = False
                self._set_result_text(self.rs_result_box, text)
                self._refresh_research_picks()
                self._append_log(f"规律扫描完成 过关{len(items)}条")

            self._post_to_main(_done)

        threading.Thread(target=_run, name="research-scan", daemon=True).start()

    def _refresh_review_sources_hint(self) -> None:
        """
        按复盘页当前品种刷新「推荐手动来源」小字。
        原理：自动仓单/快讯经常不全；换合约时换成该品种该看的网站清单。
        """
        label = getattr(self, "rv_sources_label", None)
        if label is None:
            return
        symbol = (self.rv_symbol_var.get() or "").strip()
        if not symbol or symbol == "（还没有）":
            label.configure(text="请先在配置页关注品种里选一个合约，再看推荐来源。")
            return
        from src.review.sources import sources_hint_lines

        label.configure(text="\n".join(sources_hint_lines(symbol)))

    def _on_watch_picked(self, choice: str) -> None:
        """下拉框换了当前合约。运行页顶部跟着换，列表里的其他合约还留着。"""
        if choice in ("", "（还没有）"):
            return
        self._set_watch_codes(self._watch_symbols, select=choice)

    def _on_watch_add(self) -> None:
        """把文本框里的品种加合约放进关注列表，并选中它。"""
        from tkinter import messagebox

        raw = self.watch_add_var.get().strip().replace(" ", "")
        if not raw:
            messagebox.showwarning("还没填写", "请填写品种加合约，例如 bu2612。")
            return
        letters = "".join(ch for ch in raw if ch.isalpha())
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not letters or not digits or letters + digits != raw:
            messagebox.showwarning("格式不对", "请填写品种加合约，例如 bu2612。不要只写品种，也不要写中文。")
            return
        if any(item.lower() == raw.lower() for item in self._watch_symbols):
            self._set_watch_codes(self._watch_symbols, select=raw)
            self.watch_add_var.set("")
            self._append_log(f"{raw} 已经在关注列表里，已把它选为当前品种，并写入配置")
            return
        self._set_watch_codes(self._watch_symbols + [raw], select=raw)
        self.watch_add_var.set("")
        self._append_log(f"已把 {raw} 加入关注列表，并选为当前品种。名单已经写入配置，重新打开还在。")

    def _on_watch_remove(self) -> None:
        """把下拉框里当前选中的合约从关注列表移出。"""
        from tkinter import messagebox

        current = self.watch_choice_var.get().strip()
        if not self._watch_symbols or current in ("", "（还没有）"):
            messagebox.showinfo("没有可移出的", "关注列表还是空的。")
            return
        if len(self._watch_symbols) == 1:
            messagebox.showinfo("先留一个", "至少保留一个关注品种，不然没有合约可看。")
            return
        kept = [item for item in self._watch_symbols if item.lower() != current.lower()]
        self._set_watch_codes(kept)
        self._append_log(f"已把 {current} 移出关注列表。名单已经写入配置。")

    def _on_export_cross(self) -> None:
        """把最近一轮交叉测试追加到统一 Excel 的「交叉测试」表，不盖掉以前的轮次。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_cross

        report = self._picks_path()
        if not report.exists():
            messagebox.showwarning("还不能导出", "请先点「开始交叉测试」，跑完后再导出。")
            return
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出交叉测试到统一 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, added, total = export_cross(self.root_dir, path)
        except ValueError as exc:
            messagebox.showwarning("还不能导出", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("导出交叉测试失败")
            messagebox.showerror("导出失败", str(exc))
            return
        self._clear_cross_results()
        messagebox.showinfo(
            "已导出",
            f"已追加到工作表「交叉测试」。\n这次新增 {added} 行，表里共 {total} 行。\n"
            "界面上的结果已清空，下次打开不会再画这些行。\n\n"
            f"文件：\n{out}",
        )
        self._append_log(f"交叉测试已导出到 {out}，新增 {added} 行，共 {total} 行。界面上的结果已清空")

    def _clear_cross_results(self) -> None:
        """
        导出成功后清掉交叉测试表。

        界面上的行删掉，保存的那份结果也删掉，下次打开不用再画。
        Excel 里的记录不动。这一轮如果还在测，进度提示先留着，测完仍会显示新结果。
        """
        from src.screen.picker import clear_saved_picks

        try:
            clear_saved_picks(self._picks_path())
        except OSError as exc:
            logger.warning("清掉交叉测试结果失败: {}", exc)
            return
        self._pick_baseline = None
        if self._pick_running:
            return
        self._render_pick_board(
            "结果已导出，界面上的表已清空。要再看，打开导出的 Excel。",
            [],
            empty_text="结果已导出，这里先空着。要再看，打开导出的 Excel。",
        )

    def _on_export_backtest(self) -> None:
        """把最近一次回测追加到统一 Excel 的「回测摘要」「回测成交」「回测信号」，不盖掉以前的行。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_backtest

        report = self.root_dir / "data" / "backtest_last.json"
        if not report.exists():
            messagebox.showwarning("还不能导出", "请先点「开始历史回测」，跑完后再导出。")
            return
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出历史回测到统一 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, summary_added, trade_added, summary_total, trade_total = export_backtest(self.root_dir, path)
        except ValueError as e:
            messagebox.showwarning("还不能导出", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("导出历史回测失败")
            messagebox.showerror("导出失败", str(e))
            return
        messagebox.showinfo(
            "已导出",
            f"已追加到三张表，原来的记录还在：\n"
            f"回测摘要 新增 {summary_added} 行，合计 {summary_total} 行\n"
            f"回测成交 新增 {trade_added} 行，合计 {trade_total} 行\n"
            f"回测信号 已追加本次没开成的信号和原因\n"
            f"第一列「回测时间」相同、而且内容也相同的一行不会重复写。\n"
            f"多因子还会在 data 里另存一份「多因子衰减监控-策略-时间.xlsx」，不覆盖以前的。\n\n"
            f"文件：\n{out}",
        )
        self._append_log(
            f"历史回测已追加到 {out} | 摘要新增{summary_added}合计{summary_total} | 成交新增{trade_added}合计{trade_total}"
        )

    def _on_plot_factor_decay(self) -> None:
        """按资料第二节的思路画衰减图：只读已有成交，不改开平仓。"""
        from tkinter import messagebox

        from src.export.factor_decay_plot import factor_summary, write_factor_decay_plot

        try:
            path, df = write_factor_decay_plot(self.root_dir)
        except ImportError as e:
            messagebox.showwarning("还不能画图", str(e))
            return
        except ValueError as e:
            messagebox.showwarning("还不能画图", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("画出因子衰减图失败")
            messagebox.showerror("画图失败", str(e))
            return
        lines = [f"已保存 {path}", f"一共 {len(df)} 笔带四因子分数的成交。"]
        summary = factor_summary(df)
        for row in summary.to_dict(orient="records"):
            lines.append(
                f"{row['因子']}：总盈亏 {row['总盈亏']:.2f}，有分摊 {row['交易次数']} 笔，"
                f"赚钱 {row['盈利次数']} 笔"
            )
        messagebox.showinfo("因子衰减图", "\n".join(lines))
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开衰减图 {}: {}", path, exc)
        self._append_log(f"已画出因子衰减图 {path} | {len(df)} 笔")

    def _open_local_file(self, path) -> bool:
        """用系统默认程序打开刚写好的文件。成功返回 True。"""
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开文件 {}: {}", path, exc)
            return False

    def _on_show_kline_inventory(self) -> None:
        """写出并直接打开 Excel 统计表，成功时不再弹提示框。"""
        from tkinter import messagebox

        from src.data.inventory import collect_kline_inventory, inventory_message, write_kline_inventory

        try:
            book = collect_kline_inventory(self.root_dir)
            path = write_kline_inventory(self.root_dir, book)
        except Exception as exc:  # noqa: BLE001
            logger.exception("查看本地K线库存失败")
            messagebox.showerror("打不开库存", str(exc))
            return
        opened = self._open_local_file(path)
        self._append_log(f"本地K线库存 {inventory_message(book)} | {path}")
        if not opened:
            messagebox.showerror(
                "打不开统计表",
                f"表已经写好，但系统没能打开 Excel。\n请自己打开：\n{path}",
            )

    def _on_purge_local_klines(self) -> None:
        """弹出删除窗口：可按数据源、合约、周期、时间段删，先数再删。"""
        from tkinter import messagebox

        from src.data.periods import PERIOD_MENU, canonical_period
        from src.data.quote_check import BACKTEST_SOURCE_LABELS, normalize_source
        from src.data.inventory import preview_purge, purge_klines

        dialog = ctk.CTkToplevel(self)
        dialog.title("删除本地K线")
        dialog.geometry("520x760")
        dialog.minsize(480, 640)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="某一家质量差时，只删这一家，再重新拉。不会点买卖。\n"
            "至少选一个数据源，或填一个合约。时间段留空表示这一组全部时间。\n"
            "若 CSV 正用 Excel 打开，请先关掉，否则清洗库删了还会被旧表补回来。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=420,
        ).pack(anchor="w", padx=16, pady=(16, 10))

        source_var = ctk.StringVar(value="不限")
        ctk.CTkLabel(dialog, text="数据源").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog, variable=source_var, values=["不限", *list(BACKTEST_SOURCE_LABELS)], width=240
        ).pack(anchor="w", padx=16, pady=(0, 8))

        ctk.CTkLabel(dialog, text="合约（如 fu2701，留空=该数据源全部合约）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 8))
        current = (self.bt_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)

        period_var = ctk.StringVar(value="不限")
        ctk.CTkLabel(dialog, text="周期").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog, variable=period_var, values=["不限", *[x for x in PERIOD_MENU if x != "自定义"]], width=240
        ).pack(anchor="w", padx=16, pady=(0, 8))

        from src.ui.datetime_pickers import DateTimeField

        ctk.CTkLabel(dialog, text="从（可空：日历选日期，时分可选）").pack(anchor="w", padx=16)
        from_field = DateTimeField(dialog)
        from_field.frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="到（可空）").pack(anchor="w", padx=16)
        to_field = DateTimeField(dialog)
        to_field.frame.pack(anchor="w", padx=16, pady=(0, 8))

        layer_var = ctk.StringVar(value="两份都删")
        ctk.CTkLabel(dialog, text="删哪一份").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog,
            variable=layer_var,
            values=["两份都删", "只删清洗后（开仓看的）", "只删接口原样"],
            width=260,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        hint = ctk.CTkLabel(dialog, text="", text_color=T.TEXT_DIM, wraplength=420, justify="left")
        hint.pack(anchor="w", padx=16, pady=(0, 8))

        def _args() -> dict:
            src_label = source_var.get().strip()
            source = None if src_label in {"", "不限"} else normalize_source(src_label)
            period_label = period_var.get().strip()
            period = None if period_label in {"", "不限"} else canonical_period(period_label)
            layer_label = layer_var.get()
            layers = "both"
            if "清洗" in layer_label:
                layers = "clean"
            elif "原样" in layer_label:
                layers = "raw"
            return {
                "source": source,
                "symbol": symbol_entry.get().strip() or None,
                "period": period,
                "ts_from": from_field.get() or None,
                "ts_to": to_field.get() or None,
                "layers": layers,
            }

        def _preview() -> None:
            try:
                seen = preview_purge(self.root_dir, **_args())
            except Exception as exc:  # noqa: BLE001
                hint.configure(text=str(exc))
                return
            hint.configure(
                text=f"将删除清洗后 {seen['clean_n']} 根、接口原样 {seen['raw_n']} 根。"
                "确认后不可恢复，请再点下面的删除。"
            )

        def _do_delete() -> None:
            try:
                seen = preview_purge(self.root_dir, **_args())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("还不能删", str(exc), parent=dialog)
                return
            if seen["clean_n"] + seen["raw_n"] <= 0:
                messagebox.showinfo("没有可删的", "按现在的条件，库里没有匹配的K线。", parent=dialog)
                return
            if not messagebox.askokcancel(
                "确认删除",
                f"将删除清洗后 {seen['clean_n']} 根、接口原样 {seen['raw_n']} 根。\n"
                "删完若要再用，请重新拉这一家。不可恢复。",
                parent=dialog,
            ):
                return
            try:
                result = purge_klines(self.root_dir, **_args())
            except Exception as exc:  # noqa: BLE001
                logger.exception("删除本地K线失败")
                messagebox.showerror("删除失败", str(exc), parent=dialog)
                return
            msg = (
                f"已删除清洗后 {result['clean_deleted']} 根、接口原样 {result['raw_deleted']} 根。"
            )
            locked = list(result.get("csv_locked") or [])
            if locked:
                files = "\n".join(locked)
                msg += (
                    "\n\n有 CSV 正被 Excel 打开，没能一起改掉。\n"
                    "请先关掉这些表，再点一次删除（否则下次读图会把旧 CSV 补回库里）：\n"
                    f"{files}"
                )
            self._append_log(f"删除本地K线 {msg.replace(chr(10), ' ')}")
            messagebox.showinfo("已删除" if not locked else "库已删，CSV 还被占用", msg, parent=dialog)
            if not locked:
                dialog.destroy()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))
        ctk.CTkButton(btn_row, text="先看要删多少", command=_preview, width=140).pack(side="left")
        ctk.CTkButton(btn_row, text="确认删除", command=_do_delete, width=140, fg_color="#8B2E2E").pack(
            side="right"
        )

    def _on_inspect_local_kline(self) -> None:
        """抽查某一根本地K线：选数据源、合约、周期、日期、时间，对照同花顺开高低收。"""
        from src.data.periods import PERIOD_MENU, canonical_period, period_label
        from src.data.quote_check import BACKTEST_SOURCE_LABELS, normalize_source
        from src.data.bar_inspect import format_kline_inspect, inspect_kline

        dialog = ctk.CTkToplevel(self)
        dialog.title("查看这根K线")
        dialog.geometry("520x800")
        dialog.minsize(480, 620)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="对照同花顺信息框时，只对开盘、最高、最低、收盘、成交量。\n"
            "今结、涨跌、持仓本地没有。时间是这根K线的结束时刻。不下单。",
            justify="left",
            text_color=T.TEXT_DIM,
            wraplength=480,
        ).pack(anchor="w", padx=16, pady=(16, 10))

        default_src = (self.bt_source_var.get() or "新浪").strip()
        if default_src not in BACKTEST_SOURCE_LABELS:
            default_src = "新浪"
        source_var = ctk.StringVar(value=default_src)
        ctk.CTkLabel(dialog, text="数据源").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(
            dialog, variable=source_var, values=list(BACKTEST_SOURCE_LABELS), width=240
        ).pack(anchor="w", padx=16, pady=(0, 8))

        ctk.CTkLabel(dialog, text="合约（如 fu2701）").pack(anchor="w", padx=16)
        symbol_entry = ctk.CTkEntry(dialog, height=32, placeholder_text="fu2701")
        symbol_entry.pack(fill="x", padx=16, pady=(0, 8))
        current = (self.bt_symbol_var.get() or "").strip()
        if current and current != "（还没有）":
            symbol_entry.insert(0, current)

        period_values = [x for x in PERIOD_MENU if x != "自定义"]
        try:
            default_period = period_label(
                self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
            )
        except Exception:
            default_period = "5分钟"
        if default_period not in period_values:
            default_period = period_values[0] if period_values else "5分钟"
        period_var = ctk.StringVar(value=default_period)
        ctk.CTkLabel(dialog, text="周期").pack(anchor="w", padx=16)
        ctk.CTkOptionMenu(dialog, variable=period_var, values=period_values, width=240).pack(
            anchor="w", padx=16, pady=(0, 8)
        )

        from src.ui.datetime_pickers import DateField, TimeField

        ctk.CTkLabel(dialog, text="日期").pack(anchor="w", padx=16)
        date_var = ctk.StringVar(value=date.today().isoformat())
        DateField(dialog, date_var, allow_empty=False).frame.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(dialog, text="时间（日线、周线可不选；分钟线要选到这根结束时刻）").pack(
            anchor="w", padx=16
        )
        time_var = ctk.StringVar(value="")
        TimeField(dialog, time_var, allow_empty=True).frame.pack(anchor="w", padx=16, pady=(0, 8))

        result = ctk.CTkTextbox(
            dialog, height=220, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, wrap="word"
        )
        result.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        result.insert("end", "选好日期和时间后点「查看」。不会改库，也不会下单。")
        last_paths: dict[str, str] = {}

        def _show(book: dict) -> None:
            last_paths.clear()
            last_paths.update(book.get("paths") or {})
            text = format_kline_inspect(book)
            result.delete("1.0", "end")
            result.insert("end", text)
            self._append_log(
                f"查看K线 {book.get('source_label')} {book.get('symbol')} "
                f"{book.get('period_label')} {book.get('when')}"
            )

        def _lookup() -> None:
            try:
                book = inspect_kline(
                    self.root_dir,
                    source=normalize_source(source_var.get()),
                    symbol=symbol_entry.get().strip(),
                    period=canonical_period(period_var.get()),
                    date_text=date_var.get(),
                    time_text=time_var.get(),
                )
            except ValueError as exc:
                result.delete("1.0", "end")
                result.insert("end", str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("查看这根K线失败")
                result.delete("1.0", "end")
                result.insert("end", f"查看失败：{exc}")
                return
            _show(book)

        def _open_csv() -> None:
            path = last_paths.get("csv") or ""
            if not path:
                _lookup()
            path = last_paths.get("csv") or ""
            if not path:
                return
            if not Path(path).exists():
                result.delete("1.0", "end")
                result.insert("end", f"还没有这份 CSV。\n{path}\n可改点「打开库存文件夹」。")
                return
            if not self._open_local_file(path):
                result.insert("end", f"\n系统没能打开：{path}")

        def _open_folder() -> None:
            folder = last_paths.get("folder") or str(self.root_dir / "data" / "klines")
            Path(folder).mkdir(parents=True, exist_ok=True)
            if not self._open_local_file(folder):
                result.insert("end", f"\n打不开文件夹：{folder}")

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text="查看", command=_lookup, width=100).pack(side="left")
        ctk.CTkButton(btn_row, text="打开对应CSV", command=_open_csv, width=120).pack(
            side="left", padx=(8, 0)
        )
        ctk.CTkButton(btn_row, text="打开库存文件夹", command=_open_folder, width=130).pack(
            side="right"
        )

    def _on_check_quote_quality(self) -> None:
        """
        读本地各家原样库和清洗库，写成 Excel 分析报表。

        只读 sqlite，不访问网络，也不下单。点一下才跑，不跟扫描绑在一起。
        入口只在历史回测页，用本页选的合约和周期。
        """
        from tkinter import messagebox

        from src.data.quality import inspect_quote_book, write_quality_report

        symbol = (self.bt_symbol_var.get() or "").strip()
        period = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        if not symbol:
            messagebox.showwarning("还不能检查", "请先选一个合约。")
            return
        try:
            report = inspect_quote_book(self.root_dir, symbol, period, cfg=self.cfg)
            path = write_quality_report(self.root_dir, report)
        except Exception as exc:  # noqa: BLE001
            logger.exception("检查行情质量失败")
            messagebox.showerror("检查失败", str(exc))
            return
        title = {"ok": "行情能用", "watch": "该看一眼", "empty": "还没有库存"}.get(
            report.status, "行情质量"
        )
        messagebox.showinfo(title, f"{report.message}\n\n已保存 {path}")
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开质量报告 {}: {}", path, exc)
        self._append_log(f"行情质量 {symbol} {report.status} | {path}")

    def _on_review(self) -> None:
        """后台生成收盘复盘，结果写在「复盘」页，并保存到 data/reviews。"""
        from datetime import date
        from tkinter import messagebox

        from src.review.replay import ENGINE_BY_LABEL, engine_from_label

        raw_day = self.rv_date_var.get().strip()
        try:
            trade_date = date.fromisoformat(raw_day)
        except ValueError:
            messagebox.showerror("日期不对", "请按 YYYY-MM-DD 填写，例如 2026-10-14。")
            return
        label = self.rv_engine_var.get().strip()
        if label not in ENGINE_BY_LABEL and engine_from_label(label) is None:
            messagebox.showerror("策略不对", "请重新选择「策略对照」。")
            return
        engine = ENGINE_BY_LABEL[label] if label in ENGINE_BY_LABEL else engine_from_label(label)
        symbol = self.rv_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加要复盘的合约。")
            return
        scope = self.rv_scope_var.get().strip() or "前夜盘+日盘"
        note = self.rv_note_box.get("1.0", "end").strip() if hasattr(self, "rv_note_box") else ""

        cfg = self._clone_cfg()
        st = cfg.setdefault("strategy", {})
        st["timeframe"] = self._period_code(self.rv_tf_var, self.rv_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.rv_trail_var, self.rv_trail_custom_var)
        st["symbols"] = [symbol]
        if engine:
            st["engine"] = engine

        self.tabs.set("复盘")
        self._set_result_text(
            self.rv_result_box,
            "正在生成复盘…\n需要拉取行情，如果还查库存和快讯，可能要半分钟。\n",
        )
        self._append_log(f"开始复盘 {symbol} {raw_day} {scope} | {label}")

        def _run() -> None:
            try:
                from src.review import build_review

                text = build_review(
                    cfg,
                    symbol=symbol,
                    trade_date=trade_date,
                    scope=scope,
                    engine=engine,
                    user_note=note,
                    root=self.root_dir,
                )
                # 尾部附上实盘滑点校准段：真实成交攒出来的滑点分布，回测该填几跳看它。
                try:
                    from src.account.slippage_log import summarize_text

                    text = f"{text}\n\n{summarize_text(self.root_dir)}"
                except Exception:  # noqa: BLE001
                    logger.debug("滑点汇总失败，已忽略", exc_info=True)
                self._post_to_main(lambda t=text: self._set_result_text(self.rv_result_box, t))
                self._post_to_main(lambda: self._append_log(f"复盘完成 {symbol} {raw_day}"))
            except Exception as e:  # noqa: BLE001
                logger.exception("复盘失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"复盘失败: {err}"))
                self._post_to_main(lambda m=err: self._set_result_text(self.rv_result_box, f"复盘失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("复盘失败", m))

        threading.Thread(target=_run, daemon=True).start()

    def _sync_account_mode(self) -> None:
        """同花顺账显示手改工具；CTP 账显示柜台资金表。两套界面不同时出现。"""
        from src.account.workspace import is_ctp_book, load_active_id

        ctp = is_ctp_book(load_active_id(self.root_dir))
        ths_inner = getattr(self, "ac_ths_panel", None)
        ctp_inner = getattr(self, "ac_ctp_panel", None)
        if ths_inner is None or ctp_inner is None:
            return
        ths_card = ths_inner.master
        ctp_card = ctp_inner.master
        refresh = getattr(self, "ac_common_refresh", None)
        if ctp:
            ths_card.pack_forget()
            if hasattr(self, "ac_ths_manual"):
                self.ac_ths_manual.pack_forget()
            if hasattr(self, "ac_ths_manual_note"):
                self.ac_ths_manual_note.pack_forget()
            if refresh is not None:
                ctp_card.pack(fill="x", pady=(0, 4), before=refresh)
            else:
                ctp_card.pack(fill="x", pady=(0, 4))
        else:
            ctp_card.pack_forget()
            if refresh is not None:
                ths_card.pack(fill="x", pady=(0, 4), before=refresh)
            else:
                ths_card.pack(fill="x", pady=(0, 4))
            if hasattr(self, "ac_ths_manual"):
                self.ac_ths_manual.pack(fill="x", padx=4, pady=(0, 4))
            if hasattr(self, "ac_ths_manual_note"):
                self.ac_ths_manual_note.pack(anchor="w", padx=4, pady=(0, 8))

    def _paint_ctp_fund_panel(self, book) -> None:
        """把已保存的柜台资金画到账户分析，字段和交易台资金页一致。"""
        from src.trader.ctp_display import account_pairs

        snap = (book or {}).get("ctp_snapshot") if isinstance(book, dict) else None
        if not isinstance(snap, dict):
            snap = {}
        user = str(snap.get("account_id") or snap.get("user_id") or "").strip() or "—"
        if hasattr(self, "ac_ctp_user_label"):
            self.ac_ctp_user_label.configure(text=f"资金账号  {user}")
        if hasattr(self, "ac_ctp_time_label"):
            stamp = str(snap.get("time") or "").strip()
            self.ac_ctp_time_label.configure(text=f"更新于 {stamp}" if stamp else "还没有查询记录")
        values = getattr(self, "_ac_fund_values", None)
        if not isinstance(values, dict) or not values:
            return
        for title, value in account_pairs(snap if snap else None):
            label = values.get(title)
            if label is not None:
                label.configure(text=value)

    def _paint_account_hint(self) -> None:
        """账户分析左边说明现在记的是哪一本，以及期货通该开哪边。"""
        from src.account.signal_book import load_book
        from src.account.workspace import display_label, profile_of

        info = profile_of(root=self.root_dir)
        shown_label = display_label(info["id"], self.root_dir)
        extra = ""
        book = load_book(self.root_dir)
        snap = (book or {}).get("ctp_snapshot") if isinstance(book, dict) else None
        if isinstance(snap, dict) and snap.get("time"):
            extra = (
                f"\n柜台 {snap.get('account_id') or snap.get('user_id') or ''} "
                f"动态权益 {float(snap.get('balance') or 0):.2f}，"
                f"可用 {float(snap.get('available') or 0):.2f}，"
                f"更新于 {snap.get('time')}。"
            )
        self._sync_account_mode()
        self._paint_ctp_fund_panel(book)
        hint = getattr(self, "ac_account_hint", None)
        if hint is not None:
            hint.configure(text=info["hint"] + extra)
        if hasattr(self, "ac_account_var"):
            shown = self.ac_account_var.get()
            if shown != shown_label:
                busy = self._account_switch_busy
                self._account_switch_busy = True
                try:
                    self.ac_account_var.set(shown_label)
                finally:
                    self._account_switch_busy = busy

    def _on_account_switch(self, choice: str) -> None:
        """
        换成另一本账。常驻必须先停，否则内存里的仓还是上一本的。

        只改当前记号。实盘账和模拟账的文件互不覆盖，也不会把成交抄到另一本。
        """
        from tkinter import messagebox

        from src.account.signal_book import load_book, multiplier_map, shown_equity
        from src.account.workspace import (
            display_label,
            id_from_label,
            is_ctp_book,
            load_active_id,
            profile_of,
            set_active_id,
        )

        if self._account_switch_busy:
            return
        wanted = id_from_label(choice)
        current = load_active_id(self.root_dir)
        if wanted == current:
            self._paint_account_hint()
            return
        if self.worker.running:
            messagebox.showwarning(
                "先停常驻",
                "换账会换资金和持仓记录。请先在运行页点「停止常驻」，再换到另一本账。",
            )
            self._account_switch_busy = True
            try:
                self.ac_account_var.set(display_label(current, self.root_dir))
            finally:
                self._account_switch_busy = False
            return
        target = profile_of(wanted)
        shown = display_label(wanted, self.root_dir)
        if not messagebox.askyesno(
            "换账",
            f"确定换成「{shown}」吗？\n\n"
            f"{target['hint']}\n\n"
            "另一本账的成交和持仓原样留着，不会被这本改掉。",
        ):
            self._account_switch_busy = True
            try:
                self.ac_account_var.set(display_label(current, self.root_dir))
            finally:
                self._account_switch_busy = False
            return
        set_active_id(self.root_dir, wanted)
        self._signal_book_mtime = 0.0
        self._paint_account_hint()
        self._refresh_signal_view()
        book = load_book(self.root_dir)
        if book is not None:
            self._apply_book_balance(shown_equity(book, multiplier_map(self.cfg)))
        self._refresh_status_cards()
        self._append_log(f"账户分析已换成 {shown}")
        if wanted == "paper":
            messagebox.showinfo(
                "同花顺模拟账",
                "程序这边已经换成模拟账。\n"
                "请到期货通左上角打开「模拟交易」，再点启动常驻。\n"
                "这样点单走模拟盘，不会动真实资金，也不会改实盘账。\n"
                "这本账的余额仍要手工记入金、出金或每日结算。",
            )
        elif is_ctp_book(wanted):
            messagebox.showinfo(
                shown,
                "这本账的权益以华泰柜台查询为准。\n"
                "请打开交易台，用对应账号登录后点「刷新持仓」，\n"
                "或在本页点「从柜台刷新资金」。\n"
                "同花顺登录号不要填进 CTP 资金账号。",
            )

    def _on_ctp_refresh_funds(self) -> None:
        """向已经登录的柜台再查一次资金，写入当前这本华泰账。"""
        from tkinter import messagebox

        from src.account.ctp_sync import can_pull_from_trader, push_gateway_account
        from src.account.workspace import is_ctp_book, load_active_id

        aid = load_active_id(self.root_dir)
        if not is_ctp_book(aid):
            messagebox.showinfo(
                "这本账不接柜台",
                "同花顺模拟账仍要手工改余额。\n"
                "同花顺实盘账仍是程序开平仓加手工结算。\n"
                "请换到「华泰仿真」或「华泰真实账户」再刷新。",
            )
            return
        trader = None
        probe = getattr(self, "_ctp_probe", None)
        if probe is not None and getattr(probe, "winfo_exists", lambda: False)() and getattr(probe, "trader", None):
            trader = probe.trader
        worker = getattr(self, "worker", None)
        bot = getattr(worker, "bot", None) if worker is not None else None
        if trader is None and bot is not None:
            trader = getattr(bot, "trader", None)
        if not can_pull_from_trader(trader, aid):
            messagebox.showwarning(
                "还没连上对应柜台",
                "请先打开交易台，用这一本账对应的账号登录，再点「刷新持仓」或本按钮。",
            )
            return

        def work() -> None:
            ok, text = trader.fetch_account_snapshot()
            self._post_to_main(lambda: self._append_log(text if ok else ("查询失败：" + text)))
            self._post_to_main(self._paint_account_hint)
            self._post_to_main(self._refresh_signal_view)

        threading.Thread(target=work, daemon=True).start()
        self._append_log("正在向柜台查询资金。")

    def _signal_book_or_new(self):
        """读信号账户。还没有就开始一本，本金用左边的初始资金，没有就用配置里的账户权益。"""
        from src.account.signal_book import load_book, new_book
        from src.version import read_version

        book = load_book(self.root_dir)
        if book is not None:
            return book
        return new_book(self._signal_initial_amount(), version=read_version())

    def _signal_initial_amount(self) -> float:
        """初始资金输入框。空着时用配置页的账户权益。"""
        text = self.ac_capital_var.get().strip()
        if not text:
            text = self.capital_var.get().strip()
        amount = float(text)
        if amount < 0:
            raise ValueError("初始资金不能是负数")
        return amount

    def _signal_positive_amount(self, raw: str, label: str) -> float:
        """入金、出金、结算金额。必须是数，入金和出金还要大于 0。"""
        amount = float(str(raw).strip())
        if label != "实际余额" and amount <= 0:
            raise ValueError(f"{label}要大于 0")
        return amount

    def _commit_signal_book(self, book, note: str) -> None:
        """保存账本，并把新余额写进配置和正在跑的程序。"""
        from src.account.signal_book import save_manual, write_capital_to_config

        balance = save_manual(self.root_dir, book)
        write_capital_to_config(balance)
        self._apply_book_balance(balance)
        self._refresh_signal_view()
        self._append_log(note)

    def _apply_book_balance(self, balance: float) -> None:
        """
        让配置页上的账户权益显示这笔余额。

        配置页不能手改这个数。正在跑的程序下一笔按这个余额算手数。
        常驻已经连上 CTP 时，手数仍跟期货公司数字，不用账本里估的成交盈亏去改。
        """
        shown = str(int(balance)) if float(balance) == int(balance) else f"{float(balance):.2f}"
        self.capital_var.set(shown)
        self.cfg.setdefault("risk", {})["capital"] = float(balance)
        bot = getattr(self.worker, "bot", None)
        if bot is None:
            return
        raw = getattr(getattr(bot, "trader", None), "last_broker_equity", None)
        try:
            broker = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            broker = 0.0
        if broker > 0:
            bot.sizer.capital = broker
            bot.cfg.setdefault("risk", {})["capital"] = broker
            shown = str(int(broker)) if float(broker) == int(broker) else f"{float(broker):.2f}"
            self.capital_var.set(shown)
            self.cfg.setdefault("risk", {})["capital"] = broker
            return
        bot.sizer.capital = float(balance)
        bot.cfg.setdefault("risk", {})["capital"] = float(balance)

    def _on_signal_initial(self) -> None:
        """
        记下初始资金，并把它当作新账期的起点。

        原来的账先整份存进 data/signal_periods，再另建一本新账。
        存档没成功就不会清掉当前账。同花顺里的持仓不动。
        """
        from tkinter import messagebox

        from src.account.signal_book import load_book, start_new_period
        from src.version import read_version

        try:
            amount = self._signal_initial_amount()
        except ValueError:
            messagebox.showerror("还不能记", "初始资金请填一个大于等于 0 的数。")
            return
        if load_book(self.root_dir) is not None:
            if not messagebox.askyesno(
                "开始新账期",
                "记下初始资金会先把现在这本账整份存档，再另建一本新账。\n\n"
                "存档在 data/signal_periods，里面仍有原来的交易、入金、出金、结算和盈亏。\n"
                "新账期用新的存档，从现在按时间重新统计。\n"
                f"余额会改成 {amount:g} 元，配置页的账户权益一起改。\n\n"
                "同花顺里的真实持仓不会被平掉。\n\n确定开始？",
            ):
                return
        try:
            book, archived = start_new_period(self.root_dir, amount, version=read_version())
        except OSError as exc:
            messagebox.showerror("没能存档", f"上一本账还没备份成功，所以没有开始新账期。\n{exc}")
            return
        shown = str(int(amount)) if amount == int(amount) else f"{amount:.2f}"
        self.ac_capital_var.set(shown)
        note = f"已开始新账期，初始资金 {amount:g} 元。新存档 data/signal_periods/{book.get('period_file')}"
        if archived is not None:
            note += f"。上一账期已存档 {archived.name}"
        self._commit_signal_book(book, note)

    def _on_signal_deposit(self) -> None:
        """按你填的正数记一笔入金。"""
        self._on_signal_cash("deposit", "入金")

    def _on_signal_withdraw(self) -> None:
        """按你填的正数记一笔出金。余额不够时先问一句。"""
        self._on_signal_cash("withdraw", "出金")

    def _on_signal_cash(self, kind: str, label: str) -> None:
        """入金和出金共用。账本还没有就先用当前初始资金建一本。"""
        from tkinter import messagebox

        from src.account.signal_book import add_cash, balance_of
        from src.account.workspace import is_ctp_book, load_active_id

        if is_ctp_book(load_active_id(self.root_dir)):
            messagebox.showinfo("这本账用柜台资金", "华泰仿真和华泰真实的余额请点「从柜台刷新资金」，不要手工记入金出金。")
            return

        try:
            amount = self._signal_positive_amount(self.ac_cash_var.get(), label)
            book = self._signal_book_or_new()
            if kind == "withdraw" and amount > balance_of(book) + 0.001:
                if not messagebox.askyesno("出金", "这笔出金比当前余额还大，余额会变成负数。仍要记吗？"):
                    return
            add_cash(book, kind, amount)
        except ValueError:
            messagebox.showerror("还不能记", f"{label}请填一个大于 0 的数。")
            return
        self.ac_cash_var.set("")
        self._commit_signal_book(book, f"信号账户已记{label} {amount:g} 元")

    def _on_signal_settle(self) -> None:
        """每日结算：差额单独记一笔，开平仓价格不动。"""
        from tkinter import messagebox

        from src.account.signal_book import settle_to
        from src.account.workspace import is_ctp_book, load_active_id

        if is_ctp_book(load_active_id(self.root_dir)):
            messagebox.showinfo("这本账用柜台资金", "华泰仿真和华泰真实请点「从柜台刷新资金」，不要用每日结算手改。")
            return

        try:
            target = float(self.ac_settle_var.get().strip())
        except ValueError:
            messagebox.showerror("还不能结算", "实际余额请填一个数。")
            return
        book = self._signal_book_or_new()
        delta = settle_to(book, target)
        if abs(delta) < 0.005:
            messagebox.showinfo("不用调整", "当前余额已经和你填的数一样。")
            self._refresh_signal_view()
            return
        self.ac_settle_var.set("")
        self._commit_signal_book(book, f"每日结算已把余额调到 {target:g} 元，差额 {delta:g} 元")

    def _account_action_row(self, parent, label: str, var, buttons: list[tuple[str, Any, bool]]) -> None:
        """账户分析里的一行：名称、金额框、按钮并排，不再各占一排。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(
            row, text=label, font=T.FONT_SMALL, text_color=T.TEXT_DIM, width=64, anchor="w"
        ).pack(side="left")
        ctk.CTkEntry(
            row,
            textvariable=var,
            width=96,
            height=30,
            fg_color=T.BG,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left", padx=(0, 6))
        for text, command, primary in buttons:
            ctk.CTkButton(
                row,
                text=text,
                width=72,
                height=30,
                fg_color=T.ACCENT if primary else T.BG,
                hover_color=T.ACCENT_HOVER if primary else T.BORDER,
                text_color=T.ACCENT_ON if primary else T.TEXT,
                command=command,
            ).pack(side="left", padx=(0, 4))

    def _account_stat_card(self, parent, title: str) -> ctk.CTkLabel:
        """右边四个汇总数字中的一个。数字以后由刷新填上。"""
        card = ctk.CTkFrame(parent, fg_color=T.BG, corner_radius=8)
        card.pack(side="left", expand=True, fill="x", padx=3)
        ctk.CTkLabel(card, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(anchor="w", padx=8, pady=(6, 0))
        value = ctk.CTkLabel(card, text="—", font=T.FONT_TITLE, text_color=T.TEXT)
        value.pack(anchor="w", padx=8, pady=(0, 6))
        return value

    def _on_account_span(self, name: str) -> None:
        """换时间段后，胜率、回撤、品种和权益图都按这一段重算。"""
        self.ac_span_var.set(name)
        for key, button in self._ac_span_buttons.items():
            selected = key == name
            button.configure(
                fg_color=T.ACCENT if selected else T.BG,
                text_color=T.ACCENT_ON if selected else T.TEXT,
            )
        self._refresh_signal_view()

    def _on_account_chart(self, name: str) -> None:
        """权益看余额曲线，每日盈亏看每天赚亏的柱子。"""
        self._ac_chart = name
        for key, button in self._ac_chart_buttons.items():
            selected = key == name
            button.configure(
                fg_color=T.ACCENT if selected else T.BG,
                text_color=T.ACCENT_ON if selected else T.TEXT,
            )
        self._draw_account_chart()

    def _shift_account_month(self, step: int) -> None:
        """盈亏日历翻到上一个月或下一个月。"""
        year = self._ac_cal.year
        month = self._ac_cal.month + step
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        self._ac_cal = self._ac_cal.replace(year=year, month=month, day=1)
        self._draw_account_calendar()

    def _account_margin_settings(self) -> tuple[dict[str, float], float]:
        """读配置里的每手保证金。品种单独写了就用品种的，没有就用默认值。"""
        risk = self.cfg.get("risk") or {}
        try:
            default_margin = float(risk.get("margin_per_lot") or 0)
        except (TypeError, ValueError):
            default_margin = 0.0
        margins: dict[str, float] = {}
        for key, info in (risk.get("contracts") or {}).items():
            if not isinstance(info, dict):
                continue
            try:
                margins[str(key).lower()] = float(info.get("margin") or 0)
            except (TypeError, ValueError):
                continue
        return margins, default_margin

    def _paint_account_stats(self, book) -> None:
        """按当前时间段重算右边的数字、图、品种和日历。"""
        from src.account.signal_stats import (
            analyze_book,
            format_factor,
            format_utilization,
            format_win_rate,
            span_bounds,
        )

        if not hasattr(self, "ac_win_label"):
            return
        start, end = span_bounds(self.ac_span_var.get() or "全部", futures_trading_day())
        margins, default_margin = self._account_margin_settings()
        report = analyze_book(
            book or {"trades": [], "changes": []},
            start=start,
            end=end,
            margins=margins,
            default_margin=default_margin,
        )
        self._ac_report = report
        if book and str(book.get("funding") or "") == "ctp":
            from src.account.signal_book import ctp_history_equity

            series = []
            for stamp, value in ctp_history_equity(book):
                try:
                    moment = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    continue
                if start is not None and moment.date() < start:
                    continue
                if moment.date() > end:
                    continue
                series.append((moment, value))
            if series:
                report.equity = series
                peak = series[0][1]
                max_dd = 0.0
                for _moment, point in series:
                    peak = max(peak, point)
                    max_dd = max(max_dd, peak - point)
                report.account.max_drawdown = max_dd
                snap = book.get("ctp_snapshot") if isinstance(book.get("ctp_snapshot"), dict) else {}
                try:
                    usage = float(snap.get("usage") or 0)
                except (TypeError, ValueError):
                    usage = 0.0
                if usage > 0:
                    report.account.utilization = usage
        stat = report.account
        self.ac_win_label.configure(text=format_win_rate(stat.win_rate))
        self.ac_factor_label.configure(text=format_factor(stat.profit_factor))
        self.ac_dd_label.configure(text=f"{stat.max_drawdown:.2f}")
        self.ac_use_label.configure(text=format_utilization(stat.utilization))
        if report.symbols:
            lines = []
            for item in report.symbols:
                one = item.stat
                lines.append(
                    f"{item.symbol}   {one.closed}笔   胜率 {format_win_rate(one.win_rate)}"
                    f"   盈利因子 {format_factor(one.profit_factor)}"
                    f"   盈亏 {one.net_pnl:+.2f}"
                    f"   回撤 {one.max_drawdown:.2f}"
                    f"   使用率 {format_utilization(one.utilization)}"
                )
            self.ac_symbol_label.configure(text="\n".join(lines), text_color=T.TEXT)
        else:
            self.ac_symbol_label.configure(text="这段时间还没有平仓。", text_color=T.TEXT_DIM)
        self._draw_account_chart()
        self._draw_account_calendar()

    def _draw_account_chart(self) -> None:
        """权益画成一条线，每日盈亏画成柱子。红是赚，绿是亏。"""
        canvas = getattr(self, "ac_chart", None)
        report = self._ac_report
        if canvas is None or report is None:
            return
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 520)
        height = 150
        canvas.delete("all")
        if self._ac_chart == "每日盈亏":
            points = sorted(report.daily_pnl.items())
            if self.ac_span_var.get() != "全部":
                from src.account.signal_stats import span_bounds

                start, end = span_bounds(self.ac_span_var.get(), futures_trading_day())
                points = [(day, value) for day, value in points if (start is None or day >= start) and day <= end]
            self._draw_pnl_bars(canvas, width, height, points)
            return
        self._draw_equity_line(canvas, width, height, report.equity)

    def _draw_equity_line(self, canvas, width: int, height: int, points) -> None:
        """把余额按时间连成一条线。"""
        if len(points) < 1:
            canvas.create_text(16, height / 2, anchor="w", fill=T.TEXT_DIM, text="还没有资金变动")
            return
        values = [value for _moment, value in points]
        # 标签用真实高低。线太平时只把画布上下撑开 1 元，避免一个点贴边，不改显示的数字。
        shown_low = min(values)
        shown_high = max(values)
        low = shown_low
        high = shown_high
        if high - low < 0.01:
            low -= 1
            high += 1
        pad = 16
        usable_w = max(width - pad * 2, 10)
        usable_h = height - pad * 2

        def xy(index: int, value: float) -> tuple[float, float]:
            x = pad + usable_w * (index / max(len(points) - 1, 1))
            y = pad + usable_h * (1 - (value - low) / (high - low))
            return x, y

        coords = []
        for index, (_moment, value) in enumerate(points):
            x, y = xy(index, value)
            coords.extend((x, y))
        if len(coords) >= 4:
            canvas.create_line(*coords, fill=T.ACCENT, width=2)
        else:
            canvas.create_oval(coords[0] - 3, coords[1] - 3, coords[0] + 3, coords[1] + 3, fill=T.ACCENT, outline="")
        canvas.create_text(pad, 8, anchor="nw", fill=T.TEXT_DIM, text=f"高 {shown_high:.2f}")
        canvas.create_text(pad, height - 8, anchor="sw", fill=T.TEXT_DIM, text=f"低 {shown_low:.2f}    现 {values[-1]:.2f}")

    def _draw_pnl_bars(self, canvas, width: int, height: int, points) -> None:
        """每一天一根柱子。没有平仓的日子不画。"""
        if not points:
            canvas.create_text(16, height / 2, anchor="w", fill=T.TEXT_DIM, text="这段时间还没有平仓盈亏")
            return
        values = [value for _day, value in points]
        span = max(max(abs(value) for value in values), 1)
        pad = 16
        mid = height / 2
        gap = max((width - pad * 2) / len(points), 2)
        for index, (day, value) in enumerate(points):
            x = pad + index * gap
            bar_h = (abs(value) / span) * (mid - 18)
            color = "#E85D4C" if value > 0 else T.OK
            if value >= 0:
                canvas.create_rectangle(x, mid - bar_h, x + max(gap - 2, 1), mid, fill=color, outline="")
            else:
                canvas.create_rectangle(x, mid, x + max(gap - 2, 1), mid + bar_h, fill=color, outline="")
            if len(points) <= 12:
                canvas.create_text(x, height - 8, anchor="n", fill=T.TEXT_DIM, text=f"{day.month}/{day.day}", font=T.FONT_SMALL)
        canvas.create_text(pad, 8, anchor="nw", fill=T.TEXT_DIM, text="红赚 绿亏")

    def _draw_account_calendar(self) -> None:
        """按月画出每天的盈亏。没有平仓的日子只显示日期。有盈亏时，金额写在日期右边同一行。"""
        import calendar
        import tkinter.font as tkfont
        from datetime import date as today_date

        canvas = getattr(self, "ac_calendar", None)
        report = self._ac_report
        if canvas is None or report is None:
            return
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 520)
        shown = self._ac_cal
        self.ac_cal_label.configure(text=f"{shown.year}年{shown.month}月")
        month_total = 0.0
        for day, value in report.daily_pnl.items():
            if day.year == shown.year and day.month == shown.month:
                month_total = round(month_total + value, 2)
        color = "#E85D4C" if month_total > 0 else (T.OK if month_total < 0 else T.TEXT_DIM)
        self.ac_month_pnl.configure(text=f"本月盈亏 {month_total:+.2f}", text_color=color)
        names = ["日", "一", "二", "三", "四", "五", "六"]
        cell_w = width / 7
        # 一行放下日期和盈亏。格子加高一点，字不会贴住上下边。
        cell_h = 32
        header_h = 22
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(shown.year, shown.month)
        height = header_h + len(weeks) * cell_h + 8
        canvas.configure(height=height)
        canvas.delete("all")
        for index, name in enumerate(names):
            canvas.create_text(index * cell_w + cell_w / 2, 10, text=name, fill=T.TEXT_DIM)
        # 日期用稍大的字，盈亏紧跟在右边，中间留一截空隙，避免和日期粘成一团。
        day_font = tkfont.Font(family="Segoe UI", size=11)
        pnl_font = tkfont.Font(family="Segoe UI", size=10)
        date_pnl_gap = 8
        for row, week in enumerate(weeks):
            for col, day_number in enumerate(week):
                if day_number == 0:
                    continue
                day = today_date(shown.year, shown.month, day_number)
                value = report.daily_pnl.get(day)
                # 左右各留一点空白，色块不会贴到旁边那一天的数字上。
                x0 = col * cell_w + 4
                y0 = header_h + row * cell_h
                x1 = (col + 1) * cell_w - 4
                y1 = y0 + cell_h - 6
                mid_y = (y0 + y1) / 2
                if value is None:
                    canvas.create_text((x0 + x1) / 2, mid_y, text=str(day_number), fill=T.TEXT_DIM, font=day_font)
                    continue
                fill = "#5A2A2A" if value > 0 else ("#1E4A38" if value < 0 else T.BG_ELEVATED)
                canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")
                day_text = str(day_number)
                pnl_text = f"{value:+.0f}"
                day_w = day_font.measure(day_text)
                use_font = pnl_font
                pnl_w = use_font.measure(pnl_text)
                gap = date_pnl_gap
                side_pad = 8
                room = (x1 - x0) - side_pad * 2
                # 金额特别长时把字缩小，仍然留在这一格里，不写到后一天上。
                if day_w + gap + pnl_w > room:
                    use_font = tkfont.Font(family="Segoe UI", size=8)
                    pnl_w = use_font.measure(pnl_text)
                    gap = 5
                group_w = day_w + gap + pnl_w
                start = x0 + max(side_pad, ((x1 - x0) - group_w) / 2)
                if start + group_w > x1 - side_pad:
                    start = max(x0 + 2, x1 - side_pad - group_w)
                canvas.create_text(start, mid_y, anchor="w", text=day_text, fill=T.TEXT, font=day_font)
                pnl_color = "#E85D4C" if value > 0 else T.OK
                canvas.create_text(start + day_w + gap, mid_y, anchor="w", text=pnl_text, fill=pnl_color, font=use_font)

    def _refresh_signal_view(self) -> None:
        """右边重画成绩。账本还没有时说明从这一版才开始记，不把旧的手工账显示出来。"""
        from src.account.signal_book import multiplier_map
        from src.account.signal_book import book_path, describe, load_book, shown_equity

        if not hasattr(self, "ac_result_box"):
            return
        from src.account.workspace import profile_of

        info = profile_of(root=self.root_dir)
        self._paint_account_hint()
        book = load_book(self.root_dir)
        if book is None:
            if hasattr(self, "ac_balance_label"):
                self.ac_balance_label.configure(text="当前余额  还没开始")
            self._paint_account_stats(None)
            self._paint_ctp_fund_panel(None)
            self._sync_account_mode()
            from src.account.workspace import is_ctp_book

            if is_ctp_book(info["id"]):
                empty = (
                    f"「{info['label']}」还没有期货公司资金回报。\n"
                    "请先打开交易台，用这一本账对应的账号登录，点「刷新持仓」或左边「从柜台刷新资金」。\n"
                    "查到的账号、权益、可用资金会原样存到本地，不能手改。\n"
                    "同花顺点单请换到同花顺实盘账或同花顺模拟账。"
                )
            else:
                empty = (
                    f"「{info['label']}」还没有开始记账。\n"
                    "程序按信号真正开仓或平仓后会自动记在这里。\n"
                    "也可以先点「初始化」。以前手工记的成交不带过来。\n"
                    "另一本账的记录不会出现在这里。\n"
                    "运行页的「模拟（不点单）」只看信号、不点击，那种单也不会记进这本账。"
                )
            self._set_result_text(self.ac_result_box, empty)
            return
        path = book_path(self.root_dir)
        try:
            self._signal_book_mtime = path.stat().st_mtime
        except OSError:
            self._signal_book_mtime = 0.0
        balance = shown_equity(book, multiplier_map(self.cfg))
        shown = str(int(balance)) if float(balance) == int(balance) else f"{float(balance):.2f}"
        if hasattr(self, "ac_balance_label"):
            self.ac_balance_label.configure(text=f"当前余额  {shown} 元")
        if not self.ac_capital_var.get().strip():
            initial = float(book.get("initial_capital") or 0)
            self.ac_capital_var.set(str(int(initial)) if initial == int(initial) else f"{initial:.2f}")
        if str(book.get("funding") or "") == "ctp":
            from src.account.signal_book import describe_ctp_funds

            self._set_result_text(self.ac_result_box, describe_ctp_funds(book))
        else:
            self._set_result_text(self.ac_result_box, describe(book, multiplier_map(self.cfg)))
        self._paint_ctp_fund_panel(book)
        self._sync_account_mode()
        self._paint_account_stats(book)

    def _pull_signal_balance(self) -> None:
        """
        常驻做完一笔后，把配置页的账户权益改成新余额。

        这个数字不能在配置页手改，所以余额一变就更新显示。
        账本文件没变时，不反复刷账户分析右边的文字。
        """
        from src.account.signal_book import multiplier_map
        from src.account.signal_book import book_path, load_book, shown_equity

        path = book_path(self.root_dir)
        if not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        # 账本文件没变，余额和右边文字都不会变，不必每 800ms 全量读一遍 JSON。
        if mtime == self._signal_book_mtime:
            return
        book = load_book(self.root_dir)
        if book is None:
            return
        balance = shown_equity(book, multiplier_map(self.cfg))
        try:
            shown = float(self.capital_var.get())
        except (TypeError, ValueError):
            shown = None
        if shown is None or abs(balance - shown) > 0.01:
            self._apply_book_balance(balance)
        self._refresh_signal_view()

    def _on_export_account_part(self, part: str) -> None:
        """只导出账户分析的一张表。交易记录是开平仓，资金变动记录是五种余额变动。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_account_part

        title = "导出交易记录" if part == "trades" else "导出资金变动记录"
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title=title,
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, sheet, rows = export_account_part(self.root_dir, path, part)
        except Exception as exc:  # noqa: BLE001
            logger.exception("导出账户分析失败")
            messagebox.showerror("导出失败", str(exc))
            return
        messagebox.showinfo("已导出", f"已写入工作表「{sheet}」，{rows} 行。\n\n文件：\n{out}")
        self._append_log(f"{title}已写入 {out} 的「{sheet}」")

    def _on_export_all(self) -> None:
        """把优化参数、交易记录、资金变动记录、复盘、最近回测、交叉测试和最近优化写进同一个 Excel 的不同表。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_all

        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出全部到一个 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return
        try:
            out, counts = export_all(self.root_dir, path)
        except Exception as e:  # noqa: BLE001
            logger.exception("导出全部失败")
            messagebox.showerror("导出失败", str(e))
            return
        lines = [f"{name}：{n} 行" for name, n in counts.items()]
        messagebox.showinfo(
            "已导出",
            "各功能已写入不同的工作表：\n\n"
            + "\n".join(lines)
            + "\n\n某一表如果只有一句「还没有…」，表示这项还没做过。\n"
            + "界面上的交叉测试结果已清空。\n\n"
            + f"文件：\n{out}",
        )
        self._clear_cross_results()
        self._append_log(f"已导出全部到 {out}。界面上的交叉测试结果已清空")

    def _sync_opt_write_button(self) -> None:
        """对照全面更好时，写入按钮改成切实盘；否则仍是原来的写入。"""
        btn = getattr(self, "opt_write_btn", None)
        if btn is None:
            return
        if getattr(self, "_last_evolve_better", False):
            if hasattr(self, "opt_save_preset_var"):
                self.opt_save_preset_var.set(True)
            btn.configure(
                text="写入并切实盘（先存档）",
                fg_color=T.ACCENT,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON,
                border_width=0,
            )
            return
        btn.configure(
            text="把所选参数写入配置",
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            text_color=T.ACCENT,
            border_width=1,
            border_color=T.ACCENT,
        )

    def _on_optimize(self, evolve_only: bool = False) -> None:
        """后台网格优化：对选定策略/时间段反复回测打分。

        打开「对照正在用的参数」时，搜完再和现在这套比，默认不写配置。
        """
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning("请先停止", "策略优化较耗资源，请先停止常驻扫描。")
            return
        evolve_only = bool(evolve_only) or bool(self.opt_compare_var.get())
        self._last_evolve_better = False
        self._last_evolve_reason = ""
        self._last_opt_compared = False
        self._sync_opt_write_button()

        symbol = self.opt_symbol_var.get().strip()
        if not symbol or symbol == "（还没有）":
            messagebox.showwarning("还没有合约", "请先到配置页的关注品种里添加要优化的合约。")
            return
        # 开仓限制、保证金、收盘清仓跟实盘一致（与历史回测同一套对齐逻辑）。
        self._align_backtest_trading_params_with_live()
        cfg = self._clone_cfg()
        try:
            self._apply_backtest_limits(cfg, symbol=symbol)
        except ValueError as e:
            messagebox.showerror("数字不对", str(e))
            return
        st = cfg.setdefault("strategy", {})
        engine = self.opt_engine_var.get()
        st["engine"] = engine
        from src.data.quote_check import normalize_source

        cfg.setdefault("data", {})["source"] = normalize_source(self.opt_source_var.get())
        st["timeframe"] = self._period_code(self.opt_tf_var, self.opt_tf_custom_var)
        st["trail_timeframe"] = self._period_code(self.opt_trail_var, self.opt_trail_custom_var)
        st["symbols"] = [symbol]
        self._apply_backtest_switches(cfg)
        try:
            preset = self.opt_range_var.get()
            # 和历史回测同一层：日期填错时走下面的 except，不会在启动后点优化才崩。
            start, end = self._resolve_date_range(
                preset, self.opt_start_var.get(), self.opt_end_var.get()
            )
        except ValueError as e:
            messagebox.showerror("日期不对", str(e))
            return
        if preset == "全部可用" and (start or end):
            self.opt_range_var.set("自定义")
        if start:
            self.opt_start_var.set(start)
        if end:
            self.opt_end_var.set(end)
        try:
            max_trials = max(5, int(float(self.opt_trials_var.get() or 80)))
        except ValueError:
            max_trials = 80
        try:
            holdout_pct = float(self.opt_holdout_pct_var.get() or 30)
        except ValueError:
            holdout_pct = 30.0
        try:
            walk_steps = max(1, int(float(self.opt_walk_steps_var.get() or 3)))
        except ValueError:
            walk_steps = 3
        depth = self.opt_depth_var.get().strip() or "标准"
        mode = self.opt_mode_var.get().strip() or "一次切断"
        from src.backtest.optimizer import describe_run_switches

        switch_text = describe_run_switches(
            engine,
            flatten_at_close=bool(self.bt_flatten_var.get()),
        )
        from src.backtest.engine import FillModel

        fill_text = FillModel.from_cfg(cfg, symbol).describe()

        risk_now = cfg.get("risk") or {}
        trade_now = cfg.get("trade") or {}
        # 先记下这次优化用的限制。最优一组跑完后，用那次回测的快照覆盖，里面带跳过次数。
        limit_snap = {
            "apply_limits": bool(risk_now.get("apply_limits", True)),
            "sizing_mode": risk_now.get("sizing_mode"),
            "capital": risk_now.get("capital"),
            "risk_per_trade_pct": risk_now.get("risk_per_trade_pct"),
            "fixed_volume": trade_now.get("volume"),
            "min_volume": risk_now.get("min_volume"),
            "max_daily_loss": risk_now.get("max_daily_loss"),
            "min_order_interval_sec": risk_now.get("min_order_interval_sec"),
            "max_orders_per_day": risk_now.get("max_orders_per_day"),
            "margin_per_lot": risk_now.get("margin_per_lot", 0),
            "skipped_opens": {},
        }
        cross_symbols = list(self._watch_symbols)
        job_name = "模拟重跑优化（只出报告）" if evolve_only else "开始策略优化"
        self._append_log(
            f"{job_name}… engine={engine} depth={depth} mode={mode} "
            f"检验比例={holdout_pct}% symbol={symbol} "
            f"行情={self.opt_source_var.get()} "
            f"限制={'开' if limit_snap['apply_limits'] else '关'} "
            f"trials<={max_trials} 区间={start or '起'}~{end or '止'}"
        )
        self.tabs.set("策略优化")
        self._opt_started_at = time.monotonic()
        self._opt_progress_at = 0.0
        self._show_opt_progress(0, 1, "准备行情", force=True)
        if hasattr(self, "opt_result_box"):
            try:
                from src.backtest.optimizer import describe_grid, grid_for_engine

                preview = describe_grid(grid_for_engine(engine, depth, cfg))
            except Exception:  # noqa: BLE001
                preview = ""
            mode_hint = (
                f"滚动前进 {walk_steps} 段"
                if mode == "滚动前进"
                else f"一次切断，检验比例约 {holdout_pct}%"
            )
            evolve_hint = "这轮只出报告，不会改正在用的参数。\n" if evolve_only else ""
            self._set_result_text(
                self.opt_result_box,
                f"策略优化进行中（{mode_hint}，depth={depth}，最多{max_trials}组）…\n"
                f"{evolve_hint}"
                f"请不要关闭窗口。\n\n本次搜索旋钮：\n{preview}\n",
            )

        def _run() -> None:
            try:
                from src.backtest.optimizer import (
                    describe_params,
                    format_perf_block,
                    optimize_strategy,
                    save_optimization_report,
                )

                result = optimize_strategy(
                    cfg,
                    symbol=symbol,
                    max_trials=max_trials,
                    start_date=start,
                    end_date=end,
                    engine=engine,
                    depth=depth,
                    holdout_pct=holdout_pct,
                    mode=mode,
                    walk_steps=walk_steps,
                    cross_symbols=cross_symbols,
                    progress=lambda done, total, phase: self._post_to_main(
                        lambda d=done, t=total, p=phase: self._show_opt_progress(d, t, p)
                    ),
                )
                path = self.root_dir / "data" / "optimize_last.json"
                save_optimization_report(result, path)
                evolve_block: list[str] = []
                evolve_can_apply = False
                evolve_reason = ""
                if evolve_only:
                    from src.backtest.optimizer import (
                        current_params_from_config,
                        check_params_on_holdout,
                        judge_better_than_current,
                        save_evolve_report,
                    )

                    old_params = current_params_from_config(
                        cfg, engine, list((result.best_params or {}).keys())
                    )
                    self._post_to_main(
                        lambda: self._show_opt_progress(1, 1, "对照现在的参数", force=True)
                    )
                    old_checked = check_params_on_holdout(
                        cfg,
                        params=old_params,
                        symbol=symbol,
                        engine=engine,
                        depth=depth,
                        holdout_pct=holdout_pct,
                        start_date=start,
                        end_date=end,
                        cross_symbols=cross_symbols,
                    )
                    verdict = judge_better_than_current(
                        result.holdout_performance or {},
                        old_checked.get("holdout_performance") or {},
                        accepted=bool(result.accepted),
                        new_params=dict(result.best_params or {}),
                        old_params=old_params,
                    )
                    evolve_can_apply = bool(verdict.get("can_apply_live"))
                    evolve_reason = str(verdict.get("reason") or "")
                    report_path = self.root_dir / "data" / "evolve_last.json"
                    save_evolve_report(
                        {
                            "symbol": result.symbol,
                            "engine": result.engine,
                            "new_params": dict(result.best_params or {}),
                            "old_params": old_params,
                            "new_accepted": bool(result.accepted),
                            "new_accept_reason": str(result.accept_reason or ""),
                            "new_holdout": dict(result.holdout_performance or {}),
                            "old_holdout": dict(old_checked.get("holdout_performance") or {}),
                            "old_accepted": bool(old_checked.get("accepted")),
                            "verdict": verdict,
                            "optimize_report": str(path),
                        },
                        report_path,
                    )
                    evolve_block = [
                        "",
                        "—— 和现在这套对照（模拟重跑，没有改实盘）——",
                        f"现在的参数  {describe_params(old_params)}",
                        f"现在检验净盈亏  {float((old_checked.get('holdout_performance') or {}).get('net_pnl') or 0):.0f}"
                        f"  回撤 {float((old_checked.get('holdout_performance') or {}).get('max_drawdown') or 0):.0f}"
                        f"  盈利因子 {float((old_checked.get('holdout_performance') or {}).get('profit_factor') or 0):.2f}",
                        f"新的检验净盈亏  {float((result.holdout_performance or {}).get('net_pnl') or 0):.0f}"
                        f"  回撤 {float((result.holdout_performance or {}).get('max_drawdown') or 0):.0f}"
                        f"  盈利因子 {float((result.holdout_performance or {}).get('profit_factor') or 0):.2f}",
                        *(str(x) for x in (verdict.get("checks") or [])),
                        str(verdict.get("reason") or ""),
                        f"对照报告  {report_path}",
                    ]
                    if evolve_can_apply:
                        evolve_block.append(
                            "左侧写入按钮会改成「写入并切实盘（先存档）」。切之前会留下整页配置存档和参数版本。"
                        )
                    else:
                        evolve_block.append(
                            "默认不建议切实盘。仍可点「把所选参数写入配置」，程序会再问一次。"
                        )
                self._last_opt_params = dict(result.best_params or {})
                # 同时记住引擎，写入配置时用。没通过后一段时，下面的写入按钮会拒绝保存。
                self._last_opt_engine = engine
                self._last_opt_accepted = bool(result.accepted)
                self._last_opt_reason = str(result.accept_reason or "")
                self._last_opt_start = str(start or "")
                self._last_opt_end = str(end or "")
                # 记下全部打分行和本轮设置，供「写入第几组」复核非最优组。
                self._last_opt_trials = list(result.trials or [])
                self._last_opt_ctx = {
                    "symbol": result.symbol,
                    "engine": result.engine,
                    "depth": result.depth,
                    "holdout_pct": result.holdout_pct,
                    "start_date": str(result.start_date or ""),
                    "end_date": str(result.end_date or ""),
                    "cross_symbols": list(cross_symbols or []),
                }

                train = result.train_performance or result.best_performance or {}
                hold = result.holdout_performance or {}
                top = result.trials[:8]
                mode_line = f"检验方式  {result.mode}，检验比例 {result.holdout_pct:.0f}%"
                if result.mode == "滚动前进":
                    mode_line += f"，实际 {result.walk_steps} 段"
                lines = [
                    f"策略优化完成  {result.symbol}",
                    f"策略      {result.engine}（搜索深度 {result.depth}）",
                    mode_line,
                    switch_text,
                    fill_text,
                    self._format_limit_lines(getattr(result, "best_limits", None) or limit_snap),
                    f"K线根数   一共{result.bars}根",
                    f"训练段    {result.train_bars}根，到 {result.train_end or '切断处'}，用来挑参数",
                    f"检验段    {result.holdout_bars}根，从 {result.holdout_start or '切断处'} 起，不参与挑参数",
                    f"挑出的参数  {describe_params(result.best_params)}",
                    "",
                    *format_perf_block(
                        "训练成绩",
                        train,
                        bars=result.train_bars,
                        span=f"到 {result.train_end or '切断处'}",
                        data_from=str(result.start_date or ""),
                        data_to=str(result.train_end or result.end_date or ""),
                    ),
                    "",
                    *format_perf_block(
                        "检验成绩",
                        hold,
                        bars=result.holdout_bars,
                        span=f"从 {result.holdout_start or '切断处'} 起",
                        data_from=str(result.holdout_start or result.start_date or ""),
                        data_to=str(result.end_date or ""),
                    ),
                ]
                lines.append("")
                lines.append("—— 稳健性（公开渠道常用）——")
                lines.append(
                    f"检验效率  {float(getattr(result, 'walk_efficiency', 0) or 0):.2f}"
                    "（检验净盈亏÷训练净盈亏；大约 0.50 以上较稳，低于 0.30 很虚）"
                )
                if result.mode == "滚动前进":
                    lines.append(
                        f"检验段赚钱比例  {float(getattr(result, 'oos_win_rate', 0) or 0):.0%}"
                        "（赚钱的滚动段÷有成绩的段；不到一半不能写入）"
                    )
                    stab = str(getattr(result, "param_stability", "") or "")
                    if stab:
                        lines.append(f"参数稳定  {stab}")
                if result.walk_folds:
                    lines.append("")
                    lines.append("—— 滚动每一段 ——")
                    for fold in result.walk_folds:
                        tp = fold.get("train_performance") or {}
                        hp = fold.get("holdout_performance") or {}
                        lines.append(
                            f"第{fold.get('fold')}段 训练到{fold.get('train_end')} "
                            f"净盈亏{float(tp.get('net_pnl') or 0):.0f}；"
                            f"检验{fold.get('holdout_start')}~{fold.get('holdout_end')} "
                            f"净盈亏{float(hp.get('net_pnl') or 0):.0f} "
                            f"{'过关' if fold.get('accepted') else '未过'}"
                        )
                if result.cross_symbol_rows:
                    lines.append("")
                    lines.append("—— 多品种验收（参数原样，不再重搜）——")
                    for row in result.cross_symbol_rows:
                        if row.get("error"):
                            lines.append(f"{row.get('symbol')}  失败：{row.get('error')}")
                            continue
                        cover = float(row.get("net_pnl") or 0) - float(row.get("max_drawdown") or 0)
                        lines.append(
                            f"{row.get('symbol')}  成交{int(row.get('trades') or 0)}笔 "
                            f"胜率{float(row.get('win_rate') or 0):.0%} "
                            f"净盈亏{float(row.get('net_pnl') or 0):.0f} "
                            f"盈利因子{float(row.get('profit_factor') or 0):.2f} "
                            f"最大回撤{float(row.get('max_drawdown') or 0):.0f} "
                            f"覆盖{cover:.0f}"
                        )
                lines.extend(
                    [
                        "",
                        self._last_opt_reason,
                    f"报告      {path}",
                    "",
                        "—— 本次搜索的旋钮与取值范围 ——",
                    result.grid_describe or "",
                    "",
                        "—— 训练段得分靠前的几组 ——",
                ]
                )
                for i, row in enumerate(top, 1):
                    pr = row.get("performance") or {}
                    cover = float(pr.get("net_pnl") or 0) - float(pr.get("max_drawdown") or 0)
                    lines.append(
                        f"#{i} score={float(row.get('score', 0)):.3f} "
                        f"trades={pr.get('trades', 0)} "
                        f"wr={float(pr.get('win_rate', 0)):.0%} "
                        f"net={float(pr.get('net_pnl', 0)):.0f} "
                        f"cover={cover:.0f} | {describe_params(row.get('params'))}"
                    )
                lines.append("")
                if evolve_only:
                    lines.extend(evolve_block)
                elif result.accepted:
                    lines.append(
                        "可以在左侧「写入第几组」填 #编号（默认 1=训练最优），再点「把所选参数写入配置」。"
                        "选其它组时会先按检验段重新复核，复核没过就不写。"
                    )
                    lines.append(
                        "策略、品种、周期和开仓限制要另外点历史回测页「把本页参数写入配置页」，这次不会自动写进去。"
                    )
                else:
                    lines.append(
                        "第 1 组没有通过检验，不能直接写；可以换「写入第几组」选其它组，"
                        "程序会按检验段重新复核，过了才允许写入。"
                    )
                text = "\n".join(lines)
                verdict = "可以写入" if result.accepted else "不能写入"
                if evolve_only:
                    verdict = "可以切实盘" if evolve_can_apply else "不切实盘"
                self._last_evolve_reason = evolve_reason
                self._post_to_main(lambda: self._append_log(
                    f"{'模拟重跑' if evolve_only else '策略优化'}完成 {result.symbol} | "
                    f"训练得分={result.best_score:.3f} | {verdict} | "
                    f"{describe_params(result.best_params)}"
                ))
                if hasattr(self, "opt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.opt_result_box, t))
                self._post_to_main(lambda: self._finish_opt_progress(ok=True))
                self._post_to_main(
                    lambda compared=bool(evolve_only), ok=evolve_can_apply, why=evolve_reason: (
                        setattr(self, "_last_opt_compared", compared),
                        setattr(self, "_last_evolve_better", bool(ok)),
                        setattr(self, "_last_evolve_reason", why),
                        self._sync_opt_write_button(),
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("策略优化失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"策略优化失败: {err}"))
                if hasattr(self, "opt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.opt_result_box, f"策略优化失败:\n{m}"))
                self._post_to_main(lambda: self._finish_opt_progress(ok=False))
                self._post_to_main(lambda m=err: messagebox.showerror("策略优化失败", m))

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_opt_history(self, select_id: str | None = None) -> None:
        """刷新优化页的「参数版本」下拉列表。select_id 有值时，刷新后仍停在这一条。"""
        try:
            from src.backtest.param_history import list_version_labels

            engine = self.opt_engine_var.get() if hasattr(self, "opt_engine_var") else None
            pairs = list_version_labels(self.root_dir, engine)
            self._opt_hist_id_by_label = {label: vid for label, vid in pairs}
            labels = [p[0] for p in pairs] or ["（暂无历史）"]
            chosen = labels[0]
            if select_id:
                for label, vid in pairs:
                    if vid == select_id:
                        chosen = label
                        break
            if hasattr(self, "opt_hist_menu"):
                self.opt_hist_menu.configure(values=labels)
                self.opt_hist_var.set(chosen)
            self._sync_hist_name_box()
        except Exception as e:  # noqa: BLE001
            logger.warning("刷新参数历史失败: {}", e)

    def _sync_hist_name_box(self, _label: str | None = None) -> None:
        """下拉换了一条时，名字框跟着换成这一条现在的名字。出厂默认不能改。"""
        if not hasattr(self, "opt_hist_name_var"):
            return
        from src.backtest.param_history import FACTORY_DEFAULT_ID, get_version, version_display_name

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label, "")
        if not vid or vid == FACTORY_DEFAULT_ID:
            self.opt_hist_name_var.set("出厂默认")
            if hasattr(self, "opt_hist_name_entry"):
                self.opt_hist_name_entry.configure(state="disabled")
                self.opt_hist_rename_btn.configure(state="disabled")
            if hasattr(self, "opt_hist_delete_btn"):
                self.opt_hist_delete_btn.configure(state="disabled")
            return
        entry = get_version(self.root_dir, vid) or {}
        self.opt_hist_name_var.set(version_display_name(entry))
        if hasattr(self, "opt_hist_name_entry"):
            self.opt_hist_name_entry.configure(state="normal")
            self.opt_hist_rename_btn.configure(state="normal")
        if hasattr(self, "opt_hist_delete_btn"):
            self.opt_hist_delete_btn.configure(state="normal")

    def _on_rename_opt_history(self) -> None:
        """把当前选中的参数版本改成输入框里的名字。"""
        from tkinter import messagebox

        from src.backtest.param_history import FACTORY_DEFAULT_ID, rename_version

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label)
        if not vid or vid == FACTORY_DEFAULT_ID:
            messagebox.showwarning("不能改名", "出厂默认要留着当重置锚点，不能改名。")
            return
        new_name = self.opt_hist_name_var.get()
        try:
            rename_version(self.root_dir, vid, new_name)
        except ValueError as e:
            messagebox.showwarning("改名没有成功", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("参数版本改名失败")
            messagebox.showerror("改名失败", str(e))
            return
        self._refresh_opt_history(select_id=vid)
        self._append_log(f"参数版本已改名: {new_name.strip()}")

    def _on_delete_opt_history(self) -> None:
        """删掉优化页当前选中的那一条历史。不改正在使用的参数，出厂默认也不能删。"""
        from tkinter import messagebox

        from src.backtest.param_history import FACTORY_DEFAULT_ID, delete_version

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label)
        if not vid or vid == FACTORY_DEFAULT_ID:
            messagebox.showwarning("不能删除", "出厂默认要留着当重置锚点，不能删除。")
            return
        if not messagebox.askyesno("删除历史版本", f"只从历史列表里删掉这一条，当前参数不会变：\n\n{label}\n\n确定删除？"):
            return
        try:
            removed = delete_version(self.root_dir, vid)
        except ValueError as exc:
            messagebox.showwarning("没有删掉", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除参数版本失败")
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_opt_history()
        self._append_log(f"已删除参数版本: {removed}")

    def _open_optimizer_file(self) -> None:
        """用系统默认程序打开优化参数表，不装 IDE 也能直接改网格档位和中文名。"""
        from tkinter import messagebox

        from src.backtest.param_table import ensure_param_table, resolve_param_table_path

        path = resolve_param_table_path(self.cfg)
        try:
            ensure_param_table(path)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.exception("打开优化参数表失败")
            messagebox.showerror("打不开优化参数表", f"{e}\n\n路径：{path}")

    def _on_apply_opt_params(self) -> None:
        """按「写入第几组」写入：第 1 组直接写；其它组先重新复核检验，过了再写。

        对照全面更好时走切实盘（强制整页存档）。对照过但没更好时仍允许写入，会多问一次。
        """
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning(
                "请先停止",
                "常驻正在跑，不会自己换成新参数。\n请先点「停止常驻」，再写入，然后重新启动。",
            )
            return
        if not self._last_opt_params:
            messagebox.showwarning("还没有策略优化结果", "请先在本页点「开始策略优化」，完成后再写入。")
            return
        raw = (self.opt_write_index_var.get() or "").strip() or "1"
        try:
            idx = int(raw)
        except ValueError:
            messagebox.showwarning("组号填错了", "请填结果框里 #1、#2 这样的整数。")
            return
        trials = list(getattr(self, "_last_opt_trials", []) or [])
        live_switch = bool(getattr(self, "_last_evolve_better", False))
        save_page_preset = bool(self.opt_save_preset_var.get()) or live_switch
        if not trials:
            self._write_optimized_params(
                None, group_index=1, live_switch=live_switch, save_page_preset=save_page_preset
            )
            return
        if idx < 1 or idx > len(trials):
            messagebox.showwarning(
                "组号超出范围", f"这次优化共 {len(trials)} 组，只能填 1～{len(trials)}。"
            )
            return
        if idx == 1:
            self._write_optimized_params(
                None, group_index=1, live_switch=live_switch, save_page_preset=save_page_preset
            )
            return
        trial = trials[idx - 1]
        params = dict(trial.get("params") or {})
        if not params:
            messagebox.showwarning("这组没有参数", "换个组号再试。")
            return
        self._recheck_and_write_optimized(idx, params, trial)

    def _recheck_and_write_optimized(
        self, idx: int, params: dict[str, Any], trial: dict[str, Any]
    ) -> None:
        """非最优组：后台按检验段重新验收 + 多品种，通过了才让写入。"""
        from tkinter import messagebox

        ctx = dict(getattr(self, "_last_opt_ctx", {}) or {})
        if not ctx.get("symbol"):
            messagebox.showwarning("缺少复核信息", "请重新跑一次策略优化，再选组写入。")
            return
        self._append_log(f"复核第 {idx} 组参数（检验段 + 多品种）…")
        self._opt_started_at = time.monotonic()
        self._opt_progress_at = 0.0
        self._show_opt_progress(0, 1, f"复核第 {idx} 组", force=True)
        cfg = self._clone_cfg()

        def _run() -> None:
            try:
                from src.backtest.optimizer import check_params_on_holdout

                checked = check_params_on_holdout(
                    cfg,
                    params=params,
                    symbol=str(ctx.get("symbol") or ""),
                    engine=str(ctx.get("engine") or ""),
                    depth=str(ctx.get("depth") or "标准"),
                    holdout_pct=ctx.get("holdout_pct"),
                    start_date=str(ctx.get("start_date") or "") or None,
                    end_date=str(ctx.get("end_date") or "") or None,
                    cross_symbols=list(ctx.get("cross_symbols") or []),
                    progress=lambda done, total, phase: self._post_to_main(
                        lambda d=done, t=total, p=phase: self._show_opt_progress(d, t, p)
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("复核第 {} 组失败", idx)
                self._post_to_main(lambda: self._finish_opt_progress(ok=False))
                self._post_to_main(lambda m=str(e): messagebox.showerror("复核失败", m))
                return
            accepted = bool(checked.get("accepted"))
            self._post_to_main(lambda: self._finish_opt_progress(ok=accepted))
            if not accepted:
                reason = str(checked.get("reason") or "检验段没过")
                self._post_to_main(lambda: self._append_log(f"第 {idx} 组复核没过: {reason}"))
                self._post_to_main(
                    lambda r=reason: messagebox.showwarning(
                        f"第 {idx} 组不能写入",
                        r + "\n\n可以换一组，或到历史回测页手动验证。配置没有改。",
                    )
                )
                return
            self._post_to_main(lambda: self._append_log(f"第 {idx} 组复核通过，准备写入。"))
            live_switch = bool(getattr(self, "_last_evolve_better", False))
            save_page_preset = bool(self.opt_save_preset_var.get()) or live_switch
            self._post_to_main(
                lambda: self._write_optimized_params(
                    params,
                    group_index=idx,
                    score=trial.get("score"),
                    performance=trial.get("performance") or {},
                    confirm_extra=checked,
                    live_switch=live_switch,
                    save_page_preset=save_page_preset,
                )
            )

        threading.Thread(target=_run, daemon=True, name="opt-recheck").start()

    def _write_optimized_params(
        self,
        params: dict[str, Any] | None = None,
        *,
        group_index: int = 1,
        score: float | None = None,
        performance: dict[str, Any] | None = None,
        confirm_extra: dict[str, Any] | None = None,
        live_switch: bool = False,
        save_page_preset: bool = False,
    ) -> None:
        """
        把选中的参数写入磁盘配置，并先备份当前版本便于回溯。

        params 为空时用刚才优化的第 1 组（训练最优），此时必须后一段验收通过；
        选中其它组时，调用方已先复核通过，这里不再重复门槛。
        """
        from tkinter import messagebox

        use = dict(params or self._last_opt_params or {})
        if not use:
            messagebox.showwarning("还没有策略优化结果", "请先在本页点「开始策略优化」，完成后再写入。")
            return
        if params is None and not getattr(self, "_last_opt_accepted", False):
            reason = getattr(self, "_last_opt_reason", "") or "后一段验收没过。"
            messagebox.showwarning("这次不能写入", reason + "\n\n配置里的参数没有改。")
            return
        compared = bool(getattr(self, "_last_opt_compared", False))
        save_page_preset = bool(save_page_preset) or live_switch or bool(
            getattr(self, "opt_save_preset_var", None) and self.opt_save_preset_var.get()
        )
        if compared and not live_switch:
            extra = getattr(self, "_last_evolve_reason", "") or "对照没有全面优于现在这套。"
            if not messagebox.askyesno(
                "对照没有更好",
                extra
                + "\n\n仍可以按原来的方式写入，但建议先不要切实盘。\n确定继续写入？",
            ):
                return
        from src.backtest.optimizer import describe_params, describe_run_switches
        from src.backtest.param_history import strategy_display_name

        engine = getattr(self, "_last_opt_engine", None) or self.opt_engine_var.get()
        name = strategy_display_name(engine)
        switch_text = describe_run_switches(
            engine,
            flatten_at_close=bool(self.bt_flatten_var.get()),
        )
        picked = self.opt_engine_var.get()
        mismatch = ""
        if picked and picked != engine:
            mismatch = (
                f"下拉框现在选的是{strategy_display_name(picked)}，"
                f"这次写入的是刚才策略优化的{name}。\n\n"
            )
        # 日内实体优化可能带 timeframe / eth_minutes，写入后信号周期和 ETH 分钟数会跟着变。
        period_note = (
            "若参数里带有信号周期或 ETH 分钟数，这两项会一并写入；正在用的策略名和关注品种仍不改。\n"
            if engine == "daily_body"
            else "正在用的策略、品种、周期不会改。另外几套的参数也不会改。\n"
        )
        group_note = "" if group_index == 1 else f"这次写入的是第 {group_index} 组（不是训练最优那组）。\n"
        recheck_text = ""
        if confirm_extra:
            train = confirm_extra.get("train_performance") or {}
            hold = confirm_extra.get("holdout_performance") or {}
            recheck_text = (
                f"复核成绩  训练净赚 {float(train.get('net_pnl') or 0):.0f}，"
                f"检验净赚 {float(hold.get('net_pnl') or 0):.0f}"
                f"（{int(hold.get('trades') or 0)} 笔）\n"
                f"复核结论  {confirm_extra.get('reason') or '通过'}\n\n"
            )
        title = "写入并切实盘" if live_switch else "写入配置"
        archive_line = (
            "切之前会留下整页配置存档（配置页可一键应用），并备份这一套策略的参数版本。\n"
            if save_page_preset
            else "写入前会备份这一套策略的参数版本（下面「参数版本历史」）。这次不留整页配置存档。\n"
        )
        head = (
            f"把对照后挑出的{name}写入正在用的配置。\n"
            "默认本来不切；这次是因为对照全面更好，并且你点了确认。\n"
            if live_switch
            else f"写入刚才策略优化得到的{name}。\n"
        ) + archive_line
        if not messagebox.askyesno(
            title,
            f"{head}"
            f"{group_note}"
            f"{period_note}"
            "写入前会自动备份。\n\n"
            f"{mismatch}"
            f"{recheck_text}"
            f"这次打分用的开关会一起留下：\n{switch_text}\n\n"
            "开仓限制（权益、手数、保证金）这次不写。"
            "若历史回测页和配置页的数字不同，写入后历史回测页会变回配置页的数字。"
            "要留下历史回测页的策略、品种、周期和开仓限制，请先点「把本页参数写入配置页」。\n\n"
            f"即将写入的参数：\n{describe_params(use)}\n\n确定？"
            + (
                "\n\n切坏了：下面选旧版本点「回溯」（只还原这一套旋钮）；"
                "配置页可一键应用整页存档；要出厂点「重置当前策略为出厂默认」。"
                if save_page_preset or live_switch
                else "\n\n若效果不好：下面选旧版本点「回溯」，或重置为出厂默认。"
            ),
        ):
            return
        try:
            from src.backtest.optimizer import apply_params_to_config, apply_run_switches
            from src.backtest.param_history import add_version

            cfg = self._clone_cfg()
            sym = self.opt_symbol_var.get().strip()
            if save_page_preset:
                from src.ui.config_presets import add_preset

                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prefix = "切实盘前" if live_switch else "优化写入前"
                entry = add_preset(
                    self.root_dir,
                    cfg,
                    name=f"{prefix}-{sym or '未填'}-{name}-{stamp}",
                )
                self._refresh_config_presets(select_id=str(entry.get("id") or ""))
                self._append_log(f"已留下整页配置存档: {entry.get('name')}")
            # 1) 先备份写入前的配置。记在被优化的那一套下面。
            add_version(
                self.root_dir,
                cfg,
                note="写入优化前自动备份",
                source="backup",
                engine=engine,
                symbol=sym or None,
            )
            cfg = apply_run_switches(
                cfg,
                engine,
                flatten_at_close=bool(self.bt_flatten_var.get()),
            )

            new_cfg = apply_params_to_config(cfg, use, engine=engine)
            save_config(new_cfg)
            self.cfg = new_cfg
            self._reload_values_only()

            # 2) 再存「优化写入后」版本，方便对比
            perf = dict(performance or {})
            if score is None:
                try:
                    # 从结果框无法可靠解析；若刚优化完，score 可从文件读
                    import json
                    from pathlib import Path

                    rep = Path(self.root_dir) / "data" / "optimize_last.json"
                    if rep.exists():
                        payload = json.loads(rep.read_text(encoding="utf-8"))
                        score = payload.get("best_score")
                        perf = payload.get("best_performance") or {}
                except Exception:  # noqa: BLE001
                    pass
            add_version(
                self.root_dir,
                new_cfg,
                note=f"写入优化参数（第 {group_index} 组） {describe_params(use)}",
                source="optimize",
                score=float(score) if score is not None else None,
                performance=perf,
                opt_params=dict(use),
                engine=engine,
                symbol=sym or None,
            )
            # 历史列表跟着刚才优化的那一套走，避免写入后还停在另一个策略的名单上。
            self.opt_engine_var.set(engine)
            self._refresh_opt_history()
            self._append_log(
                f"{'已切实盘' if live_switch else '已写入优化参数'}（第 {group_index} 组，只改了 {name}，已备份历史）: {describe_params(use)}"
            )
            self._last_evolve_better = False
            self._sync_opt_write_button()
            preset_msg = (
                "配置页多了一份整页存档，可一键应用回去。\n"
                if save_page_preset
                else ""
            )
            messagebox.showinfo(
                "已切实盘" if live_switch else "已写入",
                f"已保存{name}的参数，以及这次打分用到的开关。\n"
                "正在用的策略、品种和周期没有改。开仓限制也没有改。\n"
                f"{preset_msg}"
                "这一套旋钮的备份在下方「参数版本历史」。\n"
                "若效果不好：选旧版本 →「回溯到选中版本」。"
                "要出厂：点「重置当前策略为出厂默认」。",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("写入优化参数失败")
            messagebox.showerror("写入失败", str(e))

    def _apply_history_version(self, vid: str, *, confirm_title: str, confirm_body: str) -> bool:
        """
        共用：把指定历史版本（含出厂默认）写回配置。
        返回是否真正执行了回溯。
        """
        from tkinter import messagebox

        if not messagebox.askyesno(confirm_title, confirm_body):
            return False
        try:
            from src.backtest.param_history import (
                FACTORY_DEFAULT_ID,
                add_version,
                apply_engine_snapshot,
                get_version,
                strategy_display_name,
            )

            entry = get_version(self.root_dir, vid)
            if not entry or not entry.get("snapshot"):
                messagebox.showerror("回溯失败", f"找不到版本 {vid}")
                return False

            engine = self.opt_engine_var.get()
            name = strategy_display_name(engine)
            cfg = self._clone_cfg()
            sym = self.opt_symbol_var.get().strip()
            add_version(
                self.root_dir,
                cfg,
                note="回溯/重置前自动备份",
                source="backup",
                engine=engine,
                symbol=sym or None,
            )
            new_cfg = apply_engine_snapshot(cfg, entry["snapshot"], engine)
            save_config(new_cfg)
            self.cfg = new_cfg
            self._reload_values_only()

            note = (
                f"已把{name}重置为出厂默认"
                if vid == FACTORY_DEFAULT_ID
                else f"已把{name}回溯到 {vid}"
            )
            add_version(
                self.root_dir,
                new_cfg,
                note=note,
                source="restore",
                score=entry.get("score"),
                performance=entry.get("performance") or {},
                opt_params=entry.get("opt_params") or {},
                engine=engine,
                symbol=sym or None,
            )
            self._refresh_opt_history()
            self._append_log(note)
            section = {
                "daily_body": "daily_body",
                "morning_box_a": "morning_box_a",
                "trend_follow": "trend_follow",
                "supertrend": "supertrend",
                "dual_ma": "dual_ma",
                "ma_cross": "dual_ma",
                "swing_trend": "swing_trend",
            }.get(engine, engine)
            shown = (new_cfg.get("strategy") or {}).get(section)
            if hasattr(self, "opt_result_box"):
                self._set_result_text(
                    self.opt_result_box,
                    f"{note}\n时间: {entry.get('time')}\n"
                    f"说明: {entry.get('note')}\n"
                    f"只改了: {name}\n\n"
                    f"这一套现在的参数:\n{shown}\n\n"
                    "正在用的策略、品种、周期，以及另外几套的参数都没有改。",
                )
            extra = ""
            if engine in (
                "daily_body",
                "morning_box_a",
                "dual_ma",
                "ma_cross",
            ):
                extra = "\n收盘清仓也会回到这一版。听这个总开关的策略会一起变。"
            messagebox.showinfo(
                "完成",
                f"只恢复了{name}的参数。\n"
                "正在用的策略、品种和周期没有改。"
                f"{extra}",
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception("回溯/重置失败")
            messagebox.showerror("失败", str(e))
            return False

    def _on_restore_opt_history(self) -> None:
        """把策略参数回溯到历史里选中的版本（含出厂默认）。"""
        from tkinter import messagebox

        label = self.opt_hist_var.get()
        vid = (self._opt_hist_id_by_label or {}).get(label)
        if not vid:
            messagebox.showwarning("无法回溯", "请先选择一个有效的历史版本。")
            return
        self._apply_history_version(
            vid,
            confirm_title="确认回溯",
            confirm_body=(
                f"只把策略优化页当前选中的策略，改成这个版本里记下的参数：\n\n{label}\n\n"
                "正在用的策略、品种、周期，以及另外几套的参数都不会动。\n"
                "回溯前会再自动备份一次。确定？"
            ),
        )

    def _on_reset_factory_params(self) -> None:
        """一键重置为出厂默认（优化怎么都不好时用）。"""
        from src.backtest.param_history import FACTORY_DEFAULT_ID

        self._apply_history_version(
            FACTORY_DEFAULT_ID,
            confirm_title="重置当前策略为出厂默认",
            confirm_body=(
                "只把策略优化页当前选中的这一套策略，恢复成程序最初的参数。\n"
                "另外几套策略保持现在的数。\n"
                "正在用的策略、品种、周期、账号和同花顺坐标都不会改。\n\n"
                "重置前会自动备份当前参数。确定？"
            ),
        )

    def _on_export_params_excel(self) -> None:
        """把程序内最近最多30条参数历史追加到「优化参数」表，并把最近一次优化追加到「优化结果」表。"""
        from tkinter import filedialog, messagebox

        try:
            from src.backtest.param_excel import default_excel_path, export_append_excel
            from src.export.bundle import export_optimize

            default_path = default_excel_path(self.root_dir)
            path = filedialog.asksaveasfilename(
                title="导出/追加策略优化参数与优化结果到 Excel",
                initialdir=str(default_path.parent),
                initialfile=default_path.name,
                defaultextension=".xlsx",
                filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            )
            if not path:
                return
            out, added, total = export_append_excel(self.root_dir, path, include_factory=True)
            opt_msg = ""
            try:
                _out, opt_added, opt_total = export_optimize(self.root_dir, path)
                opt_msg = f"\n「优化结果」表：本次新追加 {opt_added} 行（含全部候选组），合计 {opt_total} 行。"
            except ValueError:
                opt_msg = "\n「优化结果」表：还没有跑过策略优化，这次没写。"
            msg = (
                f"已写入「优化参数」表{opt_msg}\n\n"
                f"文件：{out}\n"
                f"参数版本本次新追加：{added} 条\n"
                f"该表合计：{total} 条\n\n"
                f"「优化结果」表第一列「测试时间」是这次优化跑完的时刻，用来分清是哪一次。\n"
                f"同一个文件里还有交易记录、资金变动记录、复盘、回测摘要、回测成交、回测信号等表。\n"
                f"已存在的版本ID / 同一次优化不会重复写入。"
            )
            self._append_log(f"参数与优化结果已导出追加到Excel: {out} (参数+{added}, 共{total})")
            if hasattr(self, "opt_result_box"):
                self._set_result_text(self.opt_result_box, msg)
            messagebox.showinfo("导出完成", msg)
        except ImportError:
            messagebox.showerror(
                "缺少组件",
                "需要 openpyxl 才能写 Excel。\n请在项目目录执行：\n  pip install openpyxl",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("导出Excel失败")
            messagebox.showerror("导出失败", str(e))

    def _on_import_params_excel(self) -> None:
        """从本地 Excel 导入参数版本，合并进程序历史（仍只留最近30条）。"""
        from tkinter import filedialog, messagebox

        try:
            from src.backtest.param_excel import default_excel_path, import_from_excel

            default_path = default_excel_path(self.root_dir)
            path = filedialog.askopenfilename(
                title="从 Excel 导入策略优化参数",
                initialdir=str(default_path.parent),
                initialfile=default_path.name if default_path.exists() else "",
                filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            )
            if not path:
                return
            if not messagebox.askyesno(
                "确认导入",
                "将从 Excel 读入参数版本并合并到程序历史。\n"
                "程序内仍只保留最近 30 条（多的请继续用 Excel 存档）。\n\n"
                "导入不会自动改当前正在用的配置；需要的话再选版本「回溯」。\n\n确定导入？",
            ):
                return
            n_in, n_keep = import_from_excel(self.root_dir, path)
            self._refresh_opt_history()
            msg = (
                f"导入完成。\n\n"
                f"Excel 有效行：{n_in}\n"
                f"程序内现保留：{n_keep} 条\n\n"
                f"请在上方历史列表选择版本，点「回溯到选中版本」才会改当前配置。"
            )
            self._append_log(f"已从Excel导入参数: {path} | 有效{n_in} | 程序内{n_keep}")
            if hasattr(self, "opt_result_box"):
                self._set_result_text(self.opt_result_box, msg)
            messagebox.showinfo("导入完成", msg)
        except ImportError:
            messagebox.showerror(
                "缺少组件",
                "需要 openpyxl 才能读 Excel。\n请在项目目录执行：\n  pip install openpyxl",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("导入Excel失败")
            messagebox.showerror("导入失败", str(e))

    def _on_order_simulator(self) -> None:
        """打开下单模拟器。点击走启动常驻的同一套代码，价格按窗口里填的限价。"""
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning("请先停止", "请先停止常驻扫描，再打开下单模拟器，避免两处抢鼠标。")
            return
        from src.ui.order_simulator import open_order_simulator

        symbol = self._watch_symbols[0] if self._watch_symbols else "bu2612"
        open_order_simulator(self, self.cfg, symbol, self._append_log, self._bring_self_front)

    def _on_pick_coords(self) -> None:
        """打开坐标标定小工具。打包后的 exe 会再开一个自己，只弹标定窗口。"""
        from src.ui.coord_picker import picker_launch_command

        self._append_log("正在打开坐标标定窗口…")
        command = picker_launch_command()

        def _run() -> None:
            # python.exe 会带出一个黑框。pythonw 和打包后的程序本身没有黑框。
            launcher_name = Path(command[0]).name.lower()
            flags = 0
            if os.name == "nt" and launcher_name not in {"pythonw.exe", "auto-trading.exe"}:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            code = subprocess.call(command, creationflags=flags)
            self.after(
                0,
                lambda: self._append_log(
                    f"坐标标定结束 exit={code}。如果这是另一块屏幕，点「保存当前坐标」留一份，再改成家里或笔记本。"
                ),
            )

        threading.Thread(target=_run, daemon=True).start()

    def _post_to_main(self, callback: Callable[[], None]) -> None:
        """
        后台线程安全地把回调排到 Tk 主线程队列。

        self.after(0, ...) 官方就是线程安全的；唯一风险是窗口或其中某个子控件已
        destroy 后，排队的 lambda 才被主线程取出来执行，此时对已销毁控件调
        configure/insert 等会抛 TclError: invalid command name "...".

        这里除了吞 self.after 自己可能抛的异常，还在执行期再包一层，把「已销毁
        控件」的 TclError 也吞掉——后台线程慢半拍、弹窗先关了都算正常边界情况，
        不值得写 ERROR 到日志。
        """

        def _safe() -> None:
            try:
                callback()
            except tk.TclError:
                # 99% 的情况是 "invalid command name '.!ctktoplevel...!label'"，
                # 也就是 callback 闭包里抓着某个已经被 destroy 的子控件不放
                # （比如手工平仓弹窗已关、后台线程才跑完推荐止损计算）。
                # 静默丢弃即可，不要把正常边界情况写成 ERROR。
                pass
            except Exception:  # noqa: BLE001
                # 其他真正的 bug 还是写日志，别让它静默。
                logger.exception("_post_to_main 回调异常")

        try:
            self.after(0, _safe)
        except tk.TclError:
            # self 自己已经 destroy，after 队列也没意义了。
            pass
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        self._closing = True
        for name in ("_tick_after_id", "_log_after_id", "_login_tick_after_id"):
            after_id = getattr(self, name, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, name, None)
        if self.worker.running:
            self.worker.stop()
        self.destroy()

    def report_callback_exception(self, exc, val, tb) -> None:
        """
        界面回调里的异常也写进日志文件。

        默认实现只把 traceback 打到 stderr；用 pythonw 或双击 exe 启动时看不见，
        控制台一关就没了。这里补一份到 logs，下次出错能直接翻日志定位。
        """
        import traceback

        text = str(val or "")
        # CustomTkinter 标签被挤得太窄时会画圆角失败。不影响下单，不当成程序坏了。
        if "wrong # coordinates" in text:
            logger.debug("界面绘制跳过（窗口太窄）: {}", text)
            return
        detail = "".join(traceback.format_exception(exc, val, tb))
        logger.error("界面回调异常:\n{}", detail)
        try:
            self._append_log(f"界面出错：{val}（完整堆栈已写入 logs）")
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    """GUI 入口。"""
    try:
        app = AutoTradingApp()
        app.mainloop()
    except KeyboardInterrupt:
        # 终端里按 Ctrl+C，或 Cursor 点停止/重复运行时都会走到这里。
        # 属于人为中断，不是程序出错，不用打印一大段 traceback。
        logger.info("界面被键盘中断（Ctrl+C）关闭")
    except BaseException:
        logger.exception("图形界面异常退出")
        raise


if __name__ == "__main__":
    main()
