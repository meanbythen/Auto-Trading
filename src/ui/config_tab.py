"""
配置页（从 app.py 拆出）
功能：策略/执行/行情/账号/软件路径等全部配置控件。
原理：build_config_tab(app, tab) 直接读写 AutoTradingApp 上的变量与回调，页面状态仍挂在 app 上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from src.ui.config_tab_sections import build_account_section
from src.ui.config_tab_sections import build_entry_stop_sections
from src.ui.config_tab_sections import build_tail_sections
from src.ui.config_tab_strategy import build_strategy_section

if TYPE_CHECKING:
    from src.ui.app import AutoTradingApp


def build_config_tab(
    app: AutoTradingApp,
    tab: ctk.CTkFrame,
    engine_choices: list[str],
) -> None:
    """配置选项卡：按原顺序协调账号、策略、开止损、风控和存档区块。"""
    scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=8, pady=8)

    build_account_section(app, scroll)
    build_strategy_section(app, scroll, engine_choices)
    build_entry_stop_sections(app, scroll)
    build_tail_sections(app, scroll)
