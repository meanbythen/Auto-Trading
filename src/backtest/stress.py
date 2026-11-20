"""
回测压力测试
功能：滑点加压和参数扰动。都用同一套回测，只改一个条件，看绩效会不会掉下门槛。
原理：
  - 滑点压力：在当前滑点上再加 N 跳重跑一遍，看盈利因子还剩多少。
  - 参数扰动：把当前策略的数值参数各按 ±10% 动一下，任何一点都不许出现断崖。
这两项只在历史回测和策略优化里跑；交叉测试组合太多，不在这里跑。
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from loguru import logger

from src.backtest.engine import run_backtest
from src.backtest.evaluation import (
    EvaluationReport,
    EvaluationThresholds,
    apply_stress,
    evaluate_backtest,
)

Runner = Callable[..., Any]


def _runner_call(
    runner: Runner,
    cfg: dict[str, Any],
    *,
    symbol: str,
    start_date: str | None,
    end_date: str | None,
    engine: str | None,
    chain_capital: bool | None,
) -> Any:
    """统一调用回测。换月回测由调用方包一层 runner 传进来。"""
    return runner(
        cfg,
        symbol=symbol,
        start_date=start_date or None,
        end_date=end_date or None,
        engine=engine,
        chain_capital=chain_capital,
    )


def _base_slippage(cfg: dict[str, Any]) -> float:
    bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
    try:
        return max(0.0, float(bt.get("slippage_ticks", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def run_slippage_stress(
    cfg: dict[str, Any],
    *,
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    extra_ticks: float = 1.0,
    chain_capital: bool | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """
    在当前滑点上再加 extra_ticks 跳重跑一遍。

    返回里 error 非空表示这次没测成（例如行情拉不到），评估里会记「—」。
    """
    base = _base_slippage(cfg)
    stress = base + max(0.0, float(extra_ticks or 0))
    out: dict[str, Any] = {
        "ok": False,
        "base_ticks": base,
        "stress_ticks": stress,
        "profit_factor": None,
        "net_pnl": None,
        "trades": 0,
        "error": "",
    }
    local = copy.deepcopy(cfg)
    bt = local.setdefault("backtest", {})
    bt["slippage_ticks"] = stress
    fn = runner or run_backtest
    try:
        result = _runner_call(
            fn,
            local,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            engine=engine,
            chain_capital=chain_capital,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("滑点压力测试没跑成 {}: {}", symbol, exc)
        out["error"] = str(exc)
        return out
    perf = getattr(result, "performance", None)
    if perf is None:
        out["error"] = "回测没有返回绩效"
        return out
    out.update(
        {
            "ok": True,
            "profit_factor": float(getattr(perf, "profit_factor", 0) or 0),
            "net_pnl": float(getattr(perf, "net_pnl", 0) or 0),
            "trades": int(getattr(perf, "trades", 0) or 0),
        }
    )
    return out


def _engine_box(cfg: dict[str, Any], engine: str | None) -> dict[str, Any]:
    st = cfg.get("strategy") if isinstance(cfg.get("strategy"), dict) else {}
    name = str(engine or st.get("engine") or "").strip()
    if not name:
        return {}
    # 引擎名和参数节不一定同名：macd_swing 的参数写在 ema_swing 这一节。
    # 以前拿引擎名直接找，找不到就返回空，压力测试直接跳过。
    from src.strategy.factory import param_section

    section = param_section(name)
    box = st.get(section)
    return box if isinstance(box, dict) else {}


def perturb_param_set(
    cfg: dict[str, Any],
    param_keys: list[str] | None = None,
    *,
    step_pct: float = 0.10,
    max_points: int = 4,
) -> list[tuple[str, float, dict[str, Any]]]:
    """
    挑数值参数，各做上下扰动。

    只动当前策略自己的数值参数，布尔、字符串、日期都不碰；整数参数至少留 1。
    返回 [(参数名, 新值, 改动字典), ...]，最多 max_points 个点。
    """
    box = _engine_box(cfg, None)
    if not box:
        return []
    step = max(0.0, float(step_pct or 0))
    if step <= 0:
        return []
    wanted = [str(key) for key in (param_keys or []) if str(key)]
    keys: list[str] = []
    for key in wanted:
        if key in box and _is_number(box.get(key)) and key not in keys:
            keys.append(key)
    for key in box:
        if len(keys) >= 2:
            break
        if key not in keys and _is_number(box.get(key)):
            keys.append(key)

    points: list[tuple[str, float, dict[str, Any]]] = []
    for key in keys[:2]:
        current = box.get(key)
        for sign in (1.0, -1.0):
            if len(points) >= max_points:
                return points
            raw = float(current) * (1.0 + sign * step)
            value: float | int
            if isinstance(current, int) and not isinstance(current, bool):
                value = max(1, int(round(raw)))
                if value == int(current):
                    continue
            else:
                value = round(raw, 6)
                if abs(value - float(current)) < 1e-9:
                    continue
            points.append((key, value, {key: value}))
    return points


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _apply_patch(cfg: dict[str, Any], patch: dict[str, Any], engine: str | None) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    st = out.setdefault("strategy", {})
    name = str(engine or st.get("engine") or "").strip()
    box = st.get(name) if name else None
    if not isinstance(box, dict):
        box = st.setdefault(name, {}) if name else st
    box.update(patch)
    return out


def run_perturbation(
    cfg: dict[str, Any],
    *,
    symbol: str,
    param_keys: list[str] | None = None,
    base_pf: float = 0.0,
    thresholds: EvaluationThresholds | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    chain_capital: bool | None = None,
    runner: Runner | None = None,
    on_step: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    参数往两边各动一点，逐点回测。

    cliff=True 表示有扰动点净亏，或盈利因子跌破
    max(perturbation_pf_min, perturbation_pf_ratio × 基准PF)。
    """
    th = thresholds or EvaluationThresholds()
    points = perturb_param_set(
        cfg,
        param_keys,
        step_pct=th.perturbation_step_pct,
    )
    out: dict[str, Any] = {
        "ok": False,
        "cliff": False,
        "points": [],
        "points_count": 0,
        "worst_pf": None,
        "error": "",
    }
    if not points:
        out["error"] = "没有可扰动的数值参数"
        return out
    fn = runner or run_backtest
    floor = max(th.perturbation_pf_min, th.perturbation_pf_ratio * float(base_pf or 0))
    cliff = False
    worst = float("inf")
    details: list[dict[str, Any]] = []
    for index, (key, value, patch) in enumerate(points, start=1):
        if cancel is not None and cancel():
            out["error"] = "已取消"
            break
        if on_step is not None:
            on_step(index, len(points), f"{key}={value:g}")
        local = _apply_patch(cfg, patch, engine)
        try:
            result = _runner_call(
                fn,
                local,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                engine=engine,
                chain_capital=chain_capital,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("参数扰动 {}={} 没跑成: {}", key, value, exc)
            out["error"] = str(exc)
            continue
        perf = getattr(result, "performance", None)
        if perf is None:
            continue
        pf = float(getattr(perf, "profit_factor", 0) or 0)
        net = float(getattr(perf, "net_pnl", 0) or 0)
        worst = min(worst, pf)
        if net <= 0 or pf < floor:
            cliff = True
        details.append(
            {
                "param": key,
                "value": value,
                "profit_factor": round(pf, 4),
                "net_pnl": round(net, 2),
                "trades": int(getattr(perf, "trades", 0) or 0),
            }
        )
    out["points"] = details
    out["points_count"] = len(details)
    out["cliff"] = cliff
    out["worst_pf"] = round(worst, 4) if details else None
    out["ok"] = bool(details) and not cliff and not out["error"]
    return out


def evaluate_with_stress(
    cfg: dict[str, Any],
    result: Any,
    *,
    report: EvaluationReport | None = None,
    symbol: str,
    engine: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    chain_capital: bool | None = None,
    param_keys: list[str] | None = None,
    thresholds: EvaluationThresholds | None = None,
    runner: Runner | None = None,
    on_start: Callable[[], None] | None = None,
) -> tuple[EvaluationReport | None, dict[str, Any], dict[str, Any], str]:
    """
    对一次回测结果做完整评估：基础检查 + 滑点压力 + 参数扰动。

    历史回测页和策略优化的冠军复跑都走这里。样本门槛、压力开关、执行顺序和提示文案
    只维护这一份，避免两边各写一套后慢慢跑偏。
    返回 (评估报告, 滑点结果, 扰动结果, 压力说明)。
    """
    th = thresholds or EvaluationThresholds.from_cfg(cfg)
    report = report if report is not None else getattr(result, "evaluation", None)
    if report is None:
        if not th.enabled:
            return None, {}, {}, ""
        report = evaluate_backtest(result, thresholds=th)
        result.evaluation = report
    if not th.enabled:
        return report, {}, {}, ""

    perf = getattr(result, "performance", None)
    trades = int(getattr(perf, "trades", 0) or 0)
    if not th.stress_on_backtest:
        return report, {}, {}, "压力测试 关（配置页已关闭）"
    if trades < th.stress_min_trades:
        return report, {}, {}, f"压力测试 跳过（成交 {trades} 笔，少于 {th.stress_min_trades} 笔）"

    if on_start is not None:
        on_start()
    fn = runner or run_backtest
    slip = run_slippage_stress(
        cfg,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        extra_ticks=th.slippage_extra_ticks,
        chain_capital=chain_capital,
        runner=fn,
    )
    report = apply_stress(report, slippage=slip, thresholds=th)
    pert = run_perturbation(
        cfg,
        symbol=symbol,
        param_keys=list(param_keys) if param_keys else None,
        base_pf=float(getattr(perf, "profit_factor", 0) or 0),
        thresholds=th,
        start_date=start_date,
        end_date=end_date,
        engine=engine,
        chain_capital=chain_capital,
        runner=fn,
    )
    report = apply_stress(report, perturbation=pert, thresholds=th)
    result.evaluation = report
    return report, slip, pert, ""
