"""
启动常驻时，把同花顺下单面板的价格设成「对手价」。

功能：只在启动常驻、并且真的要点同花顺时做一次。之后每次开仓、平仓都不再改价格框。
原理：
  对手价会按对面正在挂着的买一或卖一成交，平仓和开仓一般马上能进。
  最新价只是把单子挂在刚才成交的那个价上。买的时候卖一往往更高，单子会挂住不成交。
  所以这里固定用对手价，不提供改成最新价的开关。

价格框已经显示「对手价」时，不再点下拉，避免把菜单打开后误选别的价格。
菜单点开后会盖住平仓。选中对手价之后马上按 Esc 收起，避免后面的点击落到菜单上的市价或超价。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

# 固定用这一项。排队价要排队，最新价经常挂不住，市价和超价在急涨急跌时滑得更多。
OPPONENT_PRICE = "对手价"


@dataclass
class PriceBox:
    """价格框在屏幕上的位置，以及现在写着的字。"""

    left: int
    top: int
    right: int
    bottom: int
    text: str
    # 标定时鼠标点在价格框上的位置。界面识别不到边框时，用它去点下拉箭头。
    anchor_x: int
    anchor_y: int


def price_was_overwritten(before: str | None, after: str | None, typed: str) -> bool:
    """
    判断刚打的手数是不是写进了价格框。

    价格菜单开着时，鼠标虽然点在手数框上，键盘焦点仍可能留在价格框。
    接着全选再打数字，就会把「对手价」盖成 1。盖掉之后不能再点平仓，否则会按 1 元报单。
    读不到价格时不拦，避免识别失败就把正常下单停掉。
    """
    if after is None:
        return False
    after_text = str(after).strip()
    if not after_text or text_is_opponent(after_text):
        return False
    typed_text = str(typed).strip()
    if after_text == typed_text or after_text.replace(".", "", 1).isdigit():
        return True
    before_text = str(before or "").strip()
    return bool(before_text) and text_is_opponent(before_text)


def read_price_text(cfg: dict[str, Any], *, focus: bool = True) -> str | None:
    """
    读价格框上现在的字。读不到就返回空，调用方不要据此停掉下单。

    下单流程已经把交易窗口拉到最前时，focus 传假，这里不再去激活窗口。
    """
    gui = (cfg.get("trade") or {}).get("gui") or {}
    point = gui.get("price_input")
    if not point or len(point) < 2:
        return None
    try:
        box = _UiaPricePanel(int(point[0]), int(point[1]), cfg).read_box(focus=focus)
    except Exception:  # noqa: BLE001
        logger.exception("读取价格框失败")
        return None
    if box is None:
        return None
    return str(box.text or "")


def text_is_opponent(text: str) -> bool:
    """价格框上的字是不是已经是对手价。界面有时只写「对手」，多出来的空格不算。"""
    value = str(text or "").strip()
    return value in {OPPONENT_PRICE, "对手"}


# 价格下拉里会同时出现的几项。框里当前选中的那一项才算数。
_PRICE_WORDS = {OPPONENT_PRICE, "对手", "最新价", "排队价", "市价", "超价"}


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


def price_mode_blocks_order(text: str | None) -> str:
    """
    价格框不是对手价时，返回现在的字，调用方这次不要点买卖。

    读不到就返回空。识别失败不能把所有下单都停掉。
    """
    shown = str(text or "").strip()
    if not shown or text_is_opponent(shown):
        return ""
    if shown in _PRICE_WORDS or _looks_like_price_number(shown):
        return shown
    return ""


def price_text_matches(shown: str | None, typed: str) -> bool:
    """价格框上的字和刚填进去的限价是不是同一个数。读不到就不算写成功。"""
    left = str(shown or "").replace(",", "").replace("，", "").strip()
    right = str(typed or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return abs(float(left) - float(right)) < 0.001
    except ValueError:
        return False


def format_limit_price(price: float) -> str:
    """把限价写成打进价格框的数字。整数不带小数点。"""
    number = float(price)
    if abs(number - round(number)) < 0.0001:
        return str(int(round(number)))
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text


def _looks_like_price_number(text: str) -> bool:
    """纯数字是限价，不是对手价。"""
    body = str(text or "").strip().replace(".", "", 1)
    return bool(body) and body.isdigit()


def confirm_opponent_before_click(reader, setter) -> tuple[bool, str]:
    """
    下单前再看一次价格框。

    已经写着对手价，就不要再点下拉。
    还不是对手价，才去选一次。选完仍不是，这一笔先不点买卖，下一笔信号再看。
    reader 读出现在的字，setter 负责去点菜单，测试里用假的，避免真的动鼠标。
    """
    shown = reader()
    if text_is_opponent(str(shown or "")):
        return True, "价格框已经是对手价，直接下单"
    ok, message = setter()
    if ok:
        return True, message
    shown = reader()
    if text_is_opponent(str(shown or "")):
        return True, "价格框已经是对手价，直接下单"
    return False, message or "价格框还不是对手价，这次没有点买卖"


def pick_price_menu_item(
    items: list[tuple[str, tuple[int, int, int, int]]],
    box: PriceBox,
) -> tuple[int, int, int, int] | None:
    """
    从屏幕上所有叫「对手价」的控件里，挑出下拉菜单里那一个。

    参数 items：每一项是 (名字, (左, 上, 右, 下))。
    原理：同花顺里可能还有一份藏在窗口顶部、点不到的同名控件。
    真正的菜单贴在价格框下方，所以只收价格框下沿附近、并且横向上离价格框不远的那一项。
    """
    floor = box.bottom - 8
    ceiling = box.bottom + 260
    near_left = box.left - 80
    near_right = box.right + 160
    chosen: tuple[int, int, int, int] | None = None
    chosen_top = 10**9
    for name, rect in items:
        if not text_is_opponent(name):
            continue
        left, top, right, bottom = rect
        if top < floor or top > ceiling:
            continue
        if right < near_left or left > near_right:
            continue
        # 同一块菜单里只留最靠上的一项，避免点到更下面重复画出来的字。
        if top < chosen_top:
            chosen = rect
            chosen_top = top
    return chosen


def _wait_menu_item(
    panel: Any,
    box: PriceBox,
    own_panel: bool,
    timeout: float = 0.35,
) -> tuple[int, int, int, int] | None:
    """
    等下拉菜单渲染出来。

    原来点完固定睡 0.35 秒，不管菜单是不是 50 毫秒就出来了。这里改成小步轮询：
    菜单一出现立刻返回，最坏情况下的耗时和原来一样。
    """
    deadline = time.time() + timeout
    while True:
        items = panel.list_menu_items(box) if own_panel else panel.list_menu_items()
        rect = pick_price_menu_item(items, box)
        if rect is not None or time.time() >= deadline:
            return rect
        time.sleep(0.05)


def ensure_opponent_price(
    cfg: dict[str, Any],
    panel: Any | None = None,
    *,
    focus: bool = True,
) -> tuple[bool, str]:
    """
    把价格设成对手价。

    返回 (是否可以继续挂机, 写给日志的一句话)。
    已经是对手价就不再点下拉。下单流程里窗口已经在最前时，focus 传假，避免再激活一次把界面切走。
    panel 只在测试里传入，用来代替真实的同花顺窗口。
    """
    gui = (cfg.get("trade") or {}).get("gui") or {}
    point = gui.get("price_input")
    if not point or len(point) < 2:
        return False, "没有价格框坐标，无法设置对手价。请先在配置页标定下单坐标。"

    own_panel = panel is None
    if own_panel:
        panel = _UiaPricePanel(int(point[0]), int(point[1]), cfg)
    try:
        # 测试用的假面板没有 focus 参数，真面板才按这次要不要激活来读。
        box = panel.read_box(focus=focus) if own_panel else panel.read_box()
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取同花顺价格框失败")
        return False, f"没能读到价格框（{exc}）。请看同花顺价格是不是已经手动选成对手价。"
    if box is None:
        return False, "找不到下单价格框。请先打开同花顺的期货下单面板，并确认价格坐标仍指在价格框上。"
    if text_is_opponent(box.text):
        return True, "同花顺价格已经是对手价，启动时不再改动。"

    try:
        panel.open_dropdown(box)
        rect = _wait_menu_item(panel, box, own_panel)
        # 第一次点的是控件右边缘。标定的点如果在文字中间，箭头还要更靠右，再试一次。
        if rect is None and hasattr(panel, "nudge_dropdown"):
            panel.nudge_dropdown(box)
            rect = _wait_menu_item(panel, box, own_panel)
        if rect is None:
            panel.close_menu()
            return False, "下拉菜单里没有点到「对手价」。这次先不下单，下一笔信号还会再看一次价格框。"
        panel.click_rect(rect)
        time.sleep(0.25)
        # 对手价点中后，排队价、市价、最新价、超价这块面板经常还盖着平仓。先收起再核对。
        panel.close_menu()
        # 菜单刚点完，不再激活窗口，免得把刚选中的对手价又切掉。
        again = panel.read_box(focus=False) if own_panel else panel.read_box()
    except Exception as exc:  # noqa: BLE001
        logger.exception("设置对手价失败")
        try:
            panel.close_menu()
        except Exception:  # noqa: BLE001
            pass
        return False, f"设置对手价时出错（{exc}）。请手动把价格选成对手价。"

    if again is not None and text_is_opponent(again.text):
        return True, "已把同花顺价格设为对手价。之后开仓和平仓都不再修改价格。"
    # 有些界面点完菜单后，价格框读不出文字，但菜单项已经点过了。
    return True, "已点击价格菜单里的对手价。请看一眼价格框；之后程序不会再改价格。"


class _UiaPricePanel:
    """用界面识别读同花顺价格框，并用鼠标点开下拉、选中对手价。"""

    def __init__(self, anchor_x: int, anchor_y: int, cfg: dict[str, Any]) -> None:
        self.anchor_x = anchor_x
        self.anchor_y = anchor_y
        self.cfg = cfg

    def read_box(self, *, focus: bool = True) -> PriceBox | None:
        """看标定的那个点下面是什么控件，把它的边框和文字读出来。下单中途不再激活窗口。"""
        import uiautomation as auto

        if focus:
            self._focus()
        auto.SetGlobalSearchTimeout(0.8)
        ctrl = auto.ControlFromPoint(self.anchor_x, self.anchor_y)
        if ctrl is None:
            return None
        rect = _rect_of(ctrl)
        if rect is None:
            return PriceBox(self.anchor_x, self.anchor_y, self.anchor_x, self.anchor_y, "", self.anchor_x, self.anchor_y)
        text = _visible_text(ctrl)
        left, top, right, bottom = rect
        return PriceBox(left, top, right, bottom, text, self.anchor_x, self.anchor_y)

    def open_dropdown(self, box: PriceBox) -> None:
        """点控件右边缘。那里一般是下拉箭头，点一下才会弹出排队价、对手价这些选项。"""
        import pyautogui

        mid_y = max(box.top + 4, (box.top + box.bottom) // 2)
        pyautogui.click(x=max(box.left + 4, box.right - 12), y=mid_y)
        # 只等点击落地，菜单渲染交给 list_menu_items 里的轮询，菜单快就不白等。
        time.sleep(0.05)

    def nudge_dropdown(self, box: PriceBox) -> None:
        """右边缘没把菜单点开时，改点标定位置右边大约一个下拉箭头的距离。"""
        import pyautogui

        pyautogui.click(x=box.anchor_x + 46, y=box.anchor_y)
        time.sleep(0.05)

    def list_menu_items(
        self,
        box: PriceBox | None = None,
    ) -> list[tuple[str, tuple[int, int, int, int]]]:
        """
        只在价格框下方找「对手价」。

        以前把整个「同花顺期货通」翻一遍，要好几秒，菜单又常常不在那个窗口里，所以点不到。
        下拉打开后，选项就贴在价格框下面，往下读几个位置就够。
        传入 box 时，一旦找到能点的「对手价」就停下，不再去遍历后面几个位置的父控件，
        省掉大部分花在控件树上的时间。
        """
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(0.2)
        found: list[tuple[str, tuple[int, int, int, int]]] = []
        seen: set[tuple[int, int, int, int]] = set()
        for dy in (16, 34, 52, 70, 88, 110):
            ctrl = auto.ControlFromPoint(self.anchor_x, self.anchor_y + dy)
            if ctrl is None:
                continue
            self._collect_opponent(ctrl, found, seen)
            try:
                parent = ctrl.GetParentControl()
            except Exception:  # noqa: BLE001
                parent = None
            if parent is None:
                continue
            for child, _depth in auto.WalkControl(parent, maxDepth=2):
                self._collect_opponent(child, found, seen)
            if box is not None and pick_price_menu_item(found, box) is not None:
                break
        return found

    @staticmethod
    def _collect_opponent(
        ctrl: Any,
        found: list[tuple[str, tuple[int, int, int, int]]],
        seen: set[tuple[int, int, int, int]],
    ) -> None:
        """控件上的字是对手价，并且有边框，就收进候选。同一块边框不收两次。"""
        text = _visible_text(ctrl)
        if not text_is_opponent(text):
            return
        rect = _rect_of(ctrl)
        if rect is None or rect in seen:
            return
        seen.add(rect)
        found.append((text, rect))

    def click_rect(self, rect: tuple[int, int, int, int]) -> None:
        """点菜单项的中心。"""
        import pyautogui

        left, top, right, bottom = rect
        pyautogui.click(x=(left + right) // 2, y=(top + bottom) // 2)
        time.sleep(0.05)

    def close_menu(self) -> None:
        """没点到对手价时把下拉菜单关掉，免得它挡着后面的平仓按钮。"""
        import pyautogui

        pyautogui.press("esc")
        time.sleep(0.05)

    def _focus(self) -> None:
        """
        有交易窗口就把它放到最前。

        找不到交易窗口时，不去激活「同花顺期货通」。
        那一下会把行情主界面盖到交易面板上，价格框就点不到。
        """
        from src.login.app_login import AppLoginManager, force_foreground
        from src.login.ths_login import looks_like_order_window

        cfg = dict(self.cfg)
        cfg["dry_run"] = False
        manager = AppLoginManager(cfg)
        trade = [win for win in manager.find_windows() if looks_like_order_window(getattr(win, "title", "") or "")]
        if not trade:
            return
        win = trade[0]
        hwnd = int(getattr(win, "_hWnd", 0) or 0)
        if not hwnd:
            return
        import ctypes

        front = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if front == hwnd:
            return
        force_foreground(hwnd)
        time.sleep(0.2)


def _rect_of(ctrl: Any) -> tuple[int, int, int, int] | None:
    """读控件边框。宽或高是 0 的边框不能用来点击。"""
    try:
        rect = ctrl.BoundingRectangle
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:  # noqa: BLE001
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _visible_text(ctrl: Any) -> str:
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
