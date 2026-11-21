"""
CTP 前置地址列表。

功能：一份档案可以写多台交易/行情前置。连接时全部登记给柜台，
      某一台挂了，柜台会在已登记的地址里换下一台，不用人改配置、也不用重启程序。
原理：CTP 的 RegisterFront 可以连着调用多次，再 Init。这是接口自带的热备，
      不是程序自己掐断再拨号。地址去重、顺序保留：写在最前面的优先连。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from src.trader.ctp_gateway import probe_front_tcp


def normalize_front(value: Any) -> str:
    """收成 tcp://主机:端口。空的或看不懂的返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    from src.domain import trading_account as ta

    if ta.is_self_test_front(text):
        return ta.SELF_TEST_FRONT
    parsed = urlparse(text if "://" in text else f"tcp://{text}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return ""
    scheme = (parsed.scheme or "tcp").lower()
    if scheme not in {"tcp", "udp"}:
        scheme = "tcp"
    return f"{scheme}://{host}:{int(port)}"


def split_fronts(*values: Any) -> list[str]:
    """
    把配置里的前置收成去重后的列表，先写的在前。

    可以是一句、用分号/逗号/换行隔开的多句，或 YAML 列表。
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                _add(child)
            return
        text = str(item).strip()
        if not text:
            return
        parts = (
            text.replace("；", ";")
            .replace("，", ",")
            .replace("\r", "\n")
            .replace("\n", ";")
            .replace(",", ";")
        )
        for piece in parts.split(";"):
            front = normalize_front(piece)
            if not front or front in seen:
                continue
            seen.add(front)
            found.append(front)

    for value in values:
        _add(value)
    return found


def dump_front_field(fronts: list[str]) -> str | list[str]:
    """写回配置：一台仍用字符串，多台写成列表，方便对照。"""
    if not fronts:
        return ""
    if len(fronts) == 1:
        return fronts[0]
    return list(fronts)


def format_fronts_ui(fronts: list[str] | str | None) -> str:
    """配置页单行输入框：多台用分号隔开。"""
    items = split_fronts(fronts)
    return "; ".join(items)


def collect_fronts(*values: Any) -> list[str]:
    """档案主前置 + 备用列表合成一份，顺序就是柜台尝试的顺序。"""
    return split_fronts(*values)


def register_fronts(api: Any, fronts: list[str]) -> int:
    """
    把列表里的前置全部登记。返回实际登记了几台。

    必须在 Init 之前调用。柜台连上或断线重连时，会在这些地址里换。
    """
    count = 0
    register = getattr(api, "RegisterFront", None)
    if not callable(register):
        return 0
    for front in fronts:
        text = normalize_front(front)
        if not text:
            continue
        register(text)
        count += 1
    return count


def probe_front_list(fronts: list[str], timeout_sec: float = 3.0) -> list[tuple[str, bool | None]]:
    """逐台测端口，不登录。结果按配置顺序。"""
    rows: list[tuple[str, bool | None]] = []
    for front in split_fronts(fronts):
        rows.append((front, probe_front_tcp(front, timeout_sec=timeout_sec)))
    return rows
