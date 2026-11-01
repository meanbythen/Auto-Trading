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
import subprocess
import sys
import threading
from datetime import date
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
    "morning_structure_b": "早盘高低递进B",
    "morning_b": "早盘高低递进B",
    "trend_follow": "趋势跟随",
    "supertrend": "SuperTrend趋势",
    "st": "SuperTrend趋势",
    "Single_ma": "单均线系统",
    "single_ma": "单均线系统",
    "dual_ma": "双均线趋势",
    "ma_cross": "双均线趋势",
    "swing_trend": "长线趋势加减仓",
}

# 界面下拉统一用这一份（主推在前）。
ENGINE_CHOICES = [
    "daily_body",
    "morning_box_a",
    "morning_structure_b",
    "trend_follow",
    "supertrend",
    "Single_ma",
    "dual_ma",
    "swing_trend",
]

# 旧配置里的短名字。下拉框用完整名字，打开时换成同一套策略。
_ENGINE_ALIASES = {
    "st": "supertrend",
    "single_ma": "Single_ma",
    "ma20_trend": "Single_ma",
    "ma20": "Single_ma",
    "ma_cross": "dual_ma",
    "morning_a": "morning_box_a",
    "morning_b": "morning_structure_b",
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


# 周期下拉。分钟、日线、周线都在这里。选「自定义」后，右边填分钟数，例如 120 就是 2 小时。
# 1 分钟用来测试更细的信号；公开分钟线大约只有最近一千根，1 分钟只能覆盖最近几天。


class QueueLogHandler:
    """把 loguru 日志推到界面回调。"""

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

        self._init_vars()
        self._build()
        self._hook_logger()
        self._refresh_status_cards()
        self.after(800, self._tick)
        self.after(5000, self._tick_login_schedule)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        self.sizing_mode_var = ctk.StringVar(value=str(risk.get("sizing_mode", "risk_pct")))
        self.capital_var = ctk.StringVar(value=str(risk.get("capital", 1000000)))
        self.risk_pct_var = ctk.StringVar(value=str(risk.get("risk_per_trade_pct", 0.5)))
        from src.risk.sizing import DEFAULT_MARGIN_USAGE_PCT

        self.margin_usage_pct_var = ctk.StringVar(
            value=str(risk.get("margin_usage_pct", DEFAULT_MARGIN_USAGE_PCT))
        )
        self.margin_var = ctk.StringVar(value=str(risk.get("margin_per_lot", 0) or 0))
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
        ma_box = st.get("single_ma") or {}
        dual_box = st.get("dual_ma") or {}
        # 箱沿外再让几跳：多单箱底外、空单箱顶外。默认 1。
        self.body_stop_ticks_var = ctk.StringVar(value=str(body_box.get("stop_extra_ticks", 1)))
        # 这一段开盘跳空超过几跳就不开；0=关闭。燃油每跳 1 点，默认 50 就是 50 点。
        self.body_skip_gap_var = ctk.StringVar(value=str(body_box.get("skip_gap_ticks", 50)))
        # 开盘箱体最高-最低超过几跳就不开；0=关闭。箱体越宽，箱沿止损离进场价越远。
        self.body_max_box_var = ctk.StringVar(value=str(body_box.get("max_box_ticks", 100)))
        # 开仓截止钟点：过了这个钟点，日盘/夜盘这一段不再开新仓。默认沿用旧版 10:30 / 22:15。
        self.body_day_cutoff_var = ctk.StringVar(value=str(body_box.get("day_cutoff", "10:30")))
        self.body_night_cutoff_var = ctk.StringVar(value=str(body_box.get("night_cutoff", "22:15")))
        self.body_eth2_var = ctk.StringVar(value=str(body_box.get("eth_minutes2", 30)))
        self.trend_entry_bars_var = ctk.StringVar(value=str(trend_box.get("entry_bars", 36)))
        self.trend_atr_mult_var = ctk.StringVar(value=str(trend_box.get("atr_mult", 2.0)))
        self.st_atr_period_var = ctk.StringVar(value=str(st_box.get("atr_period", 10)))
        self.st_multiplier_var = ctk.StringVar(value=str(st_box.get("multiplier", 3.0)))
        self.ma_period_var = ctk.StringVar(value=str(ma_box.get("ma_period", 20)))
        # 收盘反向穿越均线时反手开新仓（开）还是只平仓（关）。
        self.ma_reverse_var = ctk.BooleanVar(value=bool(ma_box.get("reverse_on_cross", True)))
        self.dual_short_var = ctk.StringVar(value=str(dual_box.get("short_period", 20)))
        self.dual_long_var = ctk.StringVar(value=str(dual_box.get("long_period", 60)))
        self.dual_reverse_var = ctk.BooleanVar(value=bool(dual_box.get("reverse_on_cross", True)))
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
        self._last_opt_params: dict = {}
        self._last_opt_engine: str = ""
        # 后一段验收过了才是 True。没过时参数仍显示在结果里，但不能写入配置。
        self._last_opt_accepted: bool = False
        self._last_opt_reason: str = ""
        self._last_opt_start: str = ""
        self._last_opt_end: str = ""

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

    # ---------- 搭建 ----------
    def _build(self) -> None:
        hero = ctk.CTkFrame(self, fg_color=T.BG)
        hero.pack(fill="x", padx=28, pady=(20, 4))
        ctk.CTkLabel(
            hero, text=f"Auto-Trading {__version__}", font=T.FONT_DISPLAY, text_color=T.ACCENT, anchor="w"
        ).pack(anchor="w")
        ctk.CTkLabel(
            hero,
            text="华泰期货 · 沥青 / 玉米 · 配置可改 · 默认模拟",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

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

        self._build_run_tab(self.tabs.tab("运行"))
        self._build_config_tab(self.tabs.tab("配置"))
        self._build_backtest_tab(self.tabs.tab("历史回测"))
        self._build_picker_tab(self.tabs.tab("交叉测试"))
        self._build_optimize_tab(self.tabs.tab("策略优化"))
        self._build_review_tab(self.tabs.tab("复盘"))
        self._build_account_tab(self.tabs.tab("账户分析"))
        self._load_saved_picks()
        # 打开程序先看一眼当前关注合约是不是主力，非主力时在配置页亮提示。
        self.after(300, self._refresh_main_contract_hint)

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

        # 可滚动：窗口较矮时，紧急清仓下面的导出和保存仍能滚出来，不会被裁掉。
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

        # 只留顶部状态栏没有的内容：信号周期、止损周期，以及流动性耗尽的两个开关。
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
        # 启动：待命=蓝底白字（可点）；运行中=灰底浅字（不可点）
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
        # 停止：待命=灰底浅字（不可点）；运行中=红底白字（可点）
        self.btn_stop = ctk.CTkButton(
            btn_row,
            text="停止",
            height=38,
            fg_color=T.DANGER_MUTED,
            hover_color=T.DANGER_HOVER,
            text_color=T.DANGER_TEXT,
            text_color_disabled=T.DANGER_TEXT_DISABLED,
            command=self._on_stop,
            state="disabled",
        )
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(6, 0))

        ctk.CTkLabel(
            left,
            text="点「启动常驻」会先打开同花顺、登录、确认下单界面，并把价格改成对手价。这几步不点买卖。都成功后才开始扫描。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        btn_ctp = ctk.CTkFrame(left, fg_color="transparent")
        btn_ctp.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(
            btn_ctp,
            text="仿真柜台测试",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_ctp_probe,
        ).pack(fill="x")

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
            text="紧急清仓（平掉程序记录的持仓）",
            height=36,
            fg_color=T.DANGER,
            hover_color=T.DANGER_HOVER,
            text_color=T.DANGER_TEXT,
            command=self._on_emergency_flatten,
        ).pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(
            left,
            text="按实际价格平账",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_book_actual_close,
        ).pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="交易软件里已经平掉、程序还记着仓时用。填写实际成交价。只改程序的账，不会去点同花顺。",
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
            text="保存模拟开关到配置文件",
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
        from src.data.quote_check import SOURCE_LABELS

        self._option_row(box, "行情数据源", self.bt_source_var, list(SOURCE_LABELS), width=160)
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
        self._entry_row(box, "开始日期", self.bt_start_var, hint="默认当年1月1日，可改", width=160)
        self._entry_row(box, "结束日期", self.bt_end_var, hint="填了就按这天停，空着=到最新", width=160)
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
            text="导出追加到「回测摘要」和「回测成交」。两张表第一列是回测时间，同一次的行时间相同，用来分清是哪一次。",
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
            text="下拉是当前启用的合约。添加/移出会同步进「品种与合约」总表。保证金、乘数、手续费请在总表里改；同品种必须填成一样。",
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
        self._entry_row(box, "每笔风险%", self.pk_risk_pct_var, hint="跟配置页一致，开跑前对齐", width=160)
        self._entry_row(box, "持仓手数", self.pk_volume_var, hint="跟配置页一致；各组合同手数", width=160)
        self._entry_row(box, "开仓最小间隔秒", self.pk_interval_var, hint="跟配置页一致", width=160)
        self._entry_row(box, "每天最多开仓", self.pk_max_orders_var, hint="跟配置页一致", width=160)
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
        self._entry_row(box, "开始日期", self.pk_start_var, hint="默认当年1月1日，可改", width=160)
        self._entry_row(box, "结束日期", self.pk_end_var, hint="空着=到最新", width=160)
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
        self._pick_col_widths = (
            108, 132, 48, 64, 72, 72, 72, 64, 120, 40, 72, 176, 88, 88, 40, 72, 72, 48, 52, 64,
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
        self._entry_row(box, "复盘日期", self.rv_date_var, hint="YYYY-MM-DD，默认今天", width=160)
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

        box = self._card(left)
        self._account_action_row(
            box, "初始资金", self.ac_capital_var, [("初始化", self._on_signal_initial, True)]
        )
        self._account_action_row(
            box,
            "金额",
            self.ac_cash_var,
            [("入金", self._on_signal_deposit, False), ("出金", self._on_signal_withdraw, False)],
        )
        self._account_action_row(
            box, "实际余额", self.ac_settle_var, [("每日结算", self._on_signal_settle, True)]
        )
        ctk.CTkLabel(
            box,
            text="初始化会先把旧账存档，再从现在另记一本。入金、出金填正数。红字是赚，绿字是亏。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))
        ctk.CTkButton(
            left, text="刷新", height=32, fg_color=T.BG_ELEVATED, hover_color=T.BORDER,
            border_width=1, border_color=T.BORDER, text_color=T.TEXT, command=self._refresh_signal_view,
        ).pack(fill="x", padx=4, pady=(8, 4))
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
        ctk.CTkButton(
            left,
            text="按实际价格平账",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_book_actual_close,
        ).pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(
            left,
            text="交易软件里已经平掉、程序还记着仓时用。填写实际成交价。只改程序的账，不会去点同花顺。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=390,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))

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
            text="训练段挑参数，检验段只验收，检验比例可手调。可选「滚动前进」：走一步再验一步。打分不看胜率。检验段或其它关注品种上把赚变成大亏的，不能写入。滚动前进会更久。",
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
        self._entry_row(box, "开始日期", self.opt_start_var, hint="默认当年1月1日，可改", width=160)
        self._entry_row(box, "结束日期", self.opt_end_var, hint="填了就按这天停，空着=到最新", width=160)
        self._option_row(
            box,
            "搜索深度",
            self.opt_depth_var,
            ["精简", "标准", "完整"],
            width=160,
        )
        self._entry_row(
            box,
            "最多试几组",
            self.opt_trials_var,
            hint="标准含Pin定义等；完整组合更多会随机抽",
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

        ctk.CTkButton(
            left,
            text="开始策略优化（基于历史回测打分）",
            height=44,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_optimize,
        ).pack(fill="x", padx=4, pady=(12, 6))
        ctk.CTkButton(
            left,
            text="把最优参数写入配置",
            height=36,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.ACCENT,
            text_color=T.ACCENT,
            command=self._on_apply_opt_params,
        ).pack(fill="x", padx=4, pady=(0, 8))

        # —— 参数版本回溯：防止过度优化改坏 ——
        hist = self._card(left)
        ctk.CTkLabel(
            hist,
            text="参数版本历史（防过度优化）",
            font=T.FONT_BODY,
            text_color=T.TEXT,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            hist,
            text="列表只显示上面选中的那一套策略。写入、回溯、重置都只改这一套，不改正在用的策略、品种、周期，也不改另外几套的参数。不想要的历史版本可以删除，出厂默认不能删。",
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
            text="程序内只留最近30条。导出写进 data/Auto-Trading导出.xlsx 的「优化参数」表，和其他功能共用这一个文件。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=320,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        right = ctk.CTkFrame(tab, fg_color=T.BG_ELEVATED, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 8), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="策略优化结果", font=T.FONT_TITLE, text_color=T.TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        self.opt_result_box = ctk.CTkTextbox(
            right, font=T.FONT_MONO, fg_color=T.BG, text_color=T.TEXT, corner_radius=10, wrap="word"
        )
        self.opt_result_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.opt_result_box.insert(
            "end",
            "点左侧「开始策略优化」。完成后可点「把最优参数写入配置」。\n"
            "写入前会自动备份；若新参数不好，用下方历史列表回溯。\n",
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
        # 别名都指到同一块面板（如 Single_ma / single_ma）。
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

    def _sync_strategy_param_wrap(self) -> None:
        """
        按右侧参数框当前实际宽度，刷新标题说明的换行。

        只改文字换行，不改控件宽高，避免 Tk 用非法尺寸画背景图时报 CreateDIBSection。
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
            wrap = max(200, w - 40)
            self._strategy_param_hint_wrap = max(140, w - 300)
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

    def _toggle_password(self) -> None:
        self.pwd_entry.configure(show="" if self._show_password.get() else "*")

    def _toggle_tq_password(self) -> None:
        self.tq_pwd_entry.configure(show="" if self._show_tq_password.get() else "*")

    def _refresh_tq_hint(self) -> None:
        """
        天勤只给分钟K线。选了日线/周线，或选了固定用日线的长线趋势，就在配置页直接说清楚。

        不在这里拦着不让保存：用户可能只是先改一半。真正拉行情时行情层仍会报错。
        """
        label = getattr(self, "tq_hint_label", None)
        if label is None:
            return
        from src.data.quote_check import normalize_source

        if normalize_source(self.source_var.get()) != "tq":
            label.configure(text="", text_color=T.TEXT_DIM)
            return
        codes = [
            self._period_code(self.tf_var, self.tf_custom_var),
            self._period_code(self.trail_var, self.trail_custom_var),
        ]
        # 长线趋势内部固定用日线定方向，跟上面选的信号周期无关。
        if _ui_engine(self.engine_var.get()) == "swing_trend":
            codes.append("D")
        bad = [c for c in codes if c in {"D", "W"}]
        if bad:
            label.configure(
                text=(
                    "天勤只提供分钟K线，当前要用"
                    + "、".join("日线" if c == "D" else "周线" for c in bad)
                    + "，会拉不到行情。请把周期改成分钟，或把行情来源换回新浪/东方财富。"
                ),
                text_color=T.DANGER,
            )
            return
        label.configure(text="天勤只提供分钟K线，账号密码填在上面「账号与登录」。", text_color=T.TEXT_DIM)

    # ---------- 日志 ----------
    def _hook_logger(self) -> None:
        def emit(text: str) -> None:
            self.after(0, lambda t=text: self._append_log(t))

        logger.add(QueueLogHandler(emit).write, format="{time:HH:mm:ss} | {level} | {message}", level="INFO")

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
        pending = self._held_logs
        self._held_logs = []
        for line in pending:
            self.log_box.insert("end", line + "\n")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")

    # ---------- 配置读写 ----------
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
        ma_box = st.get("single_ma") or {}
        dual_box = st.get("dual_ma") or {}
        self.body_stop_ticks_var.set(str(body_box.get("stop_extra_ticks", 1)))
        self.body_skip_gap_var.set(str(body_box.get("skip_gap_ticks", 50)))
        self.body_max_box_var.set(str(body_box.get("max_box_ticks", 100)))
        self.body_day_cutoff_var.set(str(body_box.get("day_cutoff", "10:30")))
        self.body_night_cutoff_var.set(str(body_box.get("night_cutoff", "22:15")))
        self.body_eth2_var.set(str(body_box.get("eth_minutes2", 30)))
        self.trend_entry_bars_var.set(str(trend_box.get("entry_bars", 36)))
        self.trend_atr_mult_var.set(str(trend_box.get("atr_mult", 2.0)))
        self.st_atr_period_var.set(str(st_box.get("atr_period", 10)))
        self.st_multiplier_var.set(str(st_box.get("multiplier", 3.0)))
        self.ma_period_var.set(str(ma_box.get("ma_period", 20)))
        self.ma_reverse_var.set(bool(ma_box.get("reverse_on_cross", True)))
        self.dual_short_var.set(str(dual_box.get("short_period", 20)))
        self.dual_long_var.set(str(dual_box.get("long_period", 60)))
        self.dual_reverse_var.set(bool(dual_box.get("reverse_on_cross", True)))
        swing_box = st.get("swing_trend") or {}
        from src.strategy.swing_trend import DEFAULT_SCALE, resolve_scale_pair, scale_label

        scale_code, _t, _s = resolve_scale_pair(swing_box if isinstance(swing_box, dict) else {})
        self.swing_scale_var.set(self._swing_label_by_scale.get(scale_code, scale_label(DEFAULT_SCALE)))
        self.swing_daily_ma_var.set(str(swing_box.get("daily_ma", 20)))
        self.swing_unit_var.set(str(swing_box.get("unit_volume", 1)))
        self.swing_max_adds_var.set(str(swing_box.get("max_adds", 2)))
        self.swing_add_atr_var.set(str(swing_box.get("add_min_atr", 1.0)))
        self.swing_reduce_atr_var.set(str(swing_box.get("reduce_extend_atr", 2.5)))
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
        self._copy_risk_to_backtest()

    def _apply_ui_to_cfg(self) -> dict[str, Any]:
        """把表单写回内存 cfg，并处理密码。"""
        self.cfg["dry_run"] = bool(self.dry_var.get())
        from src.data.quote_check import normalize_source

        data = self.cfg.setdefault("data", {})
        data["source"] = normalize_source(self.source_var.get())
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
        body["day_cutoff"] = _clock_text(self.body_day_cutoff_var.get(), "10:30")
        body["night_cutoff"] = _clock_text(self.body_night_cutoff_var.get(), "22:15")
        try:
            body["eth_minutes2"] = max(0, int(self.body_eth2_var.get()))
        except ValueError:
            body["eth_minutes2"] = 30
        # 旧版 ATR 底线 / 回撤止损已取消，保存时清掉，避免配置里还留着误导人。
        body.pop("initial_atr_mult", None)
        body.pop("reverse_atr_mult", None)

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

        ma_box = st.setdefault("single_ma", {})
        try:
            ma_box["ma_period"] = max(2, int(self.ma_period_var.get()))
        except ValueError:
            ma_box["ma_period"] = 20
        ma_box["reverse_on_cross"] = bool(self.ma_reverse_var.get())
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

        trade = self.cfg.setdefault("trade", {})
        trade["executor"] = self.exec_var.get()
        try:
            trade["volume"] = int(self.volume_var.get())
        except ValueError:
            trade["volume"] = 1
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
        try:
            sch["scan_interval_sec"] = int(self.scan_var.get())
        except ValueError:
            sch["scan_interval_sec"] = 30
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
        position_text = self._position_card_text(snap if running else None)
        self.card_position.configure(
            text=position_text,
            text_color=T.TEXT if position_text == "空仓" else T.OK,
        )
        # 启停成对：可点用鲜色，不可点统一变灰，一眼知道现在能点哪个
        if running:
            self.btn_start.configure(
                state="disabled",
                fg_color=T.ACCENT_MUTED,
                text_color_disabled=T.ACCENT_TEXT_DISABLED,
            )
            self.btn_stop.configure(
                state="normal",
                fg_color=T.DANGER,
                text_color=T.DANGER_TEXT,
            )
        else:
            self.btn_start.configure(
                state="normal",
                fg_color=T.ACCENT,
                text_color=T.ACCENT_ON,
            )
            self.btn_stop.configure(
                state="disabled",
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
                positions = load_state(self.root_dir / "data" / "bot_state.json").get("positions") or {}
            except Exception:  # noqa: BLE001
                positions = {}
        return format_position_text(positions)

    def _tick(self) -> None:
        self._refresh_status_cards()
        self._pull_signal_balance()
        self.after(800, self._tick)

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

    def _on_save(self) -> None:
        # 保存前用最新余额填上账户权益。这个数不能在配置页手改。
        self._pull_signal_balance()
        cfg = self._merge_disk_gui_coords(self._apply_ui_to_cfg())
        path = save_config(cfg)
        self._status = "已保存"
        self._append_log(f"配置已保存: {path}")
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
                "程序正在跑，不会自己换成新开关。\n请先点停止，再一键应用，然后重新启动。",
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
            messagebox.showwarning("请先停止常驻", "程序正在跑，不会中途换点击位置。\n请先点停止，再选用坐标，然后重新启动。")
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
        """打开仿真柜台测试窗口。不改当前执行通道，也不碰同花顺。"""
        from src.ui.ctp_probe import open_ctp_probe

        open_ctp_probe(self)

    def _on_start(self) -> None:
        from tkinter import messagebox

        if self._prep_busy:
            messagebox.showwarning("正在准备", "同花顺还在登录或改价格。这几步做完之前不会启动常驻。")
            return
        if self.worker.running:
            return

        if self.exec_var.get() == "ctp":
            self._append_log("执行通道是 CTP：会连接交易前置。前置、BrokerID 或认证码没填好时不会发单。")

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
            self.after(0, self._after_stopped)

        self.worker.start(cfg, on_stopped=on_stopped)
        self._refresh_status_cards()
        mode = "模拟(不点单)" if cfg.get("dry_run") else "实盘点击"
        self._append_log(f"已启动常驻扫描 | dry_run={cfg.get('dry_run')} | {mode}")
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

            self.after(0, _done)

        threading.Thread(target=_work, name="ths-prepare", daemon=True).start()

    def _tick_login_schedule(self) -> None:
        """
        到了配置里的登录时间，先做准备，完成后再启动常驻。

        常驻已经在跑时，不在这里再开一次。正在跑的程序会在下一轮扫描前自己登录，
        登录过程中不会同时点买卖。
        """
        try:
            self._maybe_start_at_login_time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查定时登录失败: {}", exc)
        self.after(20000, self._tick_login_schedule)

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

    def _on_stop(self) -> None:
        self.worker.stop()
        self._status = "正在停止…"
        self._append_log("已发送停止请求…")
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
            self.after(0, lambda: self._append_log(f"完整自检结束，exit={code}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_emergency_flatten(self) -> None:
        """Kill switch：立刻对策略内持仓发平仓点击。"""
        from tkinter import messagebox

        if not messagebox.askyesno(
            "紧急清仓",
            "将按程序内部持仓记录，对同花顺点击平仓。\n"
            "请确认下单面板合约正确。\n\n确定继续？",
        ):
            return
        # 若未启动常驻，临时建一个 bot 执行
        if self.worker.bot is None:
            from src.scheduler import TradingBot

            bot = TradingBot(self._clone_cfg())
            logs = bot.emergency_flatten()
        else:
            logs = self.worker.emergency_flatten()
        for line in logs:
            self._append_log(f"[紧急清仓] {line}")
        if not logs:
            self._append_log("[紧急清仓] 策略内无持仓记录")
        messagebox.showinfo("紧急清仓", "\n".join(logs) if logs else "无持仓可平")

    def _on_book_actual_close(self) -> None:
        """
        按你填的成交价，把程序账上的仓平掉。

        交易软件里已经平过、程序还记着时用。不点同花顺，也不发单。
        顶部持仓和账户分析里没平的那一笔都会清掉，盈亏按这个价格算进余额。
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
            open_position_rows,
        )

        bot = getattr(self.worker, "bot", None)
        if bot is not None and getattr(bot, "strategy", None) is not None:
            rows = open_position_rows(
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
            rows = open_position_rows(state.get("positions") or {})
        if not rows:
            messagebox.showinfo("已经是空仓", "程序账上没有还开着的仓。")
            return

        sizer = PositionSizer(self.cfg)
        for row in rows:
            word = "多" if row["side"] == "long" else "空"
            desc = f"{row['symbol']} {word}{row['volume']}手，开仓价 {row['entry_price']:g}"
            dialog = ctk.CTkInputDialog(
                text=f"程序还记着 {desc}。\n填写实际成交价。只改程序的账，不会去点同花顺。",
                title="按实际价格平账",
            )
            typed = str(dialog.get_input() or "").strip()
            if not typed:
                return
            try:
                exit_price = float(typed)
            except ValueError:
                messagebox.showerror("价格不对", "请填写数字，例如 4800。")
                return
            if exit_price <= 0:
                messagebox.showerror("价格不对", "平仓价要大于 0。")
                return
            if not messagebox.askyesno(
                "确认平账",
                f"按 {exit_price:g} 把 {desc} 记成已平。\n不会去点同花顺。\n\n确定？",
            ):
                return
            multiplier = float(sizer.multiplier_for(row["symbol"]))
            if bot is not None and getattr(bot, "strategy", None) is not None:
                closed, balance = bot.manual_close(row["symbol"], exit_price)
            else:
                closed, balance = manual_close_without_bot(
                    self.root_dir,
                    symbol=row["symbol"],
                    exit_price=exit_price,
                    multiplier=multiplier,
                    initial_capital=float(self.cfg.get("risk", {}).get("capital") or 0),
                )
            if closed is None:
                messagebox.showinfo("已经是空仓", f"{row['symbol']} 已经没有仓了。")
                continue
            direction = "多" if closed["side"] == "long" else "空"
            sign = 1.0 if closed["side"] == "long" else -1.0
            pnl = (exit_price - float(closed["entry_price"])) * sign * multiplier * int(closed["volume"])
            if balance is not None:
                # 常驻路径的余额已由 manual_close 内部的记账入口写回；只有没常驻时在这里补一次。
                if bot is None or getattr(bot, "strategy", None) is None:
                    write_capital_to_config(balance)
                self._apply_book_balance(balance)
            self._refresh_signal_view()
            self._refresh_status_cards()
            shown = self._position_card_text(None if bot is None else bot.status_snapshot())
            self._append_log(
                f"已按 {exit_price:g} 把 {closed['symbol']} {direction}{closed['volume']}手平账，"
                f"盈亏 {pnl:+.0f} 元。当前持仓：{shown or format_position_text({})}"
            )
            messagebox.showinfo(
                "已平账",
                f"已按 {exit_price:g} 把 {closed['symbol']} {direction} {closed['volume']} 手记成已平。\n"
                f"这笔盈亏 {pnl:+.0f} 元。没有去点同花顺。",
            )

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
        self._option_row(box_risk, "仓位模式", self.bt_sizing_var, ["risk_pct", "fixed"], width=160)
        self._entry_row(
            box_risk,
            "账户权益(元)",
            self.bt_capital_var,
            hint="跟配置页一致，开跑前自动对齐",
            width=160,
        )
        self._entry_row(
            box_risk,
            "每手保证金(元)",
            self.bt_margin_var,
            hint="跟品种与合约表一致，开跑前自动对齐。0=不限制",
            width=160,
        )
        self._entry_row(box_risk, "每笔风险%", self.bt_risk_pct_var, hint="2 表示权益的 2%", width=160)
        self._entry_row(
            box_risk,
            "仓位上限%",
            self.bt_margin_usage_pct_var,
            hint="跟配置页一致；权益×此比例内交保证金",
            width=160,
        )
        self._entry_row(box_risk, "固定手数", self.bt_volume_var, hint="仓位模式=fixed 时使用", width=160)
        self._entry_row(box_risk, "最少手数", self.bt_min_vol_var, hint="算出来比这个少就不开", width=160)
        self._entry_row(box_risk, "日最大亏损(元)", self.bt_max_loss_var, hint="当天亏到这里就停开", width=160)
        self._entry_row(box_risk, "开仓最小间隔秒", self.bt_interval_var, width=160)
        self._entry_row(box_risk, "每天最多开仓", self.bt_max_orders_var, hint="次数，换日清零", width=160)
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

        一张表管关注合约和保证金/乘数；同品种保证金必须相同。
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
        source_label = (self.bt_source_var.get() or "").strip()
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
            f"行情来源  {source_label or '（空）'}\n"
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
        if source_label:
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
        # 行情来源以回测页为准（_apply_ui_to_cfg 已从 source_var 写入）
        if source_label:
            cfg.setdefault("data", {})["source"] = normalize_source(source_label)
        path = save_config(cfg)
        self.cfg = cfg
        self._status = "已保存"
        self._append_log(
            "历史回测本页参数已写入配置页并保存："
            f"策略 {ENGINE_LABELS.get(engine, engine)}，品种 {symbol}，"
            f"周期 {period_label(tf_code)}/{period_label(trail_code)}，"
            f"来源 {source_label or '-'}，"
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
            f"区间={start or '起'}~{end or '止'}"
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

                result = run_backtest(cfg, symbol=symbol, start_date=start, end_date=end, engine=st["engine"])
                path = self.root_dir / "data" / "backtest_last.json"
                save_backtest_report(result, path)
                p = result.performance
                snap = result.params_snapshot or {}
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
                    str(snap.get("fill_note") or "").strip(),
                    f"K线根数   {result.bars}",
                    f"成交      {p.trades} 笔（赢{p.wins} / 亏{p.losses}）",
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
                text = "\n".join(x for x in lines if x is not None)
                self.after(0, lambda: self._append_log(
                    f"历史回测完成 {result.symbol} | {result.bars}根 | "
                    f"{p.trades}笔 胜率{p.win_rate:.0%} 净盈亏{p.net_pnl:.0f}"
                ))
                if hasattr(self, "bt_result_box"):
                    self.after(0, lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("历史回测失败")
                err = str(e)
                self.after(0, lambda: self._append_log(f"历史回测失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self.after(0, lambda m=err: self._set_result_text(self.bt_result_box, f"历史回测失败:\n{m}"))
                self.after(0, lambda m=err: messagebox.showerror("历史回测失败", m))

        threading.Thread(target=_run, daemon=True).start()

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
                self.after(0, lambda: self._append_log(f"滑点灵敏度完成 {symbol}"))
                if hasattr(self, "bt_result_box"):
                    self.after(0, lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("滑点灵敏度失败")
                err = str(e)
                self.after(0, lambda: self._append_log(f"滑点灵敏度失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self.after(0, lambda m=err: self._set_result_text(self.bt_result_box, f"滑点灵敏度失败:\n{m}"))
                self.after(0, lambda m=err: messagebox.showerror("滑点灵敏度失败", m))

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
            from src.backtest.optimizer import format_perf_block, validate_params_on_symbols
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

                    self.after(0, _stopped)
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
                    f"参数  {params}",
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

                self.after(0, _done)
            except Exception as exc:  # noqa: BLE001
                logger.exception("多品种验收失败")
                err = str(exc)

                def _fail() -> None:
                    self._pick_running = False
                    self._render_pick_board(f"多品种验收失败：{err}", [])
                    self._append_log(f"多品种验收失败: {err}")
                    messagebox.showerror("多品种验收失败", err)

                self.after(0, _fail)

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
                self.after(0, lambda i=index, n=total, name=label: self._show_pick_progress(i, n, name))

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

                self.after(0, _done)
            except Exception as exc:  # noqa: BLE001
                logger.exception("交叉测试失败")
                err = str(exc)

                def _fail() -> None:
                    self._pick_running = False
                    self._append_log(f"交叉测试失败: {err}")
                    self._render_pick_board(f"交叉测试失败：{err}", [])
                    messagebox.showerror("交叉测试失败", err)

                self.after(0, _fail)

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
        """
        label = getattr(self, "main_contract_hint", None)
        if label is None:
            return
        code = (symbol or self._config_symbol_now()).strip()
        self._main_contract_seq = getattr(self, "_main_contract_seq", 0) + 1
        seq = self._main_contract_seq
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
                self.after(0, lambda: self._apply_main_contract_result(seq, code, found))
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
        """把最近一次回测追加到统一 Excel 的「回测摘要」「回测成交」，不盖掉以前的行。"""
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
            f"已追加到两张表，原来的记录还在：\n"
            f"回测摘要 新增 {summary_added} 行，合计 {summary_total} 行\n"
            f"回测成交 新增 {trade_added} 行，合计 {trade_total} 行\n"
            f"第一列「回测时间」相同、而且内容也相同的一行不会重复写。\n\n"
            f"文件：\n{out}",
        )
        self._append_log(
            f"历史回测已追加到 {out} | 摘要新增{summary_added}合计{summary_total} | 成交新增{trade_added}合计{trade_total}"
        )

    def _on_review(self) -> None:
        """后台生成收盘复盘，结果写在「复盘」页，并保存到 data/reviews。"""
        from datetime import date
        from tkinter import messagebox

        from src.review.replay import ENGINE_BY_LABEL

        raw_day = self.rv_date_var.get().strip()
        try:
            trade_date = date.fromisoformat(raw_day)
        except ValueError:
            messagebox.showerror("日期不对", "请按 YYYY-MM-DD 填写，例如 2026-10-14。")
            return
        label = self.rv_engine_var.get().strip()
        if label not in ENGINE_BY_LABEL:
            messagebox.showerror("策略不对", "请重新选择「策略对照」。")
            return
        engine = ENGINE_BY_LABEL[label]
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
                self.after(0, lambda t=text: self._set_result_text(self.rv_result_box, t))
                self.after(0, lambda: self._append_log(f"复盘完成 {symbol} {raw_day}"))
            except Exception as e:  # noqa: BLE001
                logger.exception("复盘失败")
                err = str(e)
                self.after(0, lambda: self._append_log(f"复盘失败: {err}"))
                self.after(0, lambda m=err: self._set_result_text(self.rv_result_box, f"复盘失败:\n{m}"))
                self.after(0, lambda m=err: messagebox.showerror("复盘失败", m))

        threading.Thread(target=_run, daemon=True).start()

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
        """
        shown = str(int(balance)) if float(balance) == int(balance) else f"{float(balance):.2f}"
        self.capital_var.set(shown)
        self.cfg.setdefault("risk", {})["capital"] = float(balance)
        bot = getattr(self.worker, "bot", None)
        if bot is not None:
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
        from src.account.signal_book import balance_of, book_path, describe, load_book

        if not hasattr(self, "ac_result_box"):
            return
        book = load_book(self.root_dir)
        if book is None:
            if hasattr(self, "ac_balance_label"):
                self.ac_balance_label.configure(text="当前余额  还没开始")
            self._paint_account_stats(None)
            self._set_result_text(
                self.ac_result_box,
                "还没有开始记账。\n"
                "程序从这一版起，按信号真正开仓或平仓后会自动记在这里。\n"
                "也可以先点「初始化」。以前手工记的成交不带过来。\n"
                "模拟模式只看不下单，不会记进这本账。",
            )
            return
        path = book_path(self.root_dir)
        try:
            self._signal_book_mtime = path.stat().st_mtime
        except OSError:
            self._signal_book_mtime = 0.0
        balance = balance_of(book, multiplier_map(self.cfg))
        shown = str(int(balance)) if float(balance) == int(balance) else f"{float(balance):.2f}"
        if hasattr(self, "ac_balance_label"):
            self.ac_balance_label.configure(text=f"当前余额  {shown} 元")
        if not self.ac_capital_var.get().strip():
            initial = float(book.get("initial_capital") or 0)
            self.ac_capital_var.set(str(int(initial)) if initial == int(initial) else f"{initial:.2f}")
        self._set_result_text(self.ac_result_box, describe(book, multiplier_map(self.cfg)))
        self._paint_account_stats(book)

    def _pull_signal_balance(self) -> None:
        """
        常驻做完一笔后，把配置页的账户权益改成新余额。

        这个数字不能在配置页手改，所以余额一变就更新显示。
        账本文件没变时，不反复刷账户分析右边的文字。
        """
        from src.account.signal_book import multiplier_map
        from src.account.signal_book import balance_of, book_path, load_book

        path = book_path(self.root_dir)
        if not path.exists():
            return
        book = load_book(self.root_dir)
        if book is None:
            return
        balance = balance_of(book, multiplier_map(self.cfg))
        try:
            shown = float(self.capital_var.get())
        except (TypeError, ValueError):
            shown = None
        if shown is None or abs(balance - shown) > 0.01:
            self._apply_book_balance(balance)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime != self._signal_book_mtime:
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

    def _on_optimize(self) -> None:
        """后台网格优化：对选定策略/时间段反复回测打分。"""
        from tkinter import messagebox

        if self.worker.running:
            messagebox.showwarning("请先停止", "策略优化较耗资源，请先停止常驻扫描。")
            return

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
        self._append_log(
            f"开始策略优化… engine={engine} depth={depth} mode={mode} "
            f"检验比例={holdout_pct}% symbol={symbol} "
            f"行情={self.opt_source_var.get()} "
            f"限制={'开' if limit_snap['apply_limits'] else '关'} "
            f"trials<={max_trials} 区间={start or '起'}~{end or '止'}"
        )
        self.tabs.set("策略优化")
        if hasattr(self, "opt_result_box"):
            try:
                from src.backtest.optimizer import describe_grid, grid_for_engine

                preview = describe_grid(grid_for_engine(engine, depth))
            except Exception:  # noqa: BLE001
                preview = ""
            mode_hint = (
                f"滚动前进 {walk_steps} 段"
                if mode == "滚动前进"
                else f"一次切断，检验比例约 {holdout_pct}%"
            )
            self._set_result_text(
                self.opt_result_box,
                f"策略优化进行中（{mode_hint}，depth={depth}，最多{max_trials}组）…\n"
                f"请不要关闭窗口。\n\n本次搜索旋钮：\n{preview}\n",
            )

        def _run() -> None:
            try:
                from src.backtest.optimizer import (
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
                )
                path = self.root_dir / "data" / "optimize_last.json"
                save_optimization_report(result, path)
                self._last_opt_params = dict(result.best_params or {})
                # 同时记住引擎，写入配置时用。没通过后一段时，下面的写入按钮会拒绝保存。
                self._last_opt_engine = engine
                self._last_opt_accepted = bool(result.accepted)
                self._last_opt_reason = str(result.accept_reason or "")
                self._last_opt_start = str(start or "")
                self._last_opt_end = str(end or "")

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
                    f"挑出的参数  {result.best_params}",
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
                    "—— 本次搜索的参数（含 Pinbar 定义等）——",
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
                        f"cover={cover:.0f} | {row.get('params')}"
                    )
                lines.append("")
                if result.accepted:
                    lines.append(
                        "可以点左侧「把最优参数写入配置」。"
                        "策略、品种、周期和开仓限制要另外点历史回测页「把本页参数写入配置页」，这次不会自动写进去。"
                    )
                else:
                    lines.append("这组没有通过检验，不要点写入。配置里的参数没有改。")
                text = "\n".join(lines)
                verdict = "可以写入" if result.accepted else "不能写入"
                self.after(0, lambda: self._append_log(
                    f"策略优化完成 {result.symbol} | 训练得分={result.best_score:.3f} | {verdict} | {result.best_params}"
                ))
                if hasattr(self, "opt_result_box"):
                    self.after(0, lambda t=text: self._set_result_text(self.opt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("策略优化失败")
                err = str(e)
                self.after(0, lambda: self._append_log(f"策略优化失败: {err}"))
                if hasattr(self, "opt_result_box"):
                    self.after(0, lambda m=err: self._set_result_text(self.opt_result_box, f"策略优化失败:\n{m}"))
                self.after(0, lambda m=err: messagebox.showerror("策略优化失败", m))

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

    def _on_apply_opt_params(self) -> None:
        """把上一次优化得到的最优参数写入磁盘配置，并先备份当前版本便于回溯。"""
        from tkinter import messagebox

        if not self._last_opt_params:
            messagebox.showwarning("还没有策略优化结果", "请先在本页点「开始策略优化」，完成后再写入。")
            return
        if not getattr(self, "_last_opt_accepted", False):
            reason = getattr(self, "_last_opt_reason", "") or "后一段验收没过。"
            messagebox.showwarning("这次不能写入", reason + "\n\n配置里的参数没有改。")
            return
        from src.backtest.optimizer import describe_run_switches
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
        if not messagebox.askyesno(
            "写入配置",
            f"写入刚才策略优化得到的{name}。\n"
            f"{period_note}"
            "写入前会自动备份。\n\n"
            f"{mismatch}"
            f"这次打分用的开关会一起留下：\n{switch_text}\n\n"
            "开仓限制（权益、手数、保证金）这次不写。"
            "若历史回测页和配置页的数字不同，写入后历史回测页会变回配置页的数字。"
            "要留下历史回测页的策略、品种、周期和开仓限制，请先点「把本页参数写入配置页」。\n\n"
            f"即将写入的参数：\n{self._last_opt_params}\n\n确定？",
        ):
            return
        try:
            from src.backtest.optimizer import apply_params_to_config, apply_run_switches
            from src.backtest.param_history import add_version

            cfg = self._clone_cfg()
            sym = self.opt_symbol_var.get().strip()
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

            new_cfg = apply_params_to_config(cfg, self._last_opt_params, engine=engine)
            save_config(new_cfg)
            self.cfg = new_cfg
            self._reload_values_only()

            # 2) 再存「优化写入后」版本，方便对比
            score = None
            perf = {}
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
                note=f"写入优化参数 {self._last_opt_params}",
                source="optimize",
                score=float(score) if score is not None else None,
                performance=perf,
                opt_params=dict(self._last_opt_params),
                engine=engine,
                symbol=sym or None,
            )
            # 历史列表跟着刚才优化的那一套走，避免写入后还停在另一个策略的名单上。
            self.opt_engine_var.set(engine)
            self._refresh_opt_history()
            self._append_log(f"已写入优化参数（只改了 {name}，已备份历史）: {self._last_opt_params}")
            messagebox.showinfo(
                "已写入",
                f"已保存{name}的参数，以及这次打分用到的开关。\n"
                "正在用的策略、品种和周期没有改。开仓限制也没有改。\n"
                "若效果不好：在下方选旧版本 →「回溯到选中版本」。",
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
                "morning_structure_b": "morning_structure_b",
                "trend_follow": "trend_follow",
                "supertrend": "supertrend",
                "Single_ma": "single_ma",
                "single_ma": "single_ma",
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
                "morning_structure_b",
                "Single_ma",
                "single_ma",
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
        """把程序内最近最多30条参数历史追加到本地 Excel（长期存档）。"""
        from tkinter import filedialog, messagebox

        try:
            from src.backtest.param_excel import default_excel_path, export_append_excel

            default_path = default_excel_path(self.root_dir)
            path = filedialog.asksaveasfilename(
                title="导出/追加策略优化参数到 Excel",
                initialdir=str(default_path.parent),
                initialfile=default_path.name,
                defaultextension=".xlsx",
                filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            )
            if not path:
                return
            out, added, total = export_append_excel(self.root_dir, path, include_factory=True)
            msg = (
                f"已写入「优化参数」表。\n\n"
                f"文件：{out}\n"
                f"本次新追加：{added} 条\n"
                f"该表合计：{total} 条\n\n"
                f"同一个文件里还有交易记录、资金变动记录、复盘、回测摘要、回测成交、优化结果等表。\n"
                f"已存在的版本ID不会重复写入。"
            )
            self._append_log(f"参数已导出追加到Excel: {out} (+{added}, 共{total})")
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

    def _on_close(self) -> None:
        if self.worker.running:
            self.worker.stop()
        self.destroy()


def main() -> None:
    """GUI 入口。"""
    app = AutoTradingApp()
    app.mainloop()


if __name__ == "__main__":
    main()
