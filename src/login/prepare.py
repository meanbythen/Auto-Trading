"""
打开同花顺、登录、把价格改成对手价。

功能：这三步做完之前，不启动常驻扫描。
原理：登录只点「登录」按钮，改价格只点价格菜单里的「对手价」。
这两步都不点买开、卖开、平仓，所以不会下单。
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from loguru import logger


def startup_should_login(cfg: dict[str, Any]) -> bool:
    """
    常驻刚启动时要不要再登录一次。

    准备按钮已经打开软件并登录过时，配置里会带上 _startup_login_done。
    这时再登录会把「交易 → 期货交易」再点一遍，所以跳过。
    直接点「启动常驻」没有这个标记，仍然登录一次。
    """
    return not bool(cfg.get("_startup_login_done"))


def prepare_trading_desk(
    cfg: dict[str, Any],
    *,
    login: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    arm_price: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    confirm_settlement: Callable[[], bool] | None = None,
    dismiss_settlement: Callable[[], str] | None = None,
) -> tuple[bool, list[str]]:
    """
    拉起同花顺，自动登录，再把价格改成对手价。

    返回 (能不能接着启动常驻, 写给界面的几句话)。
    任何一步失败都返回 False，调用方就不要启动常驻。
    """
    lines: list[str] = []
    desk = copy.deepcopy(cfg)
    account = desk.setdefault("account", {})
    # 这个准备动作本身就是要登录。配置页的「自动登录」开关管的是普通启动。
    account["skip_auto_login"] = False

    login_fn = login or _login_with_saved_password
    try:
        ok, message = login_fn(desk)
    except Exception as exc:  # noqa: BLE001
        logger.exception("准备登录失败")
        lines.append(f"登录没有完成：{exc}")
        return False, lines
    lines.append(message)
    if not ok:
        lines.append("登录没完成，常驻不会启动，也不会下单。")
        return False, lines

    # 登录框关掉以后，结算单有时才弹出来。改对手价之前再看一次，避免窗口挡住价格框。
    confirm = confirm_settlement or _confirm_settlement
    try:
        settled = bool(confirm())
    except Exception as exc:  # noqa: BLE001
        logger.exception("准备时确认结算单失败")
        lines.append(f"没能点掉确认结算单：{exc}")
        return False, lines
    if not settled:
        lines.append("「确认结算单」窗口还在，没点到按钮。常驻不会启动。")
        return False, lines

    executor = str((desk.get("trade") or {}).get("executor", "sim"))
    if executor != "gui":
        lines.append("当前不是同花顺点击通道，这一步不改价格框。")
        return True, lines

    arm = arm_price or _arm_opponent_price
    dismiss = dismiss_settlement or _dismiss_settlement_state
    try:
        price_ok, price_msg = arm(desk)
    except Exception as exc:  # noqa: BLE001
        logger.exception("准备对手价失败")
        lines.append(f"没能改成对手价：{exc}")
        return False, lines
    lines.append(price_msg)
    # 结算单如果晚弹出，会盖住价格菜单，所以第一次会点不到对手价。
    # 这时再点一次确认结算单，点掉了就重新改对手价。仍然不点买卖。
    if not price_ok:
        try:
            state = dismiss()
        except Exception as exc:  # noqa: BLE001
            logger.exception("对手价失败后补点确认结算单失败")
            lines.append(f"没能点掉确认结算单：{exc}")
            state = "stuck"
        if state == "clicked":
            lines.append("确认结算单刚才挡着下单面板，已经点掉。再改一次对手价。")
            try:
                price_ok, price_msg = arm(desk)
            except Exception as exc:  # noqa: BLE001
                logger.exception("点掉结算单后改对手价失败")
                lines.append(f"没能改成对手价：{exc}")
                return False, lines
            lines.append(price_msg)
        elif state == "stuck":
            lines.append("「确认结算单」窗口还在，没点到按钮。常驻不会启动。")
            return False, lines
    if not price_ok:
        lines.append("价格没有改成对手价，常驻不会启动，避免接着去点买卖。")
        return False, lines
    lines.append("准备完成：只做了打开、登录和对手价，没有点买开、卖开或平仓。")
    return True, lines


def _confirm_settlement() -> bool:
    """再看一次结算单窗口。没有这个窗口也算通过。"""
    from src.login.ths_login import confirm_settlement_if_present

    return confirm_settlement_if_present(wait_sec=8)


def _dismiss_settlement_state() -> str:
    """对手价没点到时再看一眼结算单。返回 clicked、stuck 或 absent。"""
    from src.login.ths_login import _look_and_click_settlement

    return _look_and_click_settlement()


def _login_with_saved_password(cfg: dict[str, Any]) -> tuple[bool, str]:
    """用已经保存的密码登录。密码只交给登录框，不写进日志。"""
    from src.login import AppLoginManager

    manager = AppLoginManager(cfg)
    ok = bool(manager.login())
    if ok:
        return True, "同花顺已打开，并按保存的密码登录。没有点买卖按钮。"
    return False, "同花顺登录没有成功。请看登录框是不是还开着，或密码是不是空的。"


def _arm_opponent_price(cfg: dict[str, Any]) -> tuple[bool, str]:
    """只改价格菜单。这里不读取买开、卖开、平仓坐标。"""
    from src.trader.price_mode import ensure_opponent_price

    return ensure_opponent_price(cfg)
