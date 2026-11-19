"""
规则策略 DSL（自定义规则：不写代码，用指标条件组合搭一套策略）。

功能：
    把用户在 config/rules/*.yaml 里写的"指标 + 条件组合"严格校验成不可变对象，
    并提供条件树求值、指标计算、初始止损计算这三件运行时能力；
    RuleStrategy 把它接成运行层认识的策略（开仓、持仓止损、离场、收盘清仓）。

边界：
    开仓、离场、ATR/百分比/摆动点止损，可叠加 trail 移动止损、breakeven 保本、
    max_hold_bars 限持仓根数、filters_htf 高周期过滤。
    暂不支持加减仓、反手、分批止盈——文件里写了会直接报错，不会静默忽略。
    指标覆盖 38 种常用技术指标；expr 支持四则运算、括号、常用数学函数和滚动统计，
    这样 DualThrust / R-Breaker 这类带算术的策略也能复刻。

设计口径（防止未来函数）：
    1. 条件只看当根和更早的 K 线；prev 的 n 必须 >= 1，负数直接判非法。
    2. 条件只回答"这根收盘时该不该开/该不该平"，成交价由现有引擎决定
       （回测按下一根开盘，实盘按当前市价），DSL 不参与定价。
    3. 未知字段一律报错，不做"忽略并继续"，避免拼错后静默失效。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import yaml

from .base import StrategyBase
from .indicators import average_true_range, ema_series, macd_lines, parabolic_sar, supertrend
from .market_state import adx_series
from .types import (
    STOP_MODE_TOUCH,
    PositionState,
    StrategyAction,
    StrategyDecision,
    config_flag,
    stop_mode_from_cfg,
    stop_triggered,
)

SCHEMA_VERSION = 1
MAX_DEPTH = 8

_BAR_FIELDS = frozenset({"open", "high", "low", "close", "volume"})
_ROOT_KEYS = frozenset(
    {
        "schema",
        "name",
        "note",
        "timeframe",
        "indicators",
        "filters",
        "filters_htf",
        "entry",
        "exit",
        "stop",
        "trail",
        "breakeven",
        "max_hold_bars",
        "sizing",
        "builtin",
    }
)
_GROUP_KEYS = frozenset({"all", "any", "not"})
_COMPARE_KEYS = frozenset({"gt", "gte", "lt", "lte", "eq"})
_CROSS_KEYS = frozenset({"cross_above", "cross_below"})
_INDICATOR_KINDS = frozenset(
    {
        "ema",
        "sma",
        "stddev",
        "atr",
        "atr_pct",
        "adx",
        "plus_di",
        "minus_di",
        "highest",
        "lowest",
        "donchian_mid",
        "rsi",
        "macd_hist",
        "macd_dif",
        "macd_dea",
        "boll_upper",
        "boll_lower",
        "boll_mid",
        "boll_width",
        "keltner_upper",
        "keltner_lower",
        "stoch_k",
        "stoch_d",
        "cci",
        "williams_r",
        "roc",
        "obv",
        "vol_sma",
        "supertrend",
        "psar",
        "vwap",
        "day_open",
        "prev_day_high",
        "prev_day_low",
        "prev_day_close",
        "opening_range_high",
        "opening_range_low",
        "expr",
    }
)
_STOP_KINDS = frozenset({"atr", "percent", "swing", "none"})


class RuleConfigError(ValueError):
    """规则文件不合法。消息里带字段路径，方便用户自己改。"""


def _fail(where: str, message: str) -> RuleConfigError:
    return RuleConfigError(f"{where}: {message}")


def _require_mapping(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _fail(where, f"这里必须是键值对，实际是 {type(raw).__name__}")
    return raw


def _require_int(raw: Any, where: str, *, minimum: int = 1, maximum: int = 500) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _fail(where, f"必须是整数，实际是 {raw!r}")
    if raw < minimum or raw > maximum:
        raise _fail(where, f"必须在 {minimum}~{maximum} 之间，实际是 {raw}")
    return raw


def _require_number(raw: Any, where: str, *, positive: bool = False) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _fail(where, f"必须是数字，实际是 {raw!r}")
    value = float(raw)
    if not math.isfinite(value):
        raise _fail(where, "不能是 NaN 或无穷大")
    if positive and value <= 0:
        raise _fail(where, f"必须大于 0，实际是 {value}")
    return value


# ---------------------------------------------------------------- 算术表达式


_EXPR_TOKEN = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_][A-Za-z0-9_]*|[()+\-*/,]")
_EXPR_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_EXPR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPR_CALLS = {
    "max": 2,
    "min": 2,
    "abs": 1,
    "sqrt": 1,
    "log": 1,
    "sign": 1,
    "pow": 2,
    "clip": 3,
    "avg": 2,
    "std": 2,
    "hhv": 2,
    "llv": 2,
    "sum": 2,
}
# 第二个参数是「窗口根数」的滚动函数，必须写常数整数。
_ROLLING_CALLS = frozenset({"avg", "std", "hhv", "llv", "sum"})


@dataclass(frozen=True, slots=True)
class ExprNode:
    """算术公式的编译结果。number/name 是叶子，其余是运算符。"""

    kind: str  # number | name | add | sub | mul | div | neg | abs | max | min
    value: float = 0.0
    name: str = ""
    children: tuple["ExprNode", ...] = ()


def _tokenize_formula(formula: str, where: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(formula):
        char = formula[position]
        if char.isspace():
            position += 1
            continue
        match = _EXPR_TOKEN.match(formula, position)
        if match is None:
            raise _fail(
                where,
                f"公式 {formula!r} 第 {position + 1} 个字符 {char!r} 不认识；"
                "只支持 + - * / ( ) , max min abs 和数字/名字",
            )
        tokens.append(match.group())
        position = match.end()
    return tokens


class _FormulaParser:
    """递归下降：expr = term (+|- term)*；term = unary (*|/ unary)*；unary = - unary | atom。

    不使用 eval，只接受白名单里的数字、名字、运算符和 max/min/abs 三个函数。
    """

    def __init__(
        self,
        tokens: list[str],
        formula: str,
        where: str,
        allowed: frozenset[str],
        later: frozenset[str],
    ) -> None:
        self.tokens = tokens
        self.formula = formula
        self.where = where
        self.allowed = allowed
        self.later = later
        self.pos = 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self) -> str:
        token = self._peek()
        if token is None:
            raise self._error("公式不完整，末尾缺少操作数")
        self.pos += 1
        return token

    def _error(self, message: str) -> RuleConfigError:
        return _fail(self.where, f"{message}（公式 {self.formula!r}）")

    def parse(self) -> ExprNode:
        node = self._parse_expr()
        rest = self._peek()
        if rest is not None:
            raise self._error(f"公式末尾多了 {rest!r}，是不是少写了运算符")
        return node

    def _parse_expr(self) -> ExprNode:
        node = self._parse_term()
        while self._peek() in ("+", "-"):
            operator = self._advance()
            right = self._parse_term()
            node = ExprNode("add" if operator == "+" else "sub", children=(node, right))
        return node

    def _parse_term(self) -> ExprNode:
        node = self._parse_unary()
        while self._peek() in ("*", "/"):
            operator = self._advance()
            right = self._parse_unary()
            node = ExprNode("mul" if operator == "*" else "div", children=(node, right))
        return node

    def _parse_unary(self) -> ExprNode:
        token = self._peek()
        if token == "-":
            self._advance()
            return ExprNode("neg", children=(self._parse_unary(),))
        if token == "+":
            self._advance()
            return self._parse_unary()
        return self._parse_atom()

    def _parse_atom(self) -> ExprNode:
        token = self._advance()
        if token == "(":
            node = self._parse_expr()
            if self._advance() != ")":
                raise self._error("括号没有闭合")
            return node
        if _EXPR_NUMBER.fullmatch(token):
            return ExprNode("number", value=float(token))
        if not _EXPR_NAME.fullmatch(token):
            raise self._error(f"这里只能是数字、名字、函数或括号，收到 {token!r}")
        if token in _EXPR_CALLS:
            if self._peek() != "(":
                raise self._error(f"{token} 后面要跟括号，例如 {token}(a, b)")
            self._advance()
            args = [self._parse_expr()]
            while self._peek() == ",":
                self._advance()
                args.append(self._parse_expr())
            if self._advance() != ")":
                raise self._error(f"{token}(...) 括号没有闭合")
            if token == "abs":
                if len(args) != 1:
                    raise self._error("abs 只能有一个参数")
            elif token in {"sqrt", "log", "sign"}:
                if len(args) != 1:
                    raise self._error(f"{token} 只能有一个参数")
            elif token == "clip":
                if len(args) != 3:
                    raise self._error("clip 要写 clip(值, 下限, 上限)")
            elif token in _ROLLING_CALLS:
                if len(args) != 2:
                    raise self._error(f"{token} 要写 {token}(序列, 窗口)，例如 {token}(close, 20)")
                window = args[1]
                if window.kind != "number" or window.value < 2 or abs(window.value - round(window.value)) > 1e-9:
                    raise self._error(f"{token} 的第二个参数必须是 >=2 的整数根数，例如 {token}(close, 20)")
            elif len(args) < _EXPR_CALLS[token]:
                raise self._error(f"{token} 至少要 {_EXPR_CALLS[token]} 个参数")
            return ExprNode(token, children=tuple(args))
        if token in self.allowed:
            return ExprNode("name", name=token)
        if token in self.later:
            raise self._error(f"{token} 在下面才定义；公式只能引用前面已经写好的指标")
        raise self._error(
            f"未知名字 {token!r}；只能是行情字段 {sorted(_BAR_FIELDS)} 或前面定义过的指标名"
        )


def _parse_formula(
    formula: Any,
    where: str,
    allowed: frozenset[str],
    later: frozenset[str] = frozenset(),
) -> ExprNode:
    text = str(formula or "").strip()
    if not text:
        raise _fail(where, "formula 不能为空，例如 \"open + 0.7 * spread\"")
    parser = _FormulaParser(_tokenize_formula(text, where), text, where, allowed, later)
    return parser.parse()


def _eval_expr(node: ExprNode, values: Mapping[str, pd.Series], index: pd.Index) -> pd.Series:
    """把公式算成一条和 K 线同索引的序列。离场里取 prev 时要用到历史上的值。"""
    if node.kind == "number":
        return pd.Series(node.value, index=index, dtype="float64")
    if node.kind == "name":
        series = values.get(node.name)
        if series is None:
            return pd.Series(math.nan, index=index, dtype="float64")
        return pd.to_numeric(series, errors="coerce").astype("float64")
    if node.kind == "neg":
        return -_eval_expr(node.children[0], values, index)
    if node.kind == "abs":
        return _eval_expr(node.children[0], values, index).abs()
    if node.kind == "sqrt":
        return _eval_expr(node.children[0], values, index).clip(lower=0.0) ** 0.5
    if node.kind == "log":
        part = _eval_expr(node.children[0], values, index)
        return pd.Series(np.log(part.where(part > 0)), index=index, dtype="float64")
    if node.kind == "sign":
        return pd.Series(np.sign(_eval_expr(node.children[0], values, index)), index=index, dtype="float64")

    if node.kind in _ROLLING_CALLS:
        base = _eval_expr(node.children[0], values, index)
        window = int(node.children[1].value)
        if node.kind == "avg":
            return base.rolling(window).mean()
        if node.kind == "std":
            return base.rolling(window).std(ddof=0)
        if node.kind == "hhv":
            return base.rolling(window).max()
        if node.kind == "llv":
            return base.rolling(window).min()
        return base.rolling(window).sum()

    parts = [_eval_expr(child, values, index) for child in node.children]
    if node.kind == "add":
        return parts[0] + parts[1]
    if node.kind == "sub":
        return parts[0] - parts[1]
    if node.kind == "mul":
        return parts[0] * parts[1]
    if node.kind == "div":
        return parts[0] / parts[1].replace(0.0, math.nan)
    if node.kind == "pow":
        return parts[0].clip(lower=0.0) ** parts[1]
    if node.kind == "clip":
        return parts[0].where(parts[0] >= parts[1], parts[1]).where(parts[0] <= parts[2], parts[2])
    frame = pd.concat(parts, axis=1)
    if node.kind == "max":
        return frame.max(axis=1, skipna=False)
    if node.kind == "min":
        return frame.min(axis=1, skipna=False)
    raise RuleConfigError(f"内部错误：未知公式节点 {node.kind!r}")


def _expr_window(node: ExprNode) -> int:
    """公式里用到的最长滚动窗口，用于算预热根数。"""
    if node.kind in _ROLLING_CALLS:
        return int(node.children[1].value)
    best = 0
    for child in node.children:
        best = max(best, _expr_window(child))
    return best


# ---------------------------------------------------------------- 操作数


@dataclass(frozen=True, slots=True)
class Operand:
    """条件里的一边：行情字段、指标，或常量。offset 是往前数几根。"""

    kind: str  # bar | ind | const
    name: str = ""
    value: float = 0.0
    offset: int = 0


def _parse_operand(raw: Any, where: str, indicators: Mapping[str, "IndicatorSpec"]) -> Operand:
    if isinstance(raw, bool):
        raise _fail(where, "布尔值不能当数值用；开关请写成比较条件")
    if isinstance(raw, (int, float)):
        return Operand("const", value=_require_number(raw, where))
    if isinstance(raw, str):
        text = raw.strip()
        if text in _BAR_FIELDS:
            return Operand("bar", name=text)
        if text in indicators:
            return Operand("ind", name=text)
        raise _fail(
            where,
            f"未知字段 {text!r}；只能是 open/high/low/close/volume，或 indicators 里定义过的名字",
        )
    if isinstance(raw, dict):
        keys = set(raw)
        if keys == {"prev"} or keys == {"prev", "n"}:
            offset = _require_int(raw.get("n", 1), f"{where}.n", minimum=1, maximum=1000)
            base = _parse_operand(raw.get("prev"), f"{where}.prev", indicators)
            if base.kind == "const":
                raise _fail(f"{where}.prev", "常量没有「上一根」，只有行情字段和指标可以取 prev")
            return Operand(base.kind, base.name, base.value, offset)
        raise _fail(where, f"操作数只支持 {sorted(_BAR_FIELDS)}、指标名、数字、{{prev, n}}；收到 {sorted(keys)}")
    raise _fail(where, f"不支持的操作数类型 {type(raw).__name__}")


# ---------------------------------------------------------------- 条件树


@dataclass(frozen=True, slots=True)
class Condition:
    """编译后的条件节点。all/any/not 是组合，其余是叶子。"""

    kind: str
    children: tuple["Condition", ...] = ()
    left: Operand | None = None
    right: Operand | None = None
    low: Operand | None = None
    high: Operand | None = None
    bars: int = 0


def _parse_hhmm(raw: Any, where: str) -> float:
    """把 "09:05" 转成当天的分钟数，供 time 条件比较。"""
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match is None:
        raise _fail(where, f'时间要写成 "HH:MM"，例如 "09:05"，收到 {text!r}')
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise _fail(where, f"时间超出范围：{text!r}")
    return float(hour * 60 + minute)


def _parse_condition(
    raw: Any,
    where: str,
    indicators: Mapping[str, "IndicatorSpec"],
    depth: int = 0,
) -> Condition:
    if depth > MAX_DEPTH:
        raise _fail(where, f"条件嵌套超过 {MAX_DEPTH} 层，先把规则拆简单一点")
    node = _require_mapping(raw, where)
    if len(node) != 1:
        raise _fail(where, f"一个条件只能写一个判断，实际写了 {sorted(node)}")
    key, value = next(iter(node.items()))

    if key == "all" or key == "any":
        if not isinstance(value, list) or not value:
            raise _fail(f"{where}.{key}", "必须是非空列表")
        children = tuple(
            _parse_condition(item, f"{where}.{key}[{index}]", indicators, depth + 1)
            for index, item in enumerate(value)
        )
        return Condition(key, children=children)

    if key == "not":
        child = _parse_condition(value, f"{where}.not", indicators, depth + 1)
        return Condition("not", children=(child,))

    if key in _COMPARE_KEYS or key in _CROSS_KEYS:
        sides = _require_mapping(value, f"{where}.{key}")
        if set(sides) != {"left", "right"}:
            raise _fail(f"{where}.{key}", "必须且只能写 left 和 right")
        return Condition(
            key,
            left=_parse_operand(sides["left"], f"{where}.{key}.left", indicators),
            right=_parse_operand(sides["right"], f"{where}.{key}.right", indicators),
        )

    if key == "between":
        parts = _require_mapping(value, f"{where}.between")
        if set(parts) != {"value", "low", "high"}:
            raise _fail(f"{where}.between", "必须且只能写 value、low、high")
        return Condition(
            "between",
            left=_parse_operand(parts["value"], f"{where}.between.value", indicators),
            low=_parse_operand(parts["low"], f"{where}.between.low", indicators),
            high=_parse_operand(parts["high"], f"{where}.between.high", indicators),
        )

    if key == "rising" or key == "falling":
        parts = _require_mapping(value, f"{where}.{key}")
        if set(parts) != {"ref", "bars"}:
            raise _fail(f"{where}.{key}", "必须且只能写 ref 和 bars")
        bars = _require_int(parts["bars"], f"{where}.{key}.bars", minimum=1, maximum=50)
        return Condition(
            key,
            left=_parse_operand(parts["ref"], f"{where}.{key}.ref", indicators),
            bars=bars,
        )

    if key in {"break_high", "break_low"}:
        parts = _require_mapping(value, f"{where}.{key}")
        if set(parts) == {"period"}:
            bars = _require_int(parts["period"], f"{where}.{key}.period", minimum=2, maximum=500)
            return Condition(key, bars=bars)
        if set(parts) == {"ref"}:
            return Condition(key, left=_parse_operand(parts["ref"], f"{where}.{key}.ref", indicators))
        raise _fail(f"{where}.{key}", "写 period（自动取前 N 根高低点）或 ref（显式给参照值）")

    if key in {"sustained", "bars_since"}:
        parts = _require_mapping(value, f"{where}.{key}")
        span_key = "bars" if key == "sustained" else "within"
        if set(parts) != {"cond", span_key}:
            raise _fail(f"{where}.{key}", f"必须且只能写 cond 和 {span_key}")
        child = _parse_condition(parts["cond"], f"{where}.{key}.cond", indicators, depth + 1)
        span = _require_int(parts[span_key], f"{where}.{key}.{span_key}", minimum=1, maximum=50)
        return Condition(key, children=(child,), bars=span)

    if key == "time":
        parts = _require_mapping(value, f"{where}.time")
        if set(parts) != {"from", "to"}:
            raise _fail(f"{where}.time", '必须且只能写 from 和 to，例如 {from: "09:05", to: "14:45"}')
        return Condition(
            "time",
            left=Operand("const", value=_parse_hhmm(parts["from"], f"{where}.time.from")),
            right=Operand("const", value=_parse_hhmm(parts["to"], f"{where}.time.to")),
        )

    raise _fail(
        where,
        f"未知判断 {key!r}；可用：all/any/not/{'/'.join(sorted(_COMPARE_KEYS))}/"
        f"{'/'.join(sorted(_CROSS_KEYS))}/between/rising/falling/"
        "break_high/break_low/sustained/bars_since/time",
    )


# ---------------------------------------------------------------- 指标与止盈止损


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """一条指标的编译结果。period 只对单一周期的指标有意义，column 是取值列。"""

    kind: str
    period: int = 0
    column: str = ""  # highest/lowest/均线类的取值列，默认见各指标
    params: Mapping[str, Any] = field(default_factory=dict)
    formula: str = ""  # expr 的原始公式，报错时回显给用户
    expr: ExprNode | None = None


@dataclass(frozen=True, slots=True)
class StopSpec:
    """初始止损怎么算。"""

    kind: str
    mult: float = 0.0
    atr_name: str = ""
    pct: float = 0.0
    lookback: int = 0


@dataclass(frozen=True, slots=True)
class BreakevenSpec:
    """盈利到多少倍 ATR 后把止损抬到开仓价。"""

    trigger_atr: float
    atr_name: str


# ---------------------------------------------------------------- 编译结果


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """校验通过的规则。外部只能读，不能在运行中改。"""

    name: str
    source: str
    timeframe: str | None
    indicators: Mapping[str, IndicatorSpec]
    filters: Condition | None
    entries: Mapping[str, Condition]
    exits: Mapping[str, Condition]
    stop: StopSpec
    required_bars: int
    # 下面几项是可选的升级能力；不写就是原来的固定止损 + 只靠 entry/exit。
    filters_htf: Condition | None = None
    trail: StopSpec | None = None
    breakeven: BreakevenSpec | None = None
    max_hold_bars: int = 0

    def values(self, bars: pd.DataFrame) -> dict[str, pd.Series]:
        """把行情字段和指标算成一组同索引的序列，一次算完供条件树复用。"""
        out: dict[str, pd.Series] = {}
        for name in _BAR_FIELDS:
            if name in bars.columns:
                out[name] = pd.to_numeric(bars[name], errors="coerce")
        index = bars.index
        for name, spec in self.indicators.items():
            out[name] = _compute_indicator(spec, out, index)
        minutes = _bar_minutes(index)
        if minutes is not None:
            out["@time"] = minutes
        return out

    def signal(self, bars: pd.DataFrame, values: Mapping[str, pd.Series] | None = None) -> str | None:
        """这根收盘时该开哪个方向。多空同时成立返回 None（宁可不做）。"""
        if len(bars) < self.required_bars:
            return None
        vals = values if values is not None else self.values(bars)
        if self.filters is not None and not evaluate_condition(self.filters, vals):
            return None
        long_ok = _hit(self.entries.get("long"), vals)
        short_ok = _hit(self.entries.get("short"), vals)
        if long_ok and short_ok:
            return None
        if long_ok:
            return "long"
        if short_ok:
            return "short"
        return None

    def exit_hit(self, side: str, bars: pd.DataFrame, values: Mapping[str, pd.Series] | None = None) -> bool:
        """持仓时该不该平。没有写 exit 就返回 False，只靠止损。"""
        if len(bars) < self.required_bars:
            return False
        node = self.exits.get(side)
        if node is None:
            return False
        vals = values if values is not None else self.values(bars)
        return evaluate_condition(node, vals)

    def initial_stop(self, side: str, bars: pd.DataFrame, price: float) -> float | None:
        """按当根数据算初始止损价。side 只能是 long / short。"""
        if side not in {"long", "short"}:
            raise RuleConfigError(f"initial_stop: 方向只能是 long/short，收到 {side!r}")
        if self.stop.kind == "none":
            return None
        if self.stop.kind == "percent":
            factor = 1 - self.stop.pct / 100 if side == "long" else 1 + self.stop.pct / 100
            return float(price) * factor
        if self.stop.kind == "swing":
            column = "low" if side == "long" else "high"
            series = pd.to_numeric(bars[column], errors="coerce").rolling(self.stop.lookback)
            window = series.min() if side == "long" else series.max()
            value = window.iloc[-1]
            return None if pd.isna(value) else float(value)
        atr = self.values(bars).get(self.stop.atr_name)
        if atr is None or atr.empty or pd.isna(atr.iloc[-1]):
            return None
        pad = self.stop.mult * float(atr.iloc[-1])
        return float(price) - pad if side == "long" else float(price) + pad

    def trail_level(
        self,
        side: str,
        bars: pd.DataFrame,
        values: Mapping[str, pd.Series] | None = None,
    ) -> float | None:
        """按当根数据算移动止损位。调用方负责只朝有利方向抬，不要放宽已有止损。"""
        spec = self.trail
        if spec is None or side not in {"long", "short"}:
            return None
        vals = values if values is not None else self.values(bars)
        if spec.kind == "percent":
            close = _last_value(vals.get("close"))
            if math.isnan(close):
                return None
            factor = 1 - spec.pct / 100 if side == "long" else 1 + spec.pct / 100
            return close * factor
        if spec.kind == "swing":
            series = vals.get("low" if side == "long" else "high")
            if series is None or len(series) < spec.lookback:
                return None
            window = series.rolling(spec.lookback).min() if side == "long" else series.rolling(spec.lookback).max()
            value = _last_value(window)
            return None if math.isnan(value) else value
        atr = _last_value(vals.get(spec.atr_name))
        close = _last_value(vals.get("close"))
        if math.isnan(atr) or math.isnan(close):
            return None
        pad = spec.mult * atr
        return close - pad if side == "long" else close + pad

    def breakeven_level(
        self,
        side: str,
        bars: pd.DataFrame,
        entry: float,
        values: Mapping[str, pd.Series] | None = None,
    ) -> float | None:
        """浮盈达到 trigger × ATR 后返回开仓价，调用方据此把止损抬到保本。"""
        spec = self.breakeven
        if spec is None or side not in {"long", "short"} or not entry:
            return None
        vals = values if values is not None else self.values(bars)
        atr = _last_value(vals.get(spec.atr_name))
        close = _last_value(vals.get("close"))
        if math.isnan(atr) or math.isnan(close):
            return None
        profit = close - entry if side == "long" else entry - close
        return float(entry) if profit >= spec.trigger_atr * atr else None


def _hit(node: Condition | None, values: Mapping[str, pd.Series]) -> bool:
    return bool(node is not None and evaluate_condition(node, values))


def _series_of(operand: Operand, values: Mapping[str, pd.Series]) -> pd.Series | None:
    if operand.kind == "const":
        return None
    return values.get(operand.name)


def _at(operand: Operand, values: Mapping[str, pd.Series]) -> float:
    """取操作数在指定偏移上的值。越界或空值返回 NaN。"""
    if operand.kind == "const":
        return operand.value
    series = _series_of(operand, values)
    if series is None:
        return math.nan
    index = len(series) - 1 - operand.offset
    if index < 0:
        return math.nan
    try:
        value = series.iloc[index]
    except (IndexError, KeyError):
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _valid(*numbers: float) -> bool:
    return all(not math.isnan(number) for number in numbers)


def _last_value(series: pd.Series | None) -> float:
    """取序列最后一根的数；空序列或空值返回 NaN。"""
    if series is None or len(series) == 0:
        return math.nan
    try:
        return float(series.iloc[-1])
    except (TypeError, ValueError):
        return math.nan


def _shift_values(values: Mapping[str, pd.Series], offset: int) -> Mapping[str, pd.Series]:
    """把所有序列整体往前挪 offset 根，用来回看历史条件是否成立。"""
    if offset <= 0:
        return values
    return {name: series.iloc[: max(0, len(series) - offset)] for name, series in values.items()}


def _window_extreme(series: pd.Series, bars: int, highest: bool) -> float:
    """前 bars 根（不含当根）的最高/最低。"""
    if len(series) < bars + 1:
        return math.nan
    window = series.iloc[-1 - bars : -1]
    if window.empty or window.isna().any():
        return math.nan
    try:
        return float(window.max() if highest else window.min())
    except (TypeError, ValueError):
        return math.nan


def evaluate_condition(node: Condition, values: Mapping[str, pd.Series]) -> bool:
    """在当前这根（最后一根）上判断条件是否成立。空值一律当不成立。"""
    if node.kind == "all":
        return all(evaluate_condition(child, values) for child in node.children)
    if node.kind == "any":
        return any(evaluate_condition(child, values) for child in node.children)
    if node.kind == "not":
        return not evaluate_condition(node.children[0], values)

    if node.kind in _COMPARE_KEYS or node.kind in _CROSS_KEYS:
        assert node.left is not None and node.right is not None
        if node.kind in _CROSS_KEYS:
            left_now, right_now = _at(node.left, values), _at(node.right, values)
            left_prev = _at(Operand(node.left.kind, node.left.name, node.left.value, node.left.offset + 1), values)
            right_prev = _at(Operand(node.right.kind, node.right.name, node.right.value, node.right.offset + 1), values)
            if not _valid(left_now, right_now, left_prev, right_prev):
                return False
            if node.kind == "cross_above":
                return left_prev <= right_prev and left_now > right_now
            return left_prev >= right_prev and left_now < right_now

        left, right = _at(node.left, values), _at(node.right, values)
        if not _valid(left, right):
            return False
        if node.kind == "gt":
            return left > right
        if node.kind == "gte":
            return left >= right
        if node.kind == "lt":
            return left < right
        if node.kind == "lte":
            return left <= right
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)

    if node.kind == "between":
        assert node.left is not None and node.low is not None and node.high is not None
        value, low, high = _at(node.left, values), _at(node.low, values), _at(node.high, values)
        return _valid(value, low, high) and low <= value <= high

    if node.kind == "rising" or node.kind == "falling":
        assert node.left is not None
        series = _series_of(node.left, values)
        if series is None or len(series) < node.bars + 1 + node.left.offset:
            return False
        window = series.iloc[len(series) - 1 - node.left.offset - node.bars : len(series) - node.left.offset]
        if window.isna().any():
            return False
        diffs = window.diff().dropna()
        if node.kind == "rising":
            return bool((diffs >= 0).all())
        return bool((diffs <= 0).all())

    if node.kind in {"break_high", "break_low"}:
        series = values.get("high" if node.kind == "break_high" else "low")
        now = _last_value(series)
        if node.left is not None:
            ref = _at(node.left, values)
        elif series is not None:
            ref = _window_extreme(series, node.bars, node.kind == "break_high")
        else:
            return False
        if not _valid(now, ref):
            return False
        return now > ref if node.kind == "break_high" else now < ref

    if node.kind in {"sustained", "bars_since"}:
        child = node.children[0]
        if not values:
            return False
        if node.kind == "sustained":
            return all(evaluate_condition(child, _shift_values(values, k)) for k in range(node.bars))
        return any(evaluate_condition(child, _shift_values(values, k)) for k in range(node.bars))

    if node.kind == "time":
        minute = _last_value(values.get("@time"))
        if math.isnan(minute):
            return False
        start = node.left.value if node.left is not None else 0.0
        end = node.right.value if node.right is not None else 24 * 60.0
        if start <= end:
            return start <= minute <= end
        # 跨零点，例如 21:00 → 02:30
        return minute >= start or minute <= end

    raise RuleConfigError(f"内部错误：未知条件类型 {node.kind!r}")


def _datetime_index(index: pd.Index) -> pd.DatetimeIndex | None:
    """尽量把 K 线索引转成时间索引；转不了返回 None。"""
    if isinstance(index, pd.DatetimeIndex):
        return index
    if isinstance(index, pd.RangeIndex) or pd.api.types.is_numeric_dtype(index):
        return None
    try:
        converted = pd.to_datetime(pd.Index(index))
    except (TypeError, ValueError):
        return None
    if isinstance(converted, pd.DatetimeIndex):
        return converted
    return None


def _bar_minutes(index: pd.Index) -> pd.Series | None:
    """每根 K 线的「当天第几分钟」，供 time 条件比较。"""
    ts = _datetime_index(index)
    if ts is None:
        return None
    return pd.Series(ts.hour * 60 + ts.minute, index=index, dtype="float64")


def _trade_days(index: pd.Index) -> pd.Series | None:
    """每根 K 线属于哪个交易日。18:00 之后的夜盘算下一交易日。"""
    ts = _datetime_index(index)
    if ts is None:
        return None
    night = pd.to_timedelta((ts.hour >= 18).astype(int), unit="D")
    days = (ts.normalize() + night).normalize()
    return pd.Series(days, index=index)


def _session_series(
    spec: IndicatorSpec,
    values: Mapping[str, pd.Series],
    index: pd.Index,
) -> pd.Series:
    """会话锚定指标：当日开盘、昨高昨低昨收、开盘区间、VWAP。"""
    days = _trade_days(index)
    if days is None:
        raise RuleConfigError(
            f"indicators.{spec.kind} 需要带时间的 K 线索引（日线/分钟线都可以），"
            "当前数据没有时间列，请检查数据源"
        )
    open_ = values.get("open")
    high = values.get("high")
    low = values.get("low")
    close = values.get("close")
    empty = pd.Series(math.nan, index=index, dtype="float64")
    if open_ is None or high is None or low is None or close is None:
        return empty

    if spec.kind == "day_open":
        return open_.groupby(days).transform("first")
    if spec.kind == "prev_day_high":
        daily = high.groupby(days).max()
        return days.map(daily.shift(1))
    if spec.kind == "prev_day_low":
        daily = low.groupby(days).min()
        return days.map(daily.shift(1))
    if spec.kind == "prev_day_close":
        daily = close.groupby(days).last()
        return days.map(daily.shift(1))
    if spec.kind == "vwap":
        volume = values.get("volume")
        if volume is None:
            return empty
        typical = (high + low + close) / 3.0
        amount = (typical * volume).groupby(days).cumsum()
        total = volume.groupby(days).cumsum()
        return amount / total.replace(0, math.nan)
    if spec.kind in {"opening_range_high", "opening_range_low"}:
        upper = spec.kind == "opening_range_high"
        source = high if upper else low
        running = source.groupby(days).cummax() if upper else source.groupby(days).cummin()
        position = days.groupby(days).cumcount()
        frozen = running.where(position < max(1, spec.period))
        return frozen.groupby(days).ffill()
    return empty


def _source_series(spec: IndicatorSpec, values: Mapping[str, pd.Series], index: pd.Index) -> pd.Series:
    """指标取哪条行情序列。默认收盘价，可用 source 改成 open/high/low/volume。"""
    name = spec.column or "close"
    series = values.get(name)
    if series is None:
        return pd.Series(math.nan, index=index, dtype="float64")
    return pd.to_numeric(series, errors="coerce")


def _compute_indicator(
    spec: IndicatorSpec,
    values: Mapping[str, pd.Series],
    index: pd.Index,
) -> pd.Series:
    """按 spec 算一条指标。调用前已校验过参数，这里不再兜底。"""
    close = values.get("close")
    high = values.get("high")
    low = values.get("low")
    if spec.kind in {
        "vwap",
        "day_open",
        "prev_day_high",
        "prev_day_low",
        "prev_day_close",
        "opening_range_high",
        "opening_range_low",
    }:
        return _session_series(spec, values, index)
    if spec.kind == "ema":
        return ema_series(_source_series(spec, values, index), spec.period)
    if spec.kind == "sma":
        return _source_series(spec, values, index).rolling(spec.period).mean()
    if spec.kind == "stddev":
        return _source_series(spec, values, index).rolling(spec.period).std(ddof=0)
    if spec.kind == "atr":
        assert high is not None and low is not None and close is not None
        return average_true_range(high, low, close, period=spec.period)
    if spec.kind == "atr_pct":
        assert high is not None and low is not None and close is not None
        atr = average_true_range(high, low, close, period=spec.period)
        return atr / close.replace(0, math.nan) * 100.0
    if spec.kind in {"adx", "plus_di", "minus_di"}:
        assert high is not None and low is not None and close is not None
        adx, plus, minus = adx_series(high, low, close, period=spec.period)
        if spec.kind == "adx":
            return adx
        return plus if spec.kind == "plus_di" else minus
    if spec.kind == "highest":
        source = values.get(spec.column or "high")
        if source is None:
            return pd.Series(math.nan, index=index, dtype="float64")
        return source.rolling(spec.period).max()
    if spec.kind == "lowest":
        source = values.get(spec.column or "low")
        if source is None:
            return pd.Series(math.nan, index=index, dtype="float64")
        return source.rolling(spec.period).min()
    if spec.kind == "donchian_mid":
        assert high is not None and low is not None
        upper = high.rolling(spec.period).max()
        lower = low.rolling(spec.period).min()
        return (upper + lower) / 2.0
    if spec.kind in {"macd_hist", "macd_dif", "macd_dea"}:
        assert close is not None
        frame = macd_lines(
            close,
            fast=int(spec.params["fast"]),
            slow=int(spec.params["slow"]),
            signal=int(spec.params["signal"]),
        )
        return frame[{"macd_hist": "hist", "macd_dif": "dif", "macd_dea": "dea"}[spec.kind]]
    if spec.kind in {"boll_upper", "boll_lower", "boll_mid", "boll_width"}:
        assert close is not None
        middle = close.rolling(spec.period).mean()
        deviation = close.rolling(spec.period).std(ddof=0)
        offset = deviation * float(spec.params.get("std", 2.0))
        if spec.kind == "boll_upper":
            return middle + offset
        if spec.kind == "boll_lower":
            return middle - offset
        if spec.kind == "boll_mid":
            return middle
        return offset * 2.0 / middle.replace(0, math.nan) * 100.0
    if spec.kind in {"keltner_upper", "keltner_lower"}:
        assert high is not None and low is not None and close is not None
        middle = ema_series(close, spec.period)
        atr = average_true_range(high, low, close, period=int(spec.params["atr_period"]))
        offset = atr * float(spec.params["mult"])
        return middle + offset if spec.kind == "keltner_upper" else middle - offset
    if spec.kind in {"stoch_k", "stoch_d"}:
        assert high is not None and low is not None and close is not None
        period = spec.period
        highest = high.rolling(period).max()
        lowest = low.rolling(period).min()
        raw_k = (close - lowest) / (highest - lowest).replace(0, math.nan) * 100.0
        smooth = int(spec.params["smooth"])
        k = raw_k.rolling(smooth).mean() if smooth > 1 else raw_k
        if spec.kind == "stoch_k":
            return k
        return k.rolling(int(spec.params["d"])).mean()
    if spec.kind == "cci":
        assert high is not None and low is not None and close is not None
        typical = (high + low + close) / 3.0
        average = typical.rolling(spec.period).mean()
        deviation = typical.rolling(spec.period).apply(
            lambda window: float(np.abs(window - window.mean()).mean()), raw=True
        )
        return (typical - average) / (0.015 * deviation.replace(0, math.nan))
    if spec.kind == "williams_r":
        assert high is not None and low is not None and close is not None
        highest = high.rolling(spec.period).max()
        lowest = low.rolling(spec.period).min()
        return -100.0 * (highest - close) / (highest - lowest).replace(0, math.nan)
    if spec.kind == "roc":
        source = _source_series(spec, values, index)
        past = source.shift(spec.period)
        return (source - past) / past.replace(0, math.nan) * 100.0
    if spec.kind == "obv":
        assert close is not None
        volume = values.get("volume")
        if volume is None:
            return pd.Series(math.nan, index=index, dtype="float64")
        direction = np.sign(close.diff().fillna(0.0))
        return (direction * volume.fillna(0.0)).cumsum()
    if spec.kind == "vol_sma":
        volume = values.get("volume")
        if volume is None:
            return pd.Series(math.nan, index=index, dtype="float64")
        return volume.rolling(spec.period).mean()
    if spec.kind == "supertrend":
        assert high is not None and low is not None and close is not None
        line, _direction = supertrend(
            high, low, close, period=spec.period, multiplier=float(spec.params["mult"])
        )
        return line
    if spec.kind == "psar":
        assert high is not None and low is not None
        return parabolic_sar(
            high,
            low,
            af_start=float(spec.params["af_start"]),
            af_step=float(spec.params["af_step"]),
            af_max=float(spec.params["af_max"]),
        )
    if spec.kind == "rsi":
        assert close is not None
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / spec.period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / spec.period, adjust=False).mean()
        rs = gain / loss.replace(0, math.nan)
        return 100 - 100 / (1 + rs)
    if spec.kind == "expr":
        assert spec.expr is not None
        return _eval_expr(spec.expr, values, index)
    raise RuleConfigError(f"内部错误：未知指标 {spec.kind!r}")


# ---------------------------------------------------------------- 编译入口


def _parse_source(spec: Mapping[str, Any], where: str, default: str = "close") -> str:
    """指标的取值列。默认收盘价，可改成 open/high/low/volume。"""
    field = str(spec.get("source") or default).strip().lower()
    if field not in _BAR_FIELDS:
        raise _fail(f"{where}.source", f"只能取 {', '.join(sorted(_BAR_FIELDS))}，收到 {field!r}")
    return field


def _parse_indicators(raw: Any) -> dict[str, IndicatorSpec]:
    """indicators 可以省略：只靠行情字段 + break_high/time 这类条件也能成规则。"""
    box = _require_mapping({} if raw is None else raw, "indicators")
    all_names = [str(name) for name in box]
    specs: dict[str, IndicatorSpec] = {}
    for position, (name, item) in enumerate(box.items()):
        where = f"indicators.{name}"
        if not isinstance(name, str) or not name.strip():
            raise _fail("indicators", f"指标名必须是非空字符串，实际是 {name!r}")
        if name in _BAR_FIELDS:
            raise _fail(where, "指标名不能和行情字段重名（open/high/low/close/volume）")
        spec = _require_mapping(item, where)
        kind = str(spec.get("kind") or "").strip().lower()
        if kind not in _INDICATOR_KINDS:
            raise _fail(f"{where}.kind", f"未知指标 {kind!r}；可用：{', '.join(sorted(_INDICATOR_KINDS))}")
        allowed = {"kind"}
        if kind in {"macd_hist", "macd_dif", "macd_dea"}:
            allowed |= {"fast", "slow", "signal"}
            params = {
                "fast": _require_int(spec.get("fast", 12), f"{where}.fast"),
                "slow": _require_int(spec.get("slow", 26), f"{where}.slow"),
                "signal": _require_int(spec.get("signal", 9), f"{where}.signal"),
            }
            if params["fast"] >= params["slow"]:
                raise _fail(where, "fast 必须小于 slow")
            specs[name] = IndicatorSpec(kind, params=params)
        elif kind in {"boll_upper", "boll_lower", "boll_mid", "boll_width"}:
            allowed |= {"period", "std"}
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", 20), f"{where}.period", minimum=2),
                params={"std": _require_number(spec.get("std", 2.0), f"{where}.std", positive=True)},
            )
        elif kind in {"highest", "lowest"}:
            allowed |= {"period", "field"}
            field = str(spec.get("field") or ("high" if kind == "highest" else "low")).strip().lower()
            if field not in {"open", "high", "low", "close"}:
                raise _fail(f"{where}.field", f"只能取 open/high/low/close，收到 {field!r}")
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", 20), f"{where}.period", minimum=2),
                column=field,
            )
        elif kind in {"ema", "sma", "stddev", "roc"}:
            allowed |= {"period", "source"}
            default = 12 if kind == "roc" else 20
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", default), f"{where}.period", minimum=2),
                column=_parse_source(spec, where),
            )
        elif kind in {"keltner_upper", "keltner_lower"}:
            allowed |= {"period", "atr_period", "mult"}
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", 20), f"{where}.period", minimum=2),
                params={
                    "atr_period": _require_int(spec.get("atr_period", 20), f"{where}.atr_period", minimum=2),
                    "mult": _require_number(spec.get("mult", 2.0), f"{where}.mult", positive=True),
                },
            )
        elif kind in {"stoch_k", "stoch_d"}:
            allowed |= {"period", "smooth", "d"}
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", 14), f"{where}.period", minimum=2),
                params={
                    "smooth": _require_int(spec.get("smooth", 3), f"{where}.smooth"),
                    "d": _require_int(spec.get("d", 3), f"{where}.d"),
                },
            )
        elif kind == "supertrend":
            allowed |= {"period", "mult"}
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", 10), f"{where}.period", minimum=2),
                params={"mult": _require_number(spec.get("mult", 3.0), f"{where}.mult", positive=True)},
            )
        elif kind == "psar":
            allowed |= {"af_start", "af_step", "af_max"}
            specs[name] = IndicatorSpec(
                kind,
                params={
                    "af_start": _require_number(spec.get("af_start", 0.02), f"{where}.af_start", positive=True),
                    "af_step": _require_number(spec.get("af_step", 0.02), f"{where}.af_step", positive=True),
                    "af_max": _require_number(spec.get("af_max", 0.2), f"{where}.af_max", positive=True),
                },
            )
        elif kind in {"opening_range_high", "opening_range_low"}:
            allowed |= {"period"}
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", 30), f"{where}.period", minimum=1, maximum=240),
            )
        elif kind in {"day_open", "prev_day_high", "prev_day_low", "prev_day_close", "vwap", "obv"}:
            specs[name] = IndicatorSpec(kind)
        elif kind == "expr":
            allowed |= {"formula"}
            available = frozenset(_BAR_FIELDS) | frozenset(specs)
            later = frozenset(all_names[position + 1 :])
            formula = str(spec.get("formula") or "").strip()
            specs[name] = IndicatorSpec(
                kind,
                formula=formula,
                expr=_parse_formula(formula, f"{where}.formula", available, later),
            )
        else:
            allowed |= {"period"}
            default = 14 if kind in {"atr", "rsi", "adx", "plus_di", "minus_di", "cci", "williams_r", "atr_pct"} else 20
            specs[name] = IndicatorSpec(
                kind,
                period=_require_int(spec.get("period", default), f"{where}.period", minimum=2),
            )
        unknown = set(spec) - allowed
        if unknown:
            raise _fail(where, f"不认识的键 {sorted(unknown)}；{kind} 只支持 {sorted(allowed)}")
    return specs


def _parse_stop(raw: Any, indicators: Mapping[str, IndicatorSpec], *, where: str = "stop") -> StopSpec:
    box = _require_mapping(raw, where)
    kind = str(box.get("kind") or "").strip().lower()
    if kind not in _STOP_KINDS:
        raise _fail(f"{where}.kind", f"未知止损方式 {kind!r}；可用：{', '.join(sorted(_STOP_KINDS))}")
    if kind == "atr":
        allowed = {"kind", "mult", "atr_ref"}
        atr_ref = str(box.get("atr_ref") or "").strip()
        spec = indicators.get(atr_ref)
        if spec is None or spec.kind != "atr":
            raise _fail(f"{where}.atr_ref", f"必须指向 indicators 里一条 kind=atr 的指标，收到 {atr_ref!r}")
        result = StopSpec(
            "atr",
            mult=_require_number(box.get("mult"), f"{where}.mult", positive=True),
            atr_name=atr_ref,
        )
    elif kind == "percent":
        allowed = {"kind", "pct"}
        result = StopSpec("percent", pct=_require_number(box.get("pct"), f"{where}.pct", positive=True))
        if result.pct >= 50:
            raise _fail(f"{where}.pct", f"百分比止损不能超过 50%，实际是 {result.pct}")
    elif kind == "swing":
        allowed = {"kind", "lookback"}
        result = StopSpec("swing", lookback=_require_int(box.get("lookback"), f"{where}.lookback", minimum=2))
    else:
        allowed = {"kind"}
        result = StopSpec("none")
    unknown = set(box) - allowed
    if unknown:
        raise _fail(where, f"不认识的键 {sorted(unknown)}；{kind} 只支持 {sorted(allowed)}")
    return result


def _parse_trail(raw: Any, indicators: Mapping[str, IndicatorSpec]) -> StopSpec:
    """移动止损。沿用 stop 的三种算法，只是每根都往有利方向抬。"""
    result = _parse_stop(raw, indicators, where="trail")
    if result.kind == "none":
        raise _fail("trail.kind", "移动止损不能写 none；不想用就把整个 trail 段删掉")
    return result


def _parse_breakeven(raw: Any, indicators: Mapping[str, IndicatorSpec]) -> BreakevenSpec:
    box = _require_mapping(raw, "breakeven")
    unknown = set(box) - {"atr", "atr_ref"}
    if unknown:
        raise _fail("breakeven", f"只能写 atr 和 atr_ref，收到 {sorted(unknown)}")
    atr_ref = str(box.get("atr_ref") or "").strip()
    if not atr_ref:
        atr_ref = next((name for name, spec in indicators.items() if spec.kind == "atr"), "")
    spec = indicators.get(atr_ref)
    if spec is None or spec.kind != "atr":
        raise _fail("breakeven.atr_ref", f"必须指向 indicators 里一条 kind=atr 的指标，收到 {atr_ref!r}")
    return BreakevenSpec(
        trigger_atr=_require_number(box.get("atr", 1.0), "breakeven.atr", positive=True),
        atr_name=atr_ref,
    )


def _parse_side_conditions(raw: Any, where: str, indicators: Mapping[str, IndicatorSpec]) -> dict[str, Condition]:
    box = _require_mapping(raw, where)
    unknown = set(box) - {"long", "short"}
    if unknown:
        raise _fail(where, f"只能写 long 和 short，收到 {sorted(unknown)}")
    return {side: _parse_condition(node, f"{where}.{side}", indicators) for side, node in box.items()}


def _required_bars(indicators: Mapping[str, IndicatorSpec]) -> int:
    """预热根数：最大周期 + 2，因为交叉判断要比较当根和上一根。"""
    need = 3
    for spec in indicators.values():
        if spec.kind in {"macd_hist", "macd_dif", "macd_dea"}:
            need = max(need, int(spec.params["slow"]) + int(spec.params["signal"]) + 2)
        elif spec.kind in {"keltner_upper", "keltner_lower"}:
            need = max(need, spec.period + 2, int(spec.params["atr_period"]) + 2)
        elif spec.kind in {"stoch_k", "stoch_d"}:
            need = max(need, spec.period + int(spec.params["smooth"]) + int(spec.params["d"]) + 1)
        elif spec.kind == "expr" and spec.expr is not None:
            need = max(need, _expr_window(spec.expr) + 2)
        elif spec.period:
            need = max(need, spec.period + 2)
    return need


def compile_rule(raw: Mapping[str, Any], *, source: str = "<inline>") -> CompiledRule:
    """把 YAML 读出来的字典校验成 CompiledRule。任何不合法的写法都抛 RuleConfigError。"""
    root = _require_mapping(raw, source)
    unknown = set(root) - _ROOT_KEYS
    if unknown:
        raise _fail(source, f"不认识的顶层键 {sorted(unknown)}；可用：{sorted(_ROOT_KEYS)}")

    schema = root.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise _fail(f"{source}.schema", f"只支持 {SCHEMA_VERSION}，收到 {schema!r}")

    sizing = root.get("sizing")
    if sizing is not None:
        box = _require_mapping(sizing, f"{source}.sizing")
        unknown = set(box) - {"mode"}
        if unknown:
            raise _fail(f"{source}.sizing", f"只能写 mode，收到 {sorted(unknown)}")
        if str(box.get("mode") or "inherit").strip().lower() != "inherit":
            raise _fail(f"{source}.sizing.mode", "第一档只支持 inherit（沿用配置页的仓位设置）")

    indicators = _parse_indicators(root.get("indicators"))
    entries = _parse_side_conditions(root.get("entry"), "entry", indicators)
    if not entries:
        raise _fail("entry", "至少要写 long 或 short 一组条件")
    exits = _parse_side_conditions(root.get("exit", {}), "exit", indicators) if "exit" in root else {}

    filters: Condition | None = None
    if "filters" in root:
        raw_filters = root.get("filters")
        if isinstance(raw_filters, list):
            if not raw_filters:
                raise _fail("filters", "列表不能为空；不想要过滤就整段删掉")
            filters = Condition(
                "all",
                children=tuple(
                    _parse_condition(item, f"filters[{index}]", indicators) for index, item in enumerate(raw_filters)
                ),
            )
        else:
            filters = _parse_condition(raw_filters, "filters", indicators)

    filters_htf: Condition | None = None
    if "filters_htf" in root:
        raw_htf = root.get("filters_htf")
        if isinstance(raw_htf, list):
            if not raw_htf:
                raise _fail("filters_htf", "列表不能为空；不想要过滤就整段删掉")
            filters_htf = Condition(
                "all",
                children=tuple(
                    _parse_condition(item, f"filters_htf[{index}]", indicators)
                    for index, item in enumerate(raw_htf)
                ),
            )
        else:
            filters_htf = _parse_condition(raw_htf, "filters_htf", indicators)

    timeframe = root.get("timeframe")
    if timeframe is not None:
        timeframe = str(timeframe).strip()
        if not timeframe:
            raise _fail(f"{source}.timeframe", "不能是空字符串")

    max_hold_bars = 0
    if "max_hold_bars" in root:
        max_hold_bars = _require_int(root.get("max_hold_bars"), "max_hold_bars", minimum=1, maximum=5000)

    name = str(root.get("name") or Path(source).stem or "自定义规则").strip()

    return CompiledRule(
        name=name,
        source=source,
        timeframe=timeframe,
        indicators=indicators,
        filters=filters,
        entries=entries,
        exits=exits,
        stop=_parse_stop(root.get("stop"), indicators),
        required_bars=_required_bars(indicators),
        filters_htf=filters_htf,
        trail=_parse_trail(root["trail"], indicators) if "trail" in root else None,
        breakeven=_parse_breakeven(root["breakeven"], indicators) if "breakeven" in root else None,
        max_hold_bars=max_hold_bars,
    )


def _parse_rule_yaml(text: str, source: str) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"规则文件 {source} 不是合法 YAML: {exc}") from exc
    if raw is None:
        raise RuleConfigError(f"规则文件 {source} 是空的")
    return raw


def load_rule_file(path: str | Path) -> CompiledRule:
    """读一个 YAML 规则文件并编译。读不到、解析失败、校验失败都抛 RuleConfigError。"""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleConfigError(f"读不到规则文件 {file_path}: {exc}") from exc
    return compile_rule(_parse_rule_yaml(text, str(file_path)), source=str(file_path))


# ---------------------------------------------------------------- 规则文件管理
#
# 配置页的「策略编辑器」和策略引擎注册都走这里：
# 一个 rules/*.yaml 就是一个可选策略，引擎代号是 rule_dsl:<文件名>。

RULE_ENGINE_PREFIX = "rule_dsl:"
_RULE_STEM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

NEW_RULE_TEXT = """# 新建规则：改完点「保存」，程序会把它注册成一个策略。
schema: 1
name: "我的规则"

indicators:
  ma20: { kind: ema, period: 20 }

entry:
  long: { gt: { left: close, right: ma20 } }
  short: { lt: { left: close, right: ma20 } }

stop: { kind: percent, pct: 2 }

sizing:
  mode: inherit
"""


# 指标库：编辑器里点一下就能把 YAML 片段插进编辑框，同时显示计算公式。
# kind 和参数必须和 _parse_indicators 里的实现对得上。
INDICATOR_LIBRARY: list[dict[str, Any]] = [
    {
        "kind": "ema",
        "label": "指数均线 EMA",
        "formula": "EMA_t = α × price_t + (1-α) × EMA_{t-1}，α = 2/(period+1)",
        "params": [{"name": "period", "default": 20, "desc": "周期"}],
        "example": "  ma20: { kind: ema, period: 20 }",
    },
    {
        "kind": "sma",
        "label": "简单均线 SMA",
        "formula": "SMA_t = mean(close[t-period+1 : t])",
        "params": [{"name": "period", "default": 20, "desc": "周期"}],
        "example": "  ma20: { kind: sma, period: 20 }",
    },
    {
        "kind": "atr",
        "label": "真实波幅 ATR",
        "formula": "TR = max(high-low, |high-prev_close|, |low-prev_close|)；ATR = SMA(TR, period)",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  atr14: { kind: atr, period: 14 }",
    },
    {
        "kind": "adx",
        "label": "平均趋向指数 ADX",
        "formula": "+DM/-DM → +DI/-DI → DX = |+DI--DI|/(+DI+-DI)；ADX = SMA(DX, period)",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  adx14: { kind: adx, period: 14 }",
    },
    {
        "kind": "highest",
        "label": "窗口最高",
        "formula": "max( high[t-period+1 : t] )；可加 field 改成取 close/open",
        "params": [
            {"name": "period", "default": 20, "desc": "窗口长度"},
            {"name": "field", "default": "high", "desc": "取哪一列：open/high/low/close"},
        ],
        "example": "  hh20: { kind: highest, period: 20 }\n  hc20: { kind: highest, period: 20, field: close }",
    },
    {
        "kind": "lowest",
        "label": "窗口最低",
        "formula": "min( low[t-period+1 : t] )；可加 field 改成取 close/open",
        "params": [
            {"name": "period", "default": 20, "desc": "窗口长度"},
            {"name": "field", "default": "low", "desc": "取哪一列：open/high/low/close"},
        ],
        "example": "  ll20: { kind: lowest, period: 20 }",
    },
    {
        "kind": "rsi",
        "label": "相对强弱 RSI",
        "formula": "RS = 平均涨幅/平均跌幅；RSI = 100 - 100/(1+RS)；默认 Wilder 平滑",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  rsi14: { kind: rsi, period: 14 }",
    },
    {
        "kind": "macd_hist",
        "label": "MACD 柱状",
        "formula": "DIF = EMA(fast) - EMA(slow)；DEA = EMA(DIF, signal)；hist = DIF - DEA",
        "params": [
            {"name": "fast", "default": 12, "desc": "快线周期"},
            {"name": "slow", "default": 26, "desc": "慢线周期"},
            {"name": "signal", "default": 9, "desc": "信号线周期"},
        ],
        "example": "  macd: { kind: macd_hist, fast: 12, slow: 26, signal: 9 }",
    },
    {
        "kind": "boll_upper",
        "label": "布林上轨",
        "formula": "上轨 = SMA(period) + std × 收盘价标准差(period)",
        "params": [
            {"name": "period", "default": 20, "desc": "均线周期"},
            {"name": "std", "default": 2.0, "desc": "标准差倍数"},
        ],
        "example": "  boll_up: { kind: boll_upper, period: 20, std: 2.0 }",
    },
    {
        "kind": "boll_lower",
        "label": "布林下轨",
        "formula": "下轨 = SMA(period) - std × 收盘价标准差(period)",
        "params": [
            {"name": "period", "default": 20, "desc": "均线周期"},
            {"name": "std", "default": 2.0, "desc": "标准差倍数"},
        ],
        "example": "  boll_lo: { kind: boll_lower, period: 20, std: 2.0 }",
    },
    {
        "kind": "stddev",
        "label": "标准差",
        "formula": "STD(period) = 最近 period 根取值的总体标准差（ddof=0）",
        "params": [
            {"name": "period", "default": 20, "desc": "窗口长度"},
            {"name": "source", "default": "close", "desc": "取值列：open/high/low/close/volume"},
        ],
        "example": "  sd20: { kind: stddev, period: 20 }",
    },
    {
        "kind": "atr_pct",
        "label": "ATR 占比（波动率）",
        "formula": "ATR% = ATR(period) ÷ 收盘价 × 100，用于跨品种比较波动大小",
        "params": [{"name": "period", "default": 14, "desc": "ATR 周期"}],
        "example": "  vol_pct: { kind: atr_pct, period: 14 }",
    },
    {
        "kind": "plus_di",
        "label": "方向指标 +DI",
        "formula": "+DI = 100 × +DM 的 period 根平滑 ÷ ATR 的 period 根平滑",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  pdi: { kind: plus_di, period: 14 }",
    },
    {
        "kind": "minus_di",
        "label": "方向指标 −DI",
        "formula": "−DI = 100 × −DM 的 period 根平滑 ÷ ATR 的 period 根平滑",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  mdi: { kind: minus_di, period: 14 }",
    },
    {
        "kind": "donchian_mid",
        "label": "唐奇安中轨",
        "formula": "中轨 = (最近 period 根最高 + 最近 period 根最低) ÷ 2",
        "params": [{"name": "period", "default": 20, "desc": "窗口长度"}],
        "example": "  mid20: { kind: donchian_mid, period: 20 }",
    },
    {
        "kind": "macd_dif",
        "label": "MACD 快线 DIF",
        "formula": "DIF = EMA(fast) − EMA(slow)",
        "params": [
            {"name": "fast", "default": 12, "desc": "快线周期"},
            {"name": "slow", "default": 26, "desc": "慢线周期"},
            {"name": "signal", "default": 9, "desc": "信号线周期（DIF 本身不用，但保持同一组参数）"},
        ],
        "example": "  dif: { kind: macd_dif, fast: 12, slow: 26, signal: 9 }",
    },
    {
        "kind": "macd_dea",
        "label": "MACD 信号线 DEA",
        "formula": "DEA = DIF 的 signal 根 EMA",
        "params": [
            {"name": "fast", "default": 12, "desc": "快线周期"},
            {"name": "slow", "default": 26, "desc": "慢线周期"},
            {"name": "signal", "default": 9, "desc": "信号线周期"},
        ],
        "example": "  dea: { kind: macd_dea, fast: 12, slow: 26, signal: 9 }",
    },
    {
        "kind": "boll_mid",
        "label": "布林中轨",
        "formula": "中轨 = SMA(period)",
        "params": [
            {"name": "period", "default": 20, "desc": "均线周期"},
            {"name": "std", "default": 2.0, "desc": "标准差倍数（中轨本身不用，保持同一组参数）"},
        ],
        "example": "  boll_mid: { kind: boll_mid, period: 20 }",
    },
    {
        "kind": "boll_width",
        "label": "布林带宽",
        "formula": "带宽% = (上轨 − 下轨) ÷ 中轨 × 100，用来看挤压/扩张",
        "params": [
            {"name": "period", "default": 20, "desc": "均线周期"},
            {"name": "std", "default": 2.0, "desc": "标准差倍数"},
        ],
        "example": "  bw: { kind: boll_width, period: 20, std: 2.0 }",
    },
    {
        "kind": "keltner_upper",
        "label": "肯特纳上轨",
        "formula": "上轨 = EMA(period) + mult × ATR(atr_period)",
        "params": [
            {"name": "period", "default": 20, "desc": "EMA 周期"},
            {"name": "atr_period", "default": 20, "desc": "ATR 周期"},
            {"name": "mult", "default": 2.0, "desc": "ATR 倍数"},
        ],
        "example": "  kc_up: { kind: keltner_upper, period: 20, atr_period: 20, mult: 2.0 }",
    },
    {
        "kind": "keltner_lower",
        "label": "肯特纳下轨",
        "formula": "下轨 = EMA(period) − mult × ATR(atr_period)",
        "params": [
            {"name": "period", "default": 20, "desc": "EMA 周期"},
            {"name": "atr_period", "default": 20, "desc": "ATR 周期"},
            {"name": "mult", "default": 2.0, "desc": "ATR 倍数"},
        ],
        "example": "  kc_lo: { kind: keltner_lower, period: 20, atr_period: 20, mult: 2.0 }",
    },
    {
        "kind": "stoch_k",
        "label": "随机指标 %K",
        "formula": "rawK = (收盘 − LL(period)) ÷ (HH(period) − LL(period)) × 100；K = rawK 的 smooth 根平均",
        "params": [
            {"name": "period", "default": 14, "desc": "高低点窗口"},
            {"name": "smooth", "default": 3, "desc": "K 的平滑根数"},
            {"name": "d", "default": 3, "desc": "D 的平滑根数（%K 本身不用，保持同一组参数）"},
        ],
        "example": "  k: { kind: stoch_k, period: 14, smooth: 3, d: 3 }",
    },
    {
        "kind": "stoch_d",
        "label": "随机指标 %D",
        "formula": "%D = %K 的 d 根平均",
        "params": [
            {"name": "period", "default": 14, "desc": "高低点窗口"},
            {"name": "smooth", "default": 3, "desc": "K 的平滑根数"},
            {"name": "d", "default": 3, "desc": "D 的平滑根数"},
        ],
        "example": "  d: { kind: stoch_d, period: 14, smooth: 3, d: 3 }",
    },
    {
        "kind": "cci",
        "label": "顺势指标 CCI",
        "formula": "CCI = (典型价 − SMA(典型价)) ÷ (0.015 × 平均绝对偏差)",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  cci14: { kind: cci, period: 14 }",
    },
    {
        "kind": "williams_r",
        "label": "威廉 %R",
        "formula": "%R = −100 × (HH − 收盘) ÷ (HH − LL)",
        "params": [{"name": "period", "default": 14, "desc": "周期"}],
        "example": "  wr14: { kind: williams_r, period: 14 }",
    },
    {
        "kind": "roc",
        "label": "变动率 ROC（动量）",
        "formula": "ROC% = (当期值 − period 根前的值) ÷ period 根前的值 × 100",
        "params": [
            {"name": "period", "default": 12, "desc": "回看根数"},
            {"name": "source", "default": "close", "desc": "取值列"},
        ],
        "example": "  roc12: { kind: roc, period: 12 }",
    },
    {
        "kind": "obv",
        "label": "能量潮 OBV",
        "formula": "OBV = 逐根累加「收盘涨则加成交量、跌则减成交量」",
        "params": [],
        "example": "  obv: { kind: obv }",
    },
    {
        "kind": "vol_sma",
        "label": "成交量均线",
        "formula": "VMA(period) = 最近 period 根成交量的平均值",
        "params": [{"name": "period", "default": 20, "desc": "均线周期"}],
        "example": "  vma20: { kind: vol_sma, period: 20 }",
    },
    {
        "kind": "supertrend",
        "label": "SuperTrend",
        "formula": "通道 = (高+低)/2 ± mult × ATR(period)；收盘穿过通道即翻方向",
        "params": [
            {"name": "period", "default": 10, "desc": "ATR 周期"},
            {"name": "mult", "default": 3.0, "desc": "ATR 倍数"},
        ],
        "example": "  st: { kind: supertrend, period: 10, mult: 3.0 }",
    },
    {
        "kind": "psar",
        "label": "抛物线 SAR",
        "formula": "按加速因子逐步移动的追踪点；价格在点上方偏多、下方偏空",
        "params": [
            {"name": "af_start", "default": 0.02, "desc": "初始加速因子"},
            {"name": "af_step", "default": 0.02, "desc": "每创新高/低加多少"},
            {"name": "af_max", "default": 0.2, "desc": "加速因子上限"},
        ],
        "example": "  sar: { kind: psar, af_start: 0.02, af_step: 0.02, af_max: 0.2 }",
    },
    {
        "kind": "vwap",
        "label": "当日 VWAP",
        "formula": "VWAP = 当日累计(典型价 × 成交量) ÷ 当日累计成交量",
        "params": [],
        "example": "  vwap: { kind: vwap }",
    },
    {
        "kind": "day_open",
        "label": "当日开盘价",
        "formula": "当前交易日第一根 K 线的开盘价",
        "params": [],
        "example": "  d_open: { kind: day_open }",
    },
    {
        "kind": "prev_day_high",
        "label": "昨日最高",
        "formula": "上一个交易日的最高价（按交易日聚合，夜盘算下一交易日）",
        "params": [],
        "example": "  pdh: { kind: prev_day_high }",
    },
    {
        "kind": "prev_day_low",
        "label": "昨日最低",
        "formula": "上一个交易日的最低价",
        "params": [],
        "example": "  pdl: { kind: prev_day_low }",
    },
    {
        "kind": "prev_day_close",
        "label": "昨收",
        "formula": "上一个交易日的收盘价",
        "params": [],
        "example": "  pdc: { kind: prev_day_close }",
    },
    {
        "kind": "opening_range_high",
        "label": "开盘区间高点",
        "formula": "当日开盘后 period 根内的最高价；之后固定不变",
        "params": [{"name": "period", "default": 30, "desc": "开盘后取多少根"}],
        "example": "  orh: { kind: opening_range_high, period: 30 }",
    },
    {
        "kind": "opening_range_low",
        "label": "开盘区间低点",
        "formula": "当日开盘后 period 根内的最低价；之后固定不变",
        "params": [{"name": "period", "default": 30, "desc": "开盘后取多少根"}],
        "example": "  orl: { kind: opening_range_low, period: 30 }",
    },
    {
        "kind": "expr",
        "label": "算术表达式",
        "formula": (
            "用 + - * /、括号、一元负号组合行情字段和已有指标；"
            "函数：max/min/abs/sqrt/log/sign/pow/clip、滚动 avg/std/hhv/llv/sum(序列, 根数)"
        ),
        "params": [{"name": "formula", "default": "", "desc": "公式字符串，引用前面定义过的指标名"}],
        "example": (
            "  up: { kind: expr, formula: \"open + 0.7 * dt_range\" }\n"
            "  range: { kind: expr, formula: \"max(hh20 - lc20, hc20 - ll20)\" }\n"
            "  atr_ratio: { kind: expr, formula: \"atr14 / avg(atr14, 60)\" }"
        ),
    },
]


# ---------------------------------------------------------------- 指标库配置helper
#
# 配置页「指标库」可视化面板用这几支函数：把「类型 + 参数」拼成 YAML 片段、
# 生成带具体数值的计算公式说明、做参数校验，以及把片段插进编辑框文本。

_PARAM_TYPES = {
    "std": "float",
    "formula": "str",
    "field": "str",
    "source": "str",
    "period": "int",
    "atr_period": "int",
    "fast": "int",
    "slow": "int",
    "signal": "int",
    "smooth": "int",
    "d": "int",
    "mult": "float",
    "af_start": "float",
    "af_step": "float",
    "af_max": "float",
}

_NAME_HINTS = {
    "ema": "ema",
    "sma": "ma",
    "stddev": "std",
    "atr": "atr",
    "atr_pct": "atr_pct",
    "adx": "adx",
    "plus_di": "pdi",
    "minus_di": "mdi",
    "highest": "hh",
    "lowest": "ll",
    "donchian_mid": "mid",
    "rsi": "rsi",
    "macd_hist": "macd",
    "macd_dif": "dif",
    "macd_dea": "dea",
    "boll_upper": "boll_up",
    "boll_lower": "boll_lo",
    "boll_mid": "boll_mid",
    "boll_width": "boll_bw",
    "keltner_upper": "kc_up",
    "keltner_lower": "kc_lo",
    "stoch_k": "k",
    "stoch_d": "d",
    "cci": "cci",
    "williams_r": "wr",
    "roc": "roc",
    "obv": "obv",
    "vol_sma": "vma",
    "supertrend": "st",
    "psar": "sar",
    "vwap": "vwap",
    "day_open": "d_open",
    "prev_day_high": "pdh",
    "prev_day_low": "pdl",
    "prev_day_close": "pdc",
    "opening_range_high": "orh",
    "opening_range_low": "orl",
    "expr": "expr",
}

_FIELD_LABELS = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
}


def indicator_kinds() -> list[str]:
    """指标库支持的 kind 列表，顺序和 INDICATOR_LIBRARY 一致。"""
    return [str(item["kind"]) for item in INDICATOR_LIBRARY]


def indicator_defaults(kind: str) -> dict[str, Any]:
    """某类指标的默认参数，供面板预填。"""
    for item in INDICATOR_LIBRARY:
        if item["kind"] == kind:
            return {p["name"]: p["default"] for p in item.get("params", [])}
    return {}


def indicator_param_type(name: str) -> str:
    """参数该按整数、小数还是文本处理。"""
    return _PARAM_TYPES.get(str(name), "int")


def indicator_name_hint(kind: str) -> str:
    """给个像人话的默认指标名，例如 ema → ema20。"""
    return _NAME_HINTS.get(str(kind), "ind")


def _format_param(key: str, value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().replace('"', "'")
        return f'{key}: "{text}"'
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    return f"{key}: {value}"


def indicator_snippet(name: str, kind: str, params: Mapping[str, Any] | None = None) -> str:
    """拼一行可直接写进 indicators: 的 YAML，例如
    `  ma20: { kind: ema, period: 20 }`。空值参数自动省略。
    """
    parts = [f"kind: {kind}"]
    for key, value in (params or {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        parts.append(_format_param(key, value))
    return f"  {name}: {{ {', '.join(parts)} }}"


def indicator_formula_text(kind: str, params: Mapping[str, Any] | None = None) -> str:
    """生成带当前参数的计算公式说明，面板上直接显示。"""
    p = dict(params or {})

    def num(key: str, default: float) -> float:
        try:
            return float(p.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    if kind == "ema":
        n = int(num("period", 20))
        source = str(p.get("source") or "close")
        alpha = 2 / (n + 1)
        return (
            f"EMA({n}) = α×{source} + (1−α)×前一根 EMA，α = 2÷({n}+1) = {alpha:.4f}；"
            f"即 {alpha:.4f}×{source}_t + {1 - alpha:.4f}×EMA_(t−1)"
        )
    if kind == "sma":
        n = int(num("period", 20))
        source = str(p.get("source") or "close")
        return f"SMA({n}) = 最近 {n} 根「{_FIELD_LABELS.get(source, source)}」的算术平均值"
    if kind == "atr":
        n = int(num("period", 14))
        return f"TR = max(高−低, |高−前收|, |低−前收|)；ATR({n}) = 最近 {n} 根 TR 的简单平均"
    if kind == "adx":
        n = int(num("period", 14))
        return (
            f"ADX({n})：先算 +DM/−DM 得到 ±DI，"
            f"DX = |+DI−(−DI)| ÷ (+DI+(−DI))，再对 DX 取 {n} 根平均"
        )
    if kind in {"highest", "lowest"}:
        n = int(num("period", 20))
        field = str(p.get("field") or ("high" if kind == "highest" else "low"))
        label = _FIELD_LABELS.get(field, field)
        if kind == "highest":
            return f"HH({n}, {field}) = 最近 {n} 根「{label}」的最大值"
        return f"LL({n}, {field}) = 最近 {n} 根「{label}」的最小值"
    if kind == "rsi":
        n = int(num("period", 14))
        return (
            f"RSI({n})：涨幅/跌幅各做 {n} 根 Wilder 平滑（α=1/{n}），"
            f"RS = 平均涨幅÷平均跌幅，RSI = 100 − 100÷(1+RS)"
        )
    if kind == "macd_hist":
        fast, slow, signal = int(num("fast", 12)), int(num("slow", 26)), int(num("signal", 9))
        return (
            f"DIF = EMA({fast}) − EMA({slow})；DEA = DIF 的 {signal} 根 EMA；"
            f"柱 = DIF − DEA（>0 偏多，<0 偏空）"
        )
    if kind in {"boll_upper", "boll_lower"}:
        n = int(num("period", 20))
        std = num("std", 2.0)
        sign = "+" if kind == "boll_upper" else "−"
        name = "上轨" if kind == "boll_upper" else "下轨"
        return f"布林{name} = SMA({n}) {sign} {std:g} × 收盘价的 {n} 根标准差"
    if kind == "stddev":
        n = int(num("period", 20))
        source = str(p.get("source") or "close")
        return f"STD({n}) = 最近 {n} 根「{_FIELD_LABELS.get(source, source)}」的总体标准差（ddof=0）"
    if kind == "atr_pct":
        n = int(num("period", 14))
        return f"ATR% = ATR({n}) ÷ 收盘价 × 100"
    if kind in {"plus_di", "minus_di"}:
        n = int(num("period", 14))
        sign = "+" if kind == "plus_di" else "−"
        return f"{sign}DI({n}) = 100 × {sign}DM 的 {n} 根平滑 ÷ ATR 的 {n} 根平滑"
    if kind == "donchian_mid":
        n = int(num("period", 20))
        return f"中轨 = (HH({n}, high) + LL({n}, low)) ÷ 2"
    if kind in {"macd_dif", "macd_dea"}:
        fast, slow, signal = int(num("fast", 12)), int(num("slow", 26)), int(num("signal", 9))
        if kind == "macd_dif":
            return f"DIF = EMA({fast}) − EMA({slow})"
        return f"DEA = DIF 的 {signal} 根 EMA（DIF = EMA({fast}) − EMA({slow})）"
    if kind == "boll_mid":
        return f"布林中轨 = SMA({int(num('period', 20))})"
    if kind == "boll_width":
        n = int(num("period", 20))
        return f"带宽% = (上轨 − 下轨) ÷ 中轨 × 100，上下轨用 {n} 根、{num('std', 2.0):g} 倍标准差"
    if kind in {"keltner_upper", "keltner_lower"}:
        n = int(num("period", 20))
        atr_n = int(num("atr_period", 20))
        mult = num("mult", 2.0)
        sign = "+" if kind == "keltner_upper" else "−"
        return f"肯特纳{'上' if kind == 'keltner_upper' else '下'}轨 = EMA({n}) {sign} {mult:g} × ATR({atr_n})"
    if kind in {"stoch_k", "stoch_d"}:
        n = int(num("period", 14))
        smooth, d = int(num("smooth", 3)), int(num("d", 3))
        if kind == "stoch_k":
            return f"K = SMA(rawK, {smooth})，rawK = (收盘 − LL({n})) ÷ (HH({n}) − LL({n})) × 100"
        return f"D = SMA(K, {d})，K = SMA(rawK, {smooth})"
    if kind == "cci":
        n = int(num("period", 14))
        return f"CCI({n}) = (典型价 − SMA({n})) ÷ (0.015 × 平均绝对偏差)，典型价 = (高+低+收)÷3"
    if kind == "williams_r":
        n = int(num("period", 14))
        return f"%R({n}) = −100 × (HH({n}) − 收盘) ÷ (HH({n}) − LL({n}))"
    if kind == "roc":
        n = int(num("period", 12))
        return f"ROC({n})% = (收盘 − {n} 根前收盘) ÷ {n} 根前收盘 × 100"
    if kind == "obv":
        return "OBV = 逐根累加：收盘涨加成交量、跌减成交量、平不变"
    if kind == "vol_sma":
        return f"成交量均线 = 最近 {int(num('period', 20))} 根成交量的平均值"
    if kind == "supertrend":
        n, mult = int(num("period", 10)), num("mult", 3.0)
        return f"SuperTrend：中轨 = (高+低)÷2 ± {mult:g} × ATR({n})，收盘穿越即翻方向"
    if kind == "psar":
        return (
            f"SAR：初始 AF={num('af_start', 0.02):g}，每创新极值加 {num('af_step', 0.02):g}，"
            f"上限 {num('af_max', 0.2):g}"
        )
    if kind == "vwap":
        return "VWAP = 当日累计(典型价 × 成交量) ÷ 当日累计成交量（每交易日重置）"
    if kind == "day_open":
        return "当日开盘价 = 当前交易日第一根 K 线的开盘价"
    if kind == "prev_day_high":
        return "昨日最高 = 上一交易日的最高价"
    if kind == "prev_day_low":
        return "昨日最低 = 上一交易日的最低价"
    if kind == "prev_day_close":
        return "昨收 = 上一交易日的收盘价"
    if kind in {"opening_range_high", "opening_range_low"}:
        n = int(num("period", 30))
        return f"开盘区间{'高' if kind == 'opening_range_high' else '低'}点 = 当日前 {n} 根内的最高/最低，之后固定"
    if kind == "expr":
        formula = str(p.get("formula") or "").strip()
        return f"自定义公式：{formula}" if formula else "自定义公式：还没填，写如 open + 0.7 * range"
    return ""


def validate_indicator(name: str, kind: str, params: Mapping[str, Any] | None = None) -> None:
    """只校验一条指标的写法；不合法抛 RuleConfigError，消息能直接显示给用户。"""
    spec: dict[str, Any] = {"kind": kind}
    for key, value in (params or {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        spec[key] = value
    if kind == "expr":
        # expr 依赖前面定义过的指标，单条校验时只允许行情字段，引用别的指标由调用方补充校验。
        _parse_indicators({name: {**spec, "formula": "close"}})
        return
    _parse_indicators({name: spec})


def validate_expr_formula(formula: str, available: Any = ()) -> None:
    """校验 expr 公式：可用名字 = 行情字段 + available 里已有的指标名。"""
    allowed = frozenset(_BAR_FIELDS) | frozenset(str(item) for item in available)
    _parse_formula(formula, "formula", allowed)


def insert_indicator_line(text: str, snippet: str) -> str:
    """把一行指标定义插进规则文本的 indicators: 段末尾；没有这一段就在 entry 前新建。"""
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    snippet_lines = snippet.strip("\n").split("\n")
    header = next((i for i, line in enumerate(lines) if line.strip() == "indicators:"), None)
    if header is None:
        body = list(lines)
        if body and body[-1].strip():
            body.append("")
        exit_idx = next((i for i, line in enumerate(body) if line.strip().startswith("entry:")), None)
        block = ["indicators:", *snippet_lines, ""]
        if exit_idx is None:
            body += block
        else:
            body = body[:exit_idx] + block + body[exit_idx:]
        return "\n".join(body).rstrip("\n") + "\n"
    last = header
    cursor = header + 1
    while cursor < len(lines) and lines[cursor][:1] in (" ", "\t"):
        last = cursor
        cursor += 1
    body = lines[: last + 1] + snippet_lines + lines[last + 1 :]
    return "\n".join(body).rstrip("\n") + "\n"


def rule_engine_code(stem: str) -> str:
    """规则文件名 → 策略引擎代号，例如 dualthrust → rule_dsl:dualthrust。"""
    return f"{RULE_ENGINE_PREFIX}{str(stem).strip().lower()}"


def is_rule_stem(value: Any) -> bool:
    """文件名能不能当引擎代号用：小写字母/数字开头，只含小写字母、数字、_ 和 -。"""
    return bool(_RULE_STEM.fullmatch(str(value or "").strip().lower()))


def rule_engine_stem(name: Any) -> str | None:
    """策略引擎代号 → 规则文件名（不含 .yaml）；不是自定义规则时返回 None。"""
    text = str(name or "").strip().lower()
    if not text.startswith(RULE_ENGINE_PREFIX):
        return None
    stem = text[len(RULE_ENGINE_PREFIX) :].strip()
    return stem or None


def rules_dir() -> Path:
    """出厂/用户规则目录：<项目根>/config/rules。"""
    from src.utils.config_loader import ensure_runtime_layout

    return ensure_runtime_layout() / "config" / "rules"


def list_rule_files(directory: str | Path | None = None) -> list[dict[str, Any]]:
    """列出可当策略用的规则文件：文件名（stem）、显示名、是否内置、完整路径。

    builtin=True 表示程序自带，界面上禁止删除/重命名/覆盖，只能「另存为」成用户规则后再改。
    """
    root = Path(directory) if directory is not None else rules_dir()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.yaml")):
        stem = path.stem
        if not _RULE_STEM.fullmatch(stem):
            continue
        meta = _rule_meta(path)
        out.append({"stem": stem, "name": meta["name"], "builtin": meta["builtin"], "path": str(path)})
    return out


def _rule_meta(path: Path) -> dict[str, Any]:
    """读规则文件的 name 和 builtin 标记；读不出来就退回文件名、非内置。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {"name": path.stem, "builtin": False}
    if isinstance(raw, dict):
        name = str(raw.get("name") or "").strip() or path.stem
        builtin = bool(raw.get("builtin"))
        return {"name": name, "builtin": builtin}
    return {"name": path.stem, "builtin": False}


def is_builtin_rule(stem: str, directory: str | Path | None = None) -> bool:
    """某条规则是不是程序自带的。找不到文件时按用户规则处理。"""
    root = Path(directory) if directory is not None else rules_dir()
    path = root / f"{stem}.yaml"
    if not path.exists():
        return False
    return _rule_meta(path)["builtin"]


def delete_rule_file(stem: str, directory: str | Path | None = None) -> None:
    """删除用户规则；内置规则不允许删。"""
    if not is_rule_stem(stem):
        raise RuleConfigError(f"文件名不合法：{stem!r}")
    root = Path(directory) if directory is not None else rules_dir()
    path = root / f"{stem}.yaml"
    if not path.exists():
        raise RuleConfigError(f"规则文件不存在：{stem}.yaml")
    if is_builtin_rule(stem, root):
        raise RuleConfigError(f"{stem}.yaml 是程序自带规则，不能删除；可以「另存为」成自己的规则后再改。")
    path.unlink()


def rename_rule_file(old_stem: str, new_stem: str, directory: str | Path | None = None) -> str:
    """重命名用户规则；内置规则不允许改名。返回新文件名。"""
    if not is_rule_stem(old_stem):
        raise RuleConfigError(f"原文件名不合法：{old_stem!r}")
    if not is_rule_stem(new_stem):
        raise RuleConfigError(f"新文件名不合法：{new_stem!r}")
    root = Path(directory) if directory is not None else rules_dir()
    src = root / f"{old_stem}.yaml"
    if not src.exists():
        raise RuleConfigError(f"规则文件不存在：{old_stem}.yaml")
    if is_builtin_rule(old_stem, root):
        raise RuleConfigError(f"{old_stem}.yaml 是程序自带规则，不能改名；可以「另存为」成自己的规则。")
    dst = root / f"{new_stem}.yaml"
    if dst.exists() and old_stem != new_stem:
        raise RuleConfigError(f"{new_stem}.yaml 已经存在，换个文件名。")
    if old_stem == new_stem:
        return new_stem
    src.rename(dst)
    return new_stem


def compile_rule_text(text: str) -> CompiledRule:
    """只校验、不落盘，供编辑器「仅校验」使用。"""
    return compile_rule(_parse_rule_yaml(text, "<编辑器>"), source="<编辑器>")


def save_rule_text(path: str | Path, text: str, *, allow_overwrite_builtin: bool = False) -> CompiledRule:
    """先校验再落盘：校验不通过绝不覆盖已有文件。返回编译结果。

    内置规则默认不允许覆盖（避免用户误改自带策略）；要改内置请先「另存为」用户规则。
    """
    target = Path(path)
    rule = compile_rule(_parse_rule_yaml(text, str(target)), source=str(target))
    if target.exists() and _rule_meta(target)["builtin"] and not allow_overwrite_builtin:
        raise RuleConfigError(
            f"{target.name} 是程序自带规则，不能直接覆盖；改个文件名另存为自己的规则。"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return rule


# ---------------------------------------------------------------- 策略骨架


class RuleStrategy(StrategyBase):
    """
    按 YAML 规则给出信号的策略。

    持仓时按固定止损和离场条件平仓；空仓时按 entry 条件开仓。
    止损口径复用全策略共用的 stop_mode（touch/close），和回测引擎保持一致。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        rule_path: str | Path | None = None,
        rule_data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        strategy = cfg.get("strategy") or {}
        box = strategy.get("rule_dsl") or {}
        trade = cfg.get("trade") or {}

        if rule_data is not None:
            self.rule = compile_rule(rule_data, source="<inline>")
        else:
            configured = rule_path if rule_path is not None else box.get("file")
            if not configured:
                raise RuleConfigError("strategy.rule_dsl.file 没有配置；请指向 config/rules/ 下的 YAML")
            self.rule = load_rule_file(_resolve_rule_path(configured))

        self.cfg = cfg
        self.timeframe = str(self.rule.timeframe or strategy.get("timeframe", "15"))
        # 止损周期跟左边公共项；规则文件只覆盖信号周期。
        self.trail_timeframe = str(strategy.get("trail_timeframe") or self.timeframe)
        self.symbols: list[str] = list(strategy.get("symbols") or [trade.get("symbol", "bu2612")])
        self.volume = int(trade.get("volume", 1))
        self.required_bars = self.rule.required_bars
        self.stop_mode = stop_mode_from_cfg(cfg)
        self.stop_on_touch = self.stop_mode == STOP_MODE_TOUCH

        self.flatten_at_close = config_flag(strategy.get("flatten_at_close", True), default=True)

        from src.utils.session_clock import flatten_clocks_for_symbol, session_tuples

        live = self.symbols[0] if self.symbols else ""
        self.flatten_times = flatten_clocks_for_symbol(cfg, live)
        self.entry_sessions = session_tuples(cfg, live)

        self.positions: dict[str, PositionState] = {s: PositionState(symbol=s) for s in self.symbols}
        self._pending: dict[str, dict[str, Any]] = {}
        self._entry_bar_id: dict[str, str] = {}

    # ---- 运行层需要的公开方法 -------------------------------------

    def is_flatten_time(self, now: Any = None) -> bool:
        if not self.flatten_at_close:
            return False
        from src.utils.session_clock import in_flatten_window

        return in_flatten_window(self.flatten_times, now or datetime.now())

    def in_entry_session(self, now: Any, symbol: str | None = None) -> bool:
        from src.utils.session_clock import in_session_windows, parse_hhmm, sessions_for_symbol
        from .session_scope import scope_allows, scope_from_cfg

        if str(self.timeframe).upper() in {"D", "W"}:
            return True
        if not scope_allows(scope_from_cfg(self.cfg), now):
            return False
        windows = self.entry_sessions
        if symbol:
            rows = sessions_for_symbol(self.cfg, symbol)
            if rows:
                windows = [(parse_hhmm(a), parse_hhmm(b)) for a, b in rows]
        if not windows:
            return True
        return in_session_windows(windows, now)

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))
        pending = self._pending.pop(symbol, None) or {}
        if action in (StrategyAction.BUY_OPEN, StrategyAction.SELL_OPEN):
            pos.side = "long" if action == StrategyAction.BUY_OPEN else "short"
            pos.volume = volume
            pos.entry_price = price
            stop = float(initial_stop if initial_stop is not None else pending.get("stop") or price)
            pos.initial_stop = stop
            pos.trail_stop = stop
            pos.trail_armed = True
            when = pending.get("time") or datetime.now()
            pos.opened_at = when.isoformat(timespec="seconds")
            opened_bar = str(pending.get("bar_id") or "")
            if opened_bar:
                self.set_last_entry_bar(symbol, opened_bar)
        elif action == StrategyAction.CLOSE:
            if not pos.reduce_close(volume):
                if pending:
                    self._pending[symbol] = pending
                return
            self.positions[symbol] = PositionState(symbol=symbol)
            self._entry_bar_id.pop(symbol, None)

    def evaluate(self, symbol: str, bars: Any, now: Any = None, trail_bars: Any = None) -> StrategyDecision:
        """
        按规则给出这一根的动作。

        只读已收盘 K 线；成交价由运行层/回测引擎决定，这里不参与定价。
        """
        moment = now or datetime.now()
        pos = self.positions.setdefault(symbol, PositionState(symbol=symbol))

        if bars is None or len(bars) < self.required_bars:
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"K线不足：规则需要 {self.required_bars} 根",
            )

        values = self.rule.values(bars)
        close_series = values.get("close")
        if close_series is None or close_series.empty or pd.isna(close_series.iloc[-1]):
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根没有有效收盘价")
        close = float(close_series.iloc[-1])
        last = bars.iloc[-1]
        high = float(last.get("high", close))
        low = float(last.get("low", close))
        bar_id = str(bars.index[-1])

        if pos.side != "flat":
            if self.is_flatten_time(moment):
                return self._close_decision(symbol, pos, now=moment, bar_id=bar_id, reason="收盘清仓时间到")
            stop = float(pos.trail_stop or pos.initial_stop or 0.0)
            if stop > 0 and stop_triggered(
                side=pos.side, stop=stop, close=close, high=high, low=low, mode=self.stop_mode
            ):
                return self._close_decision(symbol, pos, now=moment, bar_id=bar_id, reason=f"触发止损 {stop:.2f}")
            if self.rule.exit_hit(pos.side, bars, values):
                return self._close_decision(
                    symbol, pos, now=moment, bar_id=bar_id, reason=f"离场条件成立（{self.rule.name}）"
                )
            if self.rule.max_hold_bars and self._held_bars(symbol, bars) >= self.rule.max_hold_bars:
                return self._close_decision(
                    symbol,
                    pos,
                    now=moment,
                    bar_id=bar_id,
                    reason=f"持仓已满 {self.rule.max_hold_bars} 根，按规则离场",
                )
            stop = self._advance_stop(pos, bars, values, stop)
            return StrategyDecision(
                StrategyAction.HOLD,
                symbol,
                f"持有{pos.side}，止损 {stop:.2f}（{self.rule.name}）",
                stop_price=stop,
                entry_price=pos.entry_price or None,
            )

        if self.is_flatten_time(moment):
            return StrategyDecision(StrategyAction.HOLD, symbol, "收盘清仓时段，不开新仓")
        if not self.in_entry_session(moment, symbol):
            return StrategyDecision(StrategyAction.HOLD, symbol, "当前不在开仓时段")
        if self.rule.filters_htf is not None:
            if trail_bars is None or len(trail_bars) < self.required_bars:
                return StrategyDecision(StrategyAction.HOLD, symbol, "高周期 K 线不足，暂不开仓")
            if not evaluate_condition(self.rule.filters_htf, self.rule.values(trail_bars)):
                return StrategyDecision(StrategyAction.HOLD, symbol, "高周期过滤没通过（filters_htf），不开仓")
        if self.get_signaled_bar(symbol) == bar_id:
            return StrategyDecision(StrategyAction.HOLD, symbol, "这一根已经处理过")

        side = self.rule.signal(bars, values)
        if side is None:
            return StrategyDecision(StrategyAction.HOLD, symbol, f"规则条件未成立（{self.rule.name}）")

        stop = self.rule.initial_stop(side, bars, close)
        if stop is None or not math.isfinite(stop):
            return StrategyDecision(StrategyAction.HOLD, symbol, "规则算不出止损价，放弃这一根")
        risk = close - stop if side == "long" else stop - close
        if risk <= 0:
            return StrategyDecision(StrategyAction.HOLD, symbol, f"止损价 {stop:.2f} 在反方向，放弃这一根")

        action = StrategyAction.BUY_OPEN if side == "long" else StrategyAction.SELL_OPEN
        self._pending[symbol] = {"time": moment, "stop": stop, "bar_id": bar_id}
        self._entry_bar_id[symbol] = bar_id
        self.set_signaled_bar(symbol, bar_id)
        return StrategyDecision(
            action,
            symbol,
            f"规则触发 {side}（{self.rule.name}），止损 {stop:.2f}",
            stop_price=stop,
            entry_price=close,
            meta={"rule": self.rule.name, "side": side, "stop": stop, "bar_id": bar_id},
        )

    def _advance_stop(
        self,
        pos: PositionState,
        bars: Any,
        values: Mapping[str, pd.Series],
        current: float,
    ) -> float:
        """按 trail / breakeven 算新止损，只往有利方向挪，绝不放宽。"""
        levels: list[float] = []
        trail = self.rule.trail_level(pos.side, bars, values)
        if trail is not None and math.isfinite(trail):
            levels.append(trail)
        floor = self.rule.breakeven_level(pos.side, bars, float(pos.entry_price or 0.0), values)
        if floor is not None and math.isfinite(floor):
            levels.append(floor)
        if not levels or current <= 0:
            return current
        updated = max([current, *levels]) if pos.side == "long" else min([current, *levels])
        if updated != current:
            pos.trail_stop = updated
        return updated

    def _held_bars(self, symbol: str, bars: Any) -> int:
        """开仓那根之后又走了几根；找不到开仓 K 线时返回 0（当作刚开始）。"""
        bar_id = self._entry_bar_id.get(symbol)
        if not bar_id:
            return 0
        try:
            position = bars.index.get_loc(bar_id)
        except (KeyError, TypeError):
            return 0
        if not isinstance(position, (int, np.integer)):
            return 0
        return max(0, len(bars) - 1 - int(position))

    def _close_decision(
        self,
        symbol: str,
        pos: PositionState,
        *,
        now: datetime,
        bar_id: str,
        reason: str,
    ) -> StrategyDecision:
        self._pending[symbol] = {"time": now, "bar_id": bar_id}
        self._entry_bar_id.pop(symbol, None)
        self.set_signaled_bar(symbol, bar_id)
        return StrategyDecision(
            StrategyAction.CLOSE,
            symbol,
            reason,
            stop_price=pos.trail_stop or pos.initial_stop or None,
            entry_price=pos.entry_price or None,
        )


def _resolve_rule_path(value: Any) -> Path:
    """相对路径按 <项目根>/config/ 解析；打包后同样适用。"""
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    from src.utils.config_loader import ensure_runtime_layout

    return ensure_runtime_layout() / "config" / path


__all__ = [
    "BreakevenSpec",
    "CompiledRule",
    "Condition",
    "ExprNode",
    "INDICATOR_LIBRARY",
    "IndicatorSpec",
    "NEW_RULE_TEXT",
    "Operand",
    "RULE_ENGINE_PREFIX",
    "RuleConfigError",
    "RuleStrategy",
    "StopSpec",
    "compile_rule",
    "compile_rule_text",
    "delete_rule_file",
    "evaluate_condition",
    "indicator_defaults",
    "indicator_formula_text",
    "indicator_kinds",
    "indicator_name_hint",
    "indicator_param_type",
    "indicator_snippet",
    "insert_indicator_line",
    "is_builtin_rule",
    "is_rule_stem",
    "list_rule_files",
    "load_rule_file",
    "rename_rule_file",
    "rule_engine_code",
    "rule_engine_stem",
    "rules_dir",
    "save_rule_text",
    "validate_expr_formula",
    "validate_indicator",
]
