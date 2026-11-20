"""
K线图表窗口（试点）。

功能：双击交叉测试结果的一行，看这个合约、这个周期的K线，下面带成交量。
原理：matplotlib 的 Figure 直接挂到 Tk 画布上，不经过 pyplot，也不改全局 backend，
      免得把出图脚本用的 Agg 换掉。数据仍走 KlineProvider，先用电脑里已经下过的K线，
      不够才联网补。取数放后台线程，取完再排回主线程画；窗口关掉后不再碰控件。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import customtkinter as ctk
from loguru import logger

from src.data.periods import period_label
from src.ui import theme as T

# 一眼能看清最近这一段就够。太多根在窗口里会挤成一团，画起来也慢。
MAX_BARS = 600

# K线颜色按国内习惯：红涨绿跌，比主题色更醒目。
UP_COLOR = "#E5484D"
DOWN_COLOR = "#2ECC8A"


def open_kline_chart(master: Any, cfg: dict[str, Any], symbol: str, period: str) -> Any:
    """打开一个K线窗口。可以同时开好几个，各自关闭。"""
    return _KlineChartWindow(master, cfg, symbol, period)


class _KlineChartWindow(ctk.CTkToplevel):
    """一个合约、一个周期的K线窗口。标题和状态栏用中文，图里保持 ASCII。"""

    def __init__(self, master: Any, cfg: dict[str, Any], symbol: str, period: str) -> None:
        super().__init__(master)
        self._cfg = cfg
        self._symbol = symbol
        self._period = period
        self._closed = False
        self._canvas: Any = None
        self.title(f"K线 {symbol} {period_label(period)}")
        self.geometry("980x620")
        self.minsize(640, 420)
        self.configure(fg_color=T.BG)
        self.status = ctk.CTkLabel(
            self,
            text="正在取K线…",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        )
        self.status.pack(fill="x", padx=12, pady=(10, 4))
        self.body = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=8)
        self.body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._load, daemon=True, name="kline-chart").start()

    def _load(self) -> None:
        """后台取数。这里不碰任何控件。"""
        try:
            from src.data.kline import KlineProvider

            bars = KlineProvider(self._cfg).fetch(
                self._symbol,
                self._period,
                max_bars=MAX_BARS,
                keep_history=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("K线窗口取数失败 {} {}: {}", self._symbol, self._period, exc)
            self._on_main(lambda message=str(exc): self._show_error(message))
            return
        self._on_main(lambda: self._draw(bars))

    def _on_main(self, callback: Callable[[], None]) -> None:
        """从后台线程排回 Tk 主线程；窗口已经关掉时安静放弃。"""
        if self._closed:
            return
        try:
            self.after(0, callback)
        except Exception:  # noqa: BLE001
            # 窗口正在销毁时 after 会抛 TclError，这不是错误。
            pass

    def _show_error(self, message: str) -> None:
        if self._closed:
            return
        self.status.configure(text=f"取不到K线：{message}", text_color=T.WARN)

    def _draw(self, bars: Any) -> None:
        """画价格和成交量。只画最近 MAX_BARS 根，避免窗口卡住。"""
        if self._closed:
            return
        if bars is None or len(bars) == 0:
            self._show_error("没有取到数据")
            return
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"没有可用的画图库（{exc}）")
            return

        frame = bars.tail(MAX_BARS)
        times = [str(stamp)[:16] for stamp in frame.index]
        opens = [float(value) for value in frame["open"]]
        highs = [float(value) for value in frame["high"]]
        lows = [float(value) for value in frame["low"]]
        closes = [float(value) for value in frame["close"]]
        colors = [UP_COLOR if close >= open_ else DOWN_COLOR for open_, close in zip(opens, closes)]
        x = list(range(len(frame)))

        fig = Figure(figsize=(9.2, 5.2), dpi=100, facecolor=T.BG)
        price_ax = fig.add_axes((0.06, 0.26, 0.92, 0.68))
        vol_ax = fig.add_axes((0.06, 0.07, 0.92, 0.14), sharex=price_ax)
        for ax in (price_ax, vol_ax):
            ax.set_facecolor(T.BG)
            for spine in ax.spines.values():
                spine.set_color(T.BORDER)
            ax.tick_params(colors=T.TEXT_DIM, labelsize=8)
            ax.grid(True, color=T.BORDER, alpha=0.35, linewidth=0.6)
            ax.set_axisbelow(True)

        price_ax.vlines(x, lows, highs, color=colors, linewidth=0.8)
        # 平盘时实体高度是 0，画不出来；给一个极小高度，至少留一条横线。
        floor = max((max(highs) - min(lows)) * 0.0005, 1e-9)
        bodies = [max(abs(close - open_), floor) for open_, close in zip(opens, closes)]
        price_ax.bar(
            x,
            bodies,
            bottom=[min(open_, close) for open_, close in zip(opens, closes)],
            width=0.62,
            color=colors,
            edgecolor=colors,
            linewidth=0.4,
        )

        volumes = (
            [float(value) for value in frame["volume"]] if "volume" in frame.columns else [0.0] * len(frame)
        )
        vol_ax.bar(x, volumes, width=0.62, color=colors, linewidth=0)
        vol_ax.set_ylabel("Vol", color=T.TEXT_DIM, fontsize=8)

        step = max(1, len(frame) // 8)
        ticks = list(range(0, len(frame), step))
        price_ax.set_xticks(ticks)
        price_ax.set_xticklabels([times[index] for index in ticks])
        price_ax.set_xlim(-1, len(frame))
        vol_ax.set_xticklabels([])
        price_ax.set_title(
            f"{self._symbol} {self._period}  last {closes[-1]:.2f}",
            color=T.TEXT,
            fontsize=11,
            loc="left",
        )

        canvas = FigureCanvasTkAgg(fig, master=self.body)
        widget = canvas.get_tk_widget()
        widget.configure(bg=T.BG, highlightthickness=0)
        widget.pack(fill="both", expand=True)
        canvas.draw_idle()
        self._canvas = canvas
        self.status.configure(
            text=f"{len(frame)} 根  {times[0]} ~ {times[-1]}",
            text_color=T.TEXT_DIM,
        )

    def _on_close(self) -> None:
        self._closed = True
        canvas = self._canvas
        self._canvas = None
        if canvas is not None:
            try:
                canvas.get_tk_widget().destroy()
                canvas.figure.clf()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            pass
