"""
运行页「测试工具」入口
功能：把试跑一轮、点击下单、完整自检、仿真柜台收进一个窗口，逻辑仍走各自入口。
原理：主界面只留一个按钮，避免三个入口挤在一起；默认选「试跑一轮」，符合日常用法。
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from src.ui import theme as T

# 选项值 → 界面短名。回调由主窗口注入，这里不直接 import 机器人。
OPTION_ONCE = "once"
OPTION_ORDER = "order"
OPTION_FULL = "full"
OPTION_COUNTER = "counter"

_LABELS = {
    OPTION_ONCE: "试跑一轮信号",
    OPTION_ORDER: "点击下单自检",
    OPTION_FULL: "完整自检",
    OPTION_COUNTER: "仿真柜台",
}

_DETAILS = {
    OPTION_ONCE: (
        "登录并只扫描一轮：拉K、看策略会不会开仓。"
        "和「启动常驻」同一套规则，但跑完就停，不会一直挂着。"
    ),
    OPTION_ORDER: (
        "打开下单模拟器：你填限价和方向，用常驻同一套点击去点同花顺。"
        "用来验坐标和委托能不能挂上。须先停止常驻；这一笔不记入账户。"
    ),
    OPTION_FULL: (
        "跑程序自检脚本：强制模拟通道，用假K线测开平仓、止损、清仓、风控。"
        "不会点真实登录。怀疑程序坏了再用；日常看盘不必点。"
    ),
    OPTION_COUNTER: (
        "启动独立本机仿真柜台，随时验证交易台现有按钮经过生产 CTP 链路的开平撤、查询和异步回报。"
        "支持挂单、部分成交、拒单、无回报和断线场景；不会连接真实或外部仿真柜台。"
    ),
}


def open_test_tools(
    master: ctk.CTk,
    *,
    on_once: Callable[[], None],
    on_order: Callable[[], None],
    on_full: Callable[[], None],
    on_counter: Callable[[], None],
) -> None:
    """打开测试工具。已经开着就提到前面。"""
    existing = getattr(master, "_test_tools_window", None)
    if existing is not None and existing.winfo_exists():
        try:
            existing.deiconify()
            existing.lift()
            existing.focus_force()
        except Exception:  # noqa: BLE001
            pass
        return
    window = _TestToolsWindow(
        master,
        on_once=on_once,
        on_order=on_order,
        on_full=on_full,
        on_counter=on_counter,
    )
    master._test_tools_window = window  # type: ignore[attr-defined]


class _TestToolsWindow(ctk.CTkToplevel):
    """四项分选 + 默认提示。点「开始」才执行对应逻辑。"""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        on_once: Callable[[], None],
        on_order: Callable[[], None],
        on_full: Callable[[], None],
        on_counter: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.title("测试工具")
        self.geometry("500x500")
        self._actions = {
            OPTION_ONCE: on_once,
            OPTION_ORDER: on_order,
            OPTION_FULL: on_full,
            OPTION_COUNTER: on_counter,
        }
        self.choice = ctk.StringVar(value=OPTION_ONCE)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(80, self._front)

    def _front(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def _build(self) -> None:
        ctk.CTkLabel(
            self,
            text="测试工具",
            font=T.FONT_TITLE,
            text_color=T.TEXT,
        ).pack(anchor="w", padx=16, pady=(16, 4))
        ctk.CTkLabel(
            self,
            text="日常先试跑；CTP 链路随时可测用「仿真柜台」，不需要外部仿真账号或日盘时段。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 12))

        box = ctk.CTkFrame(self, fg_color=T.BG_ELEVATED, corner_radius=10)
        box.pack(fill="x", padx=16, pady=(0, 8))
        for key in (OPTION_ONCE, OPTION_ORDER, OPTION_FULL, OPTION_COUNTER):
            ctk.CTkRadioButton(
                box,
                text=_LABELS[key],
                variable=self.choice,
                value=key,
                font=T.FONT_BODY,
                text_color=T.TEXT,
                fg_color=T.ACCENT,
                hover_color=T.ACCENT_HOVER,
                command=self._refresh_detail,
            ).pack(anchor="w", padx=12, pady=(10 if key == OPTION_ONCE else 6, 0))

        self.detail = ctk.CTkLabel(
            self,
            text=_DETAILS[OPTION_ONCE],
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        )
        self.detail.pack(anchor="w", padx=16, pady=(8, 12))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkButton(
            row,
            text="开始",
            height=40,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._start,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            row,
            text="取消",
            height=40,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self.destroy,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

    def _refresh_detail(self) -> None:
        key = self.choice.get()
        self.detail.configure(text=_DETAILS.get(key, ""))

    def _start(self) -> None:
        key = self.choice.get()
        action = self._actions.get(key)
        self.destroy()
        if action is not None:
            action()
