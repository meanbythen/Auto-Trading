"""
交易配置页（从 app.py 拆出）
功能：策略、开止损、仓位、风控。账号、通道、前置在「账号」页。
原理：build_config_tab(app, tab) 直接读写 AutoTradingApp 上的变量与回调，页面状态仍挂在 app 上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui.config_tab_sections import build_entry_stop_sections
from src.ui.config_tab_sections import build_multi_symbol_section
from src.ui.config_tab_sections import build_tail_sections
from src.ui.config_tab_strategy import build_strategy_section

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def build_config_tab(
    app: AutoTradingApp,
    tab: ctk.CTkFrame,
    engine_choices: list[str],
) -> None:
    """交易配置：策略、开止损、仓位和风控。通道与前置在「账号」页。"""
    scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=8, pady=8)

    build_strategy_section(app, scroll, engine_choices)
    build_multi_symbol_section(app, scroll, engine_choices)
    build_entry_stop_sections(app, scroll)
    build_tail_sections(app, scroll)
