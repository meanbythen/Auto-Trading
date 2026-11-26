"""GUI/CTP 登录、运行窗口检查和柜台启动对账。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from loguru import logger


class LoginFlowMixin:
    """登录流程方法；共享运行状态仍保存在 TradingBot 实例上。"""

    _ctp_retry_at: datetime | None

    def _warn_gui_safety(self) -> None:
        """启动时打印窗口点击路径的关键安全提醒。"""
        if not self.trader_capabilities.window_driven:
            return
        if len(self.scan_symbols) > 1:
            logger.error(
                "GUI 模式会同时扫描多个品种 {}：不会自动切合约，极易点错。"
                "请只保留一个扫描品种。",
                self.scan_symbols,
            )
        gui = (self.cfg.get("trade") or {}).get("gui") or {}
        if not gui.get("symbol_input"):
            logger.warning(
                "未标定 trade.gui.symbol_input：程序不会改合约框，"
                "请确认交易软件盘面合约与策略品种一致。"
            )
        if self.gui_assume_fill and not self.cfg.get("dry_run", True):
            logger.warning(
                "gui.assume_fill=true：点击成功后会立刻改内部持仓"
                "（点击≠交易所成交）。建议定期核对交易软件真实持仓；"
                "也可改为 assume_fill=false，只记发单不自动改仓。"
            )

    def do_login(self) -> None:
        """登录。只打开软件和登录框，不点买开、卖开、平仓。点菜单前会先看下单窗口。"""
        logger.info("=== 登录前先看下单窗口 ===")
        ok = self.login_mgr.login()
        self.logged_in = ok
        self._record_event("login", ok=ok)
        # 同一条线程里，登录返回之后才会进入下一轮扫描，所以登录过程中不会同时去点下单。
        self._arm_order_price()

    def _arm_order_price(self) -> None:
        """
        启动常驻后把交易软件价格设成对手价。

        模拟模式只看信号、不点鼠标，所以这里也不去点价格框。
        设失败只写警告，扫描照常开始。后面的开仓和平仓都不会再改价格。
        """
        if not self.trader_capabilities.window_driven:
            self._gui_price_ready = True
            return
        if self.cfg.get("dry_run", True):
            self._gui_price_ready = True
            logger.info("模拟模式不点击交易软件。真下单前请确认价格框是「对手价」。")
            return
        try:
            from src.trader.price_mode import ensure_opponent_price

            ok, msg = ensure_opponent_price(self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("设置对手价失败")
            ok, msg = False, str(exc)
        self._gui_price_ready = bool(ok)
        if ok:
            logger.info(msg)
        else:
            logger.warning("启动时对手价没有设好。出现信号时还会再看一次价格框。{}", msg)

    def _ensure_runtime_ready(self) -> bool:
        """
        运行期健康检查：交易软件窗口是否还在。
        原理：登录成功后若窗口被关掉，不能继续当「已登录」去点鼠标。
        """
        if not self.trader_capabilities.window_driven or self.cfg.get("dry_run", True):
            return True
        if self.login_mgr.window_exists():
            if getattr(self, "_window_lost_alerted", False):
                self._window_lost_alerted = False
                try:
                    from src.ops.alerts import emit_recovery

                    emit_recovery("window_up", "交易软件窗口已恢复")
                except Exception:  # noqa: BLE001
                    pass
            return True
        logger.error("未找到交易软件窗口，本轮跳过扫描。请重新打开并登录后再挂机。")
        self.logged_in = False
        self.last_error = "交易软件窗口丢失"
        self._window_lost_alerted = True
        self._record_event("window_lost")
        try:
            from src.ops.alerts import emit_alert

            emit_alert("window_lost", "交易软件窗口丢失，本轮跳过扫描", level="error", root=getattr(self, "root", None))
        except Exception:  # noqa: BLE001
            pass
        return False

    def _note_own_fill(self, symbol: str, kind: str = "open") -> None:
        """记下本程序刚成交的品种，后面几分钟对账要手下留情。"""
        key = str(symbol or "").strip().lower()
        fills = getattr(self, "_own_fills_at", None)
        if fills is None:
            self._own_fills_at: dict[str, float] = {}
            fills = self._own_fills_at
        fills[key] = time.monotonic()
        kinds = getattr(self, "_own_fill_kinds", None)
        if kinds is None:
            self._own_fill_kinds: dict[str, str] = {}
            kinds = self._own_fill_kinds
        # 开仓：柜台可能比账本快，要自动认领。平仓：柜台可能比账本慢，不能当成未认领再开回来。
        kinds[key] = "close" if str(kind or "").strip().lower() in {"close", "flat", "平仓"} else "open"

    def _own_fill_kind(self, symbol: str) -> str:
        """最近一次本程序成交是开仓还是平仓。没有记录时按开仓，兼容旧测试。"""
        kinds = getattr(self, "_own_fill_kinds", None) or {}
        return str(kinds.get(str(symbol or "").strip().lower()) or "open")

    def _own_fill_too_fresh(self, symbol: str, *, within: float = 90.0) -> bool:
        """这一品种是不是刚被本程序成交过。"""
        stamp = float(
            (getattr(self, "_own_fills_at", {}) or {}).get(str(symbol or "").strip().lower()) or 0
        )
        if stamp <= 0:
            return False
        return (time.monotonic() - stamp) < max(0.0, float(within))

    def _own_fill_context(self, symbol: str) -> tuple[bool, str]:
        """对账用：刚成交过没有、以及是开还是平。缺方法时当没成交过。"""
        fn = getattr(self, "_own_fill_too_fresh", None)
        if not callable(fn):
            return False, "open"
        try:
            fresh = bool(fn(symbol))
        except Exception:  # noqa: BLE001
            return False, "open"
        kind_fn = getattr(self, "_own_fill_kind", None)
        kind = "open"
        if callable(kind_fn):
            try:
                kind = str(kind_fn(symbol) or "open")
            except Exception:  # noqa: BLE001
                kind = "open"
        return fresh, kind

    def _internal_nets(self) -> dict[str, int]:
        """程序自己记的净持仓。多仓为正，空仓为负，用来和柜台对比。"""
        nets: dict[str, int] = {}
        for sym, pos in (getattr(self.strategy, "positions", {}) or {}).items():
            volume = int(getattr(pos, "volume", 0) or 0)
            held = getattr(pos, "side", "flat")
            if held == "long":
                nets[str(sym)] = volume
            elif held == "short":
                nets[str(sym)] = -volume
            else:
                nets[str(sym)] = 0
        return nets

    def _reconcile_ctp(self) -> None:
        """扫描前向柜台对一次账。持仓对不上才拦开仓；资金按期货公司数字改本地。"""
        if not self.trader_capabilities.can_query_positions or not hasattr(self.trader, "reconcile"):
            return
        try:
            report = self._call_reconcile()
            self._apply_reconcile(report, phase="runtime")
        except Exception as exc:  # noqa: BLE001
            logger.exception("柜台对账修复出错: {}", exc)
            self._record_event("reconcile_error", error=str(exc))
            # 热更新缺了认领相关方法时不要熔断开仓；别的对账失败仍拦开仓。
            msg = str(exc)
            skip_halt = "has no attribute" in msg and any(
                name in msg
                for name in ("_own_fill_too_fresh", "_note_own_fill", "_book_external_close")
            )
            if not skip_halt:
                mark = getattr(self.trader, "mark_reconcile_unknown", None)
                if callable(mark):
                    mark(f"对账异常：{exc}")
        try:
            self._apply_broker_equity()
        except Exception as exc:  # noqa: BLE001
            logger.warning("同步期货公司资金失败: {}", exc)

    def _uses_ctp_broker_funds(self) -> bool:
        """当前是不是已经连上 CTP、并且有期货公司资金数字可以跟。"""
        if not self.trader_capabilities.can_query_positions:
            return False
        if getattr(self.trader, "last_broker_snapshot", None):
            return True
        return getattr(self.trader, "last_broker_equity", None) is not None

    def _apply_broker_equity(self) -> None:
        """常驻算手数、风控、配置权益都改成期货公司刚查到的数字。"""
        if not hasattr(self.trader, "gateway"):
            return
        try:
            from src.account.ctp_sync import apply_broker_runtime

            apply_broker_runtime(
                self.root,
                cfg=self.cfg,
                sizer=self.sizer,
                risk=self.risk,
                trader=self.trader,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("按柜台资金改本地账失败，开平仓规则不变: {}", exc)

    _ctp_retry_sec = 60.0

    def _arm_ctp(self) -> bool:
        """
        执行通道是柜台 API 时，启动就完成认证、登录、结算单确认，并按柜台资金改本地账。

        返回是否已经连上柜台。连不上时置 logged_in=False，隔一段时间再重试，
        不能让「启动时连不上」变成整晚都不再登录。
        """
        if not self.trader_capabilities.orders_via_broker:
            return True
        if self.cfg.get("dry_run", True):
            logger.warning("模拟开关开着，CTP 不会连接柜台，也不会发单")
            self.logged_in = True
            return True
        if not hasattr(self.trader, "connect"):
            self.logged_in = True
            return True

        now = datetime.now()
        retry_at = getattr(self, "_ctp_retry_at", None)
        if retry_at is not None and now < retry_at:
            return False

        from src.trader.go_live import strategy_order_block_reason

        blocked_stage = strategy_order_block_reason(self.cfg)
        if blocked_stage:
            # 上线阶段只拦新开仓，平仓和定时清仓必须照常，所以这里照样连柜台。
            logger.warning("{}（只拦新开仓，平仓与清仓照常）", blocked_stage)

        from src.account.ctp_sync import resident_book_guard

        book_block = resident_book_guard(self.root, self.cfg)
        if book_block:
            self.logged_in = False
            self._ctp_retry_at = now + timedelta(seconds=self._ctp_retry_sec)
            logger.error("CTP 常驻未连接：{}", book_block)
            try:
                from src.ops.alerts import emit_alert

                emit_alert("ctp_book_mismatch", book_block, level="error", root=getattr(self, "root", None))
            except Exception:  # noqa: BLE001
                pass
            return False

        ok, message = self.trader.connect()
        if ok:
            self.logged_in = True
            self._ctp_retry_at = None
            logger.info("CTP {}", message)
            try:
                # 连上了就把「CTP 没有连上」这类过期告警撤掉，别让面板一直挂着旧状态。
                from src.ops.alerts import emit_recovery

                emit_recovery("ctp_up", f"CTP 已连上：{message}")
            except Exception:  # noqa: BLE001
                pass
            self._startup_equity_sync()
            return True

        self.logged_in = False
        self._ctp_retry_at = now + timedelta(seconds=self._ctp_retry_sec)
        logger.error(
            "CTP 没有连上：{}。扫描会继续，但开新仓会停，{} 秒后重试登录和对账。",
            message,
            int(self._ctp_retry_sec),
        )
        try:
            from src.ops.alerts import emit_alert

            emit_alert("ctp_down", f"CTP 没有连上：{message}", level="error", root=getattr(self, "root", None))
        except Exception:  # noqa: BLE001
            pass
        return False

    def _startup_equity_sync(self) -> None:
        """连上柜台后先查权益，按期货公司数字改本地账，再进入扫描。"""
        if not hasattr(self.trader, "reconcile"):
            return
        try:
            report = self._call_reconcile()
            self._apply_reconcile(report, phase="startup")
            self._apply_broker_equity()
            ok = bool(getattr(report, "ok", False))
            diffs = "；".join(getattr(report, "diffs", None) or [])
            if ok:
                logger.info("启动权益核对完成：{}", diffs or "持仓一致")
            else:
                logger.warning(
                    "启动权益已按柜台改本地账，但持仓还对不上：{}", diffs or "柜台查询未完成"
                )
            self._startup_reconcile_pending = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动时向柜台核对资金失败: {}", exc)
            mark = getattr(self.trader, "mark_reconcile_unknown", None)
            if callable(mark):
                mark(f"启动对账异常：{exc}")
