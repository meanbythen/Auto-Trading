"""
运行窗口日志过滤。

功能：界面只显示跟交易有关的几类话；拉 K 线、对账通过等仍写 logs 文件。
原理：文件和水槽用原来的 INFO；窗口这个水槽加过滤器。
      业界常见做法：屏幕看 WARNING 以上 + 成交/信号变化，明细进文件。
"""

from __future__ import annotations

from typing import Any

# loguru：WARNING 的 level.no 是 30。告警、错误一律上窗口。
_WARNING = 30

# 这些模块的 INFO 是过程明细：每轮扫描都会刷，窗口里没必要看。
_HIDE_INFO_PREFIXES = (
    "src.data.",
    "src.backtest.",
    "src.review.",
    "src.scheduler.session_audit",
)

# 过程成功、查询回显。对账失败走 WARNING，不会被这条挡住。
_HIDE_INFO_SNIPPETS = (
    "柜台对账通过",
    "仿真持仓查询",
    "仿真委托查询",
    "仿真成交查询",
    "柜台持仓查询",
    "柜台委托查询",
    "柜台成交查询",
    "事件日志已轮转",
    "下载窗口从",
    "拉取K线",
    "天勤K线",
)


def ui_sink_filter(record: dict[str, Any]) -> bool:
    """
    给运行日志窗口用的过滤器。True = 显示。

    调用方可以 logger.bind(ui=True/False)：True 强制上窗口，False 只写文件。
    """
    try:
        level_no = int(record["level"].no)
    except (KeyError, TypeError, ValueError, AttributeError):
        level_no = 0
    if level_no >= _WARNING:
        return True

    extra = record.get("extra") or {}
    flag = extra.get("ui")
    if flag is True:
        return True
    if flag is False:
        return False

    name = str(record.get("name") or "")
    if any(name.startswith(prefix) for prefix in _HIDE_INFO_PREFIXES):
        return False

    message = str(record.get("message") or "")
    if any(snippet in message for snippet in _HIDE_INFO_SNIPPETS):
        return False
    return True
