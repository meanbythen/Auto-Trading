"""
CTP 执行通道。

功能：给策略用的下单接口。内部完成登录、按合约报单、区分平今和平昨、接收回报、和柜台对账。
原理：策略仍然只说买开、卖开或平仓。这一层把它翻译成柜台认的方向和开平。
沥青、玉米先平昨再平今。其他品种用普通平仓。
和柜台净持仓不一致时，停止开新仓，平仓仍然允许。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from loguru import logger

from src.trader.base import OrderRequest, OrderResult, TraderBase
from src.trader.ctp_gateway import STATUS_TEXT, CtpGateway, CtpSettings
from src.trader.ctp_logic import (
    OFFSET_OPEN,
    CloseSlice,
    PositionView,
    book_differences,
    check_order_instruction,
    close_direction,
    is_terminal,
    open_direction,
    plan_close,
    product_code,
    symbol_key,
)


class CtpTrader(TraderBase):
    """华泰 CTP 交易通道。可以注入 gateway，测试时不连接真实柜台。"""

    def __init__(self, cfg: dict[str, Any], gateway: CtpGateway | None = None) -> None:
        self.cfg = cfg
        # 测试可以塞进一个假的网关。这时用网关自己的连接参数，避免和空配置各算各的。
        self.settings = gateway.settings if gateway is not None else CtpSettings.from_cfg(cfg)
        self.gateway = gateway or CtpGateway(self.settings)
        self._books_match = False
        self._last_query = 0.0
        self._last_note = "还没有和柜台对过账"
        logger.info(
            "CTP 通道已准备。AppID={}，平今平昨品种={}",
            self.settings.app_id,
            ",".join(self.settings.close_split),
        )

    def connect(self) -> tuple[bool, str]:
        """连接交易前置，并完成认证、登录、结算单确认。"""
        if self.cfg.get("dry_run", False):
            return False, "模拟开关开着，不会连接柜台"
        return self.gateway.connect()

    def shutdown(self) -> None:
        """退出时释放动态库。"""
        self.gateway.close()

    def set_fill_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        """全部成交时回调。用来把真实成交写进程序自己的账，并且只写一次。"""
        self.gateway.fill_handler = handler

    def has_working_order(self, symbol: str | None = None) -> bool:
        """这一笔还在柜台里，没有全部成交、拒单或撤单。"""
        for order in self.gateway.table.orders.values():
            if is_terminal(order.status):
                continue
            if symbol and symbol_key(order.symbol) != symbol_key(symbol):
                continue
            return True
        return False

    def blocks_new_opens(self) -> bool:
        """对账没通过，或上一笔还没完，不允许开新仓。"""
        if self.has_working_order() or self.gateway.unknown_working:
            self._last_note = "有报单还没全部成交，先停止开新仓"
            return True
        return not self._books_match

    def reconcile(self, internal: dict[str, int]) -> tuple[bool, str]:
        """
        查询柜台真实持仓和可用资金，和程序净持仓比较。

        对不上就记下失败。调用方看到失败后停止开新仓。
        查询间隔没到时，用上一次的柜台结果再比一次，避免频繁查询被柜台拒绝。
        """
        if self.cfg.get("dry_run", False):
            self._books_match = False
            self._last_note = "模拟开关开着，没有柜台持仓可对"
            return False, self._last_note
        if not self.gateway.session.ready:
            self._books_match = False
            self._last_note = "还没登录成功，先停止开新仓"
            return False, self._last_note
        now = time.time()
        if now - self._last_query >= self.settings.query_interval_sec:
            ok = self.gateway.refresh()
            self._last_query = now
            if not ok:
                self._books_match = False
                self._last_note = "查询柜台持仓或资金失败，先停止开新仓"
                return False, self._last_note
        notes = book_differences(internal, self.gateway.positions)
        available = self.gateway.available
        money = f"{available:.2f}" if available is not None else "未知"
        if notes:
            self._books_match = False
            self._last_note = "；".join(notes) + f"。可用资金 {money}。停止开新仓"
            logger.warning("柜台对账不一致: {}", self._last_note)
            return False, self._last_note
        self._books_match = True
        self._last_note = f"持仓一致。可用资金 {money}"
        logger.info("柜台对账通过: {}", self._last_note)
        return True, self._last_note

    def place_order(self, req: OrderRequest) -> OrderResult:
        """
        提交报单。

        开仓一笔。平仓在沥青、玉米上按今仓昨仓拆开，可能变成两笔。
        只有柜台确认全部成交才算 filled。点击成功不算成交。
        """
        if self.cfg.get("dry_run", False):
            return OrderResult(ok=False, message="模拟开关开着，不会向 CTP 发单", assumed_fill=False)
        missing = self.settings.missing()
        if missing:
            return OrderResult(ok=False, message="CTP 还缺：" + "、".join(missing), assumed_fill=False)
        if req.price is None or float(req.price) <= 0:
            return OrderResult(ok=False, message="CTP 限价单需要价格", assumed_fill=False)
        blocked = check_order_instruction(
            req.symbol,
            float(req.price),
            int(req.volume),
            price_tick=self.settings.price_tick,
            max_volume=self.settings.max_order_volume,
        )
        if blocked:
            logger.error(blocked)
            return OrderResult(ok=False, message=blocked, assumed_fill=False)
        if self.gateway.trading_paused:
            text = "已暂停报单，本笔没有发到柜台"
            logger.warning(text)
            return OrderResult(ok=False, message=text, assumed_fill=False)
        if not self.gateway.session.ready or self.gateway.session.need_order_check:
            ok, message = self.connect()
            if not ok:
                return OrderResult(ok=False, message=message, assumed_fill=False)

        volume = int(req.volume)
        if volume <= 0:
            return OrderResult(ok=False, message="手数必须大于 0", assumed_fill=False)

        if req.side == "close":
            slices, direction, error = self._close_plan(req)
            if error:
                return OrderResult(ok=False, message=error, assumed_fill=False)
        else:
            # 买开、卖开都是开仓，不拆今昨。
            slices = [CloseSlice(OFFSET_OPEN, volume)]
            direction = open_direction(req.side)

        orders = []
        for piece in slices:
            order = self.gateway.submit(
                symbol=req.symbol,
                direction=direction,
                offset=piece.offset,
                volume=piece.volume,
                price=float(req.price),
                stop_price=req.stop_price,
                bar_id=req.bar_id,
            )
            orders.append(order)
            logger.info(
                "CTP {} {} {} {} 手 @ {} | {}",
                req.symbol,
                _direction_text(direction),
                _offset_text(piece.offset),
                piece.volume,
                req.price,
                _status_text(order),
            )

        filled_volume = sum(int(order.filled) for order in orders)
        accounted_volume = sum(int(order.filled) for order in orders if order.accounted)
        all_filled = filled_volume == volume and all(order.status == "filled" for order in orders)
        texts = [_status_text(order) for order in orders]
        message = "，".join(texts)
        if not all_filled and filled_volume > 0:
            message += f"（共成交 {filled_volume} 手）"
        elif filled_volume <= 0 and any(order.status == "cancelled" for order in orders):
            message += "（未成交）"
        rejected = [order.message for order in orders if order.status == "rejected" and order.message]
        if rejected:
            message = message + "：" + "；".join(rejected)
        avg = _avg_price(orders)
        # 还在柜台排队、或者已经部分成交，都说明这一笔已经发出去过。
        working = any(order.status in {"submitted", "partial"} for order in orders)
        return OrderResult(
            ok=all_filled or working or filled_volume > 0,
            message=message or "没有收到回报",
            order_id=",".join(order.order_ref for order in orders if order.order_ref),
            assumed_fill=False,
            raw={
                "filled": all_filled,
                "accounted": filled_volume > 0 and accounted_volume >= filled_volume,
                "fill_price": avg,
                "filled_volume": filled_volume,
                "accounted_volume": accounted_volume,
                "statuses": texts,
                "working": working,
            },
        )

    def _close_plan(self, req: OrderRequest) -> tuple[list[Any], str, str]:
        """决定平仓方向，以及要不要拆成平今、平昨。返回 (拆分, 方向, 错误)。"""
        side = str(req.position_side or "").lower()
        view = self.gateway.positions.get(symbol_key(req.symbol), PositionView())
        if side not in {"long", "short"}:
            side = "short" if view.net < 0 else "long"
        if side == "short":
            today, yesterday = view.short_today, view.short_yesterday
        else:
            today, yesterday = view.long_today, view.long_yesterday
        product = product_code(req.symbol)
        if product in set(self.settings.close_split):
            if not self.gateway.positions_ready:
                return [], "", "还没查到今仓和昨仓，先不报平仓"
            if today + yesterday <= 0:
                return [], "", f"柜台没有 {req.symbol} 可平的{('空' if side == 'short' else '多')}仓"
        slices = plan_close(
            req.symbol,
            int(req.volume),
            today=today,
            yesterday=yesterday,
            split_products=self.settings.close_split,
        )
        return slices, close_direction(side), ""

    def cancel_latest(self, symbol: str = "") -> OrderResult:
        """撤销该合约最近一笔还没结束的报单。没有编号就先不发。"""
        chosen = None
        for order in self.gateway.table.orders.values():
            if is_terminal(order.status):
                continue
            if symbol and symbol_key(order.symbol) != symbol_key(symbol):
                continue
            chosen = order
        if chosen is None:
            return OrderResult(ok=False, message="没有可以撤销的报单", assumed_fill=False)
        ok, message = self.gateway.cancel(chosen.order_ref)
        return OrderResult(ok=ok, message=message, order_id=chosen.order_ref, assumed_fill=False)


def _direction_text(direction: str) -> str:
    return "买" if direction == "0" else "卖"


def _offset_text(offset: str) -> str:
    return {
        "0": "开仓",
        "1": "平仓",
        "3": "平今",
        "4": "平昨",
    }.get(offset, offset)


def _status_text(order: Any) -> str:
    """报单状态。撤单时如果已经成交过，要把成交手数说清楚。"""
    status = str(getattr(order, "status", "") or "")
    filled = int(getattr(order, "filled", 0) or 0)
    if status == "cancelled" and filled > 0:
        return f"成交{filled}手后撤单"
    return STATUS_TEXT.get(status, status)


def _avg_price(orders: list[Any]) -> float:
    total_qty = 0
    total_money = 0.0
    for order in orders:
        qty = int(getattr(order, "filled", 0) or 0)
        if qty <= 0:
            continue
        total_qty += qty
        total_money += float(getattr(order, "avg_price", 0) or 0) * qty
    if total_qty <= 0:
        return 0.0
    return total_money / total_qty
