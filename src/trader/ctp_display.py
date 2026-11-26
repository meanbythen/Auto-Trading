"""
交易台表格用的柜台数字翻译。

功能：把资金、持仓、合约资料收成界面能看懂的字。
原理：只做换算和中文名称，不连柜台。柜台没给的字段显示成空或横杠，不编造。
"""

from __future__ import annotations

from typing import Any

from src.trader.ctp_logic import PositionView, position_avg_price, product_code, symbol_key

EXCHANGE_CN = {
    "SHFE": "上期所",
    "DCE": "大商所",
    "CZCE": "郑商所",
    "CFFEX": "中金所",
    "INE": "上期能源",
    "GFEX": "广期所",
}

PRODUCT_CLASS_CN = {
    "1": "期货",
    "2": "期权",
    "3": "组合",
    "4": "即期",
    "5": "期转现",
    "6": "现货期权",
}

# 投保：1 投机，3 套保，2 套利。仿真几乎都是投机。
HEDGE_CN = {"1": "投机", "3": "套保", "2": "套利"}


def exchange_cn(code: str) -> str:
    """SHFE -> 上期所。不认识的原样显示。"""
    key = str(code or "").strip().upper()
    return EXCHANGE_CN.get(key, key or "—")


def product_class_cn(code: str) -> str:
    flag = str(code or "").strip()[:1]
    return PRODUCT_CLASS_CN.get(flag, "期货" if not flag else flag)


def hedge_cn(code: str) -> str:
    flag = str(code or "").strip()[:1]
    return HEDGE_CN.get(flag, "投机" if not flag else flag)


def _currency_cn(code: str) -> str:
    flag = str(code or "").strip().upper()
    if flag in {"", "CNY", "RMB"}:
        return "人民币"
    return str(code)


def money_text(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def pct_from_ratio(ratio: float) -> str:
    """0.13 -> 13%。0 表示柜台没给，留空。"""
    if abs(float(ratio or 0)) < 1e-12:
        return ""
    return f"{float(ratio) * 100:.0f}%" if abs(ratio) >= 0.001 else f"{float(ratio) * 100:.4f}%"


def fee_yuan_per_lot(
    by_volume: float,
    by_money: float,
    *,
    last_price: float,
    multiplier: float,
) -> float | None:
    """
    把柜台手续费收成「每手多少元」，好写入品种表。

    柜台两套：按成交额比例、按手数金额。品种表只认每手元。
    只有比例、又没有最新价时，算不出来，返回空，不要把 0.0002 当成 2 毫元写进去。
    """
    vol = float(by_volume or 0)
    money = float(by_money or 0)
    last = float(last_price or 0)
    mult = float(multiplier or 0)
    if abs(money) > 1e-12:
        if last <= 0 or mult <= 0:
            return None
        extra = vol if abs(vol) >= 0.01 else 0.0
        return round(last * mult * money + extra, 4)
    if abs(vol) >= 0.01:
        return round(vol, 4)
    if abs(vol) < 1e-12 and abs(money) < 1e-12:
        return 0.0
    return None


def ctp_row_to_universe_costs(row: dict[str, Any] | None, last_price: float) -> tuple[dict[str, Any], str]:
    """
    柜台合约行 → 品种表要改的数字。

    不改启用、交易时段、清仓时刻。手续费按当前最新价估成每手元。
    """
    if not row:
        return {}, "还没有合约资料。"
    mult = float(row.get("volume_multiple") or 0)
    tick = float(row.get("price_tick") or 0)
    long_m = float(row.get("long_margin_money") or 0)
    short_m = float(row.get("short_margin_money") or 0)
    ratio = max(long_m, short_m)
    fields: dict[str, Any] = {
        "symbol": str(row.get("symbol") or "").strip(),
        "name": str(row.get("name") or "").strip(),
    }
    if mult > 0:
        fields["multiplier"] = mult
    if tick > 0:
        fields["price_tick"] = tick
    if ratio > 0:
        fields["margin_rate_pct"] = round(ratio * 100.0, 4)
    notes: list[str] = []
    from src.domain.options import is_option, option_kind

    symbol_text = str(fields.get("symbol") or "")
    product_class = str(row.get("product_class") or "")
    if is_option(symbol_text) or product_class in {"2", "Options", "期权"}:
        fields["instrument_type"] = "期权"
        underlying = str(row.get("underlying_instr_id") or "").strip()
        if underlying:
            fields["underlying"] = underlying
        expire = str(row.get("expire_date") or "").strip()
        if expire:
            fields["expiry"] = expire
        strike = float(row.get("strike_price") or 0)
        if strike > 0:
            fields["strike"] = strike
        options_type = str(row.get("options_type") or "").strip()
        kind = "C" if options_type == "1" else "P" if options_type == "2" else option_kind(symbol_text)
        if kind:
            fields["option_type"] = kind
    open_fee = fee_yuan_per_lot(
        float(row.get("open_volume") or 0),
        float(row.get("open_money") or 0),
        last_price=last_price,
        multiplier=mult,
    )
    close_fee = fee_yuan_per_lot(
        float(row.get("close_volume") or 0),
        float(row.get("close_money") or 0),
        last_price=last_price,
        multiplier=mult,
    )
    today_fee = fee_yuan_per_lot(
        float(row.get("close_today_volume") or 0),
        float(row.get("close_today_money") or 0),
        last_price=last_price,
        multiplier=mult,
    )
    if open_fee is None or close_fee is None or today_fee is None:
        notes.append("手续费是成交额比例，窗口上没有最新价，这一次先不改手续费。请先订阅行情再点写入。")
    else:
        fields["commission"] = open_fee
        fields["commission_close"] = close_fee
        fields["commission_close_today"] = today_fee
    bits = [
        f"{fields.get('symbol') or '合约'}",
        f"每手{int(mult) if mult else '—'}",
        f"最小变动{tick:g}" if tick else "最小变动—",
        f"保证金{fields['margin_rate_pct']:g}%" if "margin_rate_pct" in fields else "保证金—",
    ]
    if "commission" in fields:
        bits.append(f"开仓约{fields['commission']:g}元/手")
        bits.append(f"平仓约{fields['commission_close']:g}元/手")
        bits.append(f"平今约{fields['commission_close_today']:g}元/手")
    notes.insert(0, "、".join(bits))
    return fields, "。".join(notes)


def fee_text(by_volume: float, by_money: float) -> str:
    """
    手续费：按手是金额，按金额是比率。

    同花顺两列分开。这里有数就显示，两个都是 0 就空着。
    """
    vol = float(by_volume or 0)
    money = float(by_money or 0)
    if abs(vol) < 1e-12 and abs(money) < 1e-12:
        return ""
    if abs(vol) >= 1e-12 and abs(money) < 1e-12:
        return f"{vol:g}"
    if abs(money) >= 1e-12 and abs(vol) < 1e-12:
        return f"{money * 100:.8f}%"
    return f"{vol:g} / {money * 100:.8f}%"


def _account_num(raw: dict[str, Any], key: str) -> float:
    """资金字段转成数字。缺的或写坏的当 0。"""
    try:
        return float(raw.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_account_key(raw: dict[str, Any], key: str) -> bool:
    """这个键是不是柜台或账本里真正有的，避免 0 和「没给」分不清。"""
    return key in raw and raw.get(key) is not None


def cash_in_out(raw: dict[str, Any] | None) -> tuple[float, float, float]:
    """
    入金、出金、可取资金。

    柜台原始回报：WithdrawQuota 是可取，Withdraw 是出金。
    账本里已经翻译过：withdraw 是可取，withdraw_amt 是出金。
    这两套不能再交叉减一次，否则可取资金会被当成整笔出金。
    """
    data = raw if isinstance(raw, dict) else {}
    deposit = _account_num(data, "deposit")
    if _has_account_key(data, "withdraw_quota"):
        quota = _account_num(data, "withdraw_quota")
        withdrawn = (
            _account_num(data, "withdraw_amt")
            if _has_account_key(data, "withdraw_amt")
            else _account_num(data, "withdraw")
        )
    else:
        quota = _account_num(data, "withdraw")
        withdrawn = _account_num(data, "withdraw_amt")
    return deposit, withdrawn, quota


def parse_account(row: dict[str, Any] | None) -> dict[str, float | str]:
    """把资金查询收成统一键名。缺的当 0。同一份数字再解析一遍结果不变。"""
    raw = row or {}

    def num(key: str) -> float:
        return _account_num(raw, key)

    balance = num("balance")
    margin = num("margin")
    close_pnl = num("close_profit")
    float_pnl = num("position_profit")
    deposit, withdrawn, quota = cash_in_out(raw)
    usage = (margin / balance) if abs(balance) > 1e-9 else 0.0
    close_rate = (close_pnl / balance) if abs(balance) > 1e-9 else 0.0
    float_rate = (float_pnl / balance) if abs(balance) > 1e-9 else 0.0
    return {
        "account_id": str(raw.get("account_id") or raw.get("user_id") or ""),
        "pre_balance": num("pre_balance"),
        "balance": balance,
        "available": num("available"),
        "withdraw": quota,
        "withdraw_quota": quota,
        "currency": _currency_cn(str(raw.get("currency") or "")),
        "deposit": deposit,
        "withdraw_amt": withdrawn,
        "margin": margin,
        "frozen": num("frozen_cash") + num("frozen_margin"),
        "frozen_margin": num("frozen_margin"),
        "frozen_cash": num("frozen_cash"),
        "commission": num("commission"),
        "close_profit": close_pnl,
        "position_profit": float_pnl,
        "usage": usage,
        "close_rate": close_rate,
        "float_rate": float_rate,
        "mortgage_in": num("mortgage_in"),
        "mortgage_out": num("mortgage_out"),
        "option_value": num("option_value"),
    }


def account_headline(account: dict[str, Any] | None) -> str:
    """窗口最上面那一行：权益、可用、使用率。"""
    data = parse_account(account)
    if abs(float(data["balance"])) < 1e-9 and abs(float(data["available"])) < 1e-9:
        return "资金还没查到。登录后点「刷新持仓」，或打开资金页。"
    return (
        f"动态权益 {float(data['balance']):.2f}    "
        f"可用资金 {float(data['available']):.2f}    "
        f"资金使用率 {float(data['usage']) * 100:.2f}%"
    )


def account_pairs(account: dict[str, Any] | None) -> list[tuple[str, str]]:
    """资金页左右两列，和同花顺常用项对齐。质押/期权先显示柜台原值。"""
    data = parse_account(account)
    raw = account if isinstance(account, dict) else {}
    account_no = str(data.get("account_id") or raw.get("account_id") or raw.get("user_id") or "") or "—"
    return [
        ("资金账号", account_no),
        ("期初权益", money_text(float(data["pre_balance"]))),
        ("市种", str(data["currency"])),
        ("动态权益", money_text(float(data["balance"]))),
        ("期权市值", money_text(float(data["option_value"]))),
        ("货币质入", money_text(float(data["mortgage_in"]))),
        ("市值权益", money_text(float(data["balance"]))),
        ("货币质出", money_text(float(data["mortgage_out"]))),
        ("保证金", money_text(float(data["margin"]))),
        ("出入金", money_text(float(data["deposit"]) - float(data["withdraw_amt"]))),
        # 出入金 = 入金 − 出金。可取资金在下一列，不能拿来减。
        ("冻结资金", money_text(float(data["frozen"]))),
        ("手续费", money_text(float(data["commission"]))),
        ("资金使用率", f"{float(data['usage']) * 100:.2f}%"),
        ("可用资金", money_text(float(data["available"]))),
        ("平仓盈亏（率）", f"{float(data['close_profit']):.2f}（{float(data['close_rate']) * 100:.2f}%）"),
        ("可取资金", money_text(float(data["withdraw"]))),
        ("盯市浮盈（率）", f"{float(data['position_profit']):.2f}（{float(data['float_rate']) * 100:.2f}%）"),
    ]


def position_extra(
    view: PositionView,
    *,
    multiplier: float,
    last: float | None = None,
    upper: float | None = None,
    lower: float | None = None,
    balance: float = 0.0,
    side: str = "",
) -> dict[str, str]:
    """持仓表里均价、浮盈以外的几列。没有最新价的先空着。side=long/short 时只算这一边。"""
    long_qty = int(view.long_today) + int(view.long_yesterday)
    short_qty = int(view.short_today) + int(view.short_yesterday)
    want = str(side or "").strip().lower()
    if want == "short":
        lots = short_qty
        avg = position_avg_price(view.short_open_cost, short_qty, multiplier)
        margin = float(view.short_margin)
        is_long = False
    elif want == "long":
        lots = long_qty
        avg = position_avg_price(view.long_open_cost, long_qty, multiplier)
        margin = float(view.long_margin)
        is_long = True
    else:
        lots = long_qty + short_qty
        avg = position_avg_price(view.long_open_cost, long_qty, multiplier) if long_qty else position_avg_price(
            view.short_open_cost, short_qty, multiplier
        )
        margin = float(view.long_margin) + float(view.short_margin)
        is_long = long_qty > 0
    last_px = float(last or 0)
    upper_px = float(upper or 0)
    lower_px = float(lower or 0)
    value = last_px * lots * multiplier if last_px > 0 and lots > 0 else 0.0
    spread = ""
    ratio = ""
    limit_pnl = ""
    if last_px > 0 and avg:
        if is_long:
            spread = f"{last_px - avg:g}"
            ratio = f"{(last_px / avg - 1.0) * 100:.2f}%"
            if upper_px > 0:
                limit_pnl = f"{(upper_px - avg) * lots * multiplier:.2f}"
        else:
            spread = f"{avg - last_px:g}"
            ratio = f"{(avg / last_px - 1.0) * 100:.2f}%" if last_px else ""
            cap = lower_px if lower_px > 0 else upper_px
            if cap > 0:
                limit_pnl = f"{(avg - cap) * lots * multiplier:.2f}"
    share = f"{margin / balance * 100:.2f}%" if balance > 0 and margin > 0 else ""
    return {
        "margin": money_text(margin) if margin else "",
        "value": money_text(value) if value else "",
        "spread": spread,
        "ratio": ratio,
        "limit_pnl": limit_pnl,
        "share": share,
        "hedge": hedge_cn(view.hedge),
    }


def split_position_rows(
    name: str,
    view: PositionView,
    *,
    multiplier: float,
    last: float | None = None,
    upper: float | None = None,
    lower: float | None = None,
    balance: float = 0.0,
) -> list[dict[str, str]]:
    """
    同一合约的买开、卖开拆成两行。

    同花顺也是多空分行。锁仓时两行都在，均价和浮盈各自计算。
    """
    rows: list[dict[str, str]] = []
    long_qty = int(view.long_today) + int(view.long_yesterday)
    short_qty = int(view.short_today) + int(view.short_yesterday)
    if long_qty > 0:
        extra = position_extra(
            view,
            multiplier=multiplier,
            last=last,
            upper=upper,
            lower=lower,
            balance=balance,
            side="long",
        )
        avg = position_avg_price(view.long_open_cost, long_qty, multiplier)
        rows.append(
            {
                "symbol": name,
                "side": "买",
                "lots": str(long_qty),
                "today": str(int(view.long_today)),
                "yesterday": str(int(view.long_yesterday)),
                "avg": f"{avg:g}" if avg is not None else "—",
                "pnl": f"{float(view.long_profit):+.2f}",
                **extra,
            }
        )
    if short_qty > 0:
        extra = position_extra(
            view,
            multiplier=multiplier,
            last=last,
            upper=upper,
            lower=lower,
            balance=balance,
            side="short",
        )
        avg = position_avg_price(view.short_open_cost, short_qty, multiplier)
        rows.append(
            {
                "symbol": name,
                "side": "卖",
                "lots": str(short_qty),
                "today": str(int(view.short_today)),
                "yesterday": str(int(view.short_yesterday)),
                "avg": f"{avg:g}" if avg is not None else "—",
                "pnl": f"{float(view.short_profit):+.2f}",
                **extra,
            }
        )
    return rows


def _sum_cell(text: Any) -> float:
    """把表里的数字、带正负号或百分号的字加起来。横线当 0。"""
    raw = str(text or "").strip().replace(",", "").replace("%", "").replace("+", "")
    if not raw or raw in {"—", "-", "合计"}:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _sum_money(values: list[Any], signed: bool = False) -> str:
    total = sum(_sum_cell(item) for item in values)
    if abs(total) < 1e-9:
        return "0.00" if not signed else "+0.00"
    if signed:
        return f"{total:+.2f}"
    return f"{total:.2f}"


def position_footer(rows: list[dict[str, str]]) -> tuple[str, ...]:
    """持仓底栏：手数、今昨仓、浮盈、保证金、市值、涨停浮盈、资金占比可加总。均价不能加。"""
    share = sum(_sum_cell(item.get("share")) for item in rows)
    return (
        "合计",
        "",
        str(sum(int(_sum_cell(item.get("lots"))) for item in rows)),
        str(sum(int(_sum_cell(item.get("today"))) for item in rows)),
        str(sum(int(_sum_cell(item.get("yesterday"))) for item in rows)),
        "—",
        _sum_money([item.get("pnl") for item in rows], signed=True),
        _sum_money([item.get("margin") for item in rows]),
        _sum_money([item.get("value") for item in rows]),
        "—",
        "—",
        _sum_money([item.get("limit_pnl") for item in rows], signed=True),
        f"{share:.2f}%",
        "",
    )


def trade_footer(rows: list[tuple[str, ...]]) -> tuple[str, ...]:
    """成交底栏：成交量、平仓盈亏、手续费可加总。成交价不能加。"""
    volumes = [row[5] if len(row) > 5 else 0 for row in rows]
    pnls = [row[6] if len(row) > 6 else 0 for row in rows]
    fees = [row[7] if len(row) > 7 else 0 for row in rows]
    return (
        "合计",
        f"{len(rows)}笔",
        "",
        "",
        "—",
        str(sum(int(_sum_cell(item)) for item in volumes)),
        _sum_money(pnls, signed=True),
        _sum_money(fees),
        "",
        "",
        "",
    )


def remaining_volume(order: Any) -> int:
    """还能成交的手数。柜台给过剩余就用剩余，否则用委托量减已成交。"""
    left = int(getattr(order, "remaining", 0) or 0)
    if left > 0:
        return left
    return max(0, int(getattr(order, "volume", 0) or 0) - int(getattr(order, "filled", 0) or 0))


def cancelled_volume(order: Any) -> int:
    """已撤手数：终态且还没成交完的那一截。"""
    status = str(getattr(order, "status", "") or "")
    if status != "cancelled":
        return 0
    return remaining_volume(order)


def order_matches_filter(order: Any, kind: str) -> bool:
    """委托表筛选：全部 / 可撤 / 已撤 / 成交。"""
    status = str(getattr(order, "status", "") or "")
    if kind == "可撤":
        return status in {"pending", "submitted", "partial"}
    if kind == "已撤":
        return status == "cancelled"
    if kind == "成交":
        return status == "filled"
    return True


def contract_search_key(text: str, fallback_symbol: str = "") -> str:
    """
    查询合约时填给柜台的代码。

    空着就用当前下单合约的字母前缀，避免一次把全市场都拉回来。
    """
    raw = str(text or "").strip()
    if raw:
        return raw
    return product_code(fallback_symbol)


def format_contract_row(row: dict[str, Any]) -> tuple[str, ...]:
    """合约表一行，列顺序和同花顺接近。"""
    return (
        str(row.get("symbol") or ""),
        str(row.get("name") or ""),
        exchange_cn(str(row.get("exchange") or "")),
        product_class_cn(str(row.get("product_class") or "")),
        str(int(row.get("volume_multiple") or 0) or ""),
        _tick_text(row.get("price_tick")),
        pct_from_ratio(float(row.get("long_margin_money") or 0)),
        pct_from_ratio(float(row.get("short_margin_money") or 0)),
        fee_text(float(row.get("open_volume") or 0), float(row.get("open_money") or 0)),
        fee_text(float(row.get("close_volume") or 0), float(row.get("close_money") or 0)),
        fee_text(float(row.get("close_today_volume") or 0), float(row.get("close_today_money") or 0)),
        _rate_only(float(row.get("open_money") or 0)),
        _rate_only(float(row.get("close_money") or 0)),
        _rate_only(float(row.get("close_today_money") or 0)),
        str(row.get("open_date") or ""),
    )


def _tick_text(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{number:g}"


def _rate_only(money_ratio: float) -> str:
    if abs(float(money_ratio or 0)) < 1e-12:
        return ""
    return f"{float(money_ratio) * 100:.8f}%"


def describe_contract(row: dict[str, Any] | None) -> str:
    """点中一行后，用一句话把合约资料念出来。"""
    if not row:
        return "没有合约资料。"
    name = str(row.get("name") or "") or str(row.get("symbol") or "")
    bits = [
        f"{row.get('symbol')} {name}",
        exchange_cn(str(row.get("exchange") or "")),
        f"每手 {int(row.get('volume_multiple') or 0) or '—'}",
        f"最小变动 { _tick_text(row.get('price_tick')) or '—'}",
    ]
    long_m = pct_from_ratio(float(row.get("long_margin_money") or 0))
    if long_m:
        bits.append(f"多头保证金 {long_m}")
    short_m = pct_from_ratio(float(row.get("short_margin_money") or 0))
    if short_m:
        bits.append(f"空头保证金 {short_m}")
    open_fee = fee_text(float(row.get("open_volume") or 0), float(row.get("open_money") or 0))
    if open_fee:
        bits.append(f"开仓手续费 {open_fee}")
    close_fee = fee_text(float(row.get("close_volume") or 0), float(row.get("close_money") or 0))
    if close_fee:
        bits.append(f"平仓手续费 {close_fee}")
    close_today = fee_text(float(row.get("close_today_volume") or 0), float(row.get("close_today_money") or 0))
    if close_today:
        bits.append(f"平今 {close_today}")
    if row.get("open_date"):
        bits.append(f"上市 {row.get('open_date')}")
    return "；".join(bits)


def merge_contract(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """把保证金、手续费查询叠进同一条合约。空值不覆盖已有数字。"""
    out = dict(base)
    for key, value in extra.items():
        if value in (None, "", 0, 0.0):
            continue
        out[key] = value
    return out


def same_contract(left: str, right: str) -> bool:
    return symbol_key(left) == symbol_key(right)
