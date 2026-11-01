"""
同花顺 GUI 点击交易执行器
功能：通过鼠标点击下单面板上的「买开 / 卖开 / 平仓」按钮完成交易。
原理：配置文件里预先标定按钮坐标 → pyautogui 移动点击。
风险：窗口移动、分辨率变化会导致点错；务必先 dry_run，并用 FAILSAFE（鼠标甩左上角中断）。

确认框处理：
  同花顺下单后常弹出「确定/确认」提示，不点则卡死。
  默认：稍等后按回车；若标定了 confirm_btn 再点一次确定按钮。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pyautogui
from loguru import logger

from .base import OrderRequest, OrderResult, TraderBase

# 真下单的鼠标键盘还没结束时为真。界面这时不要把自己拉到同花顺前面。
_ORDER_CLICKING = False


def order_click_in_progress() -> bool:
    """
    正在点击同花顺下单时返回真。

    运行日志一写进窗口，Windows 就会把本程序重新放到前面，后面的点击就点到自己。
    这段时间日志先攒着，等点击结束再一次性显示。
    """
    return _ORDER_CLICKING


class GuiTrader(TraderBase):
    """用鼠标键盘操作同花顺下单面板。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.dry_run = bool(cfg.get("dry_run", True))
        self._full_cfg = cfg  # 下单前激活同花顺窗口时用
        trade = cfg.get("trade", {})
        self.gui = trade.get("gui", {})
        # confirm_mode: enter=只按回车 | click=只点坐标 | both=先回车再点（默认）
        self.confirm_mode = str(self.gui.get("confirm_mode", "both")).lower()
        # 点完买卖后等多久再处理确认框（秒）
        self.confirm_delay_sec = float(self.gui.get("confirm_delay_sec", 0.6))
        # 确认框可能连弹两次，最多处理几次
        self.confirm_times = int(self.gui.get("confirm_times", 2))
        # 这次下单钉住的同花顺窗口。结束时取消钉住，避免交易窗口一直盖住控制台。
        self._pinned_hwnd = 0
        # 这一笔已经把交易窗口准备好。后面填手数、点按钮时不再反复激活。
        self._order_armed = False
        # 下单时暂时禁止激活的本程序窗口，结束时恢复。
        self._parked_hwnds: list[int] = []
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.2

    def _focus_ths_before_click(self) -> bool:
        """
        真下单前把同花顺放到最前，并在这一笔点击结束前钉住。

        原理：启动常驻时故意不置顶（以免挡住控制台），所以要点击时再激活一次。
        只能在「还没点进输入框」时调用。价格框已经点中、正要打字时再激活，
        光标会离开价格框，新价格就打不进去，框里会变成空的。
        返回 False 表示同花顺没到最前，调用方应停止，不能把字打进程序自己的窗口。
        """
        if self.dry_run:
            return True
        try:
            from src.login import AppLoginManager
            from src.login.app_login import pin_topmost

            cfg = dict(self._full_cfg)
            cfg["dry_run"] = False
            manager = AppLoginManager(cfg)
            ok = bool(manager.focus_window())
            hwnd = int(getattr(manager, "last_hwnd", 0) or 0)
            if ok and hwnd:
                pin_topmost(hwnd)
                self._pinned_hwnd = hwnd
            time.sleep(0.15)
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning("下单前激活同花顺失败: {}", e)
            return False

    def _trade_process_in_front(self) -> bool:
        """
        当前前台是不是同花顺自己的窗口。

        价格菜单也属于同花顺进程，菜单开着时也算在前面。
        还没钉住同花顺时不拦，避免测试里没有真窗口就把下单停掉。
        """
        target = int(getattr(self, "_pinned_hwnd", 0) or 0)
        if not target:
            return True
        import ctypes

        user32 = ctypes.windll.user32
        front = int(user32.GetForegroundWindow() or 0)
        if not front:
            return False
        front_pid = ctypes.c_ulong()
        target_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(front, ctypes.byref(front_pid))
        user32.GetWindowThreadProcessId(target, ctypes.byref(target_pid))
        return bool(front_pid.value) and front_pid.value == target_pid.value

    def _park_own_windows(self) -> None:
        """
        下单这一小段，禁止本程序窗口跑到同花顺前面。

        模拟器一隐藏，Windows 就会改去激活主窗口。运行日志一刷新，主窗口也会自己弹出来。
        这里不隐藏窗口，只取消置顶、沉到后面，并暂时不让它接收激活。
        点完在 _restore_own_windows 里恢复，并且不再主动把主窗口拉回最前。
        """
        if self.dry_run or self._parked_hwnds:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
            found: list[int] = []

            def _visit(hwnd: int, _lparam: int) -> bool:
                try:
                    handle = int(hwnd)
                    owner = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
                    if int(owner.value) != pid or not user32.IsWindowVisible(handle):
                        return True
                    length = int(user32.GetWindowTextLengthW(handle) or 0)
                    if length <= 0:
                        return True
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(handle, buf, length + 1)
                    title = buf.value or ""
                    # 只处理本程序的主窗口和填写窗口，不去动同花顺，也不去动别的软件。
                    if "Auto-Trading" not in title and "下单模拟器" not in title:
                        return True
                    # HWND_NOTOPMOST=-2，再 HWND_BOTTOM=1。NOACTIVATE 避免这一下又把焦点抢回来。
                    user32.SetWindowPos(handle, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                    user32.EnableWindow(handle, False)
                    user32.SetWindowPos(handle, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                    found.append(handle)
                except Exception:  # noqa: BLE001
                    return True
                return True

            callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_visit)
            user32.EnumWindows(callback, 0)
            self._parked_hwnds = found
        except Exception as exc:  # noqa: BLE001
            logger.warning("下单时没能暂时让出本程序窗口: {}", exc)

    def _restore_own_windows(self) -> None:
        """允许本程序窗口再次被点击。不把它拉到同花顺前面。"""
        parked = list(self._parked_hwnds)
        self._parked_hwnds = []
        if not parked:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            for hwnd in parked:
                user32.EnableWindow(int(hwnd), True)
                user32.SetWindowPos(int(hwnd), -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复本程序窗口失败: {}", exc)

    def _unpin_trade_window(self) -> None:
        """这一笔点击结束，取消钉住，同花顺不再一直盖住控制台。"""
        hwnd = int(getattr(self, "_pinned_hwnd", 0) or 0)
        self._pinned_hwnd = 0
        if not hwnd:
            return
        try:
            from src.login.app_login import unpin_topmost

            unpin_topmost(hwnd)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取消同花顺置顶失败: {}", exc)

    def _require_trade_front(self) -> bool:
        """
        下单动作开始前，确认同花顺交易窗口在最前。

        不在最前就停，不能一边抢窗口一边填价格或手数。
        已经点进输入框、正要打字时不要再调用，否则光标会离开输入框。
        """
        if self.dry_run:
            return True
        focused = self._focus_ths_before_click()
        if focused is False or not self._trade_process_in_front():
            logger.error("同花顺交易窗口不在最前，这次停止，避免点到别的窗口。")
            return False
        # 主窗口和交易窗口常常是同一个程序。只看进程会把主界面当成已经就绪，接着按键就把主界面切走。
        if self._pinned_hwnd and not self._foreground_is_trade_panel():
            logger.error("前台是同花顺主界面，不是交易窗口，这次停止。")
            return False
        return True

    def _bring_tonghuashun_front(self, manager: Any) -> bool:
        """
        下单开始时只把同花顺拉到最前一次。

        有交易窗口就拉交易窗口。还没有时，拉主窗口，好让下一步去点登录。
        不再连试三次，也不要在已经是这个窗口时再激活，避免界面被切来切去。
        """
        from src.login.app_login import force_foreground
        from src.login.ths_login import looks_like_order_window

        windows = list(manager.find_windows() or [])
        trade = [win for win in windows if looks_like_order_window(getattr(win, "title", "") or "")]
        chosen = trade[0] if trade else (windows[0] if windows else None)
        if chosen is None:
            return False
        hwnd = int(getattr(chosen, "_hWnd", 0) or 0)
        manager.last_hwnd = hwnd
        if not hwnd:
            return False
        import ctypes

        front = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        title = getattr(chosen, "title", "") or ""
        if front != hwnd:
            force_foreground(hwnd)
            time.sleep(0.2)
            logger.info("已把同花顺放到最前: {}", title)
        return True

    def _pin_front_window(self, manager: Any) -> None:
        """交易窗口确认还在以后，钉住刚才拉到最前的那一个，避免本程序再盖上来。"""
        from src.login.app_login import pin_topmost

        hwnd = int(getattr(manager, "last_hwnd", 0) or 0)
        if not hwnd:
            return
        pin_topmost(hwnd)
        self._pinned_hwnd = hwnd

    def _ensure_trade_desk(self) -> bool:
        """
        下单前把同花顺交易窗口准备好。每一步只做一次。

        先拉到最前。交易窗口已经开着，就直接下单，不再点登录。
        没有交易窗口时，登录一次。登录只点登录，不点买多、卖空、平仓。
        登录后仍然没有交易窗口，这次不下单。
        """
        import os

        if self.dry_run or os.environ.get("PYTEST_CURRENT_TEST"):
            return self._require_trade_front()
        import copy

        from src.login import AppLoginManager
        from src.login.ths_login import confirm_settlement_if_present, login_trade_account, order_probe_points, order_window_is_open

        desk = copy.deepcopy(self._full_cfg)
        desk["dry_run"] = False
        account = desk.setdefault("account", {})
        manager = AppLoginManager(desk)
        points = order_probe_points(self.gui)

        if not manager.window_exists():
            logger.info("同花顺还没打开，先启动，再看交易窗口。")
            if not manager.start_app():
                logger.error("没能打开同花顺，这次不下单。")
                return False
        if not self._bring_tonghuashun_front(manager):
            logger.error("没能把同花顺放到最前，这次不下单。")
            return False
        # 结算单挡着时，先点掉那一个按钮。没有这个窗口就不会点任何地方。
        if not confirm_settlement_if_present(wait_sec=0):
            logger.error("「确认结算单」还挡着，这次不下单。")
            return False
        if self._trade_panel_open(order_window_is_open, points):
            logger.info("交易窗口已在，拉到最前后直接下单。")
            self._pin_front_window(manager)
            return True

        logger.info("没有交易窗口，开始自动登录。")
        password = str(account.get("resolved_password", "") or "")
        user_id = str(account.get("user_id", "") or "")
        if user_id in ("你的资金账号", "your_account", "账号"):
            user_id = ""
        try:
            # 上面已经看过一次交易窗口。这里不再看第二遍，避免重复点菜单把窗口关掉。
            ok, message = login_trade_account(
                password,
                user_id=user_id,
                captcha_pause=str(account.get("captcha_mode", "pause")) == "pause",
                probe_points=points,
                window_open=lambda: False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("自动登录失败")
            logger.error("自动登录没有完成：{}。这次不下单。", exc)
            return False
        if not ok:
            logger.error("自动登录没有成功：{}。这次不下单。", message)
            return False
        logger.info(message)
        self._bring_tonghuashun_front(manager)
        if not self._trade_panel_open(order_window_is_open, points):
            logger.error("自动登录后仍然没有交易窗口，这次不下单。")
            return False
        logger.info("交易窗口已登录，开始下单。")
        self._pin_front_window(manager)
        return True

    def _trade_panel_open(self, checker: Any, points: list[tuple[int, int]]) -> bool:
        """看交易下单窗口在不在。只看，不点买卖。"""
        try:
            return bool(checker(points))
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查交易窗口失败: {}", exc)
            return False

    def _click(self, key: str) -> None:
        """
        点击配置中的某个坐标点。

        点击前先确认同花顺交易窗口在最前。否则坐标会落在本程序窗口上。
        """
        point = self.gui.get(key)
        if not point or len(point) < 2:
            raise ValueError(f"缺少 GUI 坐标配置: trade.gui.{key}")
        x, y = int(point[0]), int(point[1])
        if self.dry_run:
            logger.info("[模拟点击] {} -> ({}, {})", key, x, y)
            return
        # 交易窗口在这一笔开始时已经拉到最前。填手数和点买卖时不再激活，避免把行情窗口或本程序叫回来。
        if not self._order_armed and self._require_trade_front() is False:
            raise RuntimeError("同花顺交易窗口不在最前，已停止点击，避免点到程序自己的窗口。")
        pyautogui.click(x=x, y=y)
        time.sleep(0.2)

    def _type_into(self, key: str, text: str) -> None:
        """
        填写价格或手数。

        先确认同花顺在最前，再点输入框。点下去会弹出下拉框，所以先按一次 Esc 收回，
        再全选并输入。收回之后不能再激活窗口，否则字会打进别的地方。
        这里不按删除，也不按退格。
        """
        self._click(key)
        if self.dry_run:
            logger.info("[模拟输入] {} = {}", key, text)
            return
        self._close_volume_pad()
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite(str(text), interval=0.03)

    def _read_price_text(self) -> str | None:
        """读价格框。测试可以换成假读数，避免去点真的同花顺。"""
        reader = getattr(self, "_price_reader", None)
        if reader is not None:
            return reader()
        if self.dry_run:
            return None
        from src.trader.price_mode import read_price_text

        # 这一笔已经把窗口拉到最前，读价格时不再激活。
        return read_price_text(self._full_cfg, focus=not self._order_armed)

    def _foreground_is_trade_panel(self) -> bool:
        """
        当前前台是不是交易下单窗口。

        行情主窗口「同花顺期货通」不算。对它按 Esc，会把主界面切走。
        还没钉住窗口时不拦，测试里没有真的交易窗口。
        """
        target = int(getattr(self, "_pinned_hwnd", 0) or 0)
        if not target:
            return True
        import ctypes

        from src.login.ths_login import looks_like_order_window

        user32 = ctypes.windll.user32
        front = int(user32.GetForegroundWindow() or 0)
        if not front:
            return False
        if front == target:
            return True
        length = int(user32.GetWindowTextLengthW(front) or 0)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(front, buf, length + 1)
            title = buf.value or ""
        if looks_like_order_window(title):
            return True
        # 价格下拉是交易窗口弹出的，标题可能是空的，但根窗口仍是交易窗口。
        root = int(user32.GetAncestor(front, 2) or 0)
        return root == target

    def _price_control_text(self, point: list[int] | tuple[int, int]) -> tuple[Any, str]:
        """读价格坐标上的控件和它现在显示的字。读的时候不再去激活窗口。"""
        import uiautomation as auto

        from src.trader.price_mode import _visible_text

        auto.SetGlobalSearchTimeout(0.4)
        ctrl = auto.ControlFromPoint(int(point[0]), int(point[1]))
        if ctrl is None:
            return None, ""
        return ctrl, _visible_text(ctrl)

    def _replace_focused_edit(self, text: str) -> bool:
        """
        把交易窗口里当前输入框的整段文字换成新价格。

        消息只发给这个输入框。不按删除，也不按退格，避免打到自选列表上。
        焦点如果不在交易窗口的价格框里，就不发，免得改到主窗口标题。
        """
        import ctypes

        from src.trader.price_mode import _PRICE_WORDS, _looks_like_price_number

        if not self._foreground_is_trade_panel():
            return False
        user32 = ctypes.windll.user32

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class _GuiThreadInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("flags", ctypes.c_ulong),
                ("hwndActive", ctypes.c_void_p),
                ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p),
                ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p),
                ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", _Rect),
            ]

        front = int(user32.GetForegroundWindow() or 0)
        if not front:
            return False
        process_id = ctypes.c_ulong()
        thread_id = int(user32.GetWindowThreadProcessId(front, ctypes.byref(process_id)) or 0)
        info = _GuiThreadInfo()
        info.cbSize = ctypes.sizeof(_GuiThreadInfo)
        if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return False
        hwnd = int(info.hwndFocus or 0)
        pinned = int(getattr(self, "_pinned_hwnd", 0) or 0)
        if not hwnd or hwnd == front or (pinned and hwnd == pinned):
            return False
        root = int(user32.GetAncestor(hwnd, 2) or 0)
        if pinned and root not in {pinned, front}:
            return False
        buf = ctypes.create_unicode_buffer(64)
        user32.SendMessageW(hwnd, 0x000D, 64, buf)
        current = str(buf.value or "").strip()
        if current and current not in _PRICE_WORDS and not _looks_like_price_number(current):
            return False
        written = ctypes.c_wchar_p(str(text))
        # WM_SETTEXT 整段替换。再全选并替换一次，给普通输入框和组合框各一条路。
        user32.SendMessageW(hwnd, 0x000C, 0, written)
        user32.SendMessageW(hwnd, 0x00B1, 0, ctypes.c_long(-1))
        user32.SendMessageW(hwnd, 0x00C2, 1, written)
        return True

    def _write_price_into_box(self, text: str) -> bool:
        """
        把新价格整段写进价格框，不往旧数字中间插。

        先直接改价格框，不点它，这样不会打开价格菜单，也不会去切行情主界面。
        直接改不成时，只在交易窗口已经在最前的情况下点一下价格框、收回菜单，
        再把这个输入框里的字整段换掉。不按删除，也不按退格。
        写完读回来必须和要填的价格一致，否则这次不点买卖。
        """
        from src.trader.price_mode import (
            _PRICE_WORDS,
            _looks_like_price_number,
            price_text_matches,
        )

        point = self.gui.get("price_input")
        if not point or len(point) < 2:
            return False
        try:
            ctrl, shown = self._price_control_text(point)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取价格框失败: {}", exc)
            ctrl, shown = None, ""
        if shown and shown not in _PRICE_WORDS and not _looks_like_price_number(shown):
            logger.error("价格坐标上不是价格框（{}），这次不写入。", shown)
            return False
        if ctrl is not None:
            try:
                pattern = ctrl.GetValuePattern()
                if pattern is not None and not pattern.IsReadOnly:
                    pattern.SetValue(str(text), waitTime=0.05)
            except Exception:  # noqa: BLE001
                pass
            try:
                legacy = ctrl.GetLegacyIAccessiblePattern()
                if legacy is not None:
                    legacy.SetValue(str(text), waitTime=0.05)
            except Exception:  # noqa: BLE001
                pass
        try:
            _ctrl, shown_after = self._price_control_text(point)
        except Exception:  # noqa: BLE001
            shown_after = ""
        if price_text_matches(shown_after, text):
            return True
        # 直接改没有生效。只有交易窗口在最前时，才点价格框并整段替换。
        # 主界面在前时不点、不按 Esc，避免把行情页切走。
        if not self._foreground_is_trade_panel():
            logger.error("交易窗口不在最前，价格没有整段替换，这次不把数字打进旧价格。")
            return False
        x, y = int(point[0]), int(point[1])
        pyautogui.click(x=x, y=y)
        time.sleep(0.12)
        if self._foreground_is_trade_panel():
            pyautogui.press("esc")
            time.sleep(0.12)
        self._replace_focused_edit(text)
        try:
            _ctrl, shown_after = self._price_control_text(point)
        except Exception:  # noqa: BLE001
            shown_after = ""
        if price_text_matches(shown_after, text):
            return True
        logger.error("价格框现在是「{}」，不是要填的 {}。不再把数字打进旧价格中间。", shown_after or "空", text)
        return False

    def _replace_price_text(self, text: str) -> bool:
        """
        用新价格替换价格框里的整段旧数字。

        真下单时直接改价格框，避免菜单开着时全选失效、数字插进旧价格中间。
        也不再因此去激活行情主窗口。不按删除，也不按退格。
        """
        import os

        if not self.gui.get("price_input"):
            logger.error("没有价格框坐标，不能填写价格")
            return False
        if self.dry_run or os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                self._type_into("price_input", text)
            except RuntimeError:
                logger.error("填写价格时同花顺交易窗口不在最前，已停止。")
                return False
            return True
        return self._write_price_into_box(text)

    def _fill_volume(self, text: str) -> bool:
        """
        填写手数。

        真下单时先直接改手数框，不点开下面的快捷面板。
        改不上，并且交易窗口还在最前，才退回点一下再输入。
        返回 True 表示用了键盘，调用方还要按一次 Esc，免得面板挡住卖空。
        """
        import os

        if self.dry_run or os.environ.get("PYTEST_CURRENT_TEST"):
            self._type_into("volume_input", text)
            return True
        point = self.gui.get("volume_input")
        if point and self._write_number_into_box(point, text):
            return False
        if not self._foreground_is_trade_panel():
            raise RuntimeError("交易窗口不在最前，手数没有写上，这次不点买卖。")
        self._type_into("volume_input", text)
        return True

    def _write_number_into_box(self, point: list[int] | tuple[int, int], text: str) -> bool:
        """把数字整段写进指定坐标上的输入框。只接受本来就是数字或空白的框。"""
        from src.trader.price_mode import price_text_matches

        try:
            ctrl, shown = self._price_control_text(point)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取输入框失败: {}", exc)
            return False
        if ctrl is None:
            return False
        digits = "".join(ch for ch in str(shown or "") if ch.isdigit())
        if shown and not digits:
            logger.error("这个坐标上不是数字框（{}），这次不直接写入。", shown)
            return False
        try:
            pattern = ctrl.GetValuePattern()
            if pattern is not None and not pattern.IsReadOnly:
                pattern.SetValue(str(text), waitTime=0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            legacy = ctrl.GetLegacyIAccessiblePattern()
            if legacy is not None:
                legacy.SetValue(str(text), waitTime=0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            _ctrl, shown_after = self._price_control_text(point)
        except Exception:  # noqa: BLE001
            return False
        return price_text_matches(shown_after, text)

    def _write_limit_price(self, text: str) -> str:
        """
        把下单模拟器填的限价打进价格框。

        启动常驻不会调用这里。写完再读一次，整框必须等于要填的价格，
        多出来的旧数字（例如 44700700）对不上就不点买卖。
        """
        if not self.gui.get("price_input"):
            logger.error("没有价格框坐标，不能按模拟器的价格下单")
            return ""
        if not self._replace_price_text(text):
            return ""
        if self.dry_run:
            logger.info("[模拟] 价格框写入限价 {}", text)
            return text
        from src.trader.price_mode import price_text_matches

        shown = self._read_price_text()
        if not price_text_matches(shown, text):
            logger.error("价格框现在是「{}」，不是要填的 {}", shown or "空", text)
            return ""
        logger.info("下单模拟器已把价格框写成 {}", shown)
        return str(shown)

    def _prepare_opponent_price(self) -> str | None:
        """
        下单前确认价格框是对手价。

        启动时如果把「最新价」误认成已经设好，这里再改一次。
        改完仍不是对手价，调用方这次不点买卖。
        """
        shown = self._read_price_text()
        if self.dry_run:
            return shown
        from src.trader.price_mode import price_mode_blocks_order

        if not price_mode_blocks_order(shown):
            return shown
        logger.warning("下单前价格框是「{}」，先改成对手价", shown)
        try:
            from src.trader.price_mode import ensure_opponent_price

            # 窗口已经在最前，这里只点一次价格菜单，不再把同花顺拉起来第二遍。
            _ok, message = ensure_opponent_price(self._full_cfg, focus=not self._order_armed)
            logger.info(message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下单前改对手价失败: {}", exc)
        return self._read_price_text()

    def _restore_opponent_price(self) -> None:
        """手数写进价格框后，试着把价格改回对手价。改不回来也不再去点平仓。"""
        try:
            from src.trader.price_mode import ensure_opponent_price

            ensure_opponent_price(self._full_cfg, focus=not self._order_armed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("价格被手数盖掉后，没能自动改回对手价: {}", exc)

    def _close_volume_pad(self) -> None:
        """
        收起手数快捷面板，再去点买多、卖空或平仓。

        一点手数框，同花顺就在框下面弹出 1、2、3、5 和百分比。
        卖空按钮正好在这块面板底下。价格下拉如果还开着，也会盖住平仓。
        这两块还开着去点，点到的是面板，不是卖空或平仓。
        这时买卖按钮还没点，按一次 Esc 只收起面板，不会平掉已有的仓，也不会把刚填的手数清掉。
        收起之后不要马上再激活窗口。激活会把光标带离输入框，后面的数字就打不准。
        这一笔里买卖按钮也不再重新激活窗口。
        """
        if self.dry_run:
            logger.info("[模拟] 关闭手数快捷面板后再点买卖")
            return
        if not self._foreground_is_trade_panel():
            logger.info("当前不是交易窗口，不按 Esc，避免切到同花顺主界面。")
            return
        pyautogui.press("esc")
        time.sleep(0.15)

    def _dismiss_confirm(self) -> None:
        """
        关掉下单后的确认提示框。

        原理：弹窗通常默认焦点在「确定」，按 Enter 即可；
        若 Enter 无效，再用标定的 confirm_btn 坐标点击。
        """
        if self.dry_run:
            logger.info(
                "[模拟确认] mode={} delay={}s times={}",
                self.confirm_mode,
                self.confirm_delay_sec,
                self.confirm_times,
            )
            return

        # 买卖按钮刚点过，确认框就在同花顺里。交易进程还在最前时不再激活，避免把确认框切掉。
        if not (self._order_armed and self._trade_process_in_front()):
            if self._focus_ths_before_click() is False:
                logger.error("确认前同花顺不在最前，不再按回车，避免回车进到程序窗口。")
                return
        has_btn = bool(self.gui.get("confirm_btn"))
        for i in range(max(1, self.confirm_times)):
            time.sleep(self.confirm_delay_sec)
            if self.confirm_mode in ("enter", "both"):
                pyautogui.press("enter")
                logger.info("已按回车确认弹窗 ({}/{})", i + 1, self.confirm_times)
                time.sleep(0.25)
            if self.confirm_mode in ("click", "both") and has_btn:
                self._click("confirm_btn")
                logger.info("已点击确认按钮坐标 ({}/{})", i + 1, self.confirm_times)
            elif self.confirm_mode == "click" and not has_btn:
                # 没标定坐标时退回回车，避免完全卡住
                pyautogui.press("enter")
                logger.warning("未标定 confirm_btn，改按回车确认 ({}/{})", i + 1, self.confirm_times)

    def _ensure_symbol(self, symbol: str) -> None:
        """
        若标定了品种输入框，则先写入合约代码（如 bu2612）。
        主流 GUI 交易助手都会在下单前对准合约，避免点错品种。
        """
        if not self.gui.get("symbol_input"):
            return
        # 界面里常显示小写/大写均可；typewrite 用字母数字
        code = "".join(ch for ch in symbol if ch.isalnum())
        if not code:
            return
        self._type_into("symbol_input", code)
        time.sleep(0.15)

    def place_order(self, req: OrderRequest) -> OrderResult:
        global _ORDER_CLICKING
        order_id = f"GUI-{uuid.uuid4().hex[:8]}"
        # 整段点击期间，界面不要把自己拉到前面。结束时无论成败都要取消钉住。
        _ORDER_CLICKING = True
        try:
            # 下单期间本程序不能再跑到前面。先沉底并禁止激活，点完再恢复。
            self._park_own_windows()
            # 不在最前就先拉到最前。没有交易窗口就自动登录。准备好才填价格、点按钮。
            if self._ensure_trade_desk() is False:
                message = "没有把同花顺交易窗口准备好，这次没有点买多、卖空或平仓。"
                logger.error(message)
                return OrderResult(ok=False, message=message, order_id=order_id)
            # 后面只做对手价、手数和买卖按钮。不再拉起本程序，也不再打开下单模拟器。
            self._order_armed = True

            # 0) 可选：切换/确认合约
            self._ensure_symbol(req.symbol)

            # 1) 常驻只填手数，价格框保持对手价。
            # 下单模拟器才会把限价打进价格框，用来挂一张不成交的委托。
            use_limit = str(getattr(req, "gui_price", "opponent") or "opponent") == "limit"
            typed_price = ""
            if use_limit:
                if req.price is None or float(req.price) <= 0:
                    message = "下单模拟器要填写一个大于 0 的价格。这次没有点买多、卖空或平仓。"
                    logger.error(message)
                    return OrderResult(ok=False, message=message, order_id=order_id)
                from src.trader.price_mode import format_limit_price

                typed_price = format_limit_price(float(req.price))
                price_before = self._write_limit_price(typed_price)
                if not price_before:
                    message = f"价格 {typed_price} 没有写进价格框。这次没有点买多、卖空或平仓。"
                    logger.error(message)
                    return OrderResult(ok=False, message=message, order_id=order_id)
            else:
                # 先看价格框。如果还是最新价，先改成对手价；改不成就不点买卖，也不记成成交。
                price_before = self._prepare_opponent_price()
                blocked = ""
                if not self.dry_run:
                    from src.trader.price_mode import price_mode_blocks_order

                    blocked = price_mode_blocks_order(price_before)
                if blocked:
                    message = f"价格框现在是「{blocked}」，不是对手价。这次没有点买多、卖空或平仓。"
                    logger.error(message)
                    return OrderResult(ok=False, message=message, order_id=order_id)
                if not self.dry_run:
                    logger.info("下单前价格框是「{}」", price_before or "没读到")
                if req.price is not None:
                    logger.info("忽略传入的价格 {}，下单不改价格框，沿用对手价", req.price)
            # 手数优先直接写进手数框，不点它，避免弹出 1、2、3 的面板，也避免数字跑进价格框。
            # 直接写不上时，才在交易窗口里用键盘输入。用了键盘才需要再按 Esc 收起面板。
            used_keys = self._fill_volume(str(req.volume))
            price_after = self._read_price_text()
            if use_limit:
                from src.trader.price_mode import price_text_matches

                if not self.dry_run and not price_text_matches(price_after, typed_price):
                    self._close_volume_pad()
                    message = (
                        f"手数 {req.volume} 写进了价格框（现在是「{price_after}」），"
                        "这次没有点买多、卖空或平仓。"
                    )
                    logger.error(message)
                    return OrderResult(ok=False, message=message, order_id=order_id)
            else:
                from src.trader.price_mode import price_was_overwritten

                if price_was_overwritten(price_before, price_after, str(req.volume)):
                    self._close_volume_pad()
                    self._restore_opponent_price()
                    message = (
                        f"手数 {req.volume} 写进了价格框（现在是「{price_after}」），"
                        "这次没有点买多、卖空或平仓。请确认价格框是对手价。"
                    )
                    logger.error(message)
                    return OrderResult(ok=False, message=message, order_id=order_id)
            # 手数框一点开就会挡住卖空。用键盘填过手数时，先收起再点按钮。
            if used_keys:
                self._close_volume_pad()

            # 2) 按方向点不同按钮
            if req.side == "buy_open":
                self._click("buy_open_btn")
            elif req.side == "sell_open":
                self._click("sell_open_btn")
            elif req.side == "close":
                self._click("close_btn")
            else:
                return OrderResult(ok=False, message=f"未知方向: {req.side}")

            # 3) 处理「确定」提示框，否则后续点击会被挡住
            self._dismiss_confirm()

            if use_limit:
                msg = f"下单模拟器已点击，不记入账户 side={req.side} 价格={typed_price}"
                logger.info("{} symbol={} vol={}", msg, req.symbol, req.volume)
                # 这是远离现价的委托，不能当成成交去改账户。
                return OrderResult(
                    ok=True,
                    message=msg,
                    order_id=order_id,
                    assumed_fill=False,
                    raw={"assumed_fill": False, "executor": "gui", "simulator": True, "price": typed_price},
                )
            msg = "GUI 下单指令已发送" if not self.dry_run else "GUI 模拟点击完成"
            logger.info("{} side={} symbol={} vol={}", msg, req.side, req.symbol, req.volume)
            # assumed_fill=True：点击成功≠成交；调度层据此决定是否立刻改内部持仓
            return OrderResult(
                ok=True,
                message=msg,
                order_id=order_id,
                assumed_fill=not self.dry_run,
                raw={"assumed_fill": not self.dry_run, "executor": "gui"},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("GUI 下单失败: {}", e)
            return OrderResult(ok=False, message=str(e), order_id=order_id)
        finally:
            _ORDER_CLICKING = False
            self._order_armed = False
            self._unpin_trade_window()
            self._restore_own_windows()
