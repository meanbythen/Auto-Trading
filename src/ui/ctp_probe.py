"""
仿真/实盘共用的 CTP 交易窗口。

功能：给人手工测开、平、撤、反手。策略挂机请用运行页「启动常驻」，不要靠这个窗口自动扫信号。
下单走和常驻相同的 CtpTrader.place_order（含沥青/玉米平今平昨）。窗口自己连柜台。
原理：按钮把动作放到旁边线程，避免窗口卡住；持仓/委托/成交三张表直接画柜台回报。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from loguru import logger

from src.trader.base import capabilities_for
from src.trader.ctp_orders import (
    ctp_order,
    flatten_order_steps,
    prepare_live_ctp_cfg,
    prepare_self_test_cfg,
    reverse_order_steps,
)
from src.trader.ctp_display import (
    account_headline,
    account_pairs,
    cancelled_volume,
    contract_search_key,
    describe_contract,
    format_contract_row,
    hedge_cn,
    money_text,
    order_matches_filter,
    position_footer,
    remaining_volume,
    split_position_rows,
    trade_footer,
)
from src.trader.ctp_gateway import STATUS_TEXT, CtpSettings
from src.risk.sizing import PositionSizer
from src.trader.ctp_logic import (
    ctp_kind_label,
    direction_cn,
    normalize_ctp_kind,
    offset_cn,
    overlay_ctp_profile,
    symbol_key,
)
from src.trader.ctp_trader import CtpTrader
from src.domain import trading_account as ta
from src.trader.go_live import (
    HUATAI_REQUIRED,
    account_kind_for_stage,
    current_stage_from_cfg,
    load_state,
    save_state,
    stage_label,
)
from src.utils.config_loader import load_config


def _order_row_sort_key(row: tuple[str, ...]) -> tuple[str, int]:
    """委托/成交按时间从新到旧。同一秒再看编号，大的在上。"""
    clock = str(row[0] if row else "")
    ref = str(row[-1] if row else "").strip()
    try:
        number = int(ref)
    except ValueError:
        number = 0
    return (clock, number)


def resident_ctp_trader(app: object) -> CtpTrader | None:
    """
    常驻已经在用的那条柜台连接。

    有就还给交易台画持仓、资金、委托，不要再登录一次（柜台会把一边踢下线）。
    常驻没开、或还在点同花顺时，返回空，交易台自己连。
    """
    worker = getattr(app, "worker", None)
    if worker is None or not bool(getattr(worker, "running", False)):
        return None
    bot = getattr(worker, "bot", None)
    trader = getattr(bot, "trader", None)
    if not isinstance(trader, CtpTrader):
        return None
    return trader


def desk_login_block_reason(trader: CtpTrader | None) -> str:
    """交易台这条连接还登着柜台时，说明为什么不能再开常驻。没登着返回空。"""
    if trader is None:
        return ""
    session = getattr(getattr(trader, "gateway", None), "session", None)
    phase = str(getattr(session, "phase", "idle") or "idle")
    # idle / disconnected：窗口开着但没登录，常驻可以连。正在握手或已登录都不能再开第二条。
    if phase in ("", "idle", "disconnected"):
        return ""
    user = str(getattr(getattr(trader, "settings", None), "user_id", "") or "").strip()
    who = f"资金账号 {user}" if user else "当前账户"
    return (
        f"交易台还登着{who}。常驻再连会被柜台踢下线。"
        "请先关掉交易台，再启动常驻。"
    )


def probe_login_block_reason(app: tk.Misc) -> str:
    """主窗口上的交易台还登着时，返回给启动常驻看的原因。"""
    window = getattr(app, "_ctp_probe", None)
    if window is None:
        return ""
    try:
        if not window.winfo_exists():
            return ""
    except tk.TclError:
        return ""
    # 窗口只是在看常驻那条连接，不是第二条登录，启动常驻不必先关窗。
    if bool(getattr(window, "_attached_resident", False)):
        return ""
    return desk_login_block_reason(getattr(window, "trader", None))


def shutdown_probe_window(app: tk.Misc) -> str:
    """
    关掉交易台并释放柜台连接。

    窗口正忙着等回报时先不关，返回原因。成功关掉返回空字符串。
    """
    window = getattr(app, "_ctp_probe", None)
    if window is None:
        return ""
    try:
        if not window.winfo_exists():
            return ""
    except tk.TclError:
        return ""
    if bool(getattr(window, "_busy", False)):
        return "交易台还在等柜台回应，请等窗口出现结果后再关，然后再启动常驻。"
    closer = getattr(window, "_close", None)
    if callable(closer):
        closer()
    return ""


def open_ctp_probe(master: tk.Misc) -> None:
    """打开交易窗口。重复点击只把原来的窗口提到前面。"""
    existing = getattr(master, "_ctp_probe", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    window = CtpProbeWindow(master)
    master._ctp_probe = window  # type: ignore[attr-defined]


def watch_seed(cfg: dict) -> list[str]:
    """盘口初始清单：当前交易合约排第一，后面跟配置关注品种，去重保序。"""
    trade = cfg.get("trade") or {}
    strategy = cfg.get("strategy") or {}
    first = str(trade.get("symbol") or "").strip()
    items = [first] if first else []
    items.extend(str(item).strip() for item in (strategy.get("symbols") or []) if str(item).strip())
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = symbol_key(item)
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result or ["fu2701"]


@dataclass
class _QuoteRow:
    """盘口一行。单合约就是只看第一行，多合约时每行各自保存。"""

    symbol: tk.StringVar
    last: tk.StringVar
    bid: tk.StringVar
    bid_vol: tk.StringVar
    ask: tk.StringVar
    ask_vol: tk.StringVar
    upper: tk.StringVar
    lower: tk.StringVar
    quote_time: tk.StringVar
    signature: tuple[str, ...] | None = None


class CtpProbeWindow(tk.Toplevel):
    """CTP 交易窗口。仿真测试和以后实盘柜台用同一套按钮和表格。"""

    QUOTE_ROWS = 5

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("CTP 交易台（手工测开平撤；挂机请用启动常驻）")
        self.geometry("1280x900")
        self.trader: CtpTrader | None = None
        self.md = None
        cfg = load_config()
        self._watch = watch_seed(cfg)
        watch = self._watch
        self.symbol = tk.StringVar(value=watch[0])
        self.price = tk.StringVar(value="")
        self.volume = tk.StringVar(value="1")
        self._quote_rows: list[_QuoteRow] = []
        self.quote_hint = tk.StringVar(value="")
        self.watch_line = tk.StringVar(value="")
        self.account_line = tk.StringVar(value="还没登录")
        self.limit_line = tk.StringVar(value="")
        self.count_line = tk.StringVar(value="今日报单 0 笔，撤单 0 笔")
        self.money_line = tk.StringVar(value="资金还没查到。")
        self.price_mode = tk.StringVar(value="手动")
        self.ord_filter = tk.StringVar(value="全部")
        self.contract_q = tk.StringVar(value="")
        self.contract_detail = tk.StringVar(value="登录后输入代码或前缀，点查询。再点一行，会填进上方合约框并查出保证金、手续费。")
        self._fund_values: dict[str, tk.StringVar] = {}
        self._filling_tree = False
        self._followed_symbols: tuple[str, ...] = ()
        self._picked_order_ref = ""
        self._cost_loaded: set[str] = set()
        self._last_pick = ""
        self._last_pick_at = 0.0
        self._sizer = PositionSizer(cfg)
        self._root = str(cfg.get("_project_root") or ".")
        self._stage = current_stage_from_cfg(cfg)
        kind = account_kind_for_stage(self._stage)
        active = str(((cfg.get("trade") or {}).get("ctp") or {}).get("account_id") or "").strip().lower()
        self.kind = tk.StringVar(
            value=ta.SELF_TEST_LABEL if active == ta.SELF_TEST_ACCOUNT_ID else ctp_kind_label(kind)
        )
        self._go_live = load_state(self._root)
        self._huatai_vars: dict[str, tk.BooleanVar] = {}
        self._last_said = ""
        self._buttons: list[ttk.Button] = []
        self._busy = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._blotter_tick = 0
        self._quote_pump_on = False
        # True：窗口画的是常驻已登录的柜台。关掉窗口不能 shutdown，否则挂机会断线。
        self._attached_resident = False
        self._kind_box: ttk.Combobox | None = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_ui)
        self._attach_resident_if_any()

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        ttk.Label(self, textvariable=self.account_line).pack(anchor="w", padx=8, pady=(8, 0))
        ttk.Label(self, textvariable=self.money_line, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8)
        ttk.Label(self, textvariable=self.limit_line, foreground="#555").pack(anchor="w", padx=8)
        ttk.Label(self, textvariable=self.count_line, foreground="#333").pack(anchor="w", padx=8)

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="操作账").grid(row=0, column=0, sticky="w")
        kind_box = ttk.Combobox(
            top,
            textvariable=self.kind,
            values=[ctp_kind_label("sim"), ctp_kind_label("live"), ta.SELF_TEST_LABEL],
            state="readonly",
            width=18,
        )
        kind_box.grid(row=0, column=1, sticky="w", padx=(4, 12))
        kind_box.bind("<<ComboboxSelected>>", lambda _e: self._on_kind_change())
        self._kind_box = kind_box
        ttk.Label(
            top,
            text="程序自测只在本机柜台成交；仿真/真实走同一套下单。换账后请再点连接登录。",
        ).grid(row=0, column=2, sticky="w")

        form = ttk.Frame(self)
        form.pack(fill="x", **pad)
        ttk.Label(form, text="合约").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.symbol, width=12).grid(row=0, column=1, sticky="w")
        ttk.Button(form, text="订阅行情", command=self._resub).grid(row=0, column=2, padx=4)
        ttk.Label(form, text="价格").grid(row=0, column=3, sticky="w")
        ttk.Entry(form, textvariable=self.price, width=10).grid(row=0, column=4, sticky="w")
        ttk.Label(form, text="手数").grid(row=0, column=5, sticky="w")
        ttk.Entry(form, textvariable=self.volume, width=6).grid(row=0, column=6, sticky="w")
        ttk.Label(form, text="价格方式").grid(row=0, column=7, sticky="w", padx=(12, 0))
        ttk.Combobox(
            form,
            textvariable=self.price_mode,
            values=("手动", "对手价", "最新价"),
            state="readonly",
            width=8,
        ).grid(row=0, column=8, sticky="w", padx=4)

        quotes = ttk.Frame(self)
        quotes.pack(fill="x", **pad)
        titles = ("合约", "最新", "买一", "买一量", "卖一", "卖一量", "涨停", "跌停", "行情时间")
        for col, title in enumerate(titles):
            ttk.Label(quotes, text=title).grid(row=0, column=col, sticky="w", padx=6)
        for index in range(self.QUOTE_ROWS):
            row = _QuoteRow(
                symbol=tk.StringVar(
                    value=self._watch[index] if index < len(self._watch) else ""
                ),
                last=tk.StringVar(value="—"),
                bid=tk.StringVar(value="—"),
                bid_vol=tk.StringVar(value="0"),
                ask=tk.StringVar(value="—"),
                ask_vol=tk.StringVar(value="0"),
                upper=tk.StringVar(value="—"),
                lower=tk.StringVar(value="—"),
                quote_time=tk.StringVar(value="—"),
            )
            self._quote_rows.append(row)
            row.symbol.trace_add("write", lambda *_args, item=row: self._watch_symbol_edit(item))
            ttk.Entry(quotes, textvariable=row.symbol, width=12).grid(
                row=index + 1, column=0, sticky="w", padx=6, pady=2
            )
            for col, (var, fill) in enumerate(
                (
                    (row.last, "last"),
                    (row.bid, "bid"),
                    (row.bid_vol, None),
                    (row.ask, "ask"),
                    (row.ask_vol, None),
                    (row.upper, "upper"),
                    (row.lower, "lower"),
                    (row.quote_time, None),
                ),
                start=1,
            ):
                self._quote_cell(quotes, index + 1, col, var, fill, index)
        self.local_time = tk.StringVar(value="—")
        ttk.Label(quotes, textvariable=self.local_time).grid(
            row=self.QUOTE_ROWS + 1, column=8, sticky="w", padx=6
        )
        ttk.Label(
            self,
            text="点盘口数字会把这一行的合约和价格填进上方；只订阅填了代码的行。",
            foreground="#666",
        ).pack(anchor="w", padx=8)
        ttk.Label(self, textvariable=self.watch_line, foreground="#666").pack(anchor="w", padx=8)
        shown = min(len(self._watch), self.QUOTE_ROWS)
        line = f"盘口 {shown} 行来自配置关注品种（当前交易合约排第一）。"
        if len(self._watch) > self.QUOTE_ROWS:
            line += (
                f"配置关注共 {len(self._watch)} 个，其余 {len(self._watch) - self.QUOTE_ROWS} 个未订阅；"
                "需要看时先清空一行，再在上方切换；当前合约仍会订阅，但不会多占一行。"
            )
        self.watch_line.set(line)
        ttk.Label(self, textvariable=self.quote_hint, foreground="#666").pack(anchor="w", padx=8)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", **pad)
        labels = [
            ("连接登录", self._connect),
            ("刷新持仓", self._query_positions),
            ("刷新委托", self._query_orders),
            ("刷新成交", self._query_trades),
            ("买开", self._buy_open),
            ("卖开", self._sell_open),
            ("买平", self._buy_close),
            ("卖平", self._sell_close),
            ("平仓", self._flatten),
            ("反手", self._reverse),
            ("撤选中委托", self._cancel_selected),
            ("全撤未成交", self._cancel_all),
            ("暂停报单", self._pause),
            ("恢复报单", self._resume),
            ("错误合约", self._block_symbol),
            ("错误价位", self._block_tick),
            ("超过手数", self._block_lots),
        ]
        for index, (text, command) in enumerate(labels):
            button = ttk.Button(buttons, text=text, command=command)
            button.grid(row=index // 5, column=index % 5, sticky="ew", padx=4, pady=4)
            self._buttons.append(button)
        for column in range(5):
            buttons.columnconfigure(column, weight=1)

        book = ttk.Notebook(self)
        book.pack(fill="both", expand=True, padx=8, pady=4)
        self._desk_book = book
        book.bind("<<NotebookTabChanged>>", self._on_desk_tab)
        self.pos_tree = self._make_tree(
            book,
            "持仓",
            (
                "合约",
                "买卖",
                "手数",
                "今仓",
                "昨仓",
                "均价",
                "浮盈",
                "保证金",
                "市值",
                "盈利价差",
                "涨跌比例",
                "涨停浮盈",
                "资金占比",
                "投保",
            ),
            {
                "合约": 80,
                "买卖": 45,
                "手数": 45,
                "今仓": 45,
                "昨仓": 45,
                "均价": 70,
                "浮盈": 80,
                "保证金": 80,
                "市值": 80,
                "盈利价差": 70,
                "涨跌比例": 70,
                "涨停浮盈": 80,
                "资金占比": 70,
                "投保": 50,
            },
            footer=True,
        )
        self._bind_pick(self.pos_tree, self._pick_position)
        ord_page = ttk.Frame(book)
        book.add(ord_page, text="委托")
        filt = ttk.Frame(ord_page)
        filt.pack(fill="x", padx=4, pady=2)
        ttk.Label(filt, text="筛选").pack(side="left")
        for name in ("全部", "可撤", "已撤", "成交"):
            ttk.Radiobutton(
                filt,
                text=name,
                value=name,
                variable=self.ord_filter,
                command=self._paint_blotter,
            ).pack(side="left", padx=4)
        ttk.Label(filt, text="今天的委托，含已成交。点一行填入合约；双击或再点「撤选中委托」。").pack(side="left", padx=12)
        self.ord_tree = self._tree_in(
            ord_page,
            ("时间", "合约", "状态", "买卖", "开平", "委托价", "委托量", "可撤", "已成交", "已撤", "主场号", "投保", "编号"),
            {
                "时间": 70,
                "合约": 80,
                "状态": 70,
                "买卖": 45,
                "开平": 50,
                "委托价": 70,
                "委托量": 55,
                "可撤": 45,
                "已成交": 55,
                "已撤": 45,
                "主场号": 110,
                "投保": 45,
                "编号": 70,
            },
        )
        self._bind_pick(self.ord_tree, self._pick_order)
        self.ord_tree.bind("<Double-1>", lambda _e: self._cancel_selected())
        self.trd_tree = self._make_tree(
            book,
            "成交",
            ("时间", "合约", "买卖", "开平", "成交价", "成交量", "平仓盈亏", "手续费", "主场号", "投保", "编号"),
            {
                "时间": 70,
                "合约": 80,
                "买卖": 45,
                "开平": 50,
                "成交价": 70,
                "成交量": 55,
                "平仓盈亏": 80,
                "手续费": 70,
                "主场号": 110,
                "投保": 45,
                "编号": 70,
            },
            footer=True,
        )
        self._build_fund_tab(book)
        self._build_contract_tab(book)
        self._build_huatai_tab(book)
        self._fill_footer(self.pos_tree, position_footer([]))
        self._fill_footer(self.trd_tree, trade_footer([]))

        self.box = tk.Text(self, height=8, wrap="word")
        self.box.pack(fill="x", padx=8, pady=8)
        self._say(
            f"当前上线阶段：{stage_label(self._stage)}。"
            "这里只给人点：买开、卖开、买平、卖平、平仓（锁仓会平两边）、反手（先平再反向开）、撤单。"
            "和常驻策略用同一套报单。策略自动出信号要到③④再点「启动常驻」。"
            "点蓝色数字可把价格填进框。卖开用买一，买开用卖一。测涨跌停可点涨停/跌停。"
            "改价请先撤未成交委托。柜台外接测试请打开「柜台必测」页勾选。"
        )

    def _make_tree(
        self,
        book: ttk.Notebook,
        title: str,
        columns: tuple[str, ...],
        widths: dict[str, int],
        footer: bool = False,
    ) -> ttk.Treeview:
        frame = ttk.Frame(book)
        book.add(frame, text=title)
        return self._tree_in(frame, columns, widths, footer=footer)

    def _tree_in(
        self,
        parent: ttk.Frame,
        columns: tuple[str, ...],
        widths: dict[str, int],
        footer: bool = False,
    ) -> ttk.Treeview:
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(box, columns=columns, show="headings", height=8)
        for name in columns:
            tree.heading(name, text=name)
            tree.column(name, width=widths.get(name, 80), anchor="center")
        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        if footer:
            try:
                ttk.Style().layout("CtpSum.Treeview.Heading", [])
            except tk.TclError:
                pass
            foot = ttk.Treeview(
                parent,
                columns=columns,
                show="headings",
                height=1,
                selectmode="none",
                style="CtpSum.Treeview",
            )
            for name in columns:
                foot.heading(name, text="")
                foot.column(name, width=widths.get(name, 80), anchor="center")
            foot.tag_configure("sum", background="#d9e4f2")
            foot.pack(fill="x")
            tree._footer = foot  # type: ignore[attr-defined]
            tree.bind("<Configure>", lambda _e, t=tree: self._sync_footer_width(t), add="+")
        else:
            tree._footer = None  # type: ignore[attr-defined]
        return tree

    def _sync_footer_width(self, tree: ttk.Treeview) -> None:
        foot = getattr(tree, "_footer", None)
        if foot is None:
            return
        for name in tree["columns"]:
            foot.column(name, width=int(tree.column(name, "width") or 80))

    def _fill_footer(self, tree: ttk.Treeview, row: tuple[str, ...]) -> None:
        foot = getattr(tree, "_footer", None)
        if foot is None:
            return
        foot.delete(*foot.get_children())
        foot.insert("", "end", values=row, tags=("sum",))
        self._sync_footer_width(tree)

    def _build_fund_tab(self, book: ttk.Notebook) -> None:
        """资金页：柜台资金查询的完整字段。"""
        frame = ttk.Frame(book)
        book.add(frame, text="资金")
        ttk.Button(frame, text="刷新资金", command=self._query_positions).pack(anchor="w", padx=8, pady=6)
        grid = ttk.Frame(frame)
        grid.pack(fill="both", expand=True, padx=8, pady=4)
        for index, (title, _value) in enumerate(account_pairs({})):
            row, col = divmod(index, 2)
            ttk.Label(grid, text=title).grid(row=row, column=col * 2, sticky="e", padx=6, pady=3)
            var = tk.StringVar(value="—")
            self._fund_values[title] = var
            ttk.Label(grid, textvariable=var, width=22, anchor="w").grid(row=row, column=col * 2 + 1, sticky="w")

    def _build_contract_tab(self, book: ttk.Notebook) -> None:
        """合约页：搜索后列出资料，点一行带回下单区和保证金、手续费。"""
        frame = ttk.Frame(book)
        book.add(frame, text="合约")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Label(bar, text="合约搜索").pack(side="left")
        ttk.Entry(bar, textvariable=self.contract_q, width=16).pack(side="left", padx=4)
        ttk.Button(bar, text="查询", command=self._query_contracts).pack(side="left")
        ttk.Button(bar, text="写入品种表", command=self._sync_contract_to_universe).pack(side="left", padx=4)
        ttk.Label(
            bar,
            text="查询后点一行，再点「写入品种表」：每手数量、最小变动、保证金、手续费写入 config/品种与合约.xlsx。不改启用和时段。",
        ).pack(side="left", padx=8)
        self.ins_tree = self._tree_in(
            frame,
            (
                "交易代码",
                "合约名称",
                "市场",
                "类别",
                "每手数量",
                "最小变动价位",
                "多头保证金率",
                "空头保证金率",
                "开仓手续费",
                "平仓手续费",
                "平今手续费",
                "开仓手续费率",
                "平仓手续费率",
                "平今手续费率",
                "上市日",
            ),
            {
                "交易代码": 80,
                "合约名称": 90,
                "市场": 70,
                "类别": 50,
                "每手数量": 70,
                "最小变动价位": 80,
                "多头保证金率": 90,
                "空头保证金率": 90,
                "开仓手续费": 80,
                "平仓手续费": 80,
                "平今手续费": 80,
                "开仓手续费率": 90,
                "平仓手续费率": 90,
                "平今手续费率": 90,
                "上市日": 80,
            },
        )
        self._bind_pick(self.ins_tree, self._pick_contract)
        ttk.Label(frame, textvariable=self.contract_detail, wraplength=1180, justify="left").pack(
            anchor="w", padx=8, pady=6
        )

    def _drain_ui(self) -> None:
        try:
            while True:
                task = self._ui_queue.get_nowait()
                task()
        except queue.Empty:
            pass
        self._refresh_quote()
        self.local_time.set(time.strftime("%H:%M:%S"))
        self._blotter_tick += 1
        if self._blotter_tick % 8 == 0:
            self._paint_blotter()
        if self.winfo_exists():
            self.after(100, self._drain_ui)

    def _on_ui(self, task) -> None:
        self._ui_queue.put(task)

    def _log_tag(self) -> str:
        """运行日志前缀。程序自测、仿真和真实账号明确区分。"""
        from src.trader.ctp_logic import ctp_kind_label

        trader = self.trader
        if trader is None:
            return "交易台"
        if trader.settings.is_self_test:
            return "交易台（程序自测/本机仿真柜台）"
        from src.trader.ctp_md import quote_channel_word

        kind = ctp_kind_label(trader.settings.account_kind)
        channel = quote_channel_word(trader.settings)
        return f"交易台（{kind}/{channel}）"

    def _say(self, text: str) -> None:
        # 同一句话不连着刷。选中持仓、盘口没变时运行日志不再重复写。
        if text == self._last_said:
            return
        self._last_said = text
        logger.info("{}：{}", self._log_tag(), text)
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
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._buttons:
            button.configure(state=state)

    def _run_in_background(self, waiting: str, work) -> None:
        if self._busy:
            self._say("上一步还没结束，请等下面出现结果。")
            return
        self._set_busy(True)
        self._say(waiting)

        def job() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001
                logger.exception("交易台后台任务失败")
                self._say(f"出错了：{exc}")
            finally:
                self._on_ui(lambda: self._set_busy(False))
                self._on_ui(self._paint_blotter)

        threading.Thread(target=job, name="ctp-probe", daemon=True).start()

    def _selected_kind(self) -> str:
        return normalize_ctp_kind(self.kind.get())

    def _selected_self_test(self) -> bool:
        return str(self.kind.get()).strip() == ta.SELF_TEST_LABEL

    def _selected_mode(self) -> str:
        return "self_test" if self._selected_self_test() else self._selected_kind()

    @staticmethod
    def _settings_mode(trader: CtpTrader) -> str:
        if trader.settings.is_self_test:
            return "self_test"
        return normalize_ctp_kind(trader.settings.account_kind)

    @staticmethod
    def _mode_label(mode: str) -> str:
        return ta.SELF_TEST_LABEL if mode == "self_test" else ctp_kind_label(mode)

    def _trader_label(self, trader: CtpTrader) -> str:
        return self._mode_label(self._settings_mode(trader))

    def _drop_session(self) -> None:
        """换账号时先关掉旧连接，避免两本账混在一个流文件里。"""
        if self.md is not None:
            self.md.close()
            self.md = None
        if self._attached_resident:
            # 常驻那条连接不能关，只放开本窗口的引用。
            self.trader = None
            self._attached_resident = False
            return
        if self.trader is not None:
            self.trader.shutdown()
            self.trader = None

    def _on_kind_change(self) -> None:
        if self._attached_resident:
            self._say("正在看常驻柜台，不能在这里换操作账。要换请先停止常驻。")
            if self.trader is not None:
                self.kind.set(self._trader_label(self.trader))
            return
        mode = self._selected_mode()
        if self.trader is not None and self._settings_mode(self.trader) != mode:
            self._drop_session()
            self.account_line.set("已换操作账，请重新点「连接登录」")
            self._say(f"已改成{self._mode_label(mode)}。换账后必须重新登录。")

    def _ensure(self) -> CtpTrader | None:
        if self._attached_resident and self.trader is not None:
            return self.trader
        mode = self._selected_mode()
        if self.trader is not None:
            if self._settings_mode(self.trader) == mode:
                return self.trader
            self._drop_session()
        cfg = load_config()
        if mode == "self_test":
            from src.account.session import resolve_session

            session = resolve_session(self._root, cfg, account_id=ta.SELF_TEST_ACCOUNT_ID)
            if session is None or session.account_id != ta.SELF_TEST_ACCOUNT_ID:
                self._say("账号页里没有内置的「程序自测仿真柜台」。请重启程序让账号注册表自检补齐。")
                return None
            cfg = prepare_self_test_cfg(cfg)
        else:
            kind = mode
            cfg = prepare_live_ctp_cfg(cfg, kind)
            ctp = (cfg.get("trade") or {}).get("ctp") or {}
            preview = overlay_ctp_profile(ctp, kind, account=cfg.get("account"))
            if kind == "live" and not str(preview.get("td_front") or "").strip():
                self._say("真实账号还缺交易前置。请到配置页在「档案交易前置」里填实盘地址。")
                return None
            if kind == "live" and not str(preview.get("user_id") or "").strip():
                self._say("真实账号还缺资金账号。请到「账号」页填「资金账号」。")
                return None
        self.trader = CtpTrader(cfg)
        return self.trader

    def _need_login(self) -> CtpTrader | None:
        trader = self._ensure()
        if trader is None:
            return None
        if not trader.gateway.session.ready:
            self._say("请先点「连接登录」。")
            return None
        return trader

    def _attach_resident_if_any(self) -> None:
        """常驻已开时，把交易台接到同一条柜台上，立刻画出资金和持仓。"""
        trader = resident_ctp_trader(self.master)
        if trader is None:
            return
        self._attached_resident = True
        self.trader = trader
        self.kind.set(self._trader_label(trader))
        if self._kind_box is not None:
            self._kind_box.configure(state="disabled")
        bot = getattr(getattr(self.master, "worker", None), "bot", None)
        watch = ""
        if bot is not None:
            watch = str(getattr(bot, "default_symbol", "") or "").strip()
            if not watch:
                symbols = list(getattr(bot, "scan_symbols", None) or [])
                watch = str(symbols[0]).strip() if symbols else ""
        if watch:
            self.symbol.set(watch)
        if trader.settings.is_self_test:
            self.title("程序自测交易台（正在看常驻本机柜台；关掉窗口不断线）")
        else:
            self.title("CTP 交易台（正在看常驻柜台；关掉窗口不断线）")
        self._say(
            "已接到常驻正在用的柜台，不再重新登录。"
            "资金、持仓、委托、成交跟这一条连接。"
            "这里点买开卖开也会真下单，请小心。"
            "关掉本窗口不会把常驻踢下线。"
        )
        session = getattr(getattr(trader, "gateway", None), "session", None)
        if session is not None and bool(getattr(session, "ready", False)):
            self._paint_logged_in_header(trader, attached=True)
            self._sync_attached_desk(trader)
            return
        self.account_line.set("常驻正在登录柜台，登录完成后会自动填表。")
        self.after(400, lambda: self._wait_resident_ready(0))

    def _wait_resident_ready(self, tries: int) -> None:
        """常驻还在握手时，隔一会儿再看一次是否已经登录完成。"""
        if not self.winfo_exists() or not self._attached_resident:
            return
        trader = self.trader
        session = getattr(getattr(trader, "gateway", None), "session", None) if trader else None
        if trader is not None and session is not None and bool(getattr(session, "ready", False)):
            self._paint_logged_in_header(trader, attached=True)
            self._sync_attached_desk(trader)
            return
        if tries >= 40:
            self._say("常驻还没登录完成。等顶栏变成「CTP 已连接」后再点「刷新持仓」。")
            return
        self.after(400, lambda: self._wait_resident_ready(tries + 1))

    def _paint_logged_in_header(self, trader: CtpTrader, *, attached: bool) -> None:
        """登录成功或接到常驻后，把账号和限额写到窗口顶上。"""
        prefix = "正在看常驻 · " if attached else ""
        warn = trader.settings.order_count_warn
        lots = trader.settings.max_order_volume
        self_test = trader.settings.is_self_test
        if self_test:
            self.title("程序自测交易台（独立本机仿真柜台；与生产报单链路一致）")
        mode = "程序自测仿真柜台" if self_test else ctp_kind_label(trader.settings.account_kind)
        self.account_line.set(
            f"{prefix}已登录{mode}"
            f" {trader.settings.user_id}  AppID {trader.settings.app_id}"
            f"  前置 {trader.settings.td_front}"
        )
        extra = "关掉窗口不断线。" if attached else "挂单留给你手撤（测撤单用）；常驻挂机仍会超时自动撤。"
        self.limit_line.set(
            f"本机单笔最多 {lots} 手（超过会拦住，不是柜台上限）。"
            f"报单/撤单到 {warn} 笔只提醒、不拦单。"
            f"最小变动价位 {trader.settings.price_tick}。"
            f"{extra}"
        )
        self._paint_blotter()

    def _sync_attached_desk(self, trader: CtpTrader) -> None:
        """接到常驻后查一遍资金、持仓、委托、成交，并订行情。"""
        counted = len(getattr(getattr(trader.gateway, "table", None), "orders", {}) or {})
        trader.gateway.activity.sync_from_broker_count(counted)

        def work() -> None:
            self._load_price_band(trader)
            time.sleep(0.4)
            self._show_positions(trader)
            time.sleep(0.4)
            self._show_orders(trader)
            time.sleep(0.4)
            self._show_trades(trader)
            self._open_market(trader)
            self._on_ui(self._start_quote_pump)
            self._on_ui(lambda: self._paint_logged_in_header(trader, attached=True))

        self._run_in_background("正在同步常驻柜台的资金、持仓和委托。", work)

    def _ensure_counter(self) -> None:
        """自测登录前确认柜台在跑；没有就启动独立进程。"""
        from src.ui.sim_counter import counter_for

        app = getattr(self.master, "master", None)
        owner = app if app is not None and hasattr(app, "_on_close") else self.master
        counter_for(owner).start()

    def _connect(self) -> None:
        if self._attached_resident and self.trader is not None:
            if self.trader.gateway.session.ready:
                self._say("已经在看常驻柜台，不用再登录。正在刷新持仓和委托。")
                self._sync_attached_desk(self.trader)
                return
            self._say("常驻还在登录，请稍等。")
            return
        trader = self._ensure()
        if trader is None:
            return
        worker = getattr(self.master, "worker", None)
        if bool(getattr(worker, "running", False)):
            executor = str(((load_config().get("trade") or {}).get("executor") or "")).lower()
            if capabilities_for(executor).orders_via_broker:
                self._say(
                    "常驻已经在用 CTP 连这个账号。请关掉本窗口再点「打开交易台」，"
                    "会直接看到常驻的持仓，不要再单独登录。"
                )
                return
        self._say(f"准备用{self._mode_label(self._settings_mode(trader))}登录 {trader.settings.user_id}")

        def work() -> None:
            if trader.settings.is_self_test:
                try:
                    self._ensure_counter()
                except Exception as exc:  # noqa: BLE001 - 柜台起不来要显示真实原因
                    self._say(f"本机仿真柜台没起来：{exc}")
                    return
            ok, message = trader.connect()
            self._say(("连接成功：" if ok else "连接失败：") + message)
            if not ok:
                return
            self._on_ui(lambda: self._paint_logged_in_header(trader, attached=False))
            self._say(
                f"账号 {trader.settings.user_id}。本机单笔最多 {trader.settings.max_order_volume} 手。"
                f"到 {trader.settings.order_count_warn} 笔只是提醒，不是只能报那么多次。"
            )
            time.sleep(1.1)
            self._load_price_band(trader)
            time.sleep(1.1)
            self._show_positions(trader)
            time.sleep(1.1)
            self._show_orders(trader)
            time.sleep(1.1)
            self._show_trades(trader)
            self._open_market(trader)
            self._on_ui(self._start_quote_pump)

        self._run_in_background("正在连接柜台，请稍等。窗口不会卡住。", work)

    def _load_price_band(self, trader: CtpTrader, symbol: str = "") -> None:
        symbol = (symbol or self.symbol.get().strip() or "fu2701").strip()
        ok, text = trader.gateway.query_price_band(symbol)
        if not ok:
            self._say(text)
            return
        band = dict(trader.gateway._instrument or {})
        if not str(band.get("symbol") or "").strip():
            band["symbol"] = symbol
        md = self.md
        if md is not None:
            # 柜台快照用来补涨跌停；缺卖一时不要把实时盘口擦掉。
            md.apply_tick(band, replace_empty=False)
            latest_for = getattr(md, "latest_for", None)
            snapshot = latest_for(symbol) if callable(latest_for) else md.latest
            if snapshot:
                self._show_quote_soon(snapshot)
                return
        self._show_quote_soon(band)

    def _start_quote_pump(self) -> None:
        if self._quote_pump_on:
            return
        self._quote_pump_on = True
        self._keep_quotes()

    def _keep_quotes(self) -> None:
        """行情几秒不跳时，再向柜台要一次盘口，避免时间和卖一一直停住。"""
        if not self.winfo_exists():
            return
        trader = self.trader
        md = self.md
        stale = md is None or md.stale_seconds() >= 2.5
        if stale and trader is not None and trader.gateway.session.ready and not self._busy:

            def work() -> None:
                try:
                    self._load_price_band(trader, self.symbol.get())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("补刷盘口失败: {}", exc)

            threading.Thread(target=work, daemon=True, name="ctp-quote").start()
        self.after(2500, self._keep_quotes)

    def _open_market(self, trader: CtpTrader) -> None:
        if trader.settings.is_self_test:
            # 自测只替换最底层柜台通信；盘口也来自该进程，绝不加载真实 CTP 行情客户端。
            self.md = None
            self._say("程序自测盘口来自本机仿真柜台，不连接 CTP 行情前置。")
            self._ensure_top_row()
            self._load_price_band(trader)
            return
        from src.trader.ctp_md import CtpMdClient

        if self.md is None:
            self.md = CtpMdClient(trader.settings)
        ok, message = self.md.start()
        self._say(message)
        if not ok:
            return
        self._follow_symbols(trader)

    def _resub(self) -> None:
        trader = self._need_login()
        if trader is None:
            return
        self._ensure_top_row()
        self._follow_symbols(trader)
        self._load_price_band(trader)

    def _watch_symbols(self) -> list[str]:
        """盘口里填了代码的行 + 上方当前下单合约。单合约只有一行。"""
        symbols: list[str] = []
        seen: set[str] = set()
        for row in self._quote_rows:
            text = row.symbol.get().strip()
            key = symbol_key(text)
            if text and key and key not in seen:
                seen.add(key)
                symbols.append(text)
        top = self.symbol.get().strip()
        if top and symbol_key(top) not in seen:
            symbols.append(top)
        return symbols

    def _ensure_top_row(self) -> bool:
        """当前下单合约不在盘口里时，放进第一个空行，保证看得到报价。"""
        symbol_var = getattr(self, "symbol", None)
        rows = getattr(self, "_quote_rows", [])
        if symbol_var is None or not rows:
            return False
        top = symbol_var.get().strip()
        if not top:
            return False
        key = symbol_key(top)
        for row in rows:
            if key and symbol_key(row.symbol.get()) == key:
                return True
        for row in rows:
            if not row.symbol.get().strip():
                row.symbol.set(top)
                return True
        self._say(f"5 行盘口都填了合约，{top} 仍会订阅，但没有单独一行；先清空一行再加。")
        return False

    def _follow_symbols(self, trader: CtpTrader) -> None:
        """按盘口清单一次订阅。单合约就是清单里只有一个。"""
        self._ensure_top_row()
        self._follow_symbols_list(trader, self._watch_symbols())

    def _follow_symbols_list(self, trader: CtpTrader, symbols: list[str]) -> None:
        """订阅已经取好的合约清单，可在后台线程调用，不碰 Tk 变量。"""
        if self.md is None or not symbols:
            return
        key = tuple(symbol_key(symbol) for symbol in symbols)
        if key == self._followed_symbols:
            return
        ok, message = self.md.subscribe_many(symbols)
        if ok:
            self._followed_symbols = key
        self._say(message if ok else ("订阅失败：" + message))

    def _note_pick(self, signature: str) -> bool:
        """同一行只提示一次。表格刷新会再次选中这一行，不能每次都写日志。"""
        if signature == self._last_pick:
            return False
        self._last_pick = signature
        self._last_pick_at = time.time()
        return True

    def _fill_symbol_box(self, code: str, lots: int | None = None) -> None:
        """把点中的合约填进上方，手数有就一起填。"""
        name = str(code or "").strip()
        if not name:
            return
        self.symbol.set(name)
        if lots is not None and int(lots) > 0:
            self.volume.set(str(int(lots)))
        trader = self.trader
        if trader is not None and trader.gateway.session.ready:
            self._follow_symbols(trader)

    def _quote_cell(
        self,
        parent: ttk.Frame,
        row: int,
        col: int,
        var: tk.StringVar,
        fill: str | None,
        index: int,
    ) -> None:
        """
        盘口一格。买一、卖一、最新整块都能点，把这一行的合约和价格填进上面。
        原理：ttk 标签可点面积太小，这里用普通标签加大热区，手形光标表示能点。
        """
        box = tk.Frame(parent, cursor="hand2" if fill else "")
        box.grid(row=row, column=col, padx=8, pady=2)
        item = tk.Label(
            box,
            textvariable=var,
            font=("Segoe UI", 10, "bold"),
            fg="#0b57d0" if fill else None,
            width=8,
            anchor="e",
        )
        item.pack()
        if fill:
            item.configure(cursor="hand2")
            item.bind("<Button-1>", lambda _e, kind=fill, idx=index: self._use_quote(kind, idx))
            box.bind("<Button-1>", lambda _e, kind=fill, idx=index: self._use_quote(kind, idx))

    def _use_quote(self, which: str, index: int) -> None:
        if not 0 <= index < len(self._quote_rows):
            return
        row = self._quote_rows[index]
        symbol = row.symbol.get().strip()
        mapping = {
            "bid": row.bid,
            "ask": row.ask,
            "last": row.last,
            "upper": row.upper,
            "lower": row.lower,
        }
        names = {"bid": "买一", "ask": "卖一", "last": "最新", "upper": "涨停", "lower": "跌停"}
        var = mapping.get(which)
        text = var.get() if var is not None else ""
        if text and text != "—":
            if symbol:
                self.symbol.set(symbol)
            self.price.set(text)
            self.price_mode.set("手动")
            self._say(f"已切到 {symbol or '当前合约'}，并把{names.get(which, which)} {text} 填进价格框。")
            return
        missing = {
            "ask": "这一行现在没有卖一。盘口没人挂卖单。卖开请点买一；买开才用卖一。",
            "bid": "这一行现在没有买一。盘口没人挂买单。买开请点卖一。",
            "upper": "这一行现在没有涨停价。请先订阅行情。",
            "lower": "这一行现在没有跌停价。请先订阅行情。",
        }
        self._say(missing.get(which, "这一行现在没有最新价。请先订阅行情。"))

    def _show_quote_soon(self, row: dict) -> None:
        copied = dict(row)
        self._on_ui(lambda: self._show_quote(copied))

    def _refresh_quote(self) -> None:
        md = self.md
        if md is None:
            return
        latest_for = getattr(md, "latest_for", None)
        for row in self._quote_rows:
            symbol = row.symbol.get().strip()
            if not symbol:
                continue
            quote = latest_for(symbol) if callable(latest_for) else getattr(md, "latest", None)
            if quote:
                self._show_quote(quote)

    def _quote_row(self, symbol: str) -> _QuoteRow | None:
        key = symbol_key(symbol)
        if not key:
            return None
        for row in self._quote_rows:
            if symbol_key(row.symbol.get()) == key:
                return row
        return None

    def _clear_quote_row(self, row: _QuoteRow) -> None:
        row.signature = None
        row.last.set("—")
        row.bid.set("—")
        row.bid_vol.set("0")
        row.ask.set("—")
        row.ask_vol.set("0")
        row.upper.set("—")
        row.lower.set("—")
        row.quote_time.set("—")

    def _watch_symbol_edit(self, row: _QuoteRow) -> None:
        """改了合约代码就清掉旧报价，避免把上一个合约的价格填进下单框。"""
        if row.signature and symbol_key(row.signature[0]) != symbol_key(row.symbol.get()):
            self._clear_quote_row(row)

    def _show_quote(self, row: dict) -> None:
        if not self.winfo_exists():
            return
        from src.trader.ctp_md import describe_quote

        view = describe_quote(row)
        target = self._quote_row(str(view.get("symbol") or ""))
        if target is None:
            return
        signature = (
            str(view.get("symbol") or ""),
            str(view.get("last_text") or "—"),
            str(view.get("bid_text") or "—"),
            str(view.get("ask_text") or "—"),
            str(view.get("upper_text") or "—"),
            str(view.get("lower_text") or "—"),
            str(view.get("time_text") or "—"),
            str(view.get("bid_vol_text") or "0"),
            str(view.get("ask_vol_text") or "0"),
        )
        if signature == target.signature:
            return
        target.signature = signature
        target.last.set(signature[1])
        target.bid.set(signature[2])
        target.ask.set(signature[3])
        target.upper.set(signature[4])
        target.lower.set(signature[5])
        target.quote_time.set(signature[6])
        target.bid_vol.set(signature[7])
        target.ask_vol.set(signature[8])
        self.local_time.set(time.strftime("%H:%M:%S"))
        if signature[3] == "—":
            self.quote_hint.set("卖一是横线=这一行盘口暂时没人挂卖。卖开请点买一填价（对手价），不要等卖一。")
        else:
            self.quote_hint.set("点蓝色的最新/买一/卖一/涨停/跌停，会把这一行的合约和价格填进上面的框。")
        # 盘口只画在交易台数字上。买一卖一每跳一次都写日志，运行窗口会被刷满。

    def _paint_blotter(self) -> None:
        if not self.winfo_exists() or self.trader is None:
            return
        gateway = self.trader.gateway
        self.money_line.set(account_headline(gateway.account))
        self._paint_fund(gateway)
        self._fill_tree(self.pos_tree, self._pos_rows(gateway), keep=("合约", "买卖"))
        self._fill_tree(self.ord_tree, self._ord_rows(gateway), keep="编号")
        self._fill_tree(self.trd_tree, self._trd_rows(gateway), keep="编号")
        activity = gateway.activity
        orders = int(getattr(activity, "orders", 0) or 0)
        cancels = int(getattr(activity, "cancels", 0) or 0)
        line = f"今日报单 {orders} 笔，撤单 {cancels} 笔（笔数不是手数）"
        note = activity.describe() if hasattr(activity, "describe") else ""
        if "阈值" in note:
            extra = note.split("。", 1)[-1]
            line += "  " + extra
        self.count_line.set(line)

    def _on_desk_tab(self, _event=None) -> None:
        """点开资金页时立刻把已经查到的柜台数字画出来，避免还停在横杠。"""
        if not self.winfo_exists():
            return
        try:
            title = str(self._desk_book.tab(self._desk_book.select(), "text") or "")
        except Exception:  # noqa: BLE001
            return
        if title != "资金":
            return
        if self.trader is None:
            return
        self._paint_fund(self.trader.gateway)

    def _paint_fund(self, gateway) -> None:
        account = getattr(gateway, "account", None) if gateway is not None else None
        for title, value in account_pairs(account):
            var = self._fund_values.get(title)
            if var is not None:
                var.set(value)

    def _paint_contracts(self) -> None:
        if not self.winfo_exists() or self.trader is None:
            return
        rows = [format_contract_row(item) for item in self.trader.gateway.contracts]
        self._fill_tree(self.ins_tree, rows, keep="交易代码")

    def _fill_tree(
        self,
        tree: ttk.Treeview,
        rows: list[tuple[str, ...]],
        keep: str | tuple[str, ...] = "",
    ) -> None:
        """刷新表格时留着点中的行，并且不要把滚动条拽回顶部。"""
        shown = [tuple(str(v) for v in tree.item(iid, "values")) for iid in tree.get_children()]
        wanted = [tuple(str(v) for v in row) for row in rows]
        if shown == wanted:
            return
        keep_cols = (keep,) if isinstance(keep, str) and keep else tuple(keep or ())
        keep_value: tuple[str, ...] = ()
        if keep_cols:
            chosen = tree.selection()
            if chosen:
                keep_value = tuple(self._cell(tree, chosen[0], col) for col in keep_cols)
        y0 = tree.yview()[0]
        self._filling_tree = True
        try:
            tree.delete(*tree.get_children())
            restore = ""
            for row in rows:
                iid = tree.insert("", "end", values=row)
                if keep_cols and keep_value and tuple(self._cell(tree, iid, col) for col in keep_cols) == keep_value:
                    restore = iid
            if restore:
                # 只保留选中，不要 see() 把画面跳到这一行，否则刚翻下去又被拽回来。
                tree.selection_set(restore)
            tree.yview_moveto(y0)
        finally:
            # 选中事件可能排到刷新之后才执行，晚一点再放开，避免误报「已选中」。
            self.after_idle(self._end_fill_tree)

    def _end_fill_tree(self) -> None:
        self._filling_tree = False

    def _bind_pick(self, tree: ttk.Treeview, handler) -> None:
        """单击一行就挑选。刷新表格时不会误触发。"""
        tree.bind("<<TreeviewSelect>>", lambda _e: handler())
        tree.bind("<ButtonRelease-1>", lambda event, t=tree, h=handler: self._pick_from_click(t, h, event))

    def _pick_from_click(self, tree: ttk.Treeview, handler, event) -> None:
        if self._filling_tree:
            return
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)
        handler()

    def _cell(self, tree: ttk.Treeview, iid: str, column: str) -> str:
        names = list(tree["columns"])
        values = tree.item(iid, "values")
        if column not in names:
            return ""
        index = names.index(column)
        if index >= len(values):
            return ""
        return str(values[index] or "")

    def _active_quote_row(self) -> _QuoteRow | None:
        """只认当前下单合约所在的行。行里没有就不自动填价，避免拿错合约。"""
        return self._quote_row(self.symbol.get())

    def _pos_rows(self, gateway) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        items: list[dict[str, str]] = []
        for name in sorted(gateway.positions):
            view = gateway.positions[name]
            quote_row = self._quote_row(name)
            quote_last = None
            quote_upper = None
            quote_lower = None
            if quote_row is not None:
                try:
                    quote_last = float(quote_row.last.get())
                except ValueError:
                    quote_last = None
                try:
                    quote_upper = float(quote_row.upper.get())
                except ValueError:
                    quote_upper = None
                try:
                    quote_lower = float(quote_row.lower.get())
                except ValueError:
                    quote_lower = None
            for item in split_position_rows(
                name,
                view,
                multiplier=self._sizer.multiplier_for(name),
                last=quote_last,
                upper=quote_upper,
                lower=quote_lower,
                balance=float((gateway.account or {}).get("balance") or 0),
            ):
                items.append(item)
                rows.append(
                    (
                        item["symbol"],
                        item["side"],
                        item["lots"],
                        item["today"],
                        item["yesterday"],
                        item["avg"],
                        item["pnl"],
                        item["margin"],
                        item["value"],
                        item["spread"],
                        item["ratio"],
                        item["limit_pnl"],
                        item["share"],
                        item["hedge"],
                    )
                )
        self._fill_footer(self.pos_tree, position_footer(items))
        return rows

    def _ord_rows(self, gateway) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        kind = self.ord_filter.get()
        for order in gateway.table.orders.values():
            if not order_matches_filter(order, kind):
                continue
            left = remaining_volume(order)
            if order.status in {"filled", "cancelled", "rejected"}:
                can_cancel = 0
            else:
                can_cancel = left
            rows.append(
                (
                    str(order.insert_time or ""),
                    symbol_key(order.symbol),
                    STATUS_TEXT.get(order.status, order.status),
                    direction_cn(order.direction),
                    offset_cn(order.offset),
                    f"{order.price:g}" if order.price else "",
                    str(order.volume),
                    str(can_cancel),
                    str(order.filled),
                    str(cancelled_volume(order)),
                    str(order.order_sys_id or "").strip(),
                    hedge_cn(order.hedge),
                    str(order.order_ref).strip(),
                )
            )
        rows.sort(key=_order_row_sort_key, reverse=True)
        return rows

    def _trd_rows(self, gateway) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        for item in gateway.trades:
            close_pnl = float(item.get("close_profit") or 0)
            fee = float(item.get("commission") or 0)
            rows.append(
                (
                    str(item.get("time") or ""),
                    str(item.get("symbol") or ""),
                    direction_cn(str(item.get("direction") or "")),
                    offset_cn(str(item.get("offset") or "")),
                    f"{float(item.get('price') or 0):g}",
                    str(item.get("volume") or ""),
                    money_text(close_pnl) if abs(close_pnl) > 1e-9 else "—",
                    money_text(fee) if abs(fee) > 1e-9 else "—",
                    str(item.get("order_sys_id") or ""),
                    hedge_cn(str(item.get("hedge") or "")),
                    str(item.get("order_ref") or ""),
                )
            )
        rows.sort(key=_order_row_sort_key, reverse=True)
        self._fill_footer(self.trd_tree, trade_footer(rows))
        return rows

    def _build_huatai_tab(self, book: ttk.Notebook) -> None:
        """柜台程序化外接必测勾选。勾完后才能把①标成完成。"""
        frame = ttk.Frame(book)
        book.add(frame, text="柜台必测")
        ttk.Label(
            frame,
            text="除「重复报单」「批量撤单」外都必须勾上。截图要带时间戳或电脑右下角时间。",
            wraplength=860,
        ).pack(anchor="w", padx=8, pady=6)
        ticks = (self._go_live.get("huatai") or {})
        for key, title in HUATAI_REQUIRED:
            var = tk.BooleanVar(value=bool(ticks.get(key)))
            self._huatai_vars[key] = var
            ttk.Checkbutton(frame, text=title, variable=var, command=self._save_huatai).pack(
                anchor="w", padx=12, pady=1
            )
        ttk.Button(frame, text="保存勾选", command=self._save_huatai).pack(anchor="w", padx=8, pady=8)

    def _save_huatai(self) -> None:
        latest = load_state(self._root)
        latest.setdefault("huatai", {})
        for key, var in self._huatai_vars.items():
            latest["huatai"][key] = bool(var.get())
        save_state(self._root, latest)
        self._go_live = latest
        self._say("柜台必测勾选已保存。全部勾完后，请到运行页点「本阶段完成」。")

    def _block_symbol(self) -> None:
        self.symbol.set("XXXX")
        self.price.set(self.price.get() or "1")
        self._say("将用错误合约报单，应被本机拦住，不会发到柜台。")
        self._send("buy_open")

    def _block_tick(self) -> None:
        trader = self._ensure()
        tick = float(trader.settings.price_tick) if trader is not None else 1.0
        raw = self.price.get() or "0"
        try:
            price = float(raw)
        except ValueError:
            price = 0.0
        bad = price + tick / 2 if tick > 0 else price + 0.5
        self.price.set(str(bad))
        self._say("将用错误价位报单，应被本机拦住，不会发到柜台。")
        self._send("buy_open")

    def _block_lots(self) -> None:
        trader = self._ensure()
        limit = int(trader.settings.max_order_volume) if trader is not None else 2
        self.volume.set(str(limit + 1))
        self.price.set(self.price.get() or "1")
        self._say(f"将用 {limit + 1} 手报单，超过本机单笔 {limit} 手，应被拦住。")
        self._send("buy_open")

    def _pause(self) -> None:
        trader = self._ensure()
        if trader is None:
            return
        trader.gateway.trading_paused = True
        worker = getattr(self.master, "worker", None)
        if self._attached_resident and worker is not None:
            worker.set_orders_paused(True)
        self._say("已暂停开新仓，平仓仍可发。")

    def _resume(self) -> None:
        trader = self._ensure()
        if trader is None:
            return
        trader.gateway.trading_paused = False
        worker = getattr(self.master, "worker", None)
        if self._attached_resident and worker is not None:
            worker.set_orders_paused(False)
        self._say("已恢复报单。")

    def _query_positions(self) -> None:
        trader = self._need_login()
        if trader is None:
            return

        def work() -> None:
            self._show_positions(trader)

        self._run_in_background("正在查询持仓和资金。", work)

    def _show_positions(self, trader: CtpTrader) -> None:
        ok, text = trader.fetch_account_snapshot()
        self._say(text if ok else ("查询失败：" + text))

    def _query_orders(self) -> None:
        trader = self._need_login()
        if trader is None:
            return

        def work() -> None:
            self._show_orders(trader)

        self._run_in_background("正在查询委托。", work)

    def _show_orders(self, trader: CtpTrader) -> None:
        ok, text = trader.fetch_working_orders()
        self._say(text if ok else ("查询失败：" + text))

    def _query_trades(self) -> None:
        trader = self._need_login()
        if trader is None:
            return

        def work() -> None:
            self._show_trades(trader)

        self._run_in_background("正在查询成交。", work)

    def _show_trades(self, trader: CtpTrader) -> None:
        ok, text = trader.fetch_trades()
        self._say(text if ok else ("查询失败：" + text))

    def _buy_open(self) -> None:
        self._send("buy_open")

    def _sell_open(self) -> None:
        self._send("sell_open")

    def _buy_close(self) -> None:
        self._send("close", position_side="short")

    def _sell_close(self) -> None:
        self._send("close", position_side="long")

    def _flatten(self) -> None:
        """平掉当前合约每一边持仓。锁仓时买开、卖开都会发平仓，和策略收盘清仓同一套报单。"""
        trader = self._need_login()
        if trader is None:
            return
        name = symbol_key(self.symbol.get())
        view = trader.gateway.positions.get(name)
        steps = flatten_order_steps(view)
        if not steps:
            self._say("这个合约没有持仓，不能点平仓。")
            return
        self._place_chain(steps)

    def _reverse(self) -> None:
        """模拟策略反手：先平旧仓，再开反向。步骤与常驻相同，都进 place_order。"""
        trader = self._need_login()
        if trader is None:
            return
        name = symbol_key(self.symbol.get())
        view = trader.gateway.positions.get(name)
        error, steps = reverse_order_steps(view)
        if error:
            self._say(error)
            return
        self._place_chain(steps)

    def _fill_auto_price(self, side: str, position_side: str | None) -> bool:
        mode = self.price_mode.get()
        if mode == "手动":
            return True
        row = self._active_quote_row()
        if row is None:
            self._say("还没有盘口行，请先订阅行情。")
            return False
        if mode == "最新价":
            text = row.last.get()
        elif side == "buy_open" or (side == "close" and position_side == "short"):
            text = row.ask.get()
        else:
            text = row.bid.get()
        if not text or text == "—":
            self._say("还没有报价，请先订阅行情，或改回手动填价格。")
            return False
        self.price.set(text)
        return True

    def _pick_position(self) -> None:
        if self._filling_tree:
            return
        chosen = self.pos_tree.selection()
        if not chosen:
            return
        code = self._cell(self.pos_tree, chosen[0], "合约")
        if not code:
            return
        side = self._cell(self.pos_tree, chosen[0], "买卖") or "买"
        try:
            lots = int(self._cell(self.pos_tree, chosen[0], "手数") or 0)
        except ValueError:
            lots = 0
        self._fill_symbol_box(code, lots)
        if self._note_pick(f"pos|{code}|{side}"):
            close_hint = "买平" if side == "卖" else "卖平"
            self._say(f"已选中{side}仓 {code}，手数 {lots or self.volume.get()}。可点「{close_hint}」或「平仓」。")

    def _query_contracts(self) -> None:
        trader = self._need_login()
        if trader is None:
            return
        keyword = contract_search_key(self.contract_q.get(), self.symbol.get())

        def work() -> None:
            ok, text = trader.fetch_contracts(keyword)
            self._say(text if ok else ("查询失败：" + text))
            self._on_ui(self._paint_contracts)

        self._run_in_background(f"正在查询合约 {keyword}。", work)

    def _picked_contract_code(self) -> str:
        """合约页选中的交易代码；没点行就用上方下单合约。"""
        chosen = self.ins_tree.selection()
        if chosen:
            code = self._cell(self.ins_tree, chosen[0], "交易代码")
            if code:
                return str(code).strip()
        return str(self.symbol.get() or "").strip()

    def _contract_row_for(self, code: str) -> dict | None:
        trader = self.trader
        if trader is None or not code:
            return None
        want = symbol_key(code)
        for item in trader.gateway.contracts:
            if symbol_key(str(item.get("symbol") or "")) == want:
                return item
        return None

    def _quote_last_price(self) -> float:
        row = self._active_quote_row()
        text = str(row.last.get() or "").strip() if row is not None else ""
        if not text or text == "—":
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0

    def _sync_contract_to_universe(self) -> None:
        """把当前合约的每手数量、最小变动、保证金、手续费写入品种与合约表。"""
        from tkinter import messagebox

        from src.risk.universe_table import resolve_universe_path, sync_ctp_contract_to_universe
        from src.trader.ctp_display import ctp_row_to_universe_costs
        from src.utils.config_loader import load_config

        trader = self._need_login()
        if trader is None:
            return
        code = self._picked_contract_code()
        if not code:
            self._say("请先查询并点中一行合约。")
            return
        row = self._contract_row_for(code)
        if not row:
            self._say("表格里还没有这一行。请先点「查询」，再点中合约。")
            return
        has_costs = any(
            abs(float(row.get(key) or 0)) > 1e-12
            for key in (
                "long_margin_money",
                "short_margin_money",
                "open_money",
                "open_volume",
                "close_money",
                "close_volume",
                "close_today_money",
                "close_today_volume",
            )
        )
        if not has_costs:
            self._say("这一行还没查到保证金和手续费。请再点一次该行，等下面出现说明后再写入。")
            return
        last = self._quote_last_price()
        fields, note = ctp_row_to_universe_costs(row, last)
        if not fields.get("symbol"):
            self._say(note)
            return
        if not messagebox.askyesno(
            "写入品种表",
            note
            + "\n\n会写入 config/品种与合约.xlsx。"
            "不改「启用」「交易时段」「清仓时刻」。"
            "同品种其它月份的保证金和手续费也会改成一样。"
            "请先关掉正在打开的 Excel。\n\n确定写入？",
        ):
            return
        cfg = load_config()
        path = resolve_universe_path(cfg, root=self._root)
        try:
            sync_ctp_contract_to_universe(path, fields)
        except PermissionError:
            messagebox.showerror("写不进去", "品种表正在被 Excel 打开。请先关掉 Excel 再点一次。")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("写入品种表失败")
            messagebox.showerror("写入失败", str(exc))
            return
        self._say(f"已写入品种表 {path.name}：{note}")
        reload_fn = getattr(self.master, "_reload_margin_table", None)
        if callable(reload_fn):
            try:
                reload_fn(silent=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("写入后重新读取品种表失败: {}", exc)
        messagebox.showinfo(
            "已写入",
            f"{note}\n\n配置页已按新表重读。正在挂机的手数要下次启动常驻才按新保证金算。",
        )

    def _pick_contract(self) -> None:
        if self._filling_tree:
            return
        chosen = self.ins_tree.selection()
        if not chosen:
            return
        code = self._cell(self.ins_tree, chosen[0], "交易代码")
        if not code:
            return
        self.contract_q.set(code)
        self._fill_symbol_box(code)
        name = self._cell(self.ins_tree, chosen[0], "合约名称")
        if self._note_pick(f"ins|{code}"):
            self._say(f"已选中合约 {code}{(' ' + name) if name else ''}，已填进上方。")
        trader = self.trader
        if trader is None or not trader.gateway.session.ready:
            return
        if code in self._cost_loaded:
            row = next(
                (item for item in trader.gateway.contracts if symbol_key(str(item.get("symbol") or "")) == symbol_key(code)),
                None,
            )
            if row:
                self.contract_detail.set(describe_contract(row))
            return

        def work() -> None:
            try:
                self._load_price_band(trader)
                time.sleep(1.1)
                ok, row = trader.fetch_contract_costs(code)
                if not ok or not row:
                    self._say(f"已选 {code}，但保证金或手续费这次没查到。")
                    return
                self._cost_loaded.add(code)
                text = describe_contract(row)
                self._say(text)
                self._on_ui(lambda: self.contract_detail.set(text))
                self._on_ui(self._paint_contracts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("点选合约后查资料失败: {}", exc)

        threading.Thread(target=work, daemon=True, name="ctp-contract-pick").start()

    def _pick_order(self) -> None:
        if self._filling_tree:
            return
        chosen = self.ord_tree.selection()
        if not chosen:
            return
        code = self._cell(self.ord_tree, chosen[0], "合约")
        ref = self._cell(self.ord_tree, chosen[0], "编号").strip()
        status = self._cell(self.ord_tree, chosen[0], "状态")
        if code:
            self._fill_symbol_box(code)
        self._picked_order_ref = ref
        if not ref or not self._note_pick(f"ord|{ref}"):
            return
        if status in {"撤单", "全部成交", "拒单"}:
            self._say(f"已选中委托 {ref}（{status}），这一笔不能再撤。")
            return
        self._say(f"已选中委托 {ref} {code}。点「撤选中委托」或双击这一行即可撤单。")

    def _send(self, side: str, position_side: str | None = None) -> None:
        self._place_chain([{"side": side, "position_side": position_side}])

    def _current_order_limit(self, trader: CtpTrader) -> int:
        """配置页可能在登录后改过；按当前配置再压一次，避免继续用旧上限。"""
        try:
            # 只读账号和柜台字段；不重复打开品种 Excel。
            cfg = load_config(apply_universe=False)
            if trader.settings.is_self_test:
                # 自测账的账号不在配置页里，必须按内置账号重新物化一次，
                # 否则读到的只是配置页上的原始上限，和登录时用的账对不上。
                from src.account.session import resolve_session

                resolve_session(self._root, cfg, account_id=ta.SELF_TEST_ACCOUNT_ID)
                cfg = prepare_self_test_cfg(cfg)
            else:
                cfg = prepare_live_ctp_cfg(
                    cfg, normalize_ctp_kind(trader.settings.account_kind)
                )
            fresh = CtpSettings.from_cfg(cfg)
        except Exception:  # noqa: BLE001 - 读不到配置就沿用登录时的上限
            return int(trader.settings.max_order_volume)
        if fresh.account_id != trader.settings.account_id:
            return int(trader.settings.max_order_volume)
        return int(fresh.max_order_volume)

    def _place_chain(self, steps: list[dict]) -> None:
        """
        按顺序发几笔限价单。反手是两笔：先平、再开。

        每一笔都走 CtpTrader.place_order，和常驻策略相同。前一笔失败就停下，避免平仓失败又开反向。
        """
        trader = self._ensure()
        if trader is None:
            return
        self._ensure_top_row()
        followed = self._watch_symbols()

        def work() -> None:
            self._follow_symbols_list(trader, followed)
            labels = {"buy_open": "买开", "sell_open": "卖开", "close": "平仓"}
            for index, step in enumerate(steps):
                side = str(step.get("side") or "")
                position_side = step.get("position_side")
                if not self._fill_auto_price(side, position_side):
                    return
                try:
                    price = float(self.price.get())
                    volume = int(step["volume"]) if step.get("volume") is not None else int(self.volume.get())
                except (TypeError, ValueError, KeyError):
                    self._say("价格和手数要填数字。点买一或卖一可以把价格填进来。")
                    return
                request = ctp_order(
                    self.symbol.get().strip(),
                    side,  # type: ignore[arg-type]
                    volume,
                    price,
                    position_side=position_side,
                    remark="交易台手工",
                )
                result = trader.place_order(
                    request,
                    max_volume=self._current_order_limit(trader),
                )
                label = labels.get(side, side)
                self._say(f"{label}：{result.message}")
                note = trader.gateway.activity.describe()
                if "阈值" in note:
                    self._say(note)
                if not result.ok:
                    if index + 1 < len(steps):
                        self._say("这一笔没有成功，后面的反手开仓没有发。")
                    return

        self._run_in_background("正在把这笔单发给柜台。", work)

    def _cancel_selected(self) -> None:
        trader = self._need_login()
        if trader is None:
            return
        chosen = self.ord_tree.selection()
        ref = ""
        if chosen:
            ref = self._cell(self.ord_tree, chosen[0], "编号").strip()
        if not ref:
            ref = str(self._picked_order_ref or "").strip()
        if not ref:
            self._say("请先在委托表点中要撤的那一笔，再点「撤选中委托」，或直接双击那一行。")
            return

        def work() -> None:
            result = trader.cancel_ref(ref)
            self._say(result.message if result.ok else ("撤单失败：" + result.message))
            note = trader.gateway.activity.describe()
            if "阈值" in note:
                self._say(note)

        self._run_in_background(f"正在撤销编号 {ref}。", work)

    def _cancel(self) -> None:
        trader = self._need_login()
        if trader is None:
            return
        symbol = self.symbol.get().strip()

        def work() -> None:
            result = trader.cancel_latest(symbol)
            self._say(result.message)

        self._run_in_background("正在撤销最近一笔。", work)

    def _cancel_all(self) -> None:
        trader = self._need_login()
        if trader is None:
            return

        def work() -> None:
            result = trader.cancel_all_working()
            self._say(result.message)
            note = trader.gateway.activity.describe()
            if "阈值" in note:
                self._say(note)

        self._run_in_background("正在撤销全部未成交委托。", work)

    def _close(self) -> None:
        if self._busy:
            self._say("柜台还在回应，等下面出现结果后再关窗口。")
            return
        self._quote_pump_on = False
        if self.md is not None:
            self.md.close()
            self.md = None
        if self._attached_resident:
            # 只关窗口。常驻继续挂机，不能在这里登出。
            self.trader = None
            self._attached_resident = False
            self.destroy()
            return
        if self.trader is not None:
            self.trader.shutdown()
            self.trader = None
        self.destroy()
