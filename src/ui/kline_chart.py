"""
K线图表窗口。

功能：双击交叉测试结果的一行，看这个合约、这个周期的K线，下面带成交量。
      窗口里可以换周期、叠加均线；同一合约同一周期只开一个窗口，再双击时还原置顶。
原理：matplotlib 的 Figure 直接挂到 Tk 画布上，不经过 pyplot，也不改全局 backend，
      免得把出图脚本用的 Agg 换掉。数据仍走 KlineProvider，先用电脑里已经下过的K线，
      不够才联网补。取数放后台线程，取完再排回主线程画；窗口关掉后不再碰控件。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

import customtkinter as ctk
from loguru import logger

from src.data.periods import period_label
from src.ui import theme as T

# 一眼能看清最近这一段就够。太多根在窗口里会挤成一团，画起来也慢。
MAX_BARS = 600

# K线颜色按国内习惯：红涨绿跌，比主题色更醒目。
UP_COLOR = "#E5484D"
DOWN_COLOR = "#2ECC8A"

# 图上叠加的均线。窗口开的时候用同一份收盘价算，不重新取数。
MA_WINDOWS = (20, 60)
# 均线不用红绿，免得和下影线、阳阴实体混在一起。
MA_COLORS = (T.ACCENT, T.WARN)

# 下拉里能切的周期。和交叉测试可勾的档一致，自定义分钟不在这里。
CHART_PERIODS = ("5", "15", "30", "60", "D", "W")

# 同一合约、同一周期只留一个窗口；再双击时把已经开着的那个还原、置顶。
_WINDOWS: dict[tuple[str, str], "_KlineChartWindow"] = {}


def moving_average(values: Sequence[float], window: int) -> list[float | None]:
    """简单移动平均。前面不足 window 个点的位置留空，图上不画。"""
    if int(window) <= 0:
        return [None] * len(values)
    size = int(window)
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += float(value)
        if index >= size:
            total -= float(values[index - size])
        if index >= size - 1:
            out[index] = total / size
    return out


def _period_from_label(label: str, fallback: str) -> str:
    """下拉显示的是中文名，换回周期代码。认不出就保持原样。"""
    for code in CHART_PERIODS:
        if period_label(code) == label:
            return code
    return fallback


def open_kline_chart(master: Any, cfg: dict[str, Any], symbol: str, period: str) -> Any:
    """打开一个K线窗口。同一合约同一周期已经开着时，把那一个还原并置顶。"""
    key = (str(symbol), str(period))
    win = _WINDOWS.get(key)
    if win is not None:
        try:
            if win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                return win
        except Exception:  # noqa: BLE001
            # 窗口刚被销毁时 winfo_exists 也可能抛 TkError，当成没开过。
            pass
        _WINDOWS.pop(key, None)
    win = _KlineChartWindow(master, cfg, symbol, period)
    _WINDOWS[key] = win
    return win


class _KlineChartWindow(ctk.CTkToplevel):
    """一个合约、一个周期的K线窗口。标题和状态栏用中文，图里保持 ASCII。"""

    def __init__(self, master: Any, cfg: dict[str, Any], symbol: str, period: str) -> None:
        super().__init__(master)
        self._cfg = cfg
        self._symbol = symbol
        self._period = period
        self._closed = False
        self._canvas: Any = None
        self._bars: Any = None
        # 每次换周期就加一。旧线程回来的数据代次对不上，直接丢掉，不会盖住新图。
        self._load_id = 0
        self.title(self._title_text())
        self.geometry("980x620")
        self.minsize(640, 420)
        self.configure(fg_color=T.BG)
        self.toolbar = ctk.CTkFrame(self, fg_color=T.BG)
        self.toolbar.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(
            self.toolbar,
            text="周期",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).pack(side="left")
        self.period_box = ctk.CTkOptionMenu(
            self.toolbar,
            values=[period_label(code) for code in CHART_PERIODS],
            width=110,
            command=self._on_period_pick,
        )
        self.period_box.set(period_label(period))
        self.period_box.pack(side="left", padx=(6, 14))
        self.ma_var = ctk.BooleanVar(value=True)
        self.ma_switch = ctk.CTkSwitch(
            self.toolbar,
            text="均线 MA20/MA60",
            variable=self.ma_var,
            command=self._on_ma_toggle,
        )
        self.ma_switch.pack(side="left")
        self.status = ctk.CTkLabel(
            self,
            text="正在取K线…",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        )
        self.status.pack(fill="x", padx=12, pady=(6, 4))
        self.body = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=8)
        self.body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_load()

    def _title_text(self) -> str:
        return f"K线 {self._symbol} {period_label(self._period)}"

    def _start_load(self) -> None:
        """换一个代次号，再开后台线程取数。"""
        self._load_id += 1
        gen = self._load_id
        symbol, period = self._symbol, self._period
        threading.Thread(
            target=self._load,
            args=(gen, symbol, period),
            daemon=True,
            name="kline-chart",
        ).start()

    def _load(self, gen: int, symbol: str, period: str) -> None:
        """后台取数。这里不碰任何控件。"""
        try:
            from src.data.kline import KlineProvider

            bars = KlineProvider(self._cfg).fetch(
                symbol,
                period,
                max_bars=MAX_BARS,
                keep_history=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("K线窗口取数失败 {} {}: {}", symbol, period, exc)
            self._on_main(gen, lambda message=str(exc): self._show_error(message))
            return
        self._on_main(gen, lambda: self._draw(bars))

    def _on_main(self, gen: int, callback: Callable[[], None]) -> None:
        """从后台线程排回 Tk 主线程；窗口关掉或已经换了周期时安静放弃。"""
        if self._closed or gen != self._load_id:
            return

        def _run() -> None:
            if self._closed or gen != self._load_id:
                return
            callback()

        try:
            self.after(0, _run)
        except Exception:  # noqa: BLE001
            # 窗口正在销毁时 after 会抛 TclError，这不是错误。
            pass

    def _on_period_pick(self, label: str) -> None:
        """工具栏换周期。这个合约这个周期已经开着别的窗口时，切到那一个去。"""
        code = _period_from_label(label, self._period)
        if code == self._period:
            return
        other = _WINDOWS.get((self._symbol, code))
        if other is not None and other is not self:
            try:
                if other.winfo_exists():
                    other.deiconify()
                    other.lift()
                    other.focus_force()
                    self.period_box.set(period_label(self._period))
                    return
            except Exception:  # noqa: BLE001
                pass
        self._set_period(code)

    def _set_period(self, code: str) -> None:
        """换周期：改登记、清旧图、重新取数。"""
        old_key = (self._symbol, self._period)
        if _WINDOWS.get(old_key) is self:
            _WINDOWS.pop(old_key, None)
        self._period = code
        _WINDOWS[(self._symbol, code)] = self
        self.title(self._title_text())
        self.period_box.set(period_label(code))
        self._bars = None
        self._clear_chart()
        self.status.configure(text="正在取K线…", text_color=T.TEXT_DIM)
        self._start_load()

    def _on_ma_toggle(self) -> None:
        """开关均线只重画，不重新联网取数。"""
        bars = self._bars
        if bars is None or len(bars) == 0:
            return
        self._draw(bars)

    def _show_error(self, message: str) -> None:
        if self._closed:
            return
        self.status.configure(text=f"取不到K线：{message}", text_color=T.WARN)

    def _clear_chart(self) -> None:
        """拿掉旧画布。换周期、重画前先清，避免几张图叠在一起。"""
        canvas = self._canvas
        self._canvas = None
        if canvas is None:
            return
        try:
            canvas.get_tk_widget().destroy()
            canvas.figure.clf()
        except Exception:  # noqa: BLE001
            pass

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

        self._bars = bars
        frame = bars.tail(MAX_BARS)
        times = [str(stamp)[:16] for stamp in frame.index]
        opens = [float(value) for value in frame["open"]]
        highs = [float(value) for value in frame["high"]]
        lows = [float(value) for value in frame["low"]]
        closes = [float(value) for value in frame["close"]]
        colors = [UP_COLOR if close >= open_ else DOWN_COLOR for open_, close in zip(opens, closes)]
        x = list(range(len(frame)))

        self._clear_chart()
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

        if bool(self.ma_var.get()):
            for window, color in zip(MA_WINDOWS, MA_COLORS):
                points = [
                    (index, value)
                    for index, value in enumerate(moving_average(closes, window))
                    if value is not None
                ]
                if not points:
                    continue
                price_ax.plot(
                    [point[0] for point in points],
                    [point[1] for point in points],
                    color=color,
                    linewidth=1.1,
                    label=f"MA{window}",
                )
            if price_ax.lines:
                price_ax.legend(
                    loc="upper left",
                    fontsize=7,
                    framealpha=0.0,
                    labelcolor=T.TEXT_DIM,
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
        self._load_id += 1
        for key, win in list(_WINDOWS.items()):
            if win is self:
                _WINDOWS.pop(key, None)
        self._bars = None
        self._clear_chart()
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            pass
