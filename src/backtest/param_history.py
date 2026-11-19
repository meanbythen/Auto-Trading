"""
策略参数版本历史
功能：每次写入优化结果前，先把「当前参数」存档；之后可一键回溯到更早版本，防止过度优化改坏。
原理：把策略相关字段做成快照，追加到 data/param_history.json（最新在前，默认最多保留 30 条）。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

# 历史文件默认路径（相对项目根由调用方传入）
DEFAULT_HISTORY_NAME = "param_history.json"
MAX_KEEP = 30

# 出厂默认版本 ID（不写入 json，列表里永远存在，优化不出更好时可重置）
FACTORY_DEFAULT_ID = "factory_default"

# 与 config.example.yaml 策略段对齐的「出厂默认」快照
# 原理：优化改坏时有一个稳定锚点；只恢复策略相关字段，不动账号/坐标
FACTORY_DEFAULT_SNAPSHOT: dict[str, Any] = {
    "engine": "daily_body",
    "timeframe": "15",
    "trail_timeframe": "30",
    "symbols": ["bu2612"],
    "flatten_times": ["14:55", "22:55"],
    "flatten_at_close": True,
    # 止损触发方式（全策略共用）：touch=触及即止损，close=收盘价确认。
    "stop_mode": "touch",
    # 开仓触发方式（全策略共用）：trigger=触发即开仓，close=收盘价确认。
    "entry_mode": "close",
    "trend_follow": {
        "entry_bars": 36,
        "exit_bars": 12,
        "atr_period": 14,
        "atr_mult": 2.0,
        "flatten_at_close": False,
        "filter_enabled": False,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_flat": 20,
        "filter_atr_low_pct": 0.2,
    },
    "supertrend": {
        "atr_period": 10,
        "multiplier": 3.0,
        "reverse_on_flip": True,
        "flatten_at_close": False,
        "filter_enabled": False,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_flat": 20,
        "filter_atr_low_pct": 0.2,
    },
    "dual_ma": {
        "short_period": 20,
        "long_period": 60,
        "reverse_on_cross": True,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
        "filter_enabled": False,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_flat": 20,
        "filter_atr_low_pct": 0.2,
    },
    "ema_swing": {
        "ema_fast": 60,
        "ema_slow": 120,
        "dir_ema_fast": 60,
        "dir_ema_slow": 120,
        "dir_ema_pair": "60/120",
        "sig_ema_a": 5,
        "sig_ema_b": 10,
        "sig_ema_c": 30,
        "sig_ema_triple": "5/10/30",
        "stop_ema": 120,
        "side_ema": 120,
        "slope_ema": 120,
        "slope_min_deg": 3.0,
        "slope_lookback": 20,
        "slope_exit": False,
        "no_open_before_close_min": 30,
        "flatten_at_close": False,
    },
    "bollinger": {
        "boll_window": 20,
        "boll_dev": 2.0,
        "atr_period": 14,
        "atr_stop_mult": 1.5,
        "flatten_at_close": False,
        "filter_enabled": False,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_flat": 20,
        "filter_atr_low_pct": 0.2,
    },
    "sar_trend": {
        "af_start": 0.02,
        "af_step": 0.02,
        "af_max": 0.2,
        "ma_filter": 20,
        "flatten_at_close": False,
        "filter_enabled": False,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_flat": 20,
        "filter_atr_low_pct": 0.2,
    },
    "multi_factor": {
        "ma_short": 20,
        "ma_long": 60,
        "channel": 20,
        "boll_window": 20,
        "boll_dev": 2.0,
        "af_start": 0.02,
        "af_step": 0.02,
        "af_max": 0.2,
        "threshold": 0.6,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
        "use_sar": True,
        "disagree_max": 0.7,
        "bias_ma": 60,
        "trend_timeframe": "D",
        "flatten_at_close": True,
        "cta_filter": {
            "enabled": True,
            "use_adx": True,
            "use_atr": True,
            "use_disagree": True,
            "adx_period": 14,
            "adx_flat": 20.0,
            "adx_trend": 25.0,
            "atr_lookback": 60,
            "atr_low_pct": 0.2,
            "weak_score": 0.75,
            "weak_size": 0.5,
            "weak_votes": 0,
        },
    },
    "daily_body": {
        "eth_minutes": 20,
        "eth_minutes2": 30,
        "stop_extra_ticks": 1,
        "skip_gap_atr_mult": 2.5,
        "max_box_atr_mult": 5.0,
        "max_chase_atr_mult": 1.0,
        "ema_period": 20,
        "ema_buffer_ticks": 2,
        "day_cutoff": "10:30",
        "night_cutoff": "22:15",
        "filter_enabled": True,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_upper": 30.0,
        "filter_atr_high_pct": 0.8,
    },
    "swing_trend": {
        "scale": "D_60",
        "signal_timeframe": "60",
        "trend_timeframe": "D",
        "daily_ma": 20,
        "signal_ma": 20,
        "atr_period": 14,
        "pullback_lookback": 8,
        "touch_atr": 0.6,
        "unit_volume": 1,
        "max_adds": 2,
        "add_min_atr": 1.0,
        "reduce_extend_atr": 2.5,
        "min_core_units": 1,
        "stop_atr": 2.0,
        "flatten_at_close": False,
        "filter_enabled": False,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_flat": 20,
        "filter_atr_low_pct": 0.2,
    },
    "swing_box": {
        "lookback": 40,
        "peak_look": 2,
        "min_gap": 5,
        "atr_period": 20,
        "height_atr": 1.2,
        "adx_period": 14,
        "adx_min": 20.0,
        "stop_atr": 0.5,
        "trail_atr": 2.0,
        "trail_start_h": 1.0,
        "vol_mult": 0.0,
        "flatten_at_close": False,
    },
    "dual_donchian": {
        "trend_timeframe": "D",
        "entry_bars": 55,
        "exit_bars": 20,
        "htf_donchian": 55,
        "adx_period": 14,
        "adx_min": 22.0,
        "atr_period": 14,
        "atr_mult": 2.5,
        "flatten_at_close": False,
    },
    "dual_fractal": {
        "trend_timeframe": "D",
        "trend_mode": "ema",
        "trend_ema": 20,
        "htf_donchian": 55,
        "adx_period": 14,
        "adx_min": 22.0,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "atr_period": 14,
        "atr_mult": 2.8,
        "flatten_at_close": False,
    },
    "trade_symbol": "bu2612",
}


# 下拉里出厂默认的固定文字。这个名字不给普通版本用。
# 优化页会按当前选中的策略换成「只重置某一套」，避免一点重置就把全部策略改掉。
FACTORY_DEFAULT_LABEL = "★ 出厂默认（全部策略参数初始值，可重置）"

# 每个策略自己的参数放在配置的哪一节。优化、回溯、重置都只写这一节。
_ENGINE_SECTION = {
    "trend_follow": "trend_follow",
    "supertrend": "supertrend",
    "st": "supertrend",
    "dual_ma": "dual_ma",
    "ma_cross": "dual_ma",
    "ema_swing": "ema_swing",
    "macd_swing": "ema_swing",
    "bollinger": "bollinger",
    "boll": "bollinger",
    "bollinger_break": "bollinger",
    "sar_trend": "sar_trend",
    "sar": "sar_trend",
    "parabolic_sar": "sar_trend",
    "multi_factor": "multi_factor",
    "multi": "multi_factor",
    "cta": "multi_factor",
    "daily_body": "daily_body",
    "swing_trend": "swing_trend",
    "swing_box": "swing_box",
    "dual_donchian": "dual_donchian",
    "dual_fractal": "dual_fractal",
}
# 自己起的名字最长这么多字，避免下拉菜单被一行撑爆。
MAX_NAME_LENGTH = 60


def factory_default_entry() -> dict[str, Any]:
    """构造永不删除的「出厂默认」版本条目。"""
    return {
        "id": FACTORY_DEFAULT_ID,
        "name": "出厂默认",
        "time": "出厂默认",
        "source": "factory",
        "note": "所有策略参数的初始默认值（重置用）",
        "engine": FACTORY_DEFAULT_SNAPSHOT.get("engine"),
        "symbol": (FACTORY_DEFAULT_SNAPSHOT.get("symbols") or ["bu2612"])[0],
        "score": None,
        "performance": {},
        "opt_params": {},
        "snapshot": copy.deepcopy(FACTORY_DEFAULT_SNAPSHOT),
    }


def _now_id() -> str:
    """内部编号：到秒的时间戳。改名不会改这个编号，导出 Excel 靠它认出是不是同一条。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _format_write_time(when: datetime | str | None) -> str:
    """把写入时刻收成「2026-10-16 22:56:03」，用来拼默认名字。"""
    if isinstance(when, datetime):
        return when.strftime("%Y-%m-%d %H:%M:%S")
    text = str(when or "").strip().replace("T", " ")
    if len(text) >= 19 and text[4] == "-" and text[13] == ":":
        return text[:19]
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def strategy_display_name(engine: str | None) -> str:
    """配置里的策略代号换成界面上的中文，例如 daily_body → 日内实体。"""
    if not str(engine or "").strip():
        return "未填策略"
    # 放在函数里引用，避免参数历史模块和复盘模块互相导入。
    from src.review.replay import label_for_engine

    return label_for_engine(str(engine))


def default_version_name(
    symbol: str | None,
    when: datetime | str | None = None,
    engine: str | None = None,
) -> str:
    """
    默认名字：品种合约-策略名称-写入时间。

    同一套优化往往只适合某一个合约、某一种策略，所以名字里把这两样都写上。
    例如 bu2612-流动性耗尽-2026-10-16 22:56:03。之后可以再改成自己认得的名字。
    """
    contract = str(symbol or "").strip() or "未填品种"
    return f"{contract}-{strategy_display_name(engine)}-{_format_write_time(when)}"


def version_display_name(v: dict[str, Any]) -> str:
    """给人看的名字。自己改过就用改过的；老记录没写名字时，按合约、策略和时间现算。"""
    if str(v.get("id")) == FACTORY_DEFAULT_ID or v.get("source") == "factory":
        return "出厂默认"
    custom = str(v.get("name") or "").strip()
    if custom and custom.lower() != "nan":
        return custom
    engine = v.get("engine") or (v.get("snapshot") or {}).get("engine")
    return default_version_name(v.get("symbol"), v.get("time"), engine)


def history_path(root: Path | str) -> Path:
    return Path(root) / "data" / DEFAULT_HISTORY_NAME


def load_history(root: Path | str) -> list[dict[str, Any]]:
    """读取全部版本（新→旧）。文件不存在则返回空列表。"""
    path = history_path(root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "versions" in raw:
            return list(raw["versions"])
        if isinstance(raw, list):
            return raw
    except Exception as e:  # noqa: BLE001
        logger.warning("读取参数历史失败: {}", e)
    return []


def save_history(root: Path | str, versions: list[dict[str, Any]]) -> Path:
    path = history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 每个策略各自留最近 MAX_KEEP 条。优化流动性耗尽时，不要把趋势跟随的旧版本挤掉。
    payload = {"versions": _trim_per_engine(versions)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _trim_per_engine(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按策略分别计数，每一套只留最新的 MAX_KEEP 条。列表本身仍是新的在前。"""
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in versions:
        key = version_engine(item) or "_"
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= MAX_KEEP:
            kept.append(item)
    return kept


def version_engine(version: dict[str, Any]) -> str:
    """这条版本属于哪一套策略。老记录如果没写在外面，就看快照里的引擎名。"""
    engine = str(version.get("engine") or "").strip()
    if engine:
        return engine
    snap = version.get("snapshot") or {}
    return str(snap.get("engine") or "").strip()


def factory_default_label(engine: str | None = None) -> str:
    """
    出厂默认在下拉里的文字。

    指明了策略时，写清楚「只重置这一套」，避免看起来像会把全部策略打回初始值。
    """
    if not str(engine or "").strip():
        return FACTORY_DEFAULT_LABEL
    return f"★ 出厂默认（只重置{strategy_display_name(engine)}）"


def extract_strategy_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    从完整配置里抽出「和策略表现相关」的字段做成快照。
    回溯时只恢复这些字段，不动账号密码、GUI 坐标等。
    """
    st = copy.deepcopy(cfg.get("strategy") or {})
    trade = cfg.get("trade") or {}
    return {
        "engine": st.get("engine"),
        "timeframe": st.get("timeframe"),
        "trail_timeframe": st.get("trail_timeframe"),
        "symbols": list(st.get("symbols") or ([trade.get("symbol")] if trade.get("symbol") else [])),
        "flatten_times": st.get("flatten_times"),
        "flatten_at_close": st.get("flatten_at_close", True),
        "stop_mode": st.get("stop_mode"),
        "entry_mode": st.get("entry_mode"),
        # 通用止损（独立于策略自身止损），和 stop_mode 一样属于公共设置。
        "generic_stop": st.get("generic_stop") or {},
        "trend_follow": st.get("trend_follow") or {},
        "supertrend": st.get("supertrend") or {},
        "dual_ma": st.get("dual_ma") or {},
        "ema_swing": st.get("ema_swing") or st.get("macd_swing") or {},
        "bollinger": st.get("bollinger") or {},
        "sar_trend": st.get("sar_trend") or {},
        "multi_factor": st.get("multi_factor") or {},
        "daily_body": st.get("daily_body") or {},
        "swing_trend": st.get("swing_trend") or {},
        "swing_box": st.get("swing_box") or {},
        "dual_donchian": st.get("dual_donchian") or {},
        "dual_fractal": st.get("dual_fractal") or {},
        "trade_symbol": trade.get("symbol"),
        "data_source": (cfg.get("data") or {}).get("source") or "akshare",
    }


def apply_snapshot_to_config(cfg: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """把快照写回配置字典（深拷贝，不改原对象）。"""
    out = copy.deepcopy(cfg)
    st = out.setdefault("strategy", {})
    trade = out.setdefault("trade", {})

    for key in (
        "engine",
        "timeframe",
        "trail_timeframe",
        "symbols",
        "flatten_times",
        "flatten_at_close",
        "stop_mode",
        "entry_mode",
        "generic_stop",
    ):
        if key in snap and snap[key] is not None:
            st[key] = copy.deepcopy(snap[key])

    if snap.get("trend_follow") is not None:
        st["trend_follow"] = copy.deepcopy(snap["trend_follow"])
    if snap.get("supertrend") is not None:
        st["supertrend"] = copy.deepcopy(snap["supertrend"])
    if snap.get("dual_ma") is not None:
        st["dual_ma"] = copy.deepcopy(snap["dual_ma"])
    swing_box = snap.get("ema_swing")
    if swing_box is None:
        swing_box = snap.get("macd_swing")
    if swing_box is not None:
        st["ema_swing"] = copy.deepcopy(swing_box)
        st.pop("macd_swing", None)
    if snap.get("bollinger") is not None:
        st["bollinger"] = copy.deepcopy(snap["bollinger"])
    if snap.get("sar_trend") is not None:
        st["sar_trend"] = copy.deepcopy(snap["sar_trend"])
    if snap.get("multi_factor") is not None:
        st["multi_factor"] = copy.deepcopy(snap["multi_factor"])
    if snap.get("daily_body") is not None:
        st["daily_body"] = copy.deepcopy(snap["daily_body"])
    if snap.get("swing_trend") is not None:
        st["swing_trend"] = copy.deepcopy(snap["swing_trend"])
    if snap.get("swing_box") is not None:
        st["swing_box"] = copy.deepcopy(snap["swing_box"])
    if snap.get("dual_donchian") is not None:
        st["dual_donchian"] = copy.deepcopy(snap["dual_donchian"])
    if snap.get("dual_fractal") is not None:
        st["dual_fractal"] = copy.deepcopy(snap["dual_fractal"])
    if snap.get("trade_symbol"):
        trade["symbol"] = snap["trade_symbol"]
    elif snap.get("symbols"):
        trade["symbol"] = snap["symbols"][0]
    if snap.get("data_source"):
        from src.data.quote_check import normalize_source

        out.setdefault("data", {})["source"] = normalize_source(str(snap["data_source"]))
    return out


def apply_engine_snapshot(cfg: dict[str, Any], snap: dict[str, Any], engine: str) -> dict[str, Any]:
    """
    只把某一个策略自己的参数写回配置。

    不改正在使用的策略、品种、周期，也不改另外几套策略的参数。
    出厂默认和历史版本都走这里：快照里即使记着整份配置，也只取出选中的那一节。
    """
    out = copy.deepcopy(cfg)
    st = out.setdefault("strategy", {})
    section = _ENGINE_SECTION.get(str(engine or "").strip())
    if not section:
        return out
    block = (snap or {}).get(section)
    if isinstance(block, dict):
        st[section] = copy.deepcopy(block)
    # 收盘清仓是总开关。只有听这个开关的策略回溯时才把它带回去，趋势跟随不碰。
    if section in (
        "daily_body",
        "dual_ma",
    ) and (snap or {}).get("flatten_at_close") is not None:
        st["flatten_at_close"] = bool(snap.get("flatten_at_close"))
    # 止损触发方式是全策略公共设置；日内实体把它当优化参数，回溯这一套时一并带回。
    if section == "daily_body" and (snap or {}).get("stop_mode") is not None:
        st["stop_mode"] = snap["stop_mode"]
    # 通用止损是公共设置：回溯任何一套策略时都按快照恢复，保证结果可复现。
    if (snap or {}).get("generic_stop"):
        st["generic_stop"] = copy.deepcopy(snap["generic_stop"])
    # 开仓触发方式同样属于公共设置，回溯时一并带回。
    if (snap or {}).get("entry_mode") is not None:
        st["entry_mode"] = snap["entry_mode"]
    return out


def add_version(
    root: Path | str,
    cfg: dict[str, Any],
    *,
    note: str = "",
    source: str = "manual",
    score: float | None = None,
    performance: dict[str, Any] | None = None,
    opt_params: dict[str, Any] | None = None,
    engine: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """
    追加一条版本。

    参数:
        note: 给人看的说明，如「写入优化前备份」「优化得分0.42」
        source: backup | optimize | restore
        score / performance / opt_params: 优化相关附加信息（可选）
    """
    versions = load_history(root)
    snap = extract_strategy_snapshot(cfg)
    now = datetime.now()
    # 优化页可以指定「这条记录归哪一套」。这样实盘正在用别的策略时，版本仍出现在被优化的那一套下面。
    recorded_engine = str(engine or snap.get("engine") or "")
    recorded_symbol = str(symbol or "").strip() or (snap.get("symbols") or [None])[0] or snap.get("trade_symbol")
    symbol = recorded_symbol
    engine = recorded_engine
    # 同一秒里可能连写「备份」和「优化结果」两条，名字和编号都要错开，避免下拉里分不清。
    taken_names = {version_display_name(v) for v in versions}
    taken_names.add("出厂默认")
    taken_names.add(FACTORY_DEFAULT_LABEL)
    taken_names.add(factory_default_label(engine))
    name = default_version_name(symbol, now, engine)
    if name in taken_names:
        name = f"{name}-{now.strftime('%f')[:3]}"
        extra = 2
        while name in taken_names:
            name = f"{default_version_name(symbol, now, engine)}-{extra}"
            extra += 1
    taken_ids = {str(v.get("id", "")) for v in versions}
    vid = _now_id()
    if vid in taken_ids:
        vid = f"{vid}_{now.strftime('%f')[:3]}"
    entry = {
        "id": vid,
        "name": name,
        "time": now.isoformat(timespec="seconds"),
        "source": source,
        "note": note,
        "engine": engine or snap.get("engine"),
        "symbol": symbol,
        "score": score,
        "performance": performance or {},
        "opt_params": opt_params or {},
        "snapshot": snap,
    }
    versions.insert(0, entry)
    save_history(root, versions)
    logger.info("已存参数版本 {} | {} | {}", entry["id"], source, note)
    return entry


def get_version(root: Path | str, version_id: str) -> dict[str, Any] | None:
    """按 id 取版本；出厂默认不依赖磁盘文件。"""
    if str(version_id) == FACTORY_DEFAULT_ID:
        return factory_default_entry()
    for v in load_history(root):
        if str(v.get("id")) == str(version_id):
            return v
    return None


def format_version_label(v: dict[str, Any]) -> str:
    """下拉菜单用的名字。普通版本就是「品种-时间」或后来改过的名字。"""
    if str(v.get("id")) == FACTORY_DEFAULT_ID or v.get("source") == "factory":
        return FACTORY_DEFAULT_LABEL
    return version_display_name(v)


def rename_version(root: Path | str, version_id: str, new_name: str) -> dict[str, Any]:
    """
    给一条已经存下的参数版本改名。

    内部编号不变，所以回溯和 Excel 仍然认得是同一条。
    出厂默认不能改。空名字、和别人重名，都会直接拒绝。
    """
    vid = str(version_id or "").strip()
    if not vid or vid == FACTORY_DEFAULT_ID:
        raise ValueError("出厂默认不能改名")
    # 连续空格收成一个，避免看起来一样、程序却当成两个名字。
    name = " ".join(str(new_name or "").split())
    if not name:
        raise ValueError("名字不能是空的")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"名字最多 {MAX_NAME_LENGTH} 个字")
    if name in {"出厂默认", FACTORY_DEFAULT_LABEL} or name.startswith("★ 出厂默认"):
        raise ValueError("这个名字留给出厂默认")

    versions = load_history(root)
    target: dict[str, Any] | None = None
    for item in versions:
        if str(item.get("id")) == vid:
            target = item
            break
    if target is None:
        raise ValueError("找不到这个版本")
    for item in versions:
        if item is target:
            continue
        if version_display_name(item) == name:
            raise ValueError("已经有一个版本叫这个名字，请换一个")
    target["name"] = name
    save_history(root, versions)
    logger.info("参数版本 {} 已改名为 {}", vid, name)
    return target


def delete_version(root: Path | str, version_id: str) -> str:
    """
    删掉一条参数版本。

    只从历史列表里拿掉这一条，不改配置文件里正在用的参数。
    出厂默认要留着当重置锚点，不能删。
    """
    vid = str(version_id or "").strip()
    if not vid or vid == FACTORY_DEFAULT_ID:
        raise ValueError("出厂默认要留着当重置锚点，不能删除")
    versions = load_history(root)
    kept: list[dict[str, Any]] = []
    removed = ""
    for item in versions:
        if str(item.get("id")) == vid and not removed:
            removed = version_display_name(item)
            continue
        kept.append(item)
    if not removed:
        raise ValueError("找不到这个版本")
    save_history(root, kept)
    logger.info("已删除参数版本 {}", removed)
    return removed


def list_version_labels(root: Path | str, engine: str | None = None) -> list[tuple[str, str]]:
    """
    返回 [(显示文字, 版本id), ...]，供界面下拉使用。

    engine 有值时，只列出这一套策略的版本。第一项仍是出厂默认，
    但重置时也只恢复这一套，不会把别的策略一起打回初始值。
    """
    out: list[tuple[str, str]] = []
    factory_label = factory_default_label(engine)
    used = {factory_label}
    out.append((factory_label, FACTORY_DEFAULT_ID))
    want = str(engine or "").strip()
    for v in load_history(root):
        vid = str(v.get("id", ""))
        if not vid or vid == FACTORY_DEFAULT_ID:
            continue
        if want and version_engine(v) != want:
            continue
        label = format_version_label(v)
        # 极少数重名时，下拉里补上编号，避免两个选项变成同一条。
        if label in used:
            label = f"{label} ({vid})"
        used.add(label)
        out.append((label, vid))
    return out
