"""
因子权重的慢速调整（只给自适应对照策略用）。

功能：平仓之后，按最近若干笔分摊盈亏，把权重往近期还赚钱的因子挪一点。
原理：
  - 绝不在每根 K 线上改权重，避免追着行情跳。
  - 大部分仍留在出厂底仓权重上，只掺一小部分近期成绩，减轻「刚赚过的因子下一轮亏」的追涨。
  - 单因子有上下限，单次挪动有步长上限，再摊成 1。
  - 窗口里累计为负的因子，当期得分先打成 0（由策略去用），这里只标出来。
这一层不决定买还是卖，也不自动换成实盘主推策略。
"""

from __future__ import annotations

from typing import Any

from src.strategy.cta_factors import FACTOR_KEYS

_EPS = 1e-6


def rolling_factor_pnl(rows: list[dict[str, float]], lookback: int) -> dict[str, float]:
    """最近 lookback 笔里，每个因子分摊到的盈亏加总。"""
    window = list(rows)[-max(1, int(lookback)) :]
    out = {key: 0.0 for key in FACTOR_KEYS}
    for row in window:
        for key in FACTOR_KEYS:
            out[key] += float(row.get(key) or 0.0)
    return out


def update_weights(
    base: dict[str, float],
    current: dict[str, float],
    rows: list[dict[str, float]],
    *,
    lookback: int = 20,
    min_weight: float = 0.05,
    max_weight: float = 0.40,
    recent_mix: float = 0.30,
    max_step: float = 0.03,
    use_sar: bool = True,
) -> tuple[dict[str, float], dict[str, float], set[str]]:
    """
    用近期分摊盈亏算出下一组权重。

    笔数不够 lookback 时原样返回，避免前几笔就把权重拧歪。
    返回：(新权重, 窗口盈亏, 窗口为负的因子)。
    """
    lookback = max(5, int(lookback or 20))
    min_w = min(0.2, max(0.0, float(min_weight)))
    max_w = min(0.8, max(min_w + 0.01, float(max_weight)))
    mix = min(0.5, max(0.0, float(recent_mix)))
    step = min(0.1, max(0.0, float(max_step)))
    base_w = _clean(base, use_sar=use_sar)
    cur_w = _clean(current or base_w, use_sar=use_sar)
    pnl = rolling_factor_pnl(rows, lookback)
    dead = {key for key, value in pnl.items() if value < 0}
    if not use_sar:
        dead.discard("sar")
        pnl["sar"] = 0.0
    if len(rows) < lookback:
        return cur_w, pnl, dead

    # 亏损因子在近期分配里记 0，只在还赚钱的因子之间分。
    positive = {key: max(0.0, pnl[key]) for key in FACTOR_KEYS}
    if not use_sar:
        positive["sar"] = 0.0
    total_pos = sum(positive.values())
    if total_pos <= _EPS:
        recent = dict(base_w)
    else:
        recent = {key: positive[key] / total_pos for key in FACTOR_KEYS}
        if not use_sar:
            recent["sar"] = 0.0
            recent = _normalize(recent, use_sar=False)

    blended = {
        key: (1.0 - mix) * base_w[key] + mix * recent[key] for key in FACTOR_KEYS
    }
    if not use_sar:
        blended["sar"] = 0.0
    capped = {}
    for key in FACTOR_KEYS:
        if key == "sar" and not use_sar:
            capped[key] = 0.0
            continue
        capped[key] = min(max_w, max(min_w, blended[key]))
    target = _normalize(capped, use_sar=use_sar)
    # 整组一起朝目标挪，谁都不得超过 max_step，和仍为 1。
    delta = {key: target[key] - cur_w[key] for key in FACTOR_KEYS}
    max_abs = max(abs(value) for value in delta.values()) if delta else 0.0
    if step <= 0 or max_abs <= step or max_abs <= _EPS:
        new_w = target
    else:
        scale = step / max_abs
        new_w = {key: cur_w[key] + scale * delta[key] for key in FACTOR_KEYS}
        new_w = _normalize(new_w, use_sar=use_sar)
    return new_w, pnl, dead


def _clean(raw: dict[str, Any] | None, *, use_sar: bool) -> dict[str, float]:
    data = raw if isinstance(raw, dict) else {}
    out = {key: max(0.0, float(data.get(key) or 0.0)) for key in FACTOR_KEYS}
    if not use_sar:
        out["sar"] = 0.0
    return _normalize(out, use_sar=use_sar)


def _normalize(weights: dict[str, float], *, use_sar: bool) -> dict[str, float]:
    work = {key: max(0.0, float(weights.get(key) or 0.0)) for key in FACTOR_KEYS}
    if not use_sar:
        work["sar"] = 0.0
    total = sum(work.values())
    if total <= _EPS:
        if use_sar:
            return {"ma": 0.30, "turtle": 0.25, "boll": 0.25, "sar": 0.20}
        return {"ma": 0.40, "turtle": 0.30, "boll": 0.30, "sar": 0.0}
    return {key: work[key] / total for key in FACTOR_KEYS}
