"""
同花顺期货通的交易登录框。

登录框标题是「同花顺期货通交易」。里面有密码框和「登录」按钮。
程序只在这个框出现时填密码，避免把密码打进行情或下单面板。
"""

from __future__ import annotations

import threading
import time
from typing import Any

# 同花顺一次只能点一个登录菜单。几个登录挤在一起时，排队，避免连点把下单窗口关掉。
_login_lock = threading.Lock()

from loguru import logger

from src.utils.trading_day import settlement_session_day


def escape_sendkeys(text: str) -> str:
    """让密码里的大括号按原样输入，而不是被当成特殊按键。"""
    # 先换成不会出现在密码里的占位，避免替换花括号时互相影响。
    return (
        str(text)
        .replace("{", "\u0000")
        .replace("}", "\u0001")
        .replace("\u0000", "{{}")
        .replace("\u0001", "{}}")
    )


def pick_popup_menu_item(candidates: list[tuple[int, Any]]) -> Any | None:
    """
    同一个菜单名字可能有好几份。

    贴在窗口最顶上的那份通常是藏起来的，点不到。
    优先用弹出菜单里、位置更靠下的那一项。
    """
    visible = [item for top, item in candidates if top > 80]
    if visible:
        return visible[-1]
    if candidates:
        return candidates[-1][1]
    return None


def _password_edit(dialog: Any) -> Any | None:
    """在登录框里找密码输入框。不读取里面已经填了什么。"""
    import uiautomation as auto

    for ctrl, _depth in auto.WalkControl(dialog, maxDepth=8):
        if ctrl.ControlTypeName != "EditControl":
            continue
        try:
            if ctrl.IsPassword:
                return ctrl
        except Exception:  # noqa: BLE001
            continue
    return None


def text_shows_order_panel(text: str) -> bool:
    """
    标定坐标上的字是不是下单面板。

    同花顺把「普通下单」「CTP主席」画在窗口里，界面树经常读不到这些标题。
    价格框上的「对手」，以及买开、卖开、平仓、锁仓这些按钮，只有下单面板开着才点得到。
    """
    value = str(text or "").strip()
    if value in {
        "对手",
        "对手价",
        "最新价",
        "排队价",
        "市价",
        "超价",
        "买开",
        "卖开",
        "买多",
        "卖空",
        "平仓",
        "平多",
        "平空",
        "锁仓",
        "加多",
        "加空",
        "普通下单",
    }:
        return True
    return "CTP主席" in value or "非套期货" in value


def order_probe_points(gui: dict[str, Any] | None) -> list[tuple[int, int]]:
    """取出已经标定的价格框和三个买卖按钮。没有标定的跳过。"""
    points: list[tuple[int, int]] = []
    for key in ("price_input", "buy_open_btn", "sell_open_btn", "close_btn"):
        raw = (gui or {}).get(key)
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        points.append((int(raw[0]), int(raw[1])))
    return points


def _texts_at_points(points: list[tuple[int, int]]) -> list[str]:
    """读这些屏幕坐标上的字，不点击。"""
    if not points:
        return []
    try:
        import uiautomation as auto

        from src.utils.winui import _visible_text
    except Exception as exc:  # noqa: BLE001
        logger.warning("无法读取下单坐标上的字: {}", exc)
        return []
    found: list[str] = []
    auto.SetGlobalSearchTimeout(0.3)
    for x, y in points:
        try:
            ctrl = auto.ControlFromPoint(int(x), int(y))
        except Exception:  # noqa: BLE001
            continue
        if ctrl is None:
            continue
        text = _visible_text(ctrl)
        if text:
            found.append(text)
    return found


def looks_like_order_window(title: str) -> bool:
    """
    下单窗口标题里有「CTP主席」或「非套期货」。

    例如截图上的「非套期货-CTP主席-资金账号」。
    主窗口「同花顺期货通」和登录框「同花顺期货通交易」都不是它。
    """
    text = str(title or "")
    return "CTP主席" in text or "非套期货" in text


def trade_window_rank(title: str) -> int:
    """
    下单时该把哪个窗口放到最前。

    交易窗口优先。行情主窗口「同花顺期货通」被反复激活时，主界面会来回切换。
    """
    text = str(title or "")
    score = 0
    if looks_like_order_window(text):
        score += 20
    if "交易" in text and "登录" not in text:
        score += 2
    if "登录" in text:
        score -= 5
    return score


def control_shows_order_panel(
    name: str,
    control_type: str,
    top: int,
    width: int,
    height: int,
) -> bool:
    """
    这个控件是不是已经打开的下单面板。

    面板嵌在「同花顺期货通」里面时，任务栏标题仍是主窗口，所以要看里面的字。
    「普通下单」是下单页签。标题栏上的「CTP主席」「非套期货」也算。
    交易菜单里的同名项不算，避免菜单没点开时误以为窗口还在。
    """
    if str(control_type or "") == "MenuItemControl":
        return False
    if width < 16 or height < 8:
        return False
    text = str(name or "").strip()
    if text == "普通下单":
        # 贴在最顶上、高度很小的那份通常是藏起来的副本。
        return top > 30
    return "CTP主席" in text or "非套期货" in text


def order_window_is_open(points: list[tuple[int, int]] | None = None) -> bool:
    """
    只看下单窗口在不在，不点菜单。

    先看标定的价格框和买卖按钮上的字。这些字只有下单面板开着才读得到。
    再点「交易 → 期货交易」会把已经开着的下单窗口关掉。
    """
    # 快路径：先只读价格框一个点。面板开着时这里就能读到「对手价/最新价」，
    # 省掉后面枚举窗口标题、遍历控件树的时间。读不到才走完整检查。
    if points:
        first = _texts_at_points(list(points[:1]))
        if any(text_shows_order_panel(text) for text in first):
            logger.info("下单坐标上读到「{}」，窗口还在，不点登录。", "、".join(first))
            return True
        # 第一个点已经读过，后面只补读剩下的，别再读一遍。
        seen = list(first) + _texts_at_points(list(points[1:]))
    else:
        seen = []
    matched = [text for text in seen if text_shows_order_panel(text)]
    if matched:
        logger.info("下单坐标上读到「{}」，窗口还在，不点登录。", "、".join(matched))
        return True
    if points:
        logger.info("下单坐标上没有读到下单面板（读到：{}）。", "、".join(seen) or "空白")
    titles: list[str] = []
    try:
        import pygetwindow as gw
    except ImportError:
        gw = None
    if gw is not None:
        try:
            titles = [str(getattr(win, "title", "") or "") for win in gw.getAllWindows()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取窗口标题失败，改在同花顺窗口里面找下单面板: {}", exc)
    if any(looks_like_order_window(title) for title in titles):
        return True
    try:
        import uiautomation as auto

        auto.SetGlobalSearchTimeout(0.3)
        main = auto.WindowControl(searchDepth=1, Name="同花顺期货通")
        if not main.Exists(0.6):
            return False
        for ctrl, _depth in auto.WalkControl(main, maxDepth=14):
            try:
                rect = ctrl.BoundingRectangle
                top = int(rect.top)
                width = int(rect.right) - int(rect.left)
                height = int(rect.bottom) - top
            except Exception:  # noqa: BLE001
                continue
            if control_shows_order_panel(
                str(getattr(ctrl, "Name", "") or ""),
                str(getattr(ctrl, "ControlTypeName", "") or ""),
                top,
                width,
                height,
            ):
                return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("查找下单窗口失败，这次仍会尝试登录: {}", exc)
        return False


def _open_trade_login(main: Any) -> None:
    """点「交易 → 期货交易 → 期货交易(主界面)」。没登录时会弹出登录框。"""
    import uiautomation as auto

    main.SetActive()
    trade = main.TextControl(Name="交易")
    if not trade.Exists(1):
        raise RuntimeError("找不到同花顺顶部的「交易」菜单")
    trade.Click(simulateMove=False)
    time.sleep(0.4)
    futures = auto.TextControl(Name="期货交易")
    if not futures.Exists(1):
        raise RuntimeError("找不到「期货交易」菜单")
    futures.MoveCursorToInnerPos()
    time.sleep(0.45)
    found: list[tuple[int, Any]] = []
    for ctrl, _depth in auto.WalkControl(auto.GetRootControl(), maxDepth=8):
        if ctrl.ControlTypeName != "MenuItemControl":
            continue
        if (ctrl.Name or "").strip() != "期货交易(主界面)":
            continue
        rect = ctrl.BoundingRectangle
        found.append((int(rect.top), ctrl))
    item = pick_popup_menu_item(found)
    if item is None:
        raise RuntimeError("找不到「期货交易(主界面)」")
    item.Click(simulateMove=False)
    time.sleep(1.2)


def account_text_ready(text: str, expected: str = "") -> bool:
    """
    这段字是不是已经填好的资金账号。

    同花顺打开登录框后，会自己把记住的账号填进下拉框，这一步有时要一两秒。
    账号还没出现时，框里是空白或「请选择」这类提示，不能当成填完。
    配置里写了账号时，必须看到同一个数字，避免把别的编号当成账号。
    """
    value = str(text or "").strip()
    if not value or "请" in value:
        return False
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return False
    want = "".join(ch for ch in str(expected or "") if ch.isdigit())
    if want and want not in digits:
        return False
    return True


def pick_account_text(candidates: list[str], expected: str = "") -> str:
    """从登录框里读到的几段字中，挑出资金账号。没有就返回空字符串。"""
    ready = [str(item or "").strip() for item in candidates if account_text_ready(item, "")]
    want = "".join(ch for ch in str(expected or "") if ch.isdigit())
    if want:
        matched = [
            item
            for item in ready
            if want in "".join(ch for ch in item if ch.isdigit())
        ]
        return matched[0] if matched else ""
    return ready[0] if ready else ""


def account_has_settled(samples: list[str], expected: str = "") -> bool:
    """
    连续两次读到同一个账号，才算同花顺填完了。

    只读到一次还不行：下拉框可能刚闪出数字，下一次又变，变的时候会把密码清掉。
    """
    if len(samples) < 2:
        return False
    last = str(samples[-1] or "").strip()
    prev = str(samples[-2] or "").strip()
    if not last or last != prev:
        return False
    return account_text_ready(last, expected)


def _control_is_enabled(ctrl: Any) -> bool | None:
    """读按钮能不能点。读不到时返回 None，不当成被禁用。"""
    try:
        return bool(ctrl.IsEnabled)
    except Exception:  # noqa: BLE001
        return None


def _read_account_text(dialog: Any, expected: str = "") -> str:
    """
    只在这个登录框里找资金账号，不读密码框里的内容。

    先看控件名字。名字里没有账号时，再看输入框当前的值。
    """
    import uiautomation as auto

    # 查找超时改短，避免每一轮等账号时在控件上干等。看完再改回 1 秒。
    auto.SetGlobalSearchTimeout(0.2)
    try:
        names: list[str] = []
        value_controls: list[Any] = []
        for ctrl, _depth in auto.WalkControl(dialog, maxDepth=8):
            kind = str(getattr(ctrl, "ControlTypeName", "") or "")
            if kind not in {"ComboBoxControl", "EditControl", "TextControl", "ListItemControl"}:
                continue
            if kind == "EditControl":
                try:
                    if ctrl.IsPassword:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            name = str(getattr(ctrl, "Name", "") or "").strip()
            if name:
                names.append(name)
            if kind in {"ComboBoxControl", "EditControl"}:
                value_controls.append(ctrl)
        picked = pick_account_text(names, expected)
        if picked:
            return picked
        values: list[str] = []
        for ctrl in value_controls:
            for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
                try:
                    pattern = getattr(ctrl, getter)()
                    value = str(getattr(pattern, "Value", "") or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if value:
                    values.append(value)
                    break
        return pick_account_text(values, expected)
    finally:
        auto.SetGlobalSearchTimeout(1.0)


def _wait_account_settled(
    dialog: Any,
    expected: str = "",
    *,
    timeout: float = 8.0,
    pause: float = 0.35,
    read: Any = None,
    now: Any = None,
    sleep: Any = None,
    still_open: Any = None,
) -> str:
    """
    等登录框里的资金账号自己填完，并且停住不再变。

    账号一变，同花顺会把已经写下的密码清掉。所以密码必须等这一步结束再填。
    超过等待时间还没稳定，就返回当前读到的字，调用方再填密码；若随后被清空，会再填一次。
    登录框中途关掉时返回「已关闭」，表示不用再填了。
    now、sleep、read、still_open 只给测试替换，正常登录不用传。
    """
    reader = read or (lambda item: _read_account_text(item, expected))
    clock = now or time.time
    pause_fn = sleep or time.sleep
    opened = still_open or (lambda: True)
    samples: list[str] = []
    logger.info("登录框已出现，等资金账号填完再写密码")
    deadline = clock() + max(0.0, float(timeout))
    while True:
        if not opened():
            return "已关闭"
        text = str(reader(dialog) or "").strip()
        samples.append(text)
        if account_has_settled(samples, expected):
            pause_fn(0.3)
            if not opened():
                return "已关闭"
            again = str(reader(dialog) or "").strip()
            if again == text:
                logger.info("登录框里的资金账号已经填好，开始填写密码")
                return again
            samples.append(again)
        if clock() >= deadline:
            logger.info("资金账号还没稳定，先填写密码。若被清空会再填一次。")
            return samples[-1] if samples else ""
        pause_fn(pause)


def _login_button_stays_ready(
    button: Any,
    *,
    seconds: float = 1.2,
    step: float = 0.3,
    enabled: Any = None,
    now: Any = None,
    sleep: Any = None,
) -> bool | None:
    """
    填完密码后看一小会儿登录按钮。

    按钮一直能点，说明密码还在。中途变灰，说明账号刚填完把密码清掉了。
    读不到按钮状态时返回 None。
    """
    ask = enabled or _control_is_enabled
    clock = now or time.time
    pause_fn = sleep or time.sleep
    deadline = clock() + max(0.0, float(seconds))
    last: bool | None = None
    while True:
        last = ask(button)
        if last is False:
            return False
        if clock() >= deadline:
            return last
        pause_fn(step)


def _dialog_still_open(dialog: Any) -> bool:
    """登录框还在不在。检查出错时当作还在，避免漏填密码。"""
    try:
        return bool(dialog.Exists(0.2))
    except Exception:  # noqa: BLE001
        return True


def _type_password_until_kept(
    edit: Any,
    button: Any,
    dialog: Any,
    password: str,
    user_id: str,
) -> str:
    """
    把密码写进登录框。

    返回 click（可以点登录）、closed（登录框已经没了）、failed（重填后按钮仍是灰的）。
    第一次写完如果按钮变灰，就是账号晚到一步把密码清掉了，等账号再稳定后重写一次。
    """
    for attempt in (1, 2):
        _fill_password(edit, password)
        ready = _login_button_stays_ready(button)
        if ready is not False:
            return "click"
        if attempt == 2:
            return "failed"
        logger.info("资金账号填完后密码被清空，等账号不再变化后重新填写")
        state = _wait_account_settled(dialog, user_id, still_open=lambda: _dialog_still_open(dialog))
        if state == "已关闭":
            return "closed"
    return "failed"


def _fill_password(edit: Any, password: str) -> None:
    """点进密码框，清掉原来的内容，再输入配置里的密码。"""
    import uiautomation as auto

    edit.Click(simulateMove=False)
    time.sleep(0.15)
    auto.SendKeys("{Ctrl}a{Delete}")
    auto.SendKeys(escape_sendkeys(password), interval=0.02)


# 夜盘拉起同花顺后，会单独弹出这个窗口。标题和里面的按钮都是这五个字。
SETTLEMENT_TITLE = "确认结算单"


def window_title_is_settlement(title: str) -> bool:
    """窗口标题里有「确认结算单」，就是挡住下单面板的那一层。"""
    return "确认结算单" in str(title or "")


def settlement_search_host(name: str, class_name: str = "") -> bool:
    """
    这个窗口值不值得进去找「确认结算单」按钮。

    本程序标题是「期货自动交易台」，里面也有「期货」两个字，不能当成同花顺。
    Tk 窗口的控件树不稳定，走进去会报 COM 错，启动常驻会中断。
    """
    title = str(name or "")
    cls = str(class_name or "")
    if "Auto-Trading" in title:
        return False
    if cls in {"TkTopLevel", "TkChild"}:
        return False
    if window_title_is_settlement(title):
        return True
    return "同花顺" in title or "期货通" in title or "CTP主席" in title


def _walk_ui(host: Any, max_depth: int) -> list[tuple[Any, int]]:
    """
    走一遍窗口里的控件。中途 COM 报错就停，已经读到的还留着。

    同花顺控件树偶发 GetNextSiblingControl 失败，不能把整次准备打停。
    """
    import uiautomation as auto

    found: list[tuple[Any, int]] = []
    if host is None:
        return found
    try:
        for ctrl, depth in auto.WalkControl(host, maxDepth=max_depth):
            found.append((ctrl, depth))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读窗口控件中断，跳过这个窗口剩余部分: {}", exc)
    return found


def settlement_click_point(left: int, top: int, right: int, bottom: int) -> tuple[int, int] | None:
    """
    结算单窗口里找不到按钮时，点窗口内部靠下居中的位置。

    截图里蓝色的「确认结算单」就在窗口底边上面一点。
    点必须落在窗口里面，不能点到窗口下面的买多、卖空、平仓。
    窗口太大时不猜位置，避免点出结算单。
    """
    width = int(right) - int(left)
    height = int(bottom) - int(top)
    if width < 200 or height < 160 or width > 1400 or height > 1000:
        return None
    x = (int(left) + int(right)) // 2
    y = int(bottom) - 36
    if y <= int(top) + 48:
        return None
    return x, y


def name_is_settlement_button(name: str) -> bool:
    """
    这段字是不是结算单底部那个按钮。

    按钮上有时会多一个快捷键，例如「确认结算单(O)」。
    结算单正文很长，里面也会出现这五个字，那种整段文字不能当成按钮。
    """
    text = str(name or "").replace(" ", "").replace("\u3000", "").strip()
    if text == SETTLEMENT_TITLE:
        return True
    return text.startswith(SETTLEMENT_TITLE) and len(text) <= 16


def control_is_settlement_button(kind: str, name: str, width: int, height: int, top: int = 0) -> bool:
    """
    这个控件能不能当作「确认结算单」按钮来点。

    窗口标题和标题栏上的同名文字不是按钮。菜单里的同名项也不点。
    贴在屏幕最顶上的那一份通常是藏起来的副本。
    买卖、平仓名字对不上，不会进到这里。
    """
    if not name_is_settlement_button(name):
        return False
    if str(kind or "") in {"WindowControl", "TitleBarControl", "MenuItemControl"}:
        return False
    if int(width) < 40 or int(height) < 16 or int(height) > 80 or int(width) > 420:
        return False
    # 上沿为 0 表示调用方没给位置，仍允许。真正贴在最顶上的副本才排除。
    if 0 < int(top) <= 30:
        return False
    return True


def pick_settlement_button(controls: list[tuple[Any, ...]]) -> int | None:
    """
    从窗口里的控件中找出要点的那一个。

    每一项是 (控件类型, 名字, 宽, 高)，也可以再带上控件上沿。
    有真正的按钮就点按钮。同花顺有时把蓝按钮画成文字，那就点窗口里更靠下的那一段同名文字。
    """
    candidates: list[tuple[int, str, int]] = []
    for index, item in enumerate(controls):
        kind = str(item[0] or "")
        name = str(item[1] or "")
        width = int(item[2])
        height = int(item[3])
        top = int(item[4]) if len(item) > 4 else 0
        if not control_is_settlement_button(kind, name, width, height, top):
            continue
        candidates.append((index, kind, top))
    if not candidates:
        return None
    buttons = [item for item in candidates if item[1] == "ButtonControl"]
    pool = buttons or candidates
    # 上沿更大的在屏幕更下面。标题在上，蓝按钮在窗口底部。
    pool.sort(key=lambda item: item[2])
    return pool[-1][0]


def _window_rows(window: Any) -> list[tuple[str, str, int, int, int, Any]]:
    """读窗口里的控件。每一项带上控件上沿和控件本身，方便选中后再点击。"""
    found: list[tuple[str, str, int, int, int, Any]] = []
    for ctrl, _depth in _walk_ui(window, 8):
        rect = getattr(ctrl, "BoundingRectangle", None)
        if rect is None:
            continue
        top = int(getattr(rect, "top", 0))
        width = int(getattr(rect, "right", 0)) - int(getattr(rect, "left", 0))
        height = int(getattr(rect, "bottom", 0)) - top
        found.append(
            (
                str(getattr(ctrl, "ControlTypeName", "") or ""),
                str(getattr(ctrl, "Name", "") or ""),
                width,
                height,
                top,
                ctrl,
            )
        )
    return found


def _find_settlement_windows() -> list[Any]:
    """
    找出屏幕上的「确认结算单」窗口。

    它有时是桌面上的独立窗口，有时嵌在同花顺里面。
    只认标题里带这五个字的窗口，不把下单面板当成结算单。
    """
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(0.2)
    found: list[Any] = []
    seen: set[int] = set()

    def _add(ctrl: Any) -> None:
        if ctrl is None:
            return
        key = int(getattr(ctrl, "NativeWindowHandle", 0) or 0) or id(ctrl)
        if key in seen:
            return
        seen.add(key)
        found.append(ctrl)

    try:
        import pygetwindow as gw

        for win in gw.getAllWindows():
            title = str(getattr(win, "title", "") or "")
            if not window_title_is_settlement(title):
                continue
            hwnd = int(getattr(win, "_hWnd", 0) or 0)
            if not hwnd:
                continue
            try:
                _add(auto.ControlFromHandle(hwnd))
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("按窗口标题找确认结算单失败: {}", exc)

    root = auto.GetRootControl()
    for ctrl, depth in _walk_ui(root, 2):
        if depth < 1:
            continue
        name = str(getattr(ctrl, "Name", "") or "")
        kind = str(getattr(ctrl, "ControlTypeName", "") or "")
        if window_title_is_settlement(name) and kind in {"WindowControl", "PaneControl"}:
            _add(ctrl)
    for ctrl, depth in _walk_ui(root, 1):
        if depth < 1:
            continue
        name = str(getattr(ctrl, "Name", "") or "")
        cls = str(getattr(ctrl, "ClassName", "") or "")
        if not settlement_search_host(name, cls):
            continue
        for child, _child_depth in _walk_ui(ctrl, 8):
            child_name = str(getattr(child, "Name", "") or "")
            child_kind = str(getattr(child, "ControlTypeName", "") or "")
            if not window_title_is_settlement(child_name):
                continue
            if child_kind not in {"WindowControl", "PaneControl", "CustomControl"}:
                continue
            _add(child)
    return found


def _control_rect(ctrl: Any) -> tuple[int, int, int, int] | None:
    """读控件边框。宽或高是 0 的边框不能用来点击。"""
    rect = getattr(ctrl, "BoundingRectangle", None)
    if rect is None:
        return None
    left = int(getattr(rect, "left", 0))
    top = int(getattr(rect, "top", 0))
    right = int(getattr(rect, "right", 0))
    bottom = int(getattr(rect, "bottom", 0))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _owning_hwnd(ctrl: Any) -> int:
    """往上找到这个控件所属窗口的句柄，用来把它拿到最前。"""
    found = 0
    current = ctrl
    for _step in range(12):
        handle = int(getattr(current, "NativeWindowHandle", 0) or 0)
        if handle:
            found = handle
        try:
            parent = current.GetParentControl()
        except Exception:  # noqa: BLE001
            break
        if parent is None or parent is current:
            break
        current = parent
    return found


def _click_control_center(ctrl: Any) -> bool:
    """
    点控件正中间。

    先把结算单窗口拿到最前，再点按钮中心。
    界面识别的 Click 经常点不中这块蓝按钮，所以用屏幕坐标点。
    """
    rect = _control_rect(ctrl)
    hwnd = _owning_hwnd(ctrl)
    if hwnd:
        from src.utils.winui import force_foreground

        force_foreground(hwnd)
        time.sleep(0.2)
    if rect is None:
        try:
            ctrl.Click(simulateMove=False)
            return True
        except Exception:  # noqa: BLE001
            return False
    import pyautogui

    left, top, right, bottom = rect
    x = (left + right) // 2
    y = (top + bottom) // 2
    pyautogui.click(x=x, y=y)
    logger.info("已点击「确认结算单」按钮，大约在 ({}, {})。没有点买卖。", x, y)
    return True


def _find_settlement_buttons() -> list[Any]:
    """
    在同花顺窗口里面找「确认结算单」按钮。

    这个窗口有时没有独立标题，按钮嵌在期货通里面。
    只认按钮上的这五个字，不把下单面板上的买多、卖空、平仓算进来。
    """
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(0.15)
    found: list[Any] = []
    seen: set[int] = set()

    def _take(ctrl: Any) -> None:
        rect = _control_rect(ctrl)
        if rect is None:
            return
        left, top, right, bottom = rect
        if not control_is_settlement_button(
            str(getattr(ctrl, "ControlTypeName", "") or ""),
            str(getattr(ctrl, "Name", "") or ""),
            right - left,
            bottom - top,
            top,
        ):
            return
        key = (left, top, right, bottom)
        if key in seen:
            return
        seen.add(key)
        found.append(ctrl)

    roots: list[Any] = []
    try:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if hwnd:
            front = auto.ControlFromHandle(hwnd)
            name = str(getattr(front, "Name", "") or "")
            cls = str(getattr(front, "ClassName", "") or "")
            # 点「启动常驻」时最前的是本程序，走进 Tk 控件树会 COM 报错。
            if settlement_search_host(name, cls):
                roots.append(front)
    except Exception:  # noqa: BLE001
        pass
    root = auto.GetRootControl()
    for ctrl, depth in _walk_ui(root, 1):
        if depth < 1:
            continue
        name = str(getattr(ctrl, "Name", "") or "")
        cls = str(getattr(ctrl, "ClassName", "") or "")
        if not settlement_search_host(name, cls):
            continue
        roots.append(ctrl)
    for host in roots:
        if host is None:
            continue
        _take(host)
        for child, _depth in _walk_ui(host, 10):
            _take(child)
    return found


def _click_settlement_window(window: Any) -> bool:
    """点这个结算单窗口里的确认按钮。点不到按钮时，改点窗口内部靠下的位置。"""
    import uiautomation as auto

    rows = _window_rows(window)
    index = pick_settlement_button(
        [(kind, name, width, height, top) for kind, name, width, height, top, _ctrl in rows]
    )
    if index is not None:
        return _click_control_center(rows[index][5])
    rect = getattr(window, "BoundingRectangle", None)
    if rect is None:
        return False
    point = settlement_click_point(
        int(getattr(rect, "left", 0)),
        int(getattr(rect, "top", 0)),
        int(getattr(rect, "right", 0)),
        int(getattr(rect, "bottom", 0)),
    )
    if point is None:
        return False
    hwnd = _owning_hwnd(window)
    if hwnd:
        from src.utils.winui import force_foreground

        force_foreground(hwnd)
        time.sleep(0.2)
    auto.Click(point[0], point[1])
    logger.info("确认结算单没有单独的按钮边框，已点窗口靠下的位置 ({}, {})。没有点买卖。", point[0], point[1])
    return True


def _look_and_click_settlement() -> str:
    """
    看屏幕上有没有结算单窗口。

    返回 absent（没有）、clicked（点过并且窗口已经关掉）、stuck（窗口还在）。
    窗口还在时不能去改对手价，否则会点在结算单上。
    """
    try:
        buttons = _find_settlement_buttons()
        windows = _find_settlement_windows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("查找确认结算单时读窗口失败，当作没有这个窗口: {}", exc)
        return "absent"
    if not buttons and not windows:
        return "absent"
    clicked = False
    # 按钮嵌在期货通里、窗口没有「确认结算单」标题时，也要点最靠下的那一个。
    if buttons:
        def _top(ctrl: Any) -> int:
            rect = _control_rect(ctrl)
            return rect[1] if rect else 0

        buttons.sort(key=_top)
        try:
            clicked = _click_control_center(buttons[-1]) or clicked
        except Exception as exc:  # noqa: BLE001
            logger.warning("点击确认结算单失败: {}", exc)
    for window in windows:
        try:
            if _click_settlement_window(window):
                clicked = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("点击确认结算单失败: {}", exc)
    time.sleep(0.45)
    if not _settlement_still_open():
        return "clicked" if clicked else "absent"
    if clicked:
        # 第一下没把窗口关掉时，再点一次最靠下的按钮，仍然不点买卖。
        again = _find_settlement_buttons()
        if again:
            again.sort(key=lambda ctrl: (_control_rect(ctrl) or (0, 0, 0, 0))[1])
            try:
                _click_control_center(again[-1])
            except Exception:  # noqa: BLE001
                pass
        for window in _find_settlement_windows():
            try:
                _click_settlement_window(window)
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.45)
        if not _settlement_still_open():
            return "clicked"
    logger.warning("看到「确认结算单」，点过 {} 次，窗口还在。", 1 if clicked else 0)
    return "stuck"


def _settlement_still_open() -> bool:
    """按钮还在，或者标题还在，都算结算单没关掉。"""
    return bool(_find_settlement_buttons() or _find_settlement_windows())


# 结算单每个夜盘登录周期只处理一次：20:50 登录后查一次，次日日盘沿用。
# 全量找要遍历同花顺控件树，一次 4 秒左右；查过之后连窗口标题也不看。
# 缓存换日比期货交易日早一小时（20:00），避免 20:50 登录还算「今天已经查过」。
# 只有「登录之后」那次检查才占用当天的额度，登录前那一眼不记账，
# 否则窗口还没弹出来就被记成查过，当天再也不会去点它。
_SETTLEMENT_DONE_DAY = ""


def confirm_settlement_if_present(
    *,
    wait_sec: float = 0,
    look: Any = None,
    mark_day: bool = True,
) -> bool:
    """
    如果弹出了「确认结算单」，就点窗口里的那个按钮。

    没有这个窗口时直接返回 True，不点击任何地方。
    点掉了也返回 True。窗口还在、按钮没点到时返回 False。
    wait_sec 用来等登录后晚一点才弹出的窗口。look 只给测试用。

    每个夜盘登录周期只处理一次：登录完成后查一次（有就点掉、没有就记下），
    这一整天直接返回，不再读控件、也不再看窗口标题。20:00 之后的下一次登录才会再查。
    mark_day=False 时只检查、不占用当天额度，给登录前那一眼用。
    """
    global _SETTLEMENT_DONE_DAY
    today = settlement_session_day().isoformat()
    use_cache = look is None  # 测试传了假 finder，不走缓存，免得互相影响
    if use_cache and _SETTLEMENT_DONE_DAY == today:
        return True
    finder = look or _look_and_click_settlement
    deadline = time.time() + max(0.0, float(wait_sec))
    while True:
        state = finder()
        if state == "clicked":
            logger.info("已点击「确认结算单」。没有点买开、卖开或平仓。")
            if use_cache and mark_day:
                _SETTLEMENT_DONE_DAY = today
            return True
        if state == "stuck":
            logger.warning("看到「确认结算单」窗口，但没有点到同名按钮。")
            return False
        if time.time() >= deadline:
            if use_cache and mark_day:
                _SETTLEMENT_DONE_DAY = today
            return True
        time.sleep(0.4)


def login_trade_account(
    password: str,
    *,
    user_id: str = "",
    captcha_pause: bool = False,
    window_open: Any = None,
    probe_points: list[tuple[int, int]] | None = None,
) -> tuple[bool, str]:
    """
    打开交易登录框。等同花顺自己把资金账号填完并停住后，再填密码并点击「登录」。
    账号填完会把已经写下的密码清掉，所以不能一看到登录框就写密码。

    返回:
        (是否可以继续, 给人看的说明)
    启动常驻、准备按钮、定时登录、单次扫描都会走到这里。
    点菜单之前先看下单窗口。还在就直接返回。
    已经登录但窗口不在时，才会再点一次菜单。没有登录框就当作已经登录，不再输入密码。
    window_open 只给测试用，用来代替真实的窗口检查。
    """
    # 先排队再看窗口。前一次登录还在点菜单时，这一次不能再点。
    with _login_lock:
        return _login_trade_account_locked(
            password,
            user_id=user_id,
            captcha_pause=captcha_pause,
            window_open=window_open,
            probe_points=probe_points,
        )


def _login_trade_account_locked(
    password: str,
    *,
    user_id: str = "",
    captcha_pause: bool = False,
    window_open: Any = None,
    probe_points: list[tuple[int, int]] | None = None,
) -> tuple[bool, str]:
    """真正点登录。调用前必须已经拿到登录锁。"""
    import uiautomation as auto

    try:
        if window_open is None:
            already_open = order_window_is_open(probe_points)
        else:
            already_open = bool(window_open())
    except Exception as exc:  # noqa: BLE001
        logger.warning("自检下单窗口失败，继续按原登录流程: {}", exc)
        already_open = False
    if already_open:
        logger.info("下单窗口还在，这次不登录。")
        return True, "下单窗口还在，这次不登录，避免把窗口关掉。"

    if not str(password or ""):
        return False, "自动登录没有读到交易密码。请在配置页填写密码并保存。"

    auto.SetGlobalSearchTimeout(1.0)
    main = auto.WindowControl(searchDepth=1, Name="同花顺期货通")
    if not main.Exists(2):
        return False, "找不到「同花顺期货通」窗口。请先把软件打开。"

    dialog = main.WindowControl(Name="同花顺期货通交易")
    if not dialog.Exists(0.6):
        try:
            _open_trade_login(main)
        except Exception as exc:  # noqa: BLE001
            logger.warning("打开交易登录框失败: {}", exc)
        dialog = main.WindowControl(Name="同花顺期货通交易")
    if not dialog.Exists(1.5):
        return True, "没有看到交易登录框，视为已经登录。"

    edit = _password_edit(dialog)
    button = dialog.ButtonControl(Name="登录")
    if edit is None or not button.Exists(0.8):
        return False, "登录框已出现，但没有找到密码框或「登录」按钮。"

    # 同花顺会自己填资金账号，填完会把密码清掉。等账号停住再写密码。
    account_state = _wait_account_settled(
        dialog,
        user_id,
        still_open=lambda: _dialog_still_open(dialog),
    )
    if account_state == "已关闭":
        return True, "登录框已经关掉，视为已经登录。"
    typed = _type_password_until_kept(edit, button, dialog, password, user_id)
    if typed == "closed":
        return True, "登录框已经关掉，视为已经登录。"
    if typed != "click":
        return False, "资金账号填完后把密码清掉了，重填后登录按钮仍是灰的。这次没有点登录。"
    button.Click(simulateMove=False)
    logger.info("已在交易登录框填入密码并点击登录")
    if captcha_pause:
        # 没有验证码时，登录框很快消失，不用干等满 60 秒。
        logger.warning("如果弹出验证码，请在 60 秒内手动完成。")
        deadline = time.time() + 60
        while time.time() < deadline:
            if not dialog.Exists(0.4):
                break
            time.sleep(1)
    else:
        time.sleep(2)
    return True, "已填写交易密码并点击登录。请看同花顺是否已经进入交易主界面。"
