"""
仿真柜台测试窗口。

功能：明天连华泰测试前置时，用按钮完成认证登录、开仓、平仓、撤单，
以及合约、价位、手数这三项本机拦截。结果写进界面日志和 logs 目录。
原理：这个窗口自己建一条 CTP 连接，不改变运行页正在用的执行通道。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from loguru import logger

from src.trader.base import OrderRequest
from src.trader.ctp_logic import check_order_instruction
from src.trader.ctp_trader import CtpTrader
from src.utils.config_loader import load_config


def open_ctp_probe(master: tk.Misc) -> None:
    """打开测试窗口。重复点击只把原来的窗口提到前面。"""
    existing = getattr(master, "_ctp_probe", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    window = CtpProbeWindow(master)
    master._ctp_probe = window  # type: ignore[attr-defined]


class CtpProbeWindow(tk.Toplevel):
    """华泰仿真测试用的小窗口。按钮点下去的说明会同时出现在主界面日志里。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("仿真柜台测试")
        self.geometry("640x520")
        self.trader: CtpTrader | None = None
        self.md = None
        self.symbol = tk.StringVar(value="bu2612")
        self.price = tk.StringVar(value="")
        self.volume = tk.StringVar(value="1")
        # 这些数字来自仿真行情。没收到之前用横线占着，避免和同花顺的实盘价混在一起。
        self.last = tk.StringVar(value="—")
        self.bid = tk.StringVar(value="—")
        self.ask = tk.StringVar(value="—")
        self.upper = tk.StringVar(value="—")
        self.lower = tk.StringVar(value="—")
        self.quote_time = tk.StringVar(value="—")
        self._shown_quote: tuple[str, ...] | None = None
        self._logged_quote = ""
        self._buttons: list[ttk.Button] = []
        self._busy = False
        # 柜台登录和下单要等网络。这些结果先放进队列，由界面自己的定时器取出来写，避免把窗口卡死。
        self._ui_queue: queue.Queue = queue.Queue()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_ui)

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        form = ttk.Frame(self)
        form.pack(fill="x", **pad)
        ttk.Label(form, text="合约").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.symbol, width=16).grid(row=0, column=1, sticky="w")
        ttk.Label(form, text="价格").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.price, width=12).grid(row=0, column=3, sticky="w")
        ttk.Label(form, text="手数").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=self.volume, width=6).grid(row=0, column=5, sticky="w")
        # 第二行是仿真柜台自己的报价。买开、卖开要落在买一和卖一附近，并且不能超出涨停、跌停。
        labels = (
            (1, 0, "最新"),
            (1, 1, self.last),
            (1, 2, "买一"),
            (1, 3, self.bid),
            (2, 0, "卖一"),
            (2, 1, self.ask),
            (2, 2, "涨停"),
            (2, 3, self.upper),
            (3, 0, "跌停"),
            (3, 1, self.lower),
            (3, 2, "行情时间"),
            (3, 3, self.quote_time),
        )
        for row, column, item in labels:
            if isinstance(item, str):
                widget = ttk.Label(form, text=item)
            else:
                widget = ttk.Label(form, textvariable=item)
            widget.grid(row=row, column=column, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", **pad)
        labels = [
            ("连接登录", self._connect),
            ("暂停报单", self._pause),
            ("恢复报单", self._resume),
            ("买开", self._buy_open),
            ("卖平", self._sell_close),
            ("撤最近一笔", self._cancel),
            ("错误合约", self._bad_symbol),
            ("错误价位", self._bad_price),
            ("超过手数", self._too_big),
        ]
        for index, (text, command) in enumerate(labels):
            button = ttk.Button(buttons, text=text, command=command)
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4)
            self._buttons.append(button)
        for column in range(3):
            buttons.columnconfigure(column, weight=1)

        self.box = tk.Text(self, height=16, wrap="word")
        self.box.pack(fill="both", expand=True, padx=8, pady=8)
        self._say(
            "点「连接登录」后，窗口会保持可点。登录大约要等十几秒，结果出现在下面。"
            "上面的最新价、买一、卖一是仿真行情，不是同花顺的价格。"
            "日志文件在 logs 文件夹，文件名是 auto_trading_日期.log。"
        )

    def _drain_ui(self) -> None:
        """把后台线程交来的界面更新，在窗口自己的线程里做完。"""
        try:
            while True:
                task = self._ui_queue.get_nowait()
                task()
        except queue.Empty:
            pass
        self._refresh_quote()
        if self.winfo_exists():
            self.after(100, self._drain_ui)

    def _on_ui(self, task) -> None:
        """后台线程不要直接改输入框。把要做的事放进队列。"""
        self._ui_queue.put(task)

    def _say(self, text: str) -> None:
        """写进这个窗口，也写进主程序日志，方便截图和事后查看。"""
        logger.info("仿真测试：{}", text)
        if threading.current_thread() is threading.main_thread():
            self._write(text)
            return
        self._on_ui(lambda: self._write(text))

    def _write(self, text: str) -> None:
        if not self.winfo_exists():
            return
        self.box.insert("end", text + "\n")
        self.box.see("end")

    def _set_busy(self, busy: bool) -> None:
        """登录或下单还在等柜台时，按钮先停用，避免连点两次。"""
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._buttons:
            button.configure(state=state)

    def _run_in_background(self, waiting: str, work) -> None:
        """
        把会等网络的动作放到旁边的线程。

        连接登录会一直等到柜台回应，最长大约二十秒。
        如果放在窗口自己的线程里，Windows 会把标题改成「未响应」。
        """
        if self._busy:
            self._say("上一步还没结束，请等下面出现结果。")
            return
        self._set_busy(True)
        self._say(waiting)

        def job() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001
                logger.exception("仿真测试后台任务失败")
                self._say(f"出错了：{exc}")
            finally:
                self._on_ui(lambda: self._set_busy(False))

        threading.Thread(target=job, name="ctp-probe", daemon=True).start()

    def _ensure(self) -> CtpTrader | None:
        if self.trader is not None:
            return self.trader
        cfg = load_config()
        # 这个窗口只测柜台。即使配置里的模拟开关开着，这里也按真连接准备。
        cfg["dry_run"] = False
        self.trader = CtpTrader(cfg)
        return self.trader

    def _connect(self) -> None:
        trader = self._ensure()
        if trader is None:
            return

        def work() -> None:
            ok, message = trader.connect()
            self._say(("连接成功：" if ok else "连接失败：") + message)
            if ok:
                self._say(
                    f"账号 {trader.settings.user_id} AppID {trader.settings.app_id}。"
                    f"报单阈值 {trader.settings.order_count_warn} 笔，"
                    f"撤单阈值 {trader.settings.cancel_count_warn} 笔，"
                    f"单笔最多 {trader.settings.max_order_volume} 手，"
                    f"最小变动价位 {trader.settings.price_tick}。"
                )
                # 柜台两次查询至少要隔 1 秒。登录时刚查过报单，这里等一下再要报价。
                time.sleep(1.1)
                self._load_price_band(trader)
                self._open_market(trader)

        self._run_in_background("正在连接柜台，请稍等。窗口不会卡住。", work)

    def _load_price_band(self, trader: CtpTrader) -> None:
        """先向交易前置要一笔快照，窗口不用干等第一笔行情。"""
        symbol = self.symbol.get().strip() or "bu2612"
        ok, text = trader.gateway.query_price_band(symbol)
        if not ok:
            self._say(text)
            return
        band = trader.gateway._instrument or {}
        self._show_quote_soon(band)

    def _open_market(self, trader: CtpTrader) -> None:
        """
        连接仿真行情端口，订阅窗口里这个合约。

        交易登录成功之后才做。行情断了也不影响刚才的交易登录结果。
        """
        from src.trader.ctp_md import CtpMdClient

        if self.md is None:
            self.md = CtpMdClient(trader.settings)
        ok, message = self.md.start()
        self._say(message)
        if not ok:
            return
        symbol = self.symbol.get().strip() or "bu2612"
        ok, message = self.md.subscribe(symbol)
        self._say(message)

    def _show_quote_soon(self, row: dict) -> None:
        """后台线程把快照交给窗口线程去画。"""
        copied = dict(row)
        self._on_ui(lambda: self._show_quote(copied))

    def _refresh_quote(self) -> None:
        """行情线程只保存最新一笔。窗口定时来取，避免每一笔都刷日志。"""
        md = self.md
        if md is None or md.latest is None:
            return
        self._show_quote(md.latest)

    def _show_quote(self, row: dict) -> None:
        """把最新价、买一、卖一、涨跌停写到价格框上面。"""
        if not self.winfo_exists():
            return
        signature = (
            str(row.get("symbol") or ""),
            str(row.get("last_text") or "—"),
            str(row.get("bid_text") or "—"),
            str(row.get("ask_text") or "—"),
            str(row.get("upper_text") or "—"),
            str(row.get("lower_text") or "—"),
            str(row.get("time_text") or "—"),
        )
        if signature == self._shown_quote:
            return
        self._shown_quote = signature
        self.last.set(signature[1])
        self.bid.set(signature[2])
        self.ask.set(signature[3])
        self.upper.set(signature[4])
        self.lower.set(signature[5])
        self.quote_time.set(signature[6])
        from src.trader.ctp_md import quote_line

        line = quote_line(row)
        if line != self._logged_quote:
            self._logged_quote = line
            logger.info("仿真测试：{}", line)
            self._write(line)

    def _pause(self) -> None:
        trader = self._ensure()
        if trader is None:
            return
        trader.gateway.trading_paused = True
        self._say("已暂停报单。之后的开平仓不会发到柜台。点「停止」也能停掉常驻策略。")

    def _resume(self) -> None:
        trader = self._ensure()
        if trader is None:
            return
        trader.gateway.trading_paused = False
        self._say("已恢复报单。")

    def _buy_open(self) -> None:
        self._send("buy_open")

    def _sell_close(self) -> None:
        self._send("close", position_side="long")

    def _send(self, side: str, position_side: str | None = None) -> None:
        trader = self._ensure()
        if trader is None:
            return
        try:
            price = float(self.price.get())
            volume = int(self.volume.get())
        except ValueError:
            self._say("价格和手数要填数字。")
            return
        request = OrderRequest(self.symbol.get().strip(), side, volume, price, position_side=position_side)

        def work() -> None:
            result = trader.place_order(request)
            self._say(f"{side}：{result.message}")

        self._run_in_background("正在把这笔单发给柜台。", work)

    def _cancel(self) -> None:
        trader = self._ensure()
        if trader is None:
            return
        symbol = self.symbol.get().strip()

        def work() -> None:
            result = trader.cancel_latest(symbol)
            self._say(result.message)

        self._run_in_background("正在撤销最近一笔。", work)

    def _bad_symbol(self) -> None:
        self._reject("ABC", self._price_or_one(), 1)

    def _bad_price(self) -> None:
        trader = self._ensure()
        tick = trader.settings.price_tick if trader is not None else 1
        self._reject(self.symbol.get().strip() or "bu2612", float(tick) / 2, 1)

    def _too_big(self) -> None:
        trader = self._ensure()
        limit = trader.settings.max_order_volume if trader is not None else 2
        self._reject(self.symbol.get().strip() or "bu2612", self._price_or_one(), int(limit) + 1)

    def _reject(self, symbol: str, price: float, volume: int) -> None:
        trader = self._ensure()
        if trader is None:
            return
        reason = check_order_instruction(
            symbol,
            price,
            volume,
            price_tick=trader.settings.price_tick,
            max_volume=trader.settings.max_order_volume,
        )
        self._say(reason or "这笔没有被拦住，请检查阈值。")

    def _price_or_one(self) -> float:
        try:
            return float(self.price.get())
        except ValueError:
            return 1.0

    def _close(self) -> None:
        if self._busy:
            self._say("柜台还在回应，等下面出现结果后再关窗口。")
            return
        if self.md is not None:
            self.md.close()
            self.md = None
        if self.trader is not None:
            self.trader.shutdown()
            self.trader = None
        self.destroy()
