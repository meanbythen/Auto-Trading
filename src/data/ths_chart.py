"""
从同花顺期货通主图信息框读取 K 线（只给历史回测）。

原理：主图上方选好周期后，鼠标停在一根 K 线上会弹出信息框。
框顶部是这根 K 线的结束时间（右标签）。左右方向键切到上一根 / 下一根。
信息框可能在主图左侧或右侧。这里读框里的开高低收，不点买卖。

实盘不能用这一路：每根都要抢窗口、动键盘，会挡住下单、拖慢扫描。
读到的数据单独存成 @ths，不盖新浪 / 天勤。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

import pandas as pd
from loguru import logger

# 读图线程和界面「停止」按钮共用。按 ESC 也会把它打开。
_THS_STOP = threading.Event()


class ThsScrapeStopped(RuntimeError):
    """用户按 ESC 或点「停止读图」。已经读到的根可以先存下来。"""


def begin_ths_scrape() -> None:
    """新开一轮读图前清掉上次的停止标记，并丢掉还按着的 ESC。"""
    _THS_STOP.clear()
    _drain_esc()


def request_ths_stop() -> None:
    """界面点停止，或 ESC 回调。读图循环下一拍就会收手。"""
    _THS_STOP.set()


def _drain_esc() -> None:
    try:
        import ctypes

        ctypes.windll.user32.GetAsyncKeyState(0x1B)
    except Exception:  # noqa: BLE001
        pass


def _esc_is_down() -> bool:
    """ESC 正在按着，或自从上次检查后又按过一次。"""
    try:
        import ctypes

        value = int(ctypes.windll.user32.GetAsyncKeyState(0x1B))
    except Exception:  # noqa: BLE001
        return False
    return bool(value & 0x8000) or bool(value & 0x0001)


def ths_stop_requested() -> bool:
    if _THS_STOP.is_set():
        return True
    if _esc_is_down():
        _THS_STOP.set()
        return True
    return False

# 信息框顶部：11-06 21:05 或 2026-11-06 21:05，也可能只有日期。
_TIME_LINE = re.compile(
    r"(?:(?P<year>20\d{2})[-/])?(?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
    r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
# 无障碍接口常把「成交量」和「5544.00」拆成两行，或写成「成交量：5544.00」。
# 日增仓% 必须写在 日增仓 / 持仓 前面，避免先被吃掉。
_FIELD = re.compile(
    r"(日增仓%|日增仓|开盘|收盘|最高|最低|数值|成交量|成交额|持仓|涨跌|涨幅|振幅)"
    r"\s*[:：=]?\s*"
    r"([+-]?\d[\d,]*(?:\.\d+)?)(万|亿)?%?"
)
_FIELD_NAMES = {
    "开盘",
    "收盘",
    "最高",
    "最低",
    "数值",
    "成交量",
    "成交额",
    "持仓",
    "涨跌",
    "涨幅",
    "振幅",
    "日增仓",
    "日增仓%",
}


def _parse_ths_number(number: str, unit: str = "") -> float | None:
    """5544.00 / 4.25万 / 5,544 → 数字。"""
    raw = str(number or "").strip().replace(",", "").replace("%", "").replace("+", "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if unit == "万":
        value *= 10_000
    elif unit == "亿":
        value *= 100_000_000
    return value


def _fields_from_pairs(blob: str) -> dict[str, float]:
    """
    标签和数字不在同一行时，上一行是名字、下一行是数。

    无障碍读信息框时最常见：Name=成交量，旁边控件 Name=5544.00。
    """
    lines = [ln.strip().strip(":：=") for ln in blob.replace("\r", "\n").split("\n") if ln.strip()]
    out: dict[str, float] = {}
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        name = line
        if name not in _FIELD_NAMES:
            continue
        if i + 1 >= len(lines):
            continue
        nxt = lines[i + 1]
        if nxt in _FIELD_NAMES:
            continue
        unit = ""
        if nxt.endswith("万"):
            unit = "万"
            nxt = nxt[:-1]
        elif nxt.endswith("亿"):
            unit = "亿"
            nxt = nxt[:-1]
        value = _parse_ths_number(nxt, unit)
        if value is None:
            continue
        out[name] = value
        skip_next = True
    return out


def parse_ths_tooltip(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """
    把信息框里的字收成一根 K 线。

    时间用框顶部那一行，当作右标签（这根已经走完的结束时刻）。
    收盘优先用「收盘」，没有再用「数值」。
    成交量要对上图上的「成交量」；以前标签和数字拆行时会写成 0。
    """
    blob = str(text or "").replace("\u3000", " ").strip()
    if not blob:
        return None
    stamp = None
    for raw_line in blob.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("今结"):
            continue
        match = _TIME_LINE.match(line)
        if match and ("开盘" not in line and "收盘" not in line and "数值" not in line):
            stamp = _stamp_from_match(match, now=now)
            break
    if stamp is None:
        match = _TIME_LINE.search(blob[:80])
        if match:
            stamp = _stamp_from_match(match, now=now)
    if stamp is None:
        return None
    fields: dict[str, float] = _fields_from_pairs(blob)
    for name, number, unit in _FIELD.findall(blob):
        value = _parse_ths_number(number, unit)
        if value is None:
            continue
        fields[name] = value
    close = fields.get("收盘", fields.get("数值"))
    open_px = fields.get("开盘", close)
    high = fields.get("最高", max(open_px or 0, close or 0))
    low = fields.get("最低", min(open_px or 0, close or 0))
    if close is None or open_px is None:
        return None
    volume = fields.get("成交量", 0.0)
    row = {
        "datetime": stamp,
        "open": float(open_px),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume or 0.0),
    }
    if "持仓" in fields:
        row["oi"] = float(fields["持仓"])
    if "成交额" in fields:
        row["turnover"] = float(fields["成交额"])
    return row


def _stamp_from_match(match: re.Match[str], *, now: datetime | None) -> datetime:
    """缺年份时用今年；时间缺了就 00:00（日线常见）。"""
    clock = now or datetime.now()
    year = int(match.group("year") or clock.year)
    month = int(match.group("month"))
    day = int(match.group("day"))
    hour = int(match.group("hour") or 0)
    minute = int(match.group("minute") or 0)
    # 年底看到次年 1 月的框极少；开年看到去年 12 月更常见。
    if match.group("year") is None and month == 12 and clock.month == 1:
        year = clock.year - 1
    return datetime(year, month, day, hour, minute, 0)


def bars_from_tooltips(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """多根信息框收成一张表，同一时刻留最后一次。"""
    cols = ["open", "high", "low", "close", "volume", "oi"]
    if not rows:
        return pd.DataFrame(columns=cols)
    frame = pd.DataFrame(rows)
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame = frame.set_index("datetime").sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    keep = [c for c in cols if c in frame.columns]
    frame = frame[keep]
    from src.data.periods import canonicalize_ohlc

    # 信息框顶部时间已经是右标签。再走统一口径，避免带时区或列名和天勤、新浪不一致。
    out = canonicalize_ohlc(frame)
    return out if out is not None else frame


def walk_tooltips(
    read_bar: Callable[[], dict[str, Any] | None],
    press: Callable[[str], None],
    *,
    limit: int = 200,
    pause_sec: float = 0.18,
    should_stop: Callable[[], bool] | None = None,
    on_read: Callable[[int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """
    从当前这根往更早走：每读完一根按一次左方向键。

    同一时刻连续出现，或连续读失败，就停。不点买卖。
    should_stop 为真时立刻收手，已经读到的根还在。
    on_read 每读到一根就回调一次，给界面刷进度用。
    """
    want = max(2, int(limit))
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    misses = 0
    same = 0
    last_key = ""

    def _key(row: dict[str, Any]) -> str:
        return pd.Timestamp(row["datetime"]).strftime("%Y-%m-%d %H:%M:%S")

    for _ in range(want + 8):
        if should_stop is not None and should_stop():
            logger.info("同花顺图读取已停止，已读 {} 根", len(collected))
            break
        row = read_bar()
        if not row:
            misses += 1
            if misses >= 5:
                break
            # 信息框偶尔只画到一半。不要按左键，否则会白白跳掉一根。
            time.sleep(pause_sec)
            continue
        misses = 0
        key = _key(row)
        if key in seen:
            # 界面刷新可能比按键慢半拍。这时马上再按一次左键会跳根，
            # 先等一拍重读；还是同一根才认为真的到头了。
            time.sleep(pause_sec)
            again = read_bar()
            if again:
                row = again
                key = _key(row)
        if key in seen:
            same += 1
            if same >= 2:
                break
            press("left")
            time.sleep(pause_sec)
            continue
        same = 0
        seen.add(key)
        collected.append(row)
        last_key = key
        if on_read is not None:
            try:
                on_read(len(collected), row)
            except Exception:  # noqa: BLE001
                pass
        if len(collected) >= want:
            break
        press("left")
        time.sleep(pause_sec)
    logger.info("同花顺图读到 {} 根，最旧一根 {}", len(collected), last_key or "无")
    return collected


def advance_to_latest(
    read_stamp: Callable[[], str],
    press_right: Callable[[], None],
    *,
    max_presses: int = 240,
    step: int = 12,
    pause_sec: float = 0.3,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    """
    一路按右方向键，把十字光标推到最新一根。

    点击主图落点不确定，不推到最后就会漏掉较新的那一段 K 线。
    End 键实测不移动光标，只能按右键。到最右后时间不再变化，以此判断到底；
    连续两批都不变才收手，避免个别按键没被界面收到时过早停下。
    返回（按了几次, 最后看到的时间）。
    """
    pressed = 0
    stable = 0
    misses = 0
    try:
        last = str(read_stamp() or "")
    except Exception:  # noqa: BLE001
        last = ""
    want = max(0, int(max_presses))
    chunk = max(1, int(step))
    while pressed < want:
        if should_stop is not None and should_stop():
            break
        count = min(chunk, want - pressed)
        for _ in range(count):
            press_right()
        pressed += count
        if pause_sec > 0:
            time.sleep(pause_sec)
        try:
            current = str(read_stamp() or "")
        except Exception:  # noqa: BLE001
            current = ""
        if not current:
            misses += 1
            if misses >= 3:
                break
            continue
        misses = 0
        if current == last:
            stable += 1
            if stable >= 2:
                break
            continue
        stable = 0
        last = current
    return pressed, last


def _period_minutes(period: str) -> float:
    """周期折成分钟，用来判断预读位置和结束时间差几根。日线、周线按自然时间近似。"""
    text = str(period or "5").strip().upper()
    if text.isdigit():
        return float(text)
    if text == "D":
        return 1440.0
    if text == "W":
        return 10080.0
    return 5.0


def _target_is_latest(end: Any, period: str) -> bool:
    """结束时间填的是现在（或没填）才算「更新到最新一根」。"""
    if end is None:
        return True
    try:
        end_ts = pd.Timestamp(end)
    except (TypeError, ValueError):
        return True
    if end_ts.tzinfo is not None:
        end_ts = end_ts.tz_localize(None)
    slack = pd.Timedelta(minutes=max(5.0, _period_minutes(period)))
    return end_ts >= pd.Timestamp.now() - slack


def _seek_latest(hwnd: int) -> None:
    """把主图十字光标推到最新一根。只在结束时间就是现在时用。"""
    import pyautogui

    def _stamp() -> str:
        row = parse_ths_tooltip(collect_chart_texts(hwnd))
        if not row:
            return ""
        return pd.Timestamp(row["datetime"]).strftime("%Y-%m-%d %H:%M:%S")

    old_pause = pyautogui.PAUSE
    pyautogui.PAUSE = 0.02
    try:
        pressed, last = advance_to_latest(
            _stamp,
            lambda: pyautogui.press("right"),
            should_stop=ths_stop_requested,
        )
    finally:
        pyautogui.PAUSE = old_pause
    logger.info("十字光标已推到最新一根（按右键 {} 次，当前 {}）", pressed, last or "未知")
    # 推到位后信息框还要刷新一下才显示这一根。
    time.sleep(0.4)


def _manual_start(hwnd: int, *, end: Any, period: str, margin: int) -> str:
    """
    历史批次的起点由用户手动摆好，这里只做预读确认，不用方向键赶路。

    用户点确定前会把十字光标停在结束时间那根K线上。程序先读这一根，
    和请求的结束时间对一下；对不上就提示（仍按当前这根开读，多存无害）。
    再向右多走 margin 根读作余量，和上一批已经存过的K线重叠一点，接续不留缝。
    返回预读到的这一根时间。
    """
    row = parse_ths_tooltip(collect_chart_texts(hwnd))
    if row is None:
        raise RuntimeError(
            "还没有从信息框读到当前K线。请把光标停在结束时间那根K线上，"
            "确认信息框里有开盘/收盘/最高/最低，再点「从同花顺读取K线」。"
        )
    current = pd.Timestamp(row["datetime"])
    if end is not None:
        want = pd.Timestamp(end)
        gap = abs((current - want).total_seconds()) / 60.0
        if gap > max(5.0, _period_minutes(period)):
            logger.warning(
                "预读到的K线是 {}，和结束时间 {} 差 {:.0f} 分钟；按当前这根开读。",
                current.strftime("%Y-%m-%d %H:%M"),
                want.strftime("%Y-%m-%d %H:%M"),
                gap,
            )
        else:
            logger.info("预读确认：当前K线 {}，与结束时间一致", current.strftime("%Y-%m-%d %H:%M"))
    if int(margin) > 0:
        import pyautogui

        old_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0.02
        try:
            for _ in range(int(margin)):
                pyautogui.press("right")
        finally:
            pyautogui.PAUSE = old_pause
        time.sleep(0.45)
    return current.strftime("%Y-%m-%d %H:%M:%S")


def _node_blob(node: Any) -> str:
    """一个控件上能拿到的字：名称、值。信息框整段粘在一个名称里时也要留。"""
    bits: list[str] = []
    for attr in ("Name", "AutomationId"):
        try:
            value = str(getattr(node, attr, "") or "").strip()
        except Exception:  # noqa: BLE001
            value = ""
        if value:
            bits.append(value)
    try:
        pattern = node.GetValuePattern()
        raw = str(getattr(pattern, "Value", "") or "").strip() if pattern is not None else ""
        if raw:
            bits.append(raw)
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(bits)


def _walk_uia(node: Any, chunks: list[str], *, depth: int, max_depth: int, max_chunks: int) -> None:
    if depth > max_depth or len(chunks) > max_chunks:
        return
    blob = _node_blob(node)
    if blob:
        chunks.append(blob)
    try:
        children = node.GetChildren()
    except Exception:  # noqa: BLE001
        return
    for child in children[:60]:
        _walk_uia(child, chunks, depth=depth + 1, max_depth=max_depth, max_chunks=max_chunks)


def _window_texts(hwnd: int) -> str:
    """把主图窗口里能看见的字拼起来。"""
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return ""
    if not hwnd:
        return ""
    try:
        control = auto.ControlFromHandle(int(hwnd))
    except Exception:  # noqa: BLE001
        return ""
    if control is None:
        return ""
    chunks: list[str] = []
    _walk_uia(control, chunks, depth=0, max_depth=14, max_chunks=200)
    return "\n".join(chunks)


def _cursor_texts() -> str:
    """鼠标停在 K 线上时，优先读光标下的信息框。"""
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return ""
    chunks: list[str] = []
    try:
        here = auto.ControlFromCursor()
    except Exception:  # noqa: BLE001
        here = None
    if here is not None:
        _walk_uia(here, chunks, depth=0, max_depth=6, max_chunks=40)
        try:
            parent = here.GetParentControl()
        except Exception:  # noqa: BLE001
            parent = None
        if parent is not None:
            _walk_uia(parent, chunks, depth=0, max_depth=4, max_chunks=40)
    return "\n".join(chunks)


def _hwnd_caption(user32: Any, hwnd: int) -> str:
    import ctypes

    try:
        length = int(user32.GetWindowTextLengthW(int(hwnd)) or 0)
    except Exception:  # noqa: BLE001
        return ""
    if length <= 0:
        return ""
    keep = min(length, 4000)
    buf = ctypes.create_unicode_buffer(keep + 1)
    user32.GetWindowTextW(int(hwnd), buf, keep + 1)
    return buf.value or ""


def _same_pid_texts(hwnd: int) -> str:
    """
    信息框经常是期货通进程里另一块小窗，不是主图的子控件。
    把同一进程里看得见的窗口标题也扫一遍。
    """
    if not hwnd:
        return ""
    try:
        import ctypes
    except Exception:  # noqa: BLE001
        return ""
    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    want = int(pid.value)
    if want <= 0:
        return ""
    found: list[str] = []

    def _visit(handle: int, _lparam: int) -> bool:
        try:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(int(handle), ctypes.byref(owner))
            if int(owner.value) != want:
                return True
            if not user32.IsWindowVisible(int(handle)):
                return True
            text = _hwnd_caption(user32, int(handle))
            if text and ("开盘" in text or "收盘" in text or "最高" in text):
                found.append(text)
            elif text and len(text) < 80:
                found.append(text)
        except Exception:  # noqa: BLE001
            return True
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_visit)
    try:
        user32.EnumWindows(callback, 0)
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(found)


def _info_panel_texts(hwnd: int) -> str:
    """
    定点找主图右侧/左侧的信息框。

    整棵树节点上千个，从根往下扫会超时、也容易被裁掉。
    信息框里一定有「开盘」标签，先用 UIA 原生查找定位它，再收父容器。
    """
    if not hwnd:
        return ""
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return ""
    try:
        root = auto.ControlFromHandle(int(hwnd))
        if root is None:
            return ""
        node = auto.Control(searchFromControl=root, Name="开盘", searchDepth=14)
    except Exception:  # noqa: BLE001
        return ""
    if not node:
        return ""
    parent = node.GetParentControl()
    if parent is None:
        return ""
    text = ""
    for _ in range(3):
        chunks: list[str] = []
        _walk_uia(parent, chunks, depth=0, max_depth=4, max_chunks=80)
        text = "\n".join(chunks)
        if parse_ths_tooltip(text) is not None:
            return text
        # 信息框正在换行/重绘时可能只拿到标签，等一拍再读。
        time.sleep(0.12)
    return text


def collect_chart_texts(hwnd: int) -> str:
    """主图 + 鼠标下 + 同进程弹层，三路拼在一起再解析。"""
    panel = _info_panel_texts(hwnd)
    if parse_ths_tooltip(panel) is not None:
        return panel
    parts = [panel, _cursor_texts(), _window_texts(hwnd), _same_pid_texts(hwnd)]
    return "\n".join(p for p in parts if p)


def _foreground_title() -> str:
    """当前前台窗口标题。读图失败时告诉用户程序当时在看哪个窗口。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        return _hwnd_caption(user32, hwnd).strip()
    except Exception:  # noqa: BLE001
        return ""


def _top_level_handles(pid: int) -> list[int]:
    """同一进程里所有看得见的大窗口。K 线窗口标题不一定含「期货通」。"""
    if pid <= 0:
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    def _visit(handle: int, _param: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(handle), ctypes.byref(owner))
        if int(owner.value) != pid:
            return True
        if not user32.IsWindowVisible(int(handle)):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(int(handle), ctypes.byref(rect))
        if rect.right - rect.left >= 300 and rect.bottom - rect.top >= 200:
            found.append(int(handle))
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_visit)
    try:
        user32.EnumWindows(callback, 0)
    except Exception:  # noqa: BLE001
        return found
    return found


def _chart_score(text: str) -> int:
    """窗口 UIA 文字越像 K 线主图，分越高；只像自选股列表的为负。"""
    blob = str(text or "")
    if not blob:
        return 0
    score = 0
    if "KLinePage" in blob or "KLine" in blob or "K线" in blob:
        score += 5
    if "开盘" in blob and "收盘" in blob:
        score += 4
    if "QuotationPage" in blob or "FavouritePage" in blob:
        score -= 2
    if "自选股" in blob:
        score -= 1
    return score


def _focus_chart(cfg: dict[str, Any], *, click: bool = True) -> int:
    """
    把期货通主图放到前面。

    click=True 时点一下主图中央，让信息框出现（结束时间是现在，程序自己找位置）。
    click=False 时只切前台，不碰鼠标：历史批次的光标由用户手动停好，点了就会被带跑。
    """
    import ctypes

    from src.login.app_login import AppLoginManager, force_foreground

    manager = AppLoginManager(cfg)
    windows = manager.find_windows()
    if not windows:
        raise RuntimeError("找不到同花顺期货通窗口。请先打开软件，主图选好合约和周期。")
    handles: list[int] = []
    for win in windows:
        handle = int(getattr(win, "_hWnd", 0) or getattr(win, "_hwnd", 0) or 0)
        if handle and handle not in handles:
            handles.append(handle)
    if not handles:
        raise RuntimeError("找不到同花顺期货通窗口。请先打开软件，主图选好合约和周期。")
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(handles[0], ctypes.byref(pid))
    for handle in _top_level_handles(int(pid.value)):
        if handle not in handles:
            handles.append(handle)

    def _area(handle: int) -> int:
        try:
            from ctypes import wintypes

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect))
            return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        except Exception:  # noqa: BLE001
            return 0

    def _score(handle: int) -> int:
        score = _chart_score(_window_texts(handle))
        # 信息框能解析出时间+开高低收的，基本就是主图。
        if parse_ths_tooltip(_info_panel_texts(handle)) is not None:
            score += 10
        return score

    scored = [(handle, _score(handle), _area(handle)) for handle in handles]
    handle = max(scored, key=lambda item: (item[1], item[2]))[0]
    logger.info("读图候选窗口（句柄, 主图分, 面积）: {}", scored)
    chart = next((win for win in windows if int(getattr(win, "_hWnd", 0) or 0) == handle), None)
    if chart is not None:
        try:
            left, top = int(chart.left), int(chart.top)
            width, height = int(chart.width), int(chart.height)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"读不到期货通窗口位置: {exc}") from exc
    else:
        from ctypes import wintypes

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect))
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
    force_foreground(handle)
    if not click:
        return handle
    import pyautogui

    # 主图大约在窗口中部偏右，避开左边合约栏和底部成交量。
    # 信息框多半靠悬停出现：点一下后把鼠标留在 K 线上，不要移走。
    x = left + int(width * 0.62)
    y = top + int(height * 0.42)
    pyautogui.moveTo(x, y, duration=0.12)
    pyautogui.click(x, y)
    time.sleep(0.35)
    pyautogui.moveTo(x, y)
    return handle


def scrape_ths_klines(
    cfg: dict[str, Any],
    symbol: str,
    period: str,
    *,
    limit: int = 200,
    end: Any = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    """
    打开已有的期货通主图，用方向键把信息框一根根读下来。

    请你先选好合约和周期（例如 5 分）。这里不会点买开/卖开。
    end 是这批要读到的结束时间：
    - 没填或就是现在 → 程序把光标推到最新一根再往回读，把库更新到当前；
    - 填的是历史时点 → 光标由你手动停在那根K线上，程序只预读确认、向右多读几根做余量。
    progress(已读根数, 计划根数, 最新读到的时间) 每根回调一次，给界面刷进度条。
    """
    import pyautogui

    ths_cfg = (cfg.get("data") or {}).get("ths_chart") or {}
    pause = float(ths_cfg.get("pause_sec", 0.45))
    ready = float(ths_cfg.get("ready_sec", 3))
    margin = max(0, int(ths_cfg.get("margin_bars", 3) or 0))
    manual = not _target_is_latest(end, period)
    begin_ths_scrape()
    _wait_for_ready(ready, ths_stop_requested)
    if ths_stop_requested():
        raise ThsScrapeStopped("已取消，还没有开始读图")
    hwnd = _focus_chart(cfg, click=not manual)
    # 让信息框先刷新，再开始读，免得第一根拿到切换前的旧值。
    time.sleep(0.6)
    for _ in range(12):
        if ths_stop_requested():
            raise ThsScrapeStopped("已取消，还没有开始读图")
        if parse_ths_tooltip(collect_chart_texts(hwnd)) is not None:
            break
        time.sleep(0.25)

    if manual:
        start_at = _manual_start(hwnd, end=end, period=period, margin=margin)
        logger.info("历史批次从 {} 开读，向右多读了 {} 根做接续", start_at, margin)
    else:
        # 点击落在哪一根不确定。先推到最右，从最新 K 线开始往回读，否则会漏掉较新的一段。
        _seek_latest(hwnd)
    if ths_stop_requested():
        raise ThsScrapeStopped("已取消，还没有开始读图")

    logged_miss: dict[str, Any] = {"done": False, "sample": ""}

    def _read() -> dict[str, Any] | None:
        blob = collect_chart_texts(hwnd)
        row = parse_ths_tooltip(blob)
        if row is None and not logged_miss["done"]:
            logged_miss["done"] = True
            logged_miss["sample"] = blob or ""
            sample = (blob or "（窗口里没扫到字）").replace("\n", " | ")[:300]
            logger.warning("还没从信息框读到开高低收。扫到的字：{}", sample)
        elif row is not None and float(row.get("volume") or 0) <= 0 and not logged_miss["done"]:
            logged_miss["done"] = True
            sample = (blob or "").replace("\n", " | ")[:300]
            logger.warning("读到开高低收，但成交量是 0。扫到的字：{}", sample)
        return row

    def _press(key: str) -> None:
        pyautogui.press(key)

    def _on_row(read_n: int, row: dict[str, Any]) -> None:
        if progress is None:
            return
        try:
            stamp = pd.Timestamp(row["datetime"]).strftime("%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            stamp = ""
        progress(read_n, limit, stamp)

    if progress is not None:
        progress(0, limit, "")
    logger.info("开始从同花顺图读取 {} {}，最多 {} 根。按 ESC 或点「停止读图」可停。", symbol, period, limit)
    old_pause = pyautogui.PAUSE
    pyautogui.PAUSE = 0.05
    try:
        rows = walk_tooltips(
            _read,
            _press,
            limit=limit,
            pause_sec=pause,
            should_stop=ths_stop_requested,
            on_read=_on_row,
        )
    finally:
        pyautogui.PAUSE = old_pause
    frame = bars_from_tooltips(rows)
    stopped = _THS_STOP.is_set()
    if frame is None or len(frame) == 0:
        if stopped:
            raise ThsScrapeStopped("已停止，还没有读到 K 线")
        title = _foreground_title()
        sample = str(logged_miss.get("sample") or "")
        detail = ""
        if sample:
            detail = "\n扫到的字：" + sample.replace("\n", " | ")[:300]
        raise RuntimeError(
            "同花顺图没有读到 K 线。请确认期货通主图已打开、周期已点好，"
            "鼠标能看到信息框（顶部是时间，里面有开盘/收盘/最高/最低）。"
            + detail
            + (f"\n当前前台窗口：{title}" if title else "")
        )
    if stopped:
        logger.info("已停止，先把已经读到的 {} 根存下来", len(frame))
    return frame


def scrape_ths_klines_range(
    cfg: dict[str, Any],
    symbol: str,
    period: str,
    start: Any = None,
    end: Any = None,
    *,
    max_bars: int = 5000,
    progress: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    """
    按「时段 + 周期」读图：先算这段大约要多少根，再逐根往回读。

    结束时间是最新的现在时，程序自己推到最新一根；填的是历史时点，则由你手动把
    十字光标停在那根K线上再开始，程序只预读确认并向右多读几根做接续余量。
    一批读不完可以改时段多读几批，库里按 (合约, 周期, 时间戳) 接续，重复的根覆盖。
    可能读到比开始时间更早的根，多存无害。读完校验周期是否和所选一致。
    """
    from src.data.coverage import ensure_period, expected_bar_count

    ths_cfg = (cfg.get("data") or {}).get("ths_chart") or {}
    cap = max(10, min(5000, int(max_bars or 5000)))
    configured = ths_cfg.get("max_bars")
    if configured:
        cap = max(10, min(cap, int(configured)))
    if start is not None and end is not None:
        want = expected_bar_count(start, end, period) + 10
        if want > cap:
            logger.warning(
                "这段时段约需 {} 根，超过单次读图上限 {} 根；这次只读最近 {} 根，"
                "剩下的可以再改时段分几次读，库会按时间接上。",
                want,
                cap,
                cap,
            )
            want = cap
    else:
        want = cap
    want = max(10, want)
    logger.info("按时段读图 {} {}：预计 {} 根，单次上限 {} 根", symbol, period, want, cap)
    frame = scrape_ths_klines(cfg, symbol, period, limit=want, end=end, progress=progress)
    ensure_period(frame, period)
    return frame


def _wait_for_ready(seconds: float, should_stop: Callable[[], bool]) -> None:
    """点按钮后先等几秒，让你把期货通主图放到前面。这段时间还不碰鼠标。"""
    remain = max(0.0, float(seconds))
    if remain <= 0:
        return
    logger.info(
        "请把期货通主图放到前面。{} 秒后开始读，按 ESC 或点「停止读图」可取消。",
        int(remain) if remain >= 1 else remain,
    )
    end = time.time() + remain
    while time.time() < end:
        if should_stop():
            raise ThsScrapeStopped("已取消，还没有开始读图")
        time.sleep(0.1)
