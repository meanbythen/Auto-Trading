"""配置页的策略参数与各策略共用下单、仓位设置。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.trader.factory import list_executors
from src.ui import theme as T
from src.ui.config_tab_controls import cta_param
from src.ui.config_tab_controls import cta_switch
from src.ui.config_tab_controls import detail_panel
from src.ui.config_tab_controls import ema_num_row
from src.ui.config_tab_controls import note_block
from src.ui.config_tab_controls import param_entry
from src.ui.config_tab_controls import switch_row

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def build_strategy_section(
    app: AutoTradingApp,
    scroll: ctk.CTkFrame,
    engine_choices: list[str],
) -> None:
    """策略与交易。"""
    # 真·左右两列：左侧连续排「选策略 + 共用下单/仓位」；右侧整块只给当前策略参数。
    # 两列都顶对齐，右侧变高不会把左侧中间顶出空白。其它策略的数仍保存在变量里。
    app._section(scroll, "策略与交易")
    box2 = app._card(scroll)
    from src.data.quote_check import SOURCE_LABELS

    # 左侧按内容定宽；右侧 grid 铺满剩余横向空间。不用 pack_propagate(False) 手改宽高，
    # 避免宽/高算成 0 或异常值时 Tk 画圆角框触发 CreateDIBSection「参数错误」。
    app._strategy_left_min_width = 560  # 关注品种那一行最宽
    app._strategy_param_min_width = 360
    app._strategy_param_hint_wrap = 200
    app._strategy_param_entry_w = 120

    split = ctk.CTkFrame(box2, fg_color="transparent")
    split.pack(fill="x", pady=(0, 4))
    # grid：左列不抢余量，右列 weight=1 铺满；两列都顶对齐，高度互不影响。
    split.grid_columnconfigure(0, weight=0, minsize=app._strategy_left_min_width)
    split.grid_columnconfigure(1, weight=1, minsize=app._strategy_param_min_width)
    split.grid_rowconfigure(0, weight=0)
    app._strategy_split = split

    left = ctk.CTkFrame(split, fg_color="transparent")
    left.grid(row=0, column=0, sticky="nw", padx=(0, 10))
    app._strategy_left_col = left

    right_shell = ctk.CTkFrame(
        split,
        fg_color=T.BG_PANEL,
        corner_radius=10,
        border_width=1,
        border_color=T.ACCENT,
    )
    # sticky=new：横向铺满右列，竖直只贴顶；高度随当前策略参数内容自然伸缩。
    right_shell.grid(row=0, column=1, sticky="new")
    app._strategy_detail_shell = right_shell

    app._option_row(left, "行情数据源", app.source_var, list(SOURCE_LABELS), width=160)
    ctk.CTkLabel(
        left,
        text="主源天勤，备源新浪。只拉选中的这一家。同花顺图不在这里：读图会抢鼠标，只给历史回测。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))
    app.tq_hint_label = ctk.CTkLabel(
        left,
        text="",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
        anchor="w",
    )
    app.tq_hint_label.pack(anchor="w", pady=(0, 8))
    # 换来源、换周期、换策略时都重新看一眼天勤能不能用。
    for _var in (
        app.source_var,
        app.tf_var,
        app.tf_custom_var,
        app.trail_var,
        app.trail_custom_var,
        app.engine_var,
    ):
        try:
            _var.trace_add("write", lambda *_: app._refresh_tq_hint())
        except Exception:  # noqa: BLE001
            pass
    try:
        # 换成天勤才去查主力；换走新浪立刻停掉天勤登录提示。
        app.source_var.trace_add("write", lambda *_: app._refresh_main_contract_hint())
    except Exception:  # noqa: BLE001
        pass
    app._refresh_tq_hint()
    app.engine_menu = app._option_row(left, "策略引擎", app.engine_var, list(engine_choices), width=220)
    ctk.CTkLabel(
        left,
        text=(
            "右边整块是当前策略参数；换策略后右边跟着换。左侧下面是各策略共用设置。"
            "「策略优化」会按深度从右边这块和左侧公共设置里挑参数。"
        ),
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(6, 0))

    app._strategy_detail_title = ctk.CTkLabel(
        right_shell,
        text="当前策略参数",
        font=T.FONT_BODY,
        text_color=T.ACCENT,
        anchor="w",
    )
    app._strategy_detail_title.pack(fill="x", padx=12, pady=(10, 0))
    app._strategy_detail_note = ctk.CTkLabel(
        right_shell,
        text="",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
        anchor="w",
    )
    app._strategy_detail_note.pack(fill="x", padx=12, pady=(2, 4))
    app._strategy_detail_host = ctk.CTkFrame(right_shell, fg_color="transparent")
    app._strategy_detail_host.pack(fill="both", expand=True, padx=8, pady=(0, 10))
    app._strategy_detail_panels: dict[str, ctk.CTkFrame] = {}
    app._strategy_detail_meta: dict[str, tuple[str, str]] = {}

    body_box = detail_panel(
        app,
        "daily_body",
        "日内实体",
        "ETH 开盘箱体 + 收盘突破。改左边引擎才会用到这里。",
    )
    param_entry(
        app,
        body_box,
        "第一ETH窗口（分钟）",
        app.eth_min_var,
        hint="从这段开盘起算，观察这么多分钟再画第一口箱子。和第二个窗口分开判断",
    )
    param_entry(
        app,
        body_box,
        "第二ETH窗口（分钟）",
        app.body_eth2_var,
        hint="也从这段开盘起算，另画一口箱子。过了这个时间就丢掉第一窗口的判断，只认第二口。日盘止损后截止前也可按这口再开。夜盘不用。默认 30，填 0 关闭",
    )
    param_entry(
        app,
        body_box,
        "日盘开仓截止",
        app.body_day_cutoff_var,
        hint="过了这个钟点就停止开日盘新仓，默认 10:30；10:15～10:30 是上午休盘，截止 10:15 和 10:30 等效",
    )
    param_entry(
        app,
        body_box,
        "夜盘开仓截止",
        app.body_night_cutoff_var,
        hint="过了这个钟点就停止开夜盘新仓，默认 22:15",
    )
    param_entry(
        app,
        body_box,
        "箱沿外再让几跳",
        app.body_stop_ticks_var,
        hint="多守箱底、空守箱顶，再往外让几跳；0=贴着箱沿",
    )
    param_entry(
        app,
        body_box,
        "跳空超过几倍ATR不开",
        app.body_skip_gap_var,
        hint="开盘相对上一段收盘的缺口，超过开盘前ATR这么多倍就不开；0=关。出厂 2.5。尺子是信号周期、开盘前的ATR，不含今天",
    )
    param_entry(
        app,
        body_box,
        "箱体超过几倍ATR不开",
        app.body_max_box_var,
        hint="开盘箱子最高-最低超过开盘前ATR这么多倍就不开；0=关。出厂 5。箱子越宽，箱沿止损离进场越远",
    )
    param_entry(
        app,
        body_box,
        "急破超过几倍ATR不追",
        app.body_max_chase_var,
        hint="信号收盘离破位超过开盘前ATR这么多倍就不当场开，等回到破位附近再按原方向开；0=关。出厂 1",
    )
    param_entry(
        app,
        body_box,
        "回抽看哪条 EMA",
        app.body_ema_period_var,
        hint="等回抽时看这条 EMA；反抽收盘有效打穿它，这次突破作废；0=关闭这道确认",
    )
    param_entry(
        app,
        body_box,
        "有效打穿EMA几跳",
        app.body_ema_buffer_var,
        hint="收盘超过 EMA 这么多跳才算有效打穿；默认 2 跳",
    )
    note_block(
        body_box,
        "止损固定在开盘箱体对沿。触发方式在左侧公共设置里选；没触发就拿到收盘。不做回撤止损。\n"
        "策略优化会按深度搜这一块：精简只比第一ETH窗口、信号周期、止损触发方式和通用止损类型，"
        "标准再加日盘/夜盘截止钟点和交易时段，完整才放开箱沿外扩、跳空、箱宽、急破、EMA 回抽过滤，"
        "以及通用止损的回看根数、SAR 步长和 ATR 参数。\n"
        "优化日盘截止时用 09:45 / 10:15 / 10:45 三档，避开 10:15～10:30 的上午休盘。",
    )
    ctk.CTkLabel(
        body_box,
        text="本策略行情过滤",
        font=T.FONT_BODY,
        text_color=T.ACCENT,
        anchor="w",
    ).pack(fill="x", padx=4, pady=(10, 0))
    note_block(
        body_box,
        "箱子突破怕已经走出单边：ADX 高于上限就不开。盘面拧来拧去反而放行。\n"
        "极端波动仍可拦。已有持仓仍按箱沿止损。每一轮用当时 K 线重算，不是开盘判一次就整天停手。",
        pady=(0, 4),
    )
    switch_row(
        app,
        body_box,
        "启用行情过滤",
        app.body_filter_enabled_var,
            hint="开=每一轮看 ADX 是否已经单边、波动是否太极端；关=这一层不看。默认开",
    )
    param_entry(
        app,
        body_box,
        "强单边ADX上限",
        app.body_filter_adx_upper_var,
        hint="ADX 高于这个数当成已经单边，不开。默认 30。盘面拧来拧去会放行。填 0 关掉这一条",
    )
    param_entry(
        app,
        body_box,
        "极端波动ATR高分位%",
        app.body_filter_atr_high_var,
        hint="ATR 落在近 60 根最高这一档（默认 80%）就不开。填 0 关闭。不拦波动压缩",
    )
    param_entry(
        app,
        body_box,
        "过滤ADX周期",
        app.body_filter_adx_period_var,
        hint="默认 14",
    )
    param_entry(
        app,
        body_box,
        "过滤ATR周期",
        app.body_filter_atr_period_var,
        hint="默认 14",
    )
    param_entry(
        app,
        body_box,
        "过滤ATR回看根数",
        app.body_filter_lookback_var,
        hint="默认 60，用来算 ATR 处于历史几分位",
    )

    hold_box = detail_panel(
        app,
        "float_hold",
        "日内浮盈飘单",
        "等开盘噪音走完再定当天方向。止损和保本都按开盘前波动留宽度，否则拿到收盘。",
    )
    note_block(
        hold_box,
        "和日内实体分开。这套现在尽量简单：等观察窗口走完，"
        "EMA60 在 EMA120 上面就做多、下面就做空，错了用较宽的止损认赔。"
        "跳空、倾角、急破这些闸默认关掉，好让单边日能开到仓。"
        "还可以改成「观察结束看昨日密集区」、箱子或 MACD。"
        "手数仍用左边公共的凯利或每笔风险。",
        pady=(0, 4),
    )
    app._option_row(
        hold_box,
        "方向选择器",
        app.float_direction_var,
        getattr(app, "_float_direction_labels", ["开盘箱体突破"]),
        width=220,
    )
    app._option_row(
        hold_box,
        "MACD周期",
        app.float_macd_tf_var,
        getattr(app, "_float_macd_tf_labels", ["1分钟", "5分钟"]),
        width=220,
    )
    app._option_row(
        hold_box,
        "MACD参数组合",
        app.float_macd_combo_var,
        getattr(app, "_float_macd_combo_labels", ["12 / 26 / 9（常用）"]),
        width=220,
    )

    def _sync_macd_combo(*_args: object) -> None:
        """下拉常见组合时，同步改下面三格数字。"""
        from src.strategy.float_hold import parse_macd_combo

        fast, slow, signal = parse_macd_combo(app.float_macd_combo_var.get())
        app.float_macd_fast_var.set(str(fast))
        app.float_macd_slow_var.set(str(slow))
        app.float_macd_signal_var.set(str(signal))

    if not getattr(app, "_float_macd_combo_traced", False):
        app.float_macd_combo_var.trace_add("write", _sync_macd_combo)
        app._float_macd_combo_traced = True
    param_entry(
        app,
        hold_box,
        "MACD快线",
        app.float_macd_fast_var,
        hint="出厂 12。选上面常见组合会改这里，也可手填",
    )
    param_entry(
        app,
        hold_box,
        "MACD慢线",
        app.float_macd_slow_var,
        hint="出厂 26，须大于快线",
    )
    param_entry(
        app,
        hold_box,
        "MACD平滑",
        app.float_macd_signal_var,
        hint="出厂 9，柱子用这一根平滑",
    )
    app._option_row(
        hold_box,
        "定向均线对",
        app.float_ema_pair_var,
        getattr(app, "_float_ema_pair_labels", ["EMA5 / EMA20"]),
        width=220,
    )
    switch_row(
        app,
        hold_box,
        "大方向过滤",
        app.float_trend_align_var,
        hint="只给箱子/MACD 用。默认方向已经顺着 EMA60/120，不必再开。出厂关",
    )
    param_entry(
        app,
        hold_box,
        "最小倾角(度)",
        app.float_trend_slope_var,
        hint="出厂 0=只看均线谁在上面。填大了会很少开仓",
    )
    param_entry(
        app,
        hold_box,
        "斜率回看根数",
        app.float_trend_lookback_var,
        hint="看 EMA60、EMA120 自己往回几根，比均线现在和当时差多少。不是 K 线实体的斜。出厂 5",
    )
    param_entry(
        app,
        hold_box,
        "观察几分钟再定方向",
        app.float_eth_var,
        hint="对「观察结束顺大方向」、昨日密集区和箱子有效。MACD循环不等。出厂 60",
    )
    param_entry(
        app,
        hold_box,
        "密集区最宽点数",
        app.float_cluster_width_var,
        hint="上一交易日被反复摸到的窄价格带，最高减最低不超过这么多。出厂 10。只给「昨日密集区」用",
    )
    param_entry(
        app,
        hold_box,
        "日盘开仓截止",
        app.float_day_cutoff_var,
        hint="过了这个钟点就停止开日盘新仓。出厂 14:45，好让观察结束后还能开",
    )
    param_entry(
        app,
        hold_box,
        "夜盘开仓截止",
        app.float_night_cutoff_var,
        hint="过了这个钟点就停止开夜盘新仓。出厂 22:50",
    )
    param_entry(
        app,
        hold_box,
        "止损几倍开盘前ATR",
        app.float_stop_atr_var,
        hint="从进场价往回留这么多倍开盘前波动。出厂 1.5。尺子不含今天早盘",
    )
    param_entry(
        app,
        hold_box,
        "跳空超过几倍ATR不开",
        app.float_skip_gap_var,
        hint="缺口太大整段不做。出厂关（0）",
    )
    param_entry(
        app,
        hold_box,
        "跳空超过几倍ATR只顺向",
        app.float_gap_align_var,
        hint="缺口够大时，高开只做多、低开只做空。出厂关（0）",
    )
    param_entry(
        app,
        hold_box,
        "箱体超过几倍ATR不开",
        app.float_max_box_var,
        hint="箱子太宽不开。出厂关（0），默认方向不用箱子",
    )
    param_entry(
        app,
        hold_box,
        "急破超过几倍ATR不追",
        app.float_max_chase_var,
        hint="收盘离破位太远就等回抽。出厂关（0），默认方向不用箱子",
    )
    param_entry(
        app,
        hold_box,
        "浮盈几倍ATR后保本",
        app.float_be_atr_var,
        hint="收盘浮盈达到开盘前波动的这么多倍，止损提到开仓价。出厂 2，0=不做保本",
    )

    trend_box = detail_panel(
        app,
        "trend_follow",
        "趋势跟随",
        "收盘突破前 N 根高低点跟上；默认可隔夜。",
    )
    param_entry(
        app,
        trend_box,
        "趋势进场根数",
        app.trend_entry_bars_var,
        hint="收盘突破前多少根的高低点",
    )
    param_entry(
        app,
        trend_box,
        "趋势止损倍数",
        app.trend_atr_mult_var,
        hint="保护止损用几倍波动",
    )

    def _pack_trend_filter(parent: ctk.CTkFrame, section: str, note: str) -> None:
        """趋势策略自己的 ADX/ATR 过滤，数字写在该策略小节。"""
        vars_ = app._trend_filter_vars[section]
        ctk.CTkLabel(
            parent,
            text="本策略行情过滤",
            font=T.FONT_BODY,
            text_color=T.ACCENT,
            anchor="w",
        ).pack(fill="x", padx=4, pady=(10, 0))
        note_block(parent, note, pady=(0, 4))
        switch_row(
            app,
            parent,
            "启用趋势过滤",
            vars_["enabled"],
            hint="开=ADX 太低或波动压缩就不开新仓；关=这一套不再看这层。已有持仓仍按止损走",
        )
        param_entry(app, parent, "ADX周期", vars_["adx_period"], hint="默认 14")
        param_entry(app, parent, "ATR周期", vars_["atr_period"], hint="默认 14")
        param_entry(app, parent, "ATR回看根数", vars_["lookback"], hint="默认 60，算 ATR 处于历史几分位")
        param_entry(app, parent, "ADX趋势门槛", vars_["adx_flat"], hint="默认 20，低于它算没趋势")
        param_entry(app, parent, "ATR低分位(%)", vars_["atr_low_pct"], hint="默认 20，填百分数")

    _pack_trend_filter(
        trend_box,
        "trend_follow",
        "海龟怕假突破。没趋势或波动缩着时，突破容易是假的。默认关，需要再开。",
    )

    st_box = detail_panel(
        app,
        "supertrend",
        "SuperTrend 趋势",
        "ATR 通道翻转开仓；默认可隔夜。",
    )
    param_entry(app, st_box, "ST的ATR根数", app.st_atr_period_var, hint="算通道用几根K线")
    param_entry(app, st_box, "ST通道倍数", app.st_multiplier_var, hint="通道半宽几倍波动，常用 3")
    _pack_trend_filter(
        st_box,
        "supertrend",
        "震荡里通道会来回翻。没趋势时过滤能少挨鞭子。默认关，需要再开。",
    )

    dual_box = detail_panel(
        app,
        "dual_ma",
        "双均线趋势",
        "短 EMA 与长 EMA 交叉；长根数须大于短根数。",
    )
    param_entry(app, dual_box, "双均线短根数", app.dual_short_var, hint="默认 20，指数均线")
    param_entry(app, dual_box, "双均线长根数", app.dual_long_var, hint="默认 60，指数均线，须大于短根数")
    switch_row(
        app,
        dual_box,
        "反向时反手",
        app.dual_reverse_var,
        hint="开=均线反向交叉就反手开新仓；关=只平仓，等下一次顺向交叉",
    )
    _pack_trend_filter(
        dual_box,
        "dual_ma",
        "震荡里交叉特别多。没趋势时过滤能少做来回。默认关，需要再开。",
    )

    macd_swing_box = detail_panel(
        app,
        "ema_swing",
        "均线波段",
        "方向均线定多空，信号三均线排好就开。建议左边信号/止损周期都用 15 分钟。多空分界线、止损、倾角可分开填根数。",
    )
    note_block(
        macd_swing_box,
        "方向选择器金叉后只做多、死叉后只做空，没再叉就不改。框里填根数，比如 21 和 122。"
        "开仓：三条信号均线按从快到慢排好，且收盘在多空分界线正确一侧。"
        "做多要收盘站在分界线上面，做空要在下面；贴着也不开。"
        "信号三根前面要比后面小，保存时会自动排好。"
        "不用 MACD 柱子。跳空错过交叉那根也可以开。倾角均线太平或斜反了不开新仓。"
        "「倾角不够就平仓」出厂关；打开后手里的仓也会平（不反手），用来对照收益。"
        "多空分界线、止损、倾角出厂都是 120，可以分开填。"
        "空仓时日盘/夜盘收盘前若干分钟不再开（出厂 30，可改；午休不算）。"
        "仓位、信号周期都用配置页公共项。这套适合更大级别：建议信号和止损都用 15 分钟，5 分钟容易被影线扫止损。"
        "主力连续换月时，旧仓还在会在新主力同方向移过去。"
        "任何合约都不进入自己的交割月：交割月前一个月的日盘收盘必须平仓，之后改下一手。",
        pady=(0, 4),
    )
    ema_num_row(
        app,
        macd_swing_box,
        "方向选择器",
        [app.macd_swing_dir_fast_var, app.macd_swing_dir_slow_var],
        hint="出厂 60 / 120。前面要比后面小",
    )
    ema_num_row(
        app,
        macd_swing_box,
        "信号发生器",
        [app.macd_swing_sig_a_var, app.macd_swing_sig_b_var, app.macd_swing_sig_c_var],
        hint="出厂 5 / 10 / 30。从快到慢",
    )
    ema_num_row(
        app,
        macd_swing_box,
        "多空分界线",
        [app.macd_swing_side_ema_var],
        hint="做多要站上面、做空要在下面。出厂 120",
    )
    ema_num_row(
        app,
        macd_swing_box,
        "止损均线",
        [app.macd_swing_stop_ema_var],
        hint="开仓后止损跟着它走。出厂 120",
    )
    ema_num_row(
        app,
        macd_swing_box,
        "倾角均线",
        [app.macd_swing_slope_ema_var],
        hint="只看这根斜不斜。出厂 120",
    )
    param_entry(
        app,
        macd_swing_box,
        "最小倾角(度)",
        app.macd_swing_slope_var,
        hint="看倾角均线自己斜不斜，不是 K 线的斜。出厂 3。填 0 关掉",
    )
    param_entry(
        app,
        macd_swing_box,
        "斜率回看根数",
        app.macd_swing_slope_lookback_var,
        hint="用最近几根算倾角均线倾角。出厂 20",
    )
    switch_row(
        app,
        macd_swing_box,
        "倾角不够就平仓",
        app.macd_swing_slope_exit_var,
        hint="关=只拦新开仓。开=倾角不够也平掉已有仓，不反手。用来对照收益。出厂关",
    )
    param_entry(
        app,
        macd_swing_box,
        "尾盘不开仓(分钟)",
        app.macd_swing_close_guard_var,
        hint="空仓时收盘前这么多分钟不开。出厂 30。填 0 关掉。午休不算",
    )

    boll_box = detail_panel(
        app,
        "bollinger",
        "布林带突破",
        "中轨是 EMA；只做顺势突破，不做轨道反转；跌回中轨离场。",
    )
    param_entry(app, boll_box, "布林轨道周期", app.boll_window_var, hint="默认 20；中轨用 EMA")
    param_entry(app, boll_box, "标准差倍数", app.boll_dev_var, hint="默认 2.0；越大通道越宽、信号越少")
    param_entry(app, boll_box, "止损倍数", app.boll_atr_mult_var, hint="开仓价外几倍 ATR，默认 1.5")
    _pack_trend_filter(
        boll_box,
        "bollinger",
        "通道缩窄时假突破多。波动压缩时建议拦一下。默认关，需要再开。",
    )

    kb_box = detail_panel(
        app,
        "kelly_break",
        "凯利突破",
        "收盘突破最近 N 根高低点开仓；ATR 只算止损，固定盈亏比止盈。手数用左边的凯利/每笔风险。",
    )
    note_block(
        kb_box,
        "不预判方向：做多要收盘站上前 N 根最高价，做空要跌破前 N 根最低价，影线刺出不算。\n"
        "止损放在进场价外 K 倍 ATR；止盈距离 = 止损距离 × 盈亏比，收盘到价才平。\n"
        "关掉止盈就只靠止损和收盘清仓离场。是否隔夜听左边公共「收盘清仓」开关。\n"
        "这里不算手数：凯利开关和每笔风险%仍在左边公共设置里。",
        pady=(0, 4),
    )
    param_entry(app, kb_box, "突破回看根数", app.kb_lookback_var, hint="前 N 根（不含当根）的高低点，默认 20")
    param_entry(app, kb_box, "ATR周期", app.kb_atr_period_var, hint="只用来算止损距离，默认 14")
    param_entry(app, kb_box, "止损ATR倍数", app.kb_stop_atr_var, hint="进场价外几倍 ATR，默认 1.2")
    switch_row(
        app,
        kb_box,
        "固定盈亏比止盈",
        app.kb_take_profit_var,
        hint="开=到目标价就平；关=不设止盈，只靠止损和收盘清仓",
    )
    param_entry(app, kb_box, "目标盈亏比", app.kb_reward_risk_var, hint="止盈距离=止损距离×这个数，默认 2.5")
    param_entry(
        app,
        kb_box,
        "波动率下限(ATR/价)",
        app.kb_min_atr_ratio_var,
        hint="如 0.004；ATR/收盘价低于它就不开新仓。填 0 关掉",
    )

    ls_box = detail_panel(
        app,
        "liq_stop",
        "以损定开",
        "先用高低点拐点估当前周期的公共止损区；价格击穿止损区、把止损单吃掉再收回，才顺势进场。",
    )
    note_block(
        ls_box,
        "多头：最近 N 根的低点拐点聚成止损区，价格先跌破区间下沿、再收回上沿外才开多，"
        "止损放区间下沿外 K 倍 ATR。空头对称。\n"
        "只认收盘收回，不用影线预判，避免在震荡市被二次扫损；ADX 低于下限直接不做。\n"
        "止损触发方式听公共「触及/收盘确认」；是否隔夜听公共「收盘清仓」；"
        "手数仍由左边凯利/每笔风险决定。",
        pady=(0, 4),
    )
    param_entry(app, ls_box, "止损区回看根数", app.ls_zone_lookback_var, hint="用最近 N 根高低点拐点聚类，默认 20")
    param_entry(app, ls_box, "聚类容忍度", app.ls_zone_tol_var, hint="区间半宽=中枢×该值，默认 0.005 即 0.5%")
    param_entry(app, ls_box, "ATR周期", app.ls_atr_period_var, hint="进场偏移和止损都用它，默认 20")
    param_entry(app, ls_box, "ADX周期", app.ls_adx_period_var, hint="判断有没有趋势，默认 14")
    param_entry(app, ls_box, "ADX下限", app.ls_adx_min_var, hint="低于它不开新仓，默认 20；填 0 只关 ADX 门槛")
    param_entry(app, ls_box, "击穿回看根数", app.ls_break_window_var, hint="最近几根内击穿过止损区才算，默认 3")
    param_entry(app, ls_box, "进场偏移ATR", app.ls_entry_offset_var, hint="收回区间外再走几倍 ATR 才进，默认 0.5")
    param_entry(app, ls_box, "止损外侧ATR", app.ls_stop_atr_var, hint="止损放在止损区外侧几倍 ATR，默认 1.0")
    switch_row(
        app,
        ls_box,
        "固定盈亏比止盈",
        app.ls_take_profit_var,
        hint="开=到目标价就平；关=不设止盈，只靠止损和收盘清仓",
    )
    param_entry(app, ls_box, "目标盈亏比", app.ls_reward_risk_var, hint="止盈距离=止损距离×这个数，默认 2.0")
    param_entry(app, ls_box, "放量倍数", app.ls_vol_mult_var, hint="击穿窗口最大量≥前 N 根均量×此值；0=不看量")

    sar_box = detail_panel(
        app,
        "sar_trend",
        "SAR波段趋势",
        "SAR 翻转 + EMA 方向过滤；SAR 线本身就是移动止损。",
    )
    param_entry(app, sar_box, "方向过滤均线", app.sar_ma_filter_var, hint="默认 20 根 EMA；站上方只做多、下方只做空")
    param_entry(app, sar_box, "加速步长", app.sar_af_step_var, hint="默认 0.02；越大翻转越快、也越容易被打")
    _pack_trend_filter(
        sar_box,
        "sar_trend",
        "震荡里 SAR 容易来回翻。没趋势时过滤能少挨扫。默认关，需要再开。",
    )

    def _pack_cta_shared(parent: ctk.CTkFrame) -> None:
        """多因子加权打分的因子参数，信号周期仍用左边公共项。"""
        cta_param(app, parent, "总分阈值", app.multi_threshold_var, hint="默认 0.6；加权打分过这条才开")
        cta_param(app, parent, "短均线根数", app.multi_ma_short_var, hint="默认 20 根 EMA；跟左边信号周期走，不是写死 5 分钟")
        cta_param(app, parent, "长均线根数", app.multi_ma_long_var, hint="默认 60 根 EMA，须大于短均线")
        cta_param(app, parent, "唐奇安通道根数", app.multi_channel_var, hint="默认 20；比前 20 根高低点")
        cta_param(app, parent, "止损倍数", app.multi_atr_mult_var, hint="开仓价外几倍 ATR，默认 2；只往有利方向收")
        cta_param(app, parent, "因子分歧上限", app.multi_disagree_var, hint="四个因子分数差太大（默认 0.7）就不开；受下面「过滤因子分歧」开关管")
        cta_switch(
            app,
            parent,
            "启用 SAR 因子",
            app.multi_use_sar_var,
            hint="关=不算 SAR，权重摊给均线/唐奇安/布林",
        )

    def _pack_cta_filter(
        parent: ctk.CTkFrame,
        kind: str,
        *,
        show_score: bool,
        show_size: bool,
        note: str,
    ) -> None:
        """每套多因子自己的行情闸门。"""
        vars_ = app._cta_filter_vars[kind]
        ctk.CTkLabel(
            parent,
            text="本策略行情过滤",
            font=T.FONT_BODY,
            text_color=T.ACCENT,
            anchor="w",
        ).pack(fill="x", padx=4, pady=(10, 0))
        note_block(parent, note, pady=(0, 4))
        cta_switch(
            app,
            parent,
            "启用行情过滤",
            vars_["enabled"],
            hint="开=按下面几条拦新开仓；关=这一套自己不再看行情",
        )
        cta_switch(app, parent, "过滤ADX", vars_["use_adx"], hint="开=没趋势就停手；弱趋势还要加严")
        cta_switch(app, parent, "过滤ATR", vars_["use_atr"], hint="开=波动缩在最低分位就不开")
        cta_switch(app, parent, "过滤因子分歧", vars_["use_disagree"], hint="开=四个因子打架就不开")
        cta_param(app, parent, "ADX没趋势门槛", vars_["adx_flat"], hint="默认 20，低于它不开")
        cta_param(app, parent, "ADX明确趋势", vars_["adx_trend"], hint="默认 25，夹在中间算弱趋势")
        cta_param(app, parent, "ATR回看根数", vars_["atr_lookback"], hint="默认 60，按当前信号周期的根数算")
        cta_param(app, parent, "ATR低分位(%)", vars_["atr_low_pct"], hint="默认 20，填百分数")
        if show_score:
            cta_param(
                app,
                parent,
                "弱趋势高分门槛",
                vars_["weak_score"],
                hint="ADX 20~25 时总分至少这么高才开，默认 0.75",
            )
        if show_size:
            cta_param(
                app,
                parent,
                "弱趋势仓位成数",
                vars_["weak_size"],
                hint="ADX 20~25 时按几成风控手数，默认 0.5；1 手账户仍是 1 手",
            )

    multi_box = detail_panel(
        app,
        "multi_factor",
        "多因子CTA（加权打分）",
        "完整版固定权重。日线收盘相对 EMA 定方向，信号周期跟左边公共项；SAR 开关只对这一族生效。平仓后分摊盈亏，导出写入衰减表。",
    )
    _pack_cta_shared(multi_box)
    _pack_cta_filter(
        multi_box,
        "multi_factor",
        show_score=True,
        show_size=True,
        note="加权打分：ADX 太低或波动太缩就不开；弱趋势只收更高总分，并且仓位打成数。",
    )

    swing_box = detail_panel(
        app,
        "swing_trend",
        "长线趋势加减仓",
        "大周期定方向 + 信号周期回调；可加可减。回调跟左边信号周期，止损跟左边止损周期。",
    )
    app._option_row(
        swing_box,
        "长线大周期",
        app.swing_scale_var,
        list(app._swing_label_by_scale.values()),
        width=260,
    )
    note_block(
        swing_box,
        "上面只选「用日线还是 60 分钟来定方向」。\n"
        "回调进出跟左边「信号周期」，止损跟左边「止损周期」。想用 60 分钟进出，把信号周期改成 60 分钟即可。",
        pady=(0, 4),
    )
    param_entry(
        app,
        swing_box,
        "长线大周期均线",
        app.swing_daily_ma_var,
        hint="大周期几根 EMA 定方向，默认 20",
    )
    param_entry(
        app,
        swing_box,
        "长线单元手数",
        app.swing_unit_var,
        hint="首开/每次加仓/每次减仓的手数",
    )
    param_entry(
        app,
        swing_box,
        "长线最多加仓",
        app.swing_max_adds_var,
        hint="浮盈后再回调，最多加几次",
    )
    param_entry(
        app,
        swing_box,
        "长线加仓门槛",
        app.swing_add_atr_var,
        hint="相对均价至少浮盈几倍波动才允许加",
    )
    param_entry(
        app,
        swing_box,
        "长线减仓伸展",
        app.swing_reduce_atr_var,
        hint="离小周期均线冲出几倍波动时先减仓",
    )
    _pack_trend_filter(
        swing_box,
        "swing_trend",
        "大周期没趋势时加减仓没意义。默认关，需要再开。",
    )

    dd_box = detail_panel(
        app,
        "dual_donchian",
        "双周期通道突破",
        "大周期通道+ADX 定方向，小周期唐奇安突破。进出跟左边信号周期，止损跟左边止损周期。",
    )
    app._option_row(dd_box, "大周期", app.dd_htf_var, list(app._htf_menu), width=160)
    note_block(
        dd_box,
        "大周期只回答允许多还是空。小周期收盘冲出前 N 根高点才做多、跌破低点才做空。\n"
        "想用资料里的周线+日线：左边信号周期选日线，这里大周期选周线。",
        pady=(0, 4),
    )
    param_entry(app, dd_box, "进场通道根数", app.dd_entry_var, hint="小周期前多少根高低点，默认 55")
    param_entry(app, dd_box, "离场通道根数", app.dd_exit_var, hint="更短的一段，默认 20")
    param_entry(app, dd_box, "大周期ADX下限", app.dd_adx_min_var, hint="低于它当震荡，不开。默认 22")
    param_entry(app, dd_box, "止损ATR倍数", app.dd_atr_mult_var, hint="开仓价外面几倍波动，默认 2.5")

    df_box = detail_panel(
        app,
        "dual_fractal",
        "双周期回调分型",
        "大周期定方向，小周期等回调里的强分型，并要 DIF 不创新低/新高。均线用指数平均。",
    )
    app._option_row(df_box, "大周期", app.df_htf_var, list(app._htf_menu), width=160)
    app._option_row(
        df_box,
        "大周期怎么定方向",
        app.df_mode_var,
        list(app._fractal_mode_labels.values()),
        width=160,
    )
    note_block(
        df_box,
        "指数均线：收盘在大周期 EMA 上方只做多。通道突破：收盘冲出大周期通道才给方向。\n"
        "小周期必须同时出现强分型和 DIF 背离才开仓。没有做缠论中枢递归。",
        pady=(0, 4),
    )
    param_entry(app, df_box, "大周期均线根数", app.df_ema_var, hint="选指数均线时用，默认 20")
    param_entry(app, df_box, "大周期ADX下限", app.df_adx_min_var, hint="低于它当震荡，不开。默认 22")
    param_entry(app, df_box, "止损ATR倍数", app.df_atr_mult_var, hint="开仓价外面几倍波动，默认 2.8")

    sbox = detail_panel(
        app,
        "swing_box",
        "长线箱体突破",
        "按左边信号周期画震荡箱子，收盘走出才顺着做。止损周期跟左边那一格。默认不收盘清仓。",
    )
    note_block(
        sbox,
        "和日内实体一样：只认收盘、开仓后箱子冻住、假突破收回箱子就走。\n"
        "周期跟左边「信号周期 / 止损周期」。想测日线就把信号周期改成日线；想测 60 分钟就改成 60 分钟。\n"
        "「箱子回看根数」是这个周期上的根数：日线 40 根大约两个月，60 分钟 40 根大约两周。\n"
        "手数仍用左边的每笔风险%。ADX 只用来挡住完全没方向的行情，不会等趋势走完再追。",
        pady=(0, 4),
    )
    param_entry(app, sbox, "箱子回看根数", app.sbox_lookback_var, hint="按信号周期计，默认 40 根")
    param_entry(app, sbox, "箱子最矮几倍ATR", app.sbox_height_atr_var, hint="太窄的箱子假突破多，默认 1.2")
    param_entry(app, sbox, "ADX下限", app.sbox_adx_min_var, hint="低于这个数当没方向，不开。默认 20。填 0 关掉")
    param_entry(app, sbox, "止损缓冲ATR", app.sbox_stop_atr_var, hint="多单止损在箱顶下面这么多倍 ATR，默认 0.5")
    param_entry(app, sbox, "跟踪ATR倍数", app.sbox_trail_atr_var, hint="浮盈够了以后，止损离新高/新低几倍 ATR，默认 2")
    param_entry(app, sbox, "几倍箱高才跟踪", app.sbox_trail_h_var, hint="浮盈达到箱子高度的几倍才开始跟踪，默认 1")

    rule_box = detail_panel(
        app,
        "rule_dsl",
        "自定义规则（DSL）",
        "规则文件由「策略编辑器」页签维护；这里只显示当前策略对应的规则。",
    )
    ctk.CTkLabel(
        rule_box,
        textvariable=app.rule_summary_var,
        font=T.FONT_SMALL,
        text_color=T.TEXT,
        wraplength=420,
        justify="left",
        anchor="w",
    ).pack(fill="x", padx=6, pady=(2, 6))
    ctk.CTkButton(
        rule_box,
        text="打开策略编辑器 →",
        width=140,
        command=app._open_rule_editor,
        fg_color=T.ACCENT,
        hover_color=T.ACCENT_HOVER,
        text_color=T.TEXT,
    ).pack(anchor="w", padx=6, pady=(0, 6))
    note_block(
        rule_box,
        "支持：开仓、离场、ATR/百分比/摆动点止损，可加移动止损、保本、限持仓根数和高周期过滤；"
        "暂不支持加减仓和反手。\n"
        "内置模板只读：dualthrust / r_breaker / turtle_adx / adx_atr_break / dual_ma_equiv。\n"
        "保存规则不会自动切换运行策略，需要时在左侧策略引擎里选。",
        pady=(0, 4),
    )

    try:
        app.engine_var.trace_add("write", app._refresh_strategy_param_panel)
    except Exception:  # noqa: BLE001
        pass
    # 窗口变宽/变窄时微调右侧说明换行，但不改变参数框目标宽度。
    try:
        split.bind("<Configure>", app._on_strategy_split_configure, add="+")
    except Exception:  # noqa: BLE001
        pass
    app._refresh_strategy_param_panel()

    # 共用设置挂在左侧列里，跟「选策略」连成一条；右侧多高也不会把这里顶下去。
    ctk.CTkLabel(
        left,
        text="—— 各策略共用的下单与仓位设置 ——",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        anchor="w",
    ).pack(anchor="w", pady=(12, 4))
    app._option_row(left, "执行通道", app.exec_var, list_executors(), width=220)
    app._option_row(
        left,
        "CTP操作账",
        app.ctp_kind_var,
        getattr(app, "ctp_kind_choices", ["仿真账号", "真实账号"]),
        width=220,
    )
    ctk.CTkLabel(
        left,
        text="仿真=仿真账号+仿真前置。真实=上面的资金账号+实盘前置。账号不用填两遍。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    app._option_row(
        left,
        "券商档案",
        app.ctp_profile_var,
        getattr(app, "ctp_profile_choices", ["default"]),
        width=220,
    )
    ctk.CTkLabel(
        left,
        text="换期货公司只改档案，不改代码。没有 ctp_profiles 时这里显示 default，按下面的旧字段运行。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    ctk.CTkLabel(
        left,
        textvariable=app.ctp_profile_note_var,
        font=T.FONT_SMALL,
        text_color=T.WARN,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    app._entry_row(
        left,
        "档案交易前置",
        app.ctp_profile_td_var,
        hint="当前档案的交易地址，例如 tcp://主机:端口",
        width=160,
    )
    app._entry_row(
        left,
        "档案行情前置",
        app.ctp_profile_md_var,
        hint="行情地址必须单独填，程序不再按交易端口推断行情端口",
        width=160,
    )
    app._entry_row(
        left,
        "档案BrokerID",
        app.ctp_profile_broker_var,
        hint="柜台给的 BrokerID，例如 8080 / 9999",
        width=160,
    )
    app._entry_row(
        left,
        "档案资金账号",
        app.ctp_profile_user_var,
        hint="仿真档案填仿真账号；真实档案留空时沿用上面的资金账号",
        width=160,
    )
    app._entry_row(
        left,
        "档案密码变量",
        app.ctp_profile_pwd_env_var,
        hint="环境变量名，默认 CTP_PASSWORD；真实档案默认 CTP_LIVE_PASSWORD",
        width=160,
    )
    app._entry_row(
        left,
        "档案认证码变量",
        app.ctp_profile_auth_env_var,
        hint="环境变量名，默认 CTP_AUTH_CODE；该柜台不需要认证码时留空",
        width=160,
    )
    ctk.CTkButton(
        left,
        text="测试交易前置连通",
        width=160,
        command=app._test_ctp_front,
        fg_color=T.ACCENT,
        hover_color=T.ACCENT_HOVER,
        text_color=T.TEXT,
    ).pack(anchor="w", pady=(2, 8))
    app._entry_row(
        left,
        "开仓滑点上限(×ATR)",
        app.max_slip_var,
        hint="信号价和现价差超过它就不开这一根，等下一根；0=关闭",
        width=160,
    )
    app._entry_row(
        left,
        "开仓滑点上限(点)",
        app.max_slip_points_var,
        hint="直接写死多少点；0=不用。和上一行同时开，取更严的那个",
        width=160,
    )
    from src.strategy.session_scope import SCOPE_LABELS

    app._option_row(
        left,
        "交易时段",
        app.session_scope_var,
        list(SCOPE_LABELS.values()),
        width=200,
    )
    ctk.CTkLabel(
        left,
        text="全时段 / 只做日盘 / 只做夜盘：只决定能不能开新仓，已经拿着的仓仍按止损和收盘清仓走。"
        "夜日连续（跳空小）：只建议隔夜跳空不大的品种用。日内实体会把夜盘拿到日盘 14:55，"
        "跳空大时最大回撤会明显变大。燃油这类常大幅跳空的，继续用全时段，让夜盘自己清仓。",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=420,
        justify="left",
    ).pack(anchor="w", pady=(0, 6))
    app._entry_row(
        left,
        "扫描间隔秒",
        app.scan_var,
        hint="一轮扫描之间隔多久，5~600 秒；止损发现快慢跟着它",
        width=160,
    )
    app._pack_watch_editor(left)
    app._option_row(left, "仓位模式", app.sizing_mode_var, ["risk_pct", "fixed"], width=220)
    app._entry_row(
        left,
        "账户权益(元)",
        app.capital_var,
        hint="当前账户余额，要改去账户分析",
        readonly=True,
        width=160,
    )
    app._entry_row(
        left,
        "兜底每手保证金(元)",
        app.margin_var,
        hint="品种表没填保证金比例时用这个固定金额；填了比例就按价格×每手数量×比例，0=不限制",
        width=160,
    )
    # 品种保证金+乘数只认 Excel，不再手打长串，避免和配置文件各改各的对不齐。
    app._pack_margin_table_row(left)
    # 每笔风险%、仓位上限% 放在下面「风控与资金管理」，和日亏损等放一起。
    app._entry_row(
        left,
        "固定手数(后备)",
        app.volume_var,
        hint="仓位模式=fixed 时用；或缺止损价时",
        width=160,
    )
    app._entry_row(
        left,
        "清仓时刻",
        app.flatten_var,
        hint="如 14:55,22:55，开关关掉时不使用",
        width=160,
    )
    flat_row = ctk.CTkFrame(left, fg_color="transparent")
    flat_row.pack(fill="x", pady=8)
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
        variable=app.flatten_at_close_var,
        progress_color=T.OK,
        fg_color=T.BORDER,
    ).pack(side="left")
    ctk.CTkLabel(
        flat_row,
        text="开=到点强平；关=仓可以留过午盘、夜盘和周末，只靠止损。周五临收仍不开新仓",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=280,
        justify="left",
        anchor="w",
    ).pack(side="left", padx=(10, 0))

    # 模拟开关只放在运行台，避免两处开关互相覆盖。

    # 收盘后关机
    sd_row = ctk.CTkFrame(left, fg_color="transparent")
    sd_row.pack(fill="x", pady=8)
    ctk.CTkLabel(
        sd_row,
        text="收盘后关机",
        font=T.FONT_BODY,
        text_color=T.TEXT_DIM,
        width=140,
        anchor="w",
    ).pack(side="left", padx=(0, 8))
    ctk.CTkSwitch(
        sd_row,
        text="",
        variable=app.auto_shutdown_var,
        progress_color=T.OK,
        fg_color=T.BORDER,
    ).pack(side="left")
    ctk.CTkLabel(
        sd_row,
        text="开=当日最后清仓后再关电脑（有夜盘会等到夜里；倒计时内可 shutdown /a 取消）",
        font=T.FONT_SMALL,
        text_color=T.TEXT_DIM,
        wraplength=280,
        justify="left",
        anchor="w",
    ).pack(side="left", padx=(10, 0))
