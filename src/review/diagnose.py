"""
缺陷诊断与改进建议
功能：根据当天盘面、信号质量、信号/实盘差距，标出策略可能的问题，并给出可执行的下一步。
原理：
  优秀交易软件的复盘不只是「今天赚了多少」，而是把结果归因到规则缺陷：
  止损过紧、利润回吐、有行情无信号、信号与执行脱节等。
  每一条建议都指向本程序已有页面（历史回测 / 策略优化 / 账户分析），不当黑盒玄学。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.review.gap import GapReport
from src.review.quality import QualityReport, SignalTrade
from src.review.replay import StrategyReplay
from src.review.technical import TechnicalView


@dataclass
class Diagnosis:
    """缺陷标签 + 人话说明 + 下一步。"""

    defects: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)


def diagnose(
    *,
    technicals: list[TechnicalView],
    replay: StrategyReplay,
    quality: QualityReport,
    gap: GapReport,
) -> Diagnosis:
    """综合当天材料，输出缺陷清单和改进建议。"""
    out = Diagnosis()
    trades = quality.trades

    if replay.engine is None:
        out.lines.append("你选了只看盘面，不做策略缺陷诊断。若要指导改参数，请在「策略对照」里选一套策略再生成。")
        out.improvements.append("选好策略后重新生成复盘，再带着第八节去「策略优化」页调检验比例或滚动前进。")
        return out

    # —— 缺陷检测 ——
    _check_giveback(out, trades)
    _check_stop_tight(out, trades)
    _check_missed_move(out, technicals, replay)
    _check_overtrade(out, trades)
    _check_adverse_direction(out, technicals, replay)
    _check_gap(out, gap)
    _check_watch_cluster(out, replay)

    if not out.defects:
        out.lines.append("今天没有标出明显规则缺陷。仍建议把第五节质量数字记下来，连续几天对照，比单日结论更可靠。")
    else:
        out.lines.append(f"标出 {len(out.defects)} 类问题：{'、'.join(out.defects)}。")
        out.lines.append("下面每条都是「可能的规则问题」，要用后面几天的复盘或历史回测再验证，不要凭一天就改死参数。")

    # 把已收集的缺陷说明（检测函数写入的细节）保证有序号感
    if out.improvements:
        out.lines.append("")
        out.lines.append("建议的下一步（按优先级）：")
        for i, tip in enumerate(out.improvements[:6], 1):
            out.lines.append(f"  {i}. {tip}")
    return out


def _check_giveback(out: Diagnosis, trades: list[SignalTrade]) -> None:
    """利润回吐：MFE 不小，但捕获率很低。"""
    bad = [
        t
        for t in trades
        if (not t.still_open)
        and t.mfe >= 8
        and t.capture_ratio is not None
        and t.capture_ratio < 0.30
        and t.pnl_points < t.mfe * 0.5
    ]
    if not bad:
        return
    out.defects.append("利润回吐过多")
    out.lines.append(
        f"有 {len(bad)} 笔曾经浮盈不错（MFE≥8点），最终却只留下不到三成。"
        "常见于出场太晚、或没有把止损上移锁住利润。"
    )
    out.improvements.append(
        "到「历史回测」用同一策略看出场原因分布；若大量是「收到回撤才平」，可在策略优化里收紧出场相关参数后再做滚动检验。"
    )


def _check_stop_tight(out: Diagnosis, trades: list[SignalTrade]) -> None:
    """
    止损过紧的弱证据：亏损接近止损距离，且 MAE≈风险，说明几乎一碰到止损就出。
    真正的「止损后价格反转大赚」需要出场后继续扫K，这里用「亏满止损 + 当日波动更大」近似提醒。
    """
    suspects = []
    for t in trades:
        if t.still_open or t.r_multiple is None or t.risk_points <= 0:
            continue
        if t.r_multiple > -0.85:
            continue
        if t.mae >= t.risk_points * 0.9 and t.mfe < t.risk_points * 0.5:
            suspects.append(t)
    if not suspects:
        return
    out.defects.append("止损可能过紧")
    out.lines.append(
        f"有 {len(suspects)} 笔几乎贴着止损出场，持仓期间顺风也很小。"
        "可能是止损距离小于正常噪音，也可能是进场位置本身不好。"
    )
    out.improvements.append(
        "在「策略优化」里不要只盯净盈亏：看检验段胜率和回撤。若训练段很好、检验段止损频繁，优先放宽止损相关格点或换进场过滤，而不是加仓。"
    )


def _check_missed_move(
    out: Diagnosis,
    technicals: list[TechnicalView],
    replay: StrategyReplay,
) -> None:
    """有行情无信号：盘面偏强/偏弱且有针，但策略全段观望。"""
    if replay.opened:
        return
    strong = any(v.has_bars and v.bias in ("up", "down") and "震荡" not in (v.character or "") for v in technicals)
    pins = sum(v.pin_count for v in technicals)
    if not strong and pins == 0:
        return
    out.defects.append("有行情无信号")
    why = replay.main_watch_reason or "未留下主要观望原因"
    out.lines.append(
        f"盘面有方向或针形（针 {pins} 根），策略却整段观望。最常卡在：{why}。"
        "这不一定是坏事——过滤假突破正是策略的工作；但若连续多天都错过单边，过滤可能过严。"
    )
    out.improvements.append(
        "把第四节出现最多的观望原因记下来，去「策略优化」只动与该原因相关的参数（例如时段、确认条件），"
        "用滚动前进看检验段会不会突然变差。"
    )


def _check_overtrade(out: Diagnosis, trades: list[SignalTrade]) -> None:
    if len(trades) < 4:
        return
    out.defects.append("当日开仓偏多")
    out.lines.append(
        f"同一窗口重放开了 {len(trades)} 次。日内策略若频繁翻炒，滑点和误点同花顺的风险都会上升。"
    )
    out.improvements.append(
        "检查配置里的「开仓最小间隔」和每天最多开仓；到交叉测试页也可看同参数在其它品种是否同样密集。"
    )


def _check_adverse_direction(
    out: Diagnosis,
    technicals: list[TechnicalView],
    replay: StrategyReplay,
) -> None:
    biases = [v.bias for v in technicals if v.has_bars]
    if not biases or not replay.opened:
        return
    tape = "up" if all(b == "up" for b in biases) else "down" if all(b == "down" for b in biases) else "mixed"
    if tape == "mixed":
        return
    if replay.direction == "long" and tape == "down":
        out.defects.append("方向与收盘相悖")
        out.lines.append("策略做多，但几段盘面收势偏弱。可能是反转针做对了局部，也可能是逆势单最终被收盘打脸。")
        out.improvements.append("对照第五节该笔的 MAE/MFE：若一直浮亏，优先检查做多过滤；若先赚后吐，看出场。")
    elif replay.direction == "short" and tape == "up":
        out.defects.append("方向与收盘相悖")
        out.lines.append("策略做空，但几段盘面收势偏强。同上，用质量指标区分「局部反转成功」和「逆势扛单」。")
        out.improvements.append("对照第五节该笔的 MAE/MFE，并与账户分析里真实盈亏是否同向。")


def _check_gap(out: Diagnosis, gap: GapReport) -> None:
    if gap.alignment == "signal_only":
        out.defects.append("信号与实盘脱节")
        out.lines.append("重放有信号、挂机无记录：复盘发现的「策略行为」还没有在实盘闭环里验证。")
        out.improvements.append("先确认当天是否启动常驻、是否模拟通道；再用运行页下单模拟器测一次点击，不要先改策略参数。")
    elif gap.alignment == "actual_only":
        out.defects.append("信号与实盘脱节")
        out.lines.append("挂机有单、重放无信号：今天的复盘参数可能和挂机当时不一致，或管的是隔夜老仓。")
        out.improvements.append("到配置页确认当前策略/周期与挂机一致；隔夜仓以账户分析和同花顺持仓为准。")
    elif "笔数偏差大" in gap.notes:
        out.defects.append("执行笔数偏差")
        out.lines.append("信号次数和实际发单次数差得多，优先查风控拦截和窗口健康，而不是先怪开仓规则。")
        out.improvements.append("阅读第五节事件里「被风控拦住 / 行情太旧」的条目；必要时降低扫描间隔或检查期货通窗口。")


def _check_watch_cluster(out: Diagnosis, replay: StrategyReplay) -> None:
    """观望原因过度集中在单一条件时，提醒这可能是瓶颈参数。"""
    if replay.opened or not replay.main_watch_reason:
        return
    # 已经在 missed_move 提过类似建议时仍可补充「瓶颈」标签
    if "有行情无信号" in out.defects:
        return
    if "K线不足" in replay.main_watch_reason or "没有" in replay.main_watch_reason:
        return
    out.defects.append("观望原因过度集中")
    out.lines.append(
        f"观望时几乎总是同一类原因：{replay.main_watch_reason}。"
        "说明当天过滤条件里有一个开关几乎一直关着，值得单独拿去优化网格里试。"
    )
    out.improvements.append(
        "在策略优化页只放开与该原因相关的参数范围，其它参数锁死，避免一次改太多导致说不清是谁起作用。"
    )
