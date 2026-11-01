"""
下单模拟器。

功能：按你填的品种、手数、价格和买多、卖空、平仓，去点同花顺。
原理：点击步骤调用启动常驻用的 GuiTrader.place_order。
常驻下单仍把价格框留在对手价。只有这里会把你填的限价打进价格框。
这一笔不记入账户。价格请离现价远一些，让同花顺只生成委托，不要马上成交。
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

import customtkinter as ctk
from loguru import logger

from src.trader.base import OrderRequest, OrderResult
from src.trader.gui_trader import GuiTrader
from src.ui import theme as T

SIDE_BY_LABEL = {"买多": "buy_open", "卖空": "sell_open", "平仓": "close"}


def parse_simulator_order(symbol: str, volume: str, price: str, side_label: str) -> OrderRequest | str:
    """
    把窗口里填的字收成一笔下单请求。

    填错时返回一段说明，不返回请求，这样还没点同花顺就能停住。
    """
    code = "".join(ch for ch in str(symbol or "") if ch.isalnum())
    if len(code) < 2:
        return "请填写品种，例如 bu2612。"
    try:
        lots = int(str(volume).strip())
    except ValueError:
        return "手数要填整数。"
    if lots <= 0:
        return "手数要大于 0。"
    try:
        limit = float(str(price).strip())
    except ValueError:
        return "价格要填数字。"
    if limit <= 0:
        return "价格要大于 0。"
    side = SIDE_BY_LABEL.get(str(side_label or "").strip())
    if side is None:
        return "请选择买多、卖空或平仓。"
    return OrderRequest(code, side, lots, limit, gui_price="limit")


def run_simulator_order(cfg: dict[str, Any], request: OrderRequest) -> OrderResult:
    """
    用启动常驻的同一套点击去下这一笔。

    只把 dry_run 在这份副本里关掉，好让鼠标真的点下去。
    不写配置文件，也不把成交记进账户。
    """
    live = copy.deepcopy(cfg)
    live["dry_run"] = False
    trader = GuiTrader(live)
    return trader.place_order(request)


def open_order_simulator(
    master: ctk.CTk,
    cfg: dict[str, Any],
    default_symbol: str,
    log: Callable[[str], None],
    bring_front: Callable[[], None],
) -> None:
    """打开模拟器窗口。已经开着就只把它拿到前面。"""
    existing = getattr(master, "_order_simulator", None)
    if existing is not None and existing.winfo_exists():
        existing._show_in_front()
        return
    window = _SimulatorWindow(master, cfg, default_symbol, log, bring_front)
    master._order_simulator = window  # type: ignore[attr-defined]


class _SimulatorWindow(ctk.CTkToplevel):
    """填写品种、手数、价格和方向，确认后才去点同花顺。"""

    def __init__(
        self,
        master: ctk.CTk,
        cfg: dict[str, Any],
        default_symbol: str,
        log: Callable[[str], None],
        bring_front: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.title("下单模拟器")
        self.geometry("460x420")
        self.cfg = cfg
        self.log = log
        self.bring_front = bring_front
        self.symbol = ctk.StringVar(value=default_symbol or "bu2612")
        self.volume = ctk.StringVar(value="1")
        self.price = ctk.StringVar(value="")
        self.side = ctk.StringVar(value="卖空")
        self._busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        # 按钮松开后，Windows 会把主窗口重新盖到新窗口上。等这次点击结束再露出来。
        # 这时还没去点同花顺，不钉住，也不动同花顺的前后顺序。
        self.after(80, self._show_in_front)

    def _show_in_front(self) -> None:
        """
        把模拟器窗口放到主程序前面，方便填写。

        只在打开窗口时用。短暂置顶是为了盖过刚才被点击的主窗口，随即取消。
        真正下单时另有安排：确认后这个窗口会先躲开，同花顺才被放到最前。
        """
        if not self.winfo_exists() or self._busy:
            return
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(250, self._release_topmost)
        except Exception:  # noqa: BLE001
            pass

    def _release_topmost(self) -> None:
        """取消短暂置顶。窗口留在主程序前面，但不再一直盖住同花顺。正在点击时不再把窗口拉回来。"""
        if not self.winfo_exists() or self._busy:
            return
        try:
            self.attributes("-topmost", False)
            self.lift()
        except Exception:  # noqa: BLE001
            pass

    def _build(self) -> None:
        has_symbol_box = bool(((self.cfg.get("trade") or {}).get("gui") or {}).get("symbol_input"))
        symbol_note = (
            "已标定品种框，确认后会把品种写进同花顺。"
            if has_symbol_box
            else "还没标定品种框，程序不会改同花顺上的合约。请先让面板上的合约和这里一致。"
        )
        ctk.CTkLabel(
            self,
            text=(
                "确认后会调用启动常驻的同一套点击：填价格、填手数、点你选的按钮、按回车。"
                "常驻扫描仍使用对手价，不会改成这里的限价。这一笔不记入账户。"
                "请把价格放得离现价远一些，让它停在委托里。看完后请在同花顺撤掉。"
                "紧急停止：鼠标甩到屏幕左上角。\n"
                + symbol_note
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(16, 8))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=16, pady=4)
        self._row(form, 0, "品种", self.symbol)
        self._row(form, 1, "手数", self.volume)
        self._row(form, 2, "价格", self.price)
        ctk.CTkLabel(form, text="操作", font=T.FONT_BODY, text_color=T.TEXT, width=64, anchor="w").grid(
            row=3, column=0, sticky="w", pady=6
        )
        ctk.CTkOptionMenu(
            form,
            variable=self.side,
            values=["买多", "卖空", "平仓"],
            width=180,
            fg_color=T.BG_ELEVATED,
            button_color=T.BORDER,
            button_hover_color=T.ACCENT,
        ).grid(row=3, column=1, sticky="w", pady=6)

        self.confirm = ctk.CTkButton(
            self,
            text="确认",
            height=36,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._confirm,
        )
        self.confirm.pack(fill="x", padx=16, pady=16)

    def _row(self, form: ctk.CTkFrame, row: int, label: str, variable: ctk.StringVar) -> None:
        ctk.CTkLabel(form, text=label, font=T.FONT_BODY, text_color=T.TEXT, width=64, anchor="w").grid(
            row=row, column=0, sticky="w", pady=6
        )
        ctk.CTkEntry(form, textvariable=variable, width=180, fg_color=T.BG, border_color=T.BORDER).grid(
            row=row, column=1, sticky="w", pady=6
        )

    def _confirm(self) -> None:
        """先检查填写，再问一次，然后才去点同花顺。"""
        from tkinter import messagebox

        if self._busy:
            return
        parsed = parse_simulator_order(self.symbol.get(), self.volume.get(), self.price.get(), self.side.get())
        if isinstance(parsed, str):
            messagebox.showwarning("还不能点", parsed, parent=self)
            return
        request = parsed
        side_name = self.side.get()
        agreed = messagebox.askyesno(
            "确认点击同花顺",
            f"将按常驻的点击步骤发送：\n\n"
            f"{request.symbol}  {side_name}  {request.volume} 手  价格 {request.price}\n\n"
            "这会真的点同花顺。这一笔不记入账户。\n"
            "请确认价格远离现价。看完委托后请手动撤掉。",
            parent=self,
        )
        if not agreed:
            return
        self._busy = True
        self.confirm.configure(state="disabled")
        self.log(
            f"下单模拟器开始：{request.symbol} {side_name} {request.volume} 手，价格 {request.price}。"
            "走常驻的点击代码，不记入账户。"
        )
        # 不要在这里隐藏窗口。一隐藏，Windows 就会把主程序叫到同花顺前面。
        # 真正点击时，常驻那套下单代码会自己把本程序沉到后面。

        def job() -> None:
            # 等确认框关掉，再开始点击，避免确认框还占着焦点。
            time.sleep(0.2)
            try:
                result = run_simulator_order(self.cfg, request)
            except Exception as exc:  # noqa: BLE001
                logger.exception("下单模拟器失败")
                text = f"下单模拟器出错：{exc}"
                self.after(0, lambda: self._finish(text, False))
                return
            text = result.message if result.ok else f"没有点成：{result.message}"
            self.after(0, lambda: self._finish(text, result.ok))

        threading.Thread(target=job, name="order-simulator", daemon=True).start()

    def _finish(self, text: str, ok: bool) -> None:
        from tkinter import messagebox

        self._busy = False
        if self.winfo_exists():
            self.deiconify()
            self.confirm.configure(state="normal")
        self.log(text)
        # 点完不要把主程序拉到最前。结果用下面这个小提示即可，避免再次挡住同花顺。
        if not self.winfo_exists():
            return
        if ok:
            messagebox.showinfo(
                "点击已发出",
                text + "\n\n请到同花顺的委托里核对。确认是你要的那张单之后，请撤掉。下次启动常驻仍会把价格改回对手价。",
                parent=self,
            )
        else:
            messagebox.showwarning("没有点成", text, parent=self)
