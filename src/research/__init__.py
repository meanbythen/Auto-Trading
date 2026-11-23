"""
因子挖掘研究层（蓝图第二期）。

只在历史 K 线上扫「因子和后面价格怎么变」的统计关系，写成报告放进候选池。
这里不下单、不改策略、不生成买卖点。
"""

from src.research.pool import load_pool, save_pool
from src.research.scan import scan_regularities

__all__ = ["load_pool", "save_pool", "scan_regularities"]
