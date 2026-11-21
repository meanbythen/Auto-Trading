"""
研究稳健性分析（只读、纯函数、固定种子、只用标准库）。

回答三个问题：
  1. 这组成交盈亏的均值是不是靠运气撑的？     -> bootstrap_ci
  2. 试过 N 组参数后，夏普还显著吗？           -> deflated_sharpe (DSR)
  3. 选出来的最优参数是不是过拟合？            -> pbo_cscv (CSCV)、plateau_analysis

全部不触发新回测、不写任何数据、不改账户状态；样本不足时返回 skipped=True 与原因，不抛异常。
同一种子多次调用结果完全一致（可复现）。
"""

from __future__ import annotations

import itertools
import math
import random
from statistics import NormalDist
from typing import Any

# CSCV 组合数超过这个阈值时，不再穷举，改用固定种子随机抽样，避免组合爆炸拖慢报告。
_CSCV_MAX_COMBINATIONS = 20000
# Euler–Mascheroni 常数，DSR 的 E_max 公式用（Bailey & López de Prado 2014）。
_EULER_GAMMA = 0.5772156649015329
_PHI = NormalDist()


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _quantile_linear(sorted_vals: list[float], q: float) -> float:
    """
    线性插值分位数（numpy 默认的 type-7）。

    h = (n-1)*q，在相邻两个顺序统计量之间线性插值。sorted_vals 必须已升序。
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    if q <= 0.0:
        return float(sorted_vals[0])
    if q >= 1.0:
        return float(sorted_vals[-1])
    h = (n - 1) * float(q)
    lo = int(math.floor(h))
    hi = int(math.ceil(h))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = h - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def _finite_float(x: Any, default: float = 0.0) -> float:
    """转成有限 float，失败/非有限回退 default，保证 JSON 安全。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


# --------------------------------------------------------------------------- #
# 1. bootstrap 置信区间
# --------------------------------------------------------------------------- #
def bootstrap_ci(
    pnls: list[float],
    *,
    n: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict[str, Any]:
    """
    对每笔盈亏有放回重抽，算「净盈亏均值」的置信区间。

    每次重抽 len(pnls) 笔算一个均值，共 n 个均值；low/high 是该均值分布的
    (1-c)/2 与 1-(1-c)/2 分位数（线性插值）。同种子完全可复现。
    pnls 少于 2 笔时 skipped。
    """
    values = [_finite_float(x) for x in (pnls or [])]
    runs = int(n) if n and int(n) > 0 else 1
    conf = _finite_float(confidence, default=0.95)
    conf = min(max(conf, 0.0), 1.0)
    if len(values) < 2:
        return {
            "low": 0.0,
            "high": 0.0,
            "mean": 0.0,
            "n": 0,
            "original_mean": float(sum(values)) / len(values) if values else 0.0,
            "confidence": conf,
            "skipped": True,
            "skip_reason": f"成交只有 {len(values)} 笔，少于 2 笔，bootstrap 置信区间不计算。",
        }

    rng = random.Random(int(seed))
    k = len(values)
    means: list[float] = []
    for _ in range(runs):
        sample = rng.choices(values, k=k)
        means.append(sum(sample) / k)
    means.sort()

    alpha = (1.0 - conf) / 2.0
    low = _quantile_linear(means, alpha)
    high = _quantile_linear(means, 1.0 - alpha)
    return {
        "low": low,
        "high": high,
        "mean": sum(means) / len(means),
        "n": runs,
        "original_mean": sum(values) / k,
        "confidence": conf,
        "skipped": False,
        "skip_reason": "",
    }


# --------------------------------------------------------------------------- #
# 2. Deflated Sharpe Ratio
# --------------------------------------------------------------------------- #
def deflated_sharpe(
    pnls: list[float],
    n_trials: int,
    *,
    sharpe: float | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """
    Bailey & López de Prado (2014) 的 Deflated Sharpe Ratio。

    T=len(pnls)；sharpe 缺省时用 mean/std（总体标准差）。
    skew/kurt 为总体三、四阶标准化矩（E[(x-μ)^k]/σ^k）。
    E_max=(1-γ)Φ⁻¹(1-1/N)+γΦ⁻¹(1-1/(N·e))。
    DSR=Φ((sharpe-E_max)·√(T-1)/√(1-skew·sharpe+(kurt-1)/4·sharpe²))，夹到 [0,1]。
    T<2 / n_trials<2 / 方差为 0 / 分母<=0 时 skipped。
    """
    values = [_finite_float(x) for x in (pnls or [])]
    t_obs = len(values)
    trials = int(n_trials) if n_trials is not None else 0

    empty = {
        "dsr": 0.0,
        "sharpe": 0.0,
        "n_trials": trials,
        "t_obs": t_obs,
        "skew": 0.0,
        "kurt": 0.0,
        "skipped": True,
        "skip_reason": "",
    }
    if t_obs < 2:
        empty["skip_reason"] = f"成交只有 {t_obs} 笔，少于 2 笔，DSR 不计算。"
        return empty
    if trials < 2:
        empty["skip_reason"] = f"试验次数 {trials} 少于 2，DSR 不计算（N=1 时无需校正）。"
        return empty

    mean = sum(values) / t_obs
    var = sum((x - mean) ** 2 for x in values) / t_obs  # 总体方差
    if var <= 1e-18:
        empty["skip_reason"] = "盈亏方差为 0（每笔都一样），无法计算夏普/偏度/峰度。"
        return empty
    std = math.sqrt(var)
    skew = sum((x - mean) ** 3 for x in values) / t_obs / (std ** 3)
    kurt = sum((x - mean) ** 4 for x in values) / t_obs / (std ** 4)

    sr = _finite_float(sharpe) if sharpe is not None else mean / std
    if not math.isfinite(sr):
        empty["sharpe"] = 0.0
        empty["skip_reason"] = "夏普非有限值，DSR 不计算。"
        return empty

    # E[max]：N 个标准正态最大值的期望（近似）。
    inv1 = _PHI.inv_cdf(1.0 - 1.0 / trials)
    inv2 = _PHI.inv_cdf(1.0 - 1.0 / (trials * math.e))
    e_max = (1.0 - _EULER_GAMMA) * inv1 + _EULER_GAMMA * inv2

    radicand = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if radicand <= 0.0:
        return {
            "dsr": 0.0,
            "sharpe": sr,
            "n_trials": trials,
            "t_obs": t_obs,
            "skew": skew,
            "kurt": kurt,
            "skipped": True,
            "skip_reason": (
                "DSR 分母内的根号项 <= 0（偏度/峰度与夏普组合使方差校正失效），不计算。"
            ),
        }
    denom = math.sqrt(radicand)
    t_stat = (sr - e_max) * math.sqrt(t_obs - 1) / denom
    dsr = _PHI.cdf(t_stat)
    dsr = min(max(dsr, 0.0), 1.0)
    return {
        "dsr": dsr,
        "sharpe": sr,
        "n_trials": trials,
        "t_obs": t_obs,
        "skew": skew,
        "kurt": kurt,
        "skipped": False,
        "skip_reason": "",
    }


# --------------------------------------------------------------------------- #
# 3. PBO / CSCV
# --------------------------------------------------------------------------- #
def _split_blocks(n_rows: int, n_partitions: int) -> list[list[int]]:
    """把 0..n_rows-1 分成 S 个尽量等大的连续块。"""
    s = int(n_partitions)
    block_size = n_rows // s
    extra = n_rows % s
    blocks: list[list[int]] = []
    start = 0
    for i in range(s):
        size = block_size + (1 if i < extra else 0)
        blocks.append(list(range(start, start + size)))
        start += size
    return blocks


def _column_means(matrix: list[list[float]], row_idx: list[int], n_cols: int) -> list[float]:
    """指定行集合上，每一列的均值（行里缺列时跳过该行对该列的贡献）。"""
    out: list[float] = []
    for c in range(n_cols):
        total = 0.0
        count = 0
        for r in row_idx:
            row = matrix[r]
            if c < len(row):
                total += _finite_float(row[c])
                count += 1
        out.append(total / count if count else 0.0)
    return out


def pbo_cscv(
    returns_matrix: list[list[float]],
    *,
    n_partitions: int = 16,
    seed: int = 42,
) -> dict[str, Any]:
    """
    组合对称交叉验证（CSCV）算回测过拟合概率 PBO ∈ [0,1]。

    行=期、列=参数组合的每期收益。把行分成 S 个等大块，枚举 C(S, S/2) 种 IS/OOS
    划分（组合数 > 20000 时用固定种子随机子集）。每个划分：IS 上选均值最高的列，
    看它在 OOS 的排名。rank(1=最差 OOS 收益，符合 CSCV 标准定义)，omega=rank/N
    夹到 [1/(N+1), N/(N+1)]，logit=ln(omega/(1-omega))，PBO=logit<=0 的比例。
    行数 < n_partitions 或列数 < 2 时 skipped。
    """
    matrix = returns_matrix or []
    s = int(n_partitions) if n_partitions and int(n_partitions) >= 2 else 0
    n_rows = len(matrix)
    n_cols = max((len(r) for r in matrix), default=0)

    base = {
        "pbo": 0.0,
        "n_obs": n_rows,
        "n_strategies": n_cols,
        "n_combinations": 0,
        "n_partitions": s,
        "skipped": True,
        "skip_reason": "",
        "logits_sample": [],
    }
    if s < 2:
        base["skip_reason"] = f"n_partitions={n_partitions} 不足 2，无法划分 IS/OOS。"
        return base
    if n_rows < s:
        base["skip_reason"] = (
            f"期数 {n_rows} 少于分区数 {s}，CSCV 无法分块（需要每期至少一块）。"
        )
        return base
    if n_cols < 2:
        base["skip_reason"] = f"参数组合（列）只有 {n_cols} 个，少于 2 个，无法比较排名。"
        return base

    blocks = _split_blocks(n_rows, s)
    half = s // 2
    total_combos = math.comb(s, half)

    rng = random.Random(int(seed))
    if total_combos <= _CSCV_MAX_COMBINATIONS:
        combos = list(itertools.combinations(range(s), half))
    else:
        # 组合太多，固定种子随机抽不重复的划分，估计 PBO。
        combos = []
        seen: set[frozenset[int]] = set()
        attempts = 0
        target = min(total_combos, _CSCV_MAX_COMBINATIONS)
        while len(seen) < target and attempts < target * 20:
            pick = tuple(sorted(rng.sample(range(s), half)))
            fs = frozenset(pick)
            if fs not in seen:
                seen.add(fs)
                combos.append(pick)
            attempts += 1

    logits: list[float] = []
    n_strat = n_cols
    for combo in combos:
        is_blocks = set(combo)
        is_rows = [r for i in range(s) if i in is_blocks for r in blocks[i]]
        oos_rows = [r for i in range(s) if i not in is_blocks for r in blocks[i]]
        if not is_rows or not oos_rows:
            continue
        is_means = _column_means(matrix, is_rows, n_strat)
        oos_means = _column_means(matrix, oos_rows, n_strat)
        best_col = max(range(n_strat), key=lambda c: is_means[c])
        # rank 1 = 最差 OOS 收益（升序）。IS 最优在 OOS 排名越靠后（rank 越大）越不过拟合。
        ranked = sorted(range(n_strat), key=lambda c: (oos_means[c], c))
        rank = ranked.index(best_col) + 1
        omega = rank / n_strat
        lo = 1.0 / (n_strat + 1.0)
        hi = n_strat / (n_strat + 1.0)
        omega = min(max(omega, lo), hi)
        logits.append(math.log(omega / (1.0 - omega)))

    if not logits:
        base["skip_reason"] = "没有可用的 IS/OOS 划分，PBO 不计算。"
        return base

    overfit = sum(1 for x in logits if x <= 0.0)
    pbo = overfit / len(logits)
    pbo = min(max(pbo, 0.0), 1.0)
    return {
        "pbo": pbo,
        "n_obs": n_rows,
        "n_strategies": n_strat,
        "n_combinations": len(logits),
        "n_partitions": s,
        "skipped": False,
        "skip_reason": "",
        "logits_sample": logits,
    }


# --------------------------------------------------------------------------- #
# 4. 参数平原判定
# --------------------------------------------------------------------------- #
def _hamming_one(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两个参数 dict 是否恰有一个键的取值不同（缺键算不同）。"""
    keys = set(a.keys()) | set(b.keys())
    diffs = 0
    for k in keys:
        av = a.get(k)
        bv = b.get(k)
        if av != bv:
            diffs += 1
            if diffs > 1:
                return False
    return diffs == 1


def plateau_analysis(trials: list[dict], best_idx: int | None = None) -> dict[str, Any]:
    """
    在最优参数邻域上判定是孤峰还是稳定平原。

    邻域 = 与最优参数恰有一个取值不同的 trial（hamming 距离 == 1，缺键算不同）。
    collapsed = 邻域最高分 < best*0.5（best>0 时）。
    verdict：
      - 邻域中位 >= best*0.7          -> 稳定平原
      - collapsed                     -> 孤峰（邻域塌陷）
      - 有邻域但介于两者之间          -> 邻域一般
      - 无邻域                        -> 无邻域数据
      - trials < 2                    -> 样本不足（skipped）
    """
    empty = {
        "verdict": "样本不足",
        "best_score": 0.0,
        "best_idx": None,
        "n_neighbors": 0,
        "neighbor_scores": [],
        "collapsed": False,
        "skipped": True,
        "skip_reason": "",
        "note": "邻域=与最优参数恰有一个取值不同（hamming==1，缺键算不同）；阈值：邻域中位>=best*0.7 稳定平原，邻域最高<best*0.5 视为塌陷。",
    }
    # 只看有合法参数和有限分数的 trial。
    valid: list[tuple[int, dict, float]] = []
    for i, t in enumerate(trials or []):
        if not isinstance(t, dict):
            continue
        params = t.get("params")
        score = t.get("score")
        if not isinstance(params, dict):
            continue
        try:
            sc = float(score)
        except (TypeError, ValueError):
            continue
        if math.isnan(sc) or math.isinf(sc):
            continue
        valid.append((i, params, sc))

    if len(valid) < 2:
        empty["skip_reason"] = f"有效 trials 只有 {len(valid)} 个，少于 2 个，平原判定不计算。"
        return empty

    if best_idx is not None and 0 <= int(best_idx) < len(trials):
        # 在原始列表里定位；若该 idx 不在 valid，回退到 valid 里的最高分。
        target = int(best_idx)
        hit = next(((i, p, sc) for (i, p, sc) in valid if i == target), None)
        if hit is not None:
            best_i, best_params, best_score = hit
        else:
            best_i, best_params, best_score = max(valid, key=lambda x: x[2])
    else:
        best_i, best_params, best_score = max(valid, key=lambda x: x[2])

    neighbor_scores = [sc for (i, p, sc) in valid if i != best_i and _hamming_one(best_params, p)]
    n_neighbors = len(neighbor_scores)
    collapsed = bool(best_score > 0 and neighbor_scores and max(neighbor_scores) < best_score * 0.5)

    if n_neighbors == 0:
        verdict = "无邻域数据"
    else:
        sorted_ns = sorted(neighbor_scores)
        mid = sorted_ns[len(sorted_ns) // 2]
        if mid >= best_score * 0.7:
            verdict = "稳定平原"
        elif collapsed:
            verdict = "孤峰（邻域塌陷）"
        else:
            verdict = "邻域一般"

    return {
        "verdict": verdict,
        "best_score": best_score,
        "best_idx": best_i,
        "n_neighbors": n_neighbors,
        "neighbor_scores": neighbor_scores,
        "collapsed": collapsed,
        "skipped": False,
        "skip_reason": "",
        "note": (
            "邻域=与最优参数恰有一个取值不同（hamming==1，缺键算不同）；"
            "阈值：邻域中位>=best*0.7 判稳定平原，邻域最高<best*0.5 判塌陷。"
        ),
    }


# --------------------------------------------------------------------------- #
# 5. 组合分析入口
# --------------------------------------------------------------------------- #
def _skipped_pbo(reason: str) -> dict[str, Any]:
    return {
        "pbo": 0.0,
        "n_obs": 0,
        "n_strategies": 0,
        "n_combinations": 0,
        "n_partitions": 16,
        "skipped": True,
        "skip_reason": reason,
        "logits_sample": [],
    }


def analyze_experiment(payload: dict) -> dict[str, Any]:
    """
    从一次实验快照里提取成交与 trials，跑完整稳健性分析。

    payload:
      trades(list, 每项含 pnl) -> pnls
      trials(list)             -> 试验次数（DSR 的 N）与平原判定
      trials_returns(矩阵)      -> 可选，CSCV 的逐期收益；缺失时 PBO 跳过并说明
    """
    box = dict(payload or {})
    trades = box.get("trades") or []
    pnls: list[float] = []
    for t in trades:
        try:
            pnls.append(float(t.get("pnl", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            pnls.append(0.0)
    trials = box.get("trials") or []
    trials_returns = box.get("trials_returns")

    pbo = (
        pbo_cscv(trials_returns)
        if isinstance(trials_returns, list) and trials_returns
        else _skipped_pbo(
            "缺少每期收益矩阵 trials_returns，CSCV 需要每个参数组合的逐期收益；"
            "当前归档未保存，PBO 暂不计算。"
        )
    )
    return {
        "bootstrap": bootstrap_ci(pnls),
        "dsr": deflated_sharpe(pnls, len(trials)),
        "pbo": pbo,
        "plateau": plateau_analysis(trials),
    }


# --------------------------------------------------------------------------- #
# 6. 文本分区
# --------------------------------------------------------------------------- #
def format_robustness_lines(summary: dict) -> list[str]:
    """
    生成「—— 稳健性 ——」文本分区，展示 bootstrap CI、DSR、PBO、平原判定。

    每项带含义说明；全部 skipped 时只显示一行说明。
    """
    lines = ["—— 稳健性 ——"]
    box = dict(summary or {})
    bs = dict(box.get("bootstrap") or {})
    dsr = dict(box.get("dsr") or {})
    pbo = dict(box.get("pbo") or {})
    plat = dict(box.get("plateau") or {})

    if all(d.get("skipped") for d in (bs, dsr, pbo, plat)):
        lines.append("（样本不足或缺少数据，未计算）")
        return lines

    # bootstrap
    if bs.get("skipped"):
        lines.append("bootstrap 置信区间：" + str(bs.get("skip_reason") or "未计算"))
    else:
        conf = float(bs.get("confidence") or 0.95)
        lines.append(
            f"bootstrap 净盈亏均值 {conf * 100:.0f}% 置信区间 "
            f"[{float(bs.get('low') or 0):.2f}, {float(bs.get('high') or 0):.2f}] 元"
            f"（重抽 {int(bs.get('n') or 0)} 次；原始均值 {float(bs.get('original_mean') or 0):.2f} 元；"
            f"区间不跨 0 才比较像真的正期望）"
        )

    # DSR
    if dsr.get("skipped"):
        lines.append("Deflated Sharpe：" + str(dsr.get("skip_reason") or "未计算"))
    else:
        lines.append(
            f"Deflated Sharpe  {float(dsr.get('dsr') or 0):.3f}"
            f"（夏普 {float(dsr.get('sharpe') or 0):.3f}，经 {int(dsr.get('n_trials') or 0)} 次试验校正；"
            f"接近 1 = 校正后仍显著，低于 0.95 更可能是挑出来的运气）"
        )

    # PBO
    if pbo.get("skipped"):
        lines.append("PBO 过拟合概率：" + str(pbo.get("skip_reason") or "未计算"))
    else:
        lines.append(
            f"PBO 回测过拟合概率  {float(pbo.get('pbo') or 0):.3f}"
            f"（CSCV，{int(pbo.get('n_combinations') or 0)} 种 IS/OOS 划分，"
            f"{int(pbo.get('n_strategies') or 0)} 个参数组合；越低越好，>0.5 警惕过拟合）"
        )

    # 平原
    if plat.get("skipped"):
        lines.append("参数平原：" + str(plat.get("skip_reason") or "未计算"))
    else:
        nbr = int(plat.get("n_neighbors") or 0)
        lines.append(
            f"参数平原  {str(plat.get('verdict') or '')}"
            f"（最优分 {float(plat.get('best_score') or 0):.3f}，邻域 {nbr} 个；"
            f"{str(plat.get('note') or '')}）"
        )
    return lines
