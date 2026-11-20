"""策略引擎中文名与旧名映射。

工厂、复盘、参数历史和界面都读这一份，避免各写一套别名表。
"""

from __future__ import annotations

from src.strategy.factory import (
    CROSS_TEST_ENGINE_ORDER,
    ENGINE_ALIASES,
    ENGINE_LABELS,
)

NO_STRATEGY_LABEL = "只看盘面（不套策略）"

# 旧界面名仍认，避免老配置/老报告打不开。
_LEGACY_LABELS: dict[str, str] = {
    "日内实体": "daily_body",
    "SuperTrend": "supertrend",
    "MACD波段": "ema_swing",
    "多因子CTA": "multi_factor",
}

# 界面中文名 → 配置里的 engine。None 表示不套策略。
# 名单和中文名统一从策略工厂取，新增策略只改 factory.py。
ENGINE_BY_LABEL: dict[str, str | None] = {NO_STRATEGY_LABEL: None}
for _code in CROSS_TEST_ENGINE_ORDER:
    ENGINE_BY_LABEL.setdefault(ENGINE_LABELS[_code], _code)
for _legacy_label, _legacy_code in _LEGACY_LABELS.items():
    ENGINE_BY_LABEL.setdefault(_legacy_label, _legacy_code)

# 引擎 → 界面名。daily_body 用短名，和旧报告一致。
LABEL_BY_ENGINE: dict[str, str] = {
    code: ENGINE_LABELS[code] for code in CROSS_TEST_ENGINE_ORDER
}
LABEL_BY_ENGINE.update(
    {
        alias: ENGINE_LABELS[target]
        for alias, target in ENGINE_ALIASES.items()
        if target in ENGINE_LABELS
    }
)
LABEL_BY_ENGINE["daily_body"] = "日内实体"


def label_for_engine(engine: str | None) -> str:
    """配置里的引擎名换成界面上的中文。不认识的就原样显示。"""
    if not engine:
        return NO_STRATEGY_LABEL
    return LABEL_BY_ENGINE.get(engine, engine)


def engine_from_label(label: str | None) -> str | None:
    """界面中文名换成引擎。旧名字「加权打分·主推」仍认。"""
    text = str(label or "").strip()
    if text in ENGINE_BY_LABEL:
        return ENGINE_BY_LABEL[text]
    if text in {"多因子CTA（加权打分·主推）"}:
        return "multi_factor"
    return None
