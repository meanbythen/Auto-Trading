"""策略研究容器：把历史回测/交叉测试/策略优化/复盘/规律候选收进一个一级页签。"""

from __future__ import annotations

from src.ui import theme as T
import customtkinter as ctk


class _ResearchTabview(ctk.CTkTabview):
    """二级页签。去掉默认标题上方那段留白，避免和一级页签隔得太远。"""

    _outer_spacing = 0


class ResearchHubMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    # 属于「策略研究」的二级页签。跳页时先切一级，再切这里。
    _RESEARCH_TABS = ("历史回测", "交叉测试", "策略优化", "复盘", "规律候选")

    def _build_research_hub(self, tab: ctk.CTkFrame) -> None:
        """外层只留一个入口，五个研究页作为二级页签，各自布局不变。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        sub = _ResearchTabview(
            tab,
            fg_color="transparent",
            segmented_button_fg_color=T.BG_ELEVATED,
            segmented_button_selected_color=T.ACCENT,
            segmented_button_selected_hover_color=T.ACCENT_HOVER,
            segmented_button_unselected_color=T.BG_ELEVATED,
            segmented_button_unselected_hover_color=T.BORDER,
            text_color=T.TEXT,
            corner_radius=12,
        )
        sub.grid(row=0, column=0, sticky="nsew")
        self.research_tabs = sub
        for name in self._RESEARCH_TABS:
            sub.add(name)

        self._build_backtest_tab(sub.tab("历史回测"))
        self._build_picker_tab(sub.tab("交叉测试"))
        self._build_optimize_tab(sub.tab("策略优化"))
        self._build_review_tab(sub.tab("复盘"))
        self._build_research_tab(sub.tab("规律候选"))
        sub.configure(command=self._on_research_tab_change)

    def _on_research_tab_change(self) -> None:
        """研究页内部切换。交叉测试名单跟当前程序策略走，每次点开都刷新。"""
        try:
            name = self.research_tabs.get()
        except Exception:
            return
        if name == "交叉测试" and not getattr(self, "_pick_running", False):
            self._refresh_cross_lists()

    def _open_tab(self, name: str) -> None:
        """切页签。研究类目标先切到「策略研究」，再选中对应二级页签。"""
        if name in self._RESEARCH_TABS:
            self.tabs.set("策略研究")
            self.research_tabs.set(name)
            return
        self.tabs.set(name)
