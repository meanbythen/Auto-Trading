"""Windows 窗口与控件读取工具。

登录、下单和行情窗口都要抢前台、读控件文字，放在这里避免登录与交易模块互相导入。
"""

from __future__ import annotations

import ctypes
from typing import Any

OPPONENT_PRICE = "对手价"
# 价格下拉里会同时出现的几项。框里当前选中的那一项才算数。
_PRICE_WORDS = {OPPONENT_PRICE, "对手", "最新价", "排队价", "市价", "超价"}


def force_foreground(hwnd: int) -> bool:
    """
    把窗口放到最前。

    Windows 不允许后台程序随便抢焦点。pygetwindow 一失败就报错，
    错误码 0 的意思其实是「操作成功完成」，错误码 2 经常是上一步启动程序留下的旧错误。
    这里先挂到当前前台窗口的输入队列上，再置顶，最后只看前台是不是已经变成这个窗口。
    """
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE，先从最小化还原
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    if user32.GetForegroundWindow() == hwnd:
        return True

    our_thread = kernel32.GetCurrentThreadId()
    process_id = ctypes.c_ulong()
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id)) if foreground else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    attached: list[int] = []
    for other in (int(foreground_thread or 0), int(target_thread or 0)):
        if other and other != our_thread and other not in attached:
            if user32.AttachThreadInput(our_thread, other, True):
                attached.append(other)
    user32.BringWindowToTop(hwnd)
    kernel32.SetLastError(0)
    user32.SetForegroundWindow(hwnd)
    for other in attached:
        user32.AttachThreadInput(our_thread, other, False)
    return user32.GetForegroundWindow() == hwnd


def _looks_like_price_number(text: str) -> bool:
    """纯数字是限价，不是对手价。"""
    body = str(text or "").strip().replace(".", "", 1)
    return bool(body) and body.isdigit()


def choose_shown_price(values: list[str], names: list[str]) -> str:
    """
    决定价格框上现在显示的是哪一个字。

    输入框里的值是当前选项。控件名字里有时还会带上菜单中的「对手价」。
    框里明明是「最新价」时，不能因为名字里出现过对手价，就当成已经设好。
    """
    shown_values = [str(item or "").strip() for item in values if str(item or "").strip()]
    for text in shown_values:
        if text in _PRICE_WORDS or _looks_like_price_number(text):
            return OPPONENT_PRICE if text == "对手" else text
    shown_names = [str(item or "").strip() for item in names if str(item or "").strip()]
    for text in shown_names:
        if text in _PRICE_WORDS:
            return OPPONENT_PRICE if text == "对手" else text
    if shown_values:
        return shown_values[0]
    return shown_names[0] if shown_names else ""


def visible_text(ctrl: Any) -> str:
    """
    读控件上给人看的字。

    输入值是框里当前的选项。名字里再出现「对手价」，也不能把「最新价」说成已经设好。
    """
    chunks: list[str] = []
    try:
        name = str(ctrl.Name or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    if name:
        chunks.append(name)
    for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
        try:
            pattern = getattr(ctrl, getter)()
        except Exception:  # noqa: BLE001
            continue
        if pattern is None:
            continue
        for attr in ("Value", "Name"):
            try:
                text = str(getattr(pattern, attr, "") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                chunks.append(text)
    values = [item for item in chunks[1:] if item]
    names = [chunks[0]] if chunks else []
    # 值是框里当前的选项。名字里夹着的「对手价」不能盖过「最新价」。
    return choose_shown_price(values, names)


# 旧名保留：登录与下单模块原来都叫 _visible_text。
_visible_text = visible_text
