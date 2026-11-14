"""
品种与合约总表扩充 / 刷新工具。

功能：
    1. 遍历天勤覆盖的全部 ~80 个期货品种（_EXCHANGES）。
    2. 问每个品种的当前主力合约（main_contract）。
    3. 顺手问每手数量、每跳（天勤行情有）。保证金比例天勤没有，请手填或从 CTP 抄。
    4. 旧表已有比例的沿用；只有旧每手保证金金额、又问到最新价时，用 金额÷(价×每手数量) 估一个比例。
    5. 旧表里没有的品种，先写进去（启用=否），留给你后续补比例。
    6. 整表重写 Excel（启用=是 的表示你关注）。保留原来的交易时段和清仓时刻。

用法（第一次跑需要天勤账号）：
    set TQ_USER=你的天勤手机号
    set TQ_PASSWORD=你的天勤密码
    python tools/refresh_universe.py

刷新后再跑一次：
    python -m pytest -q
确认 universe_table.py 的 apply_universe_table_to_cfg 仍正常载入。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# 确保项目根在路径上
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger

from src.data.tq_source import (
    EXCHANGE_BY_PRODUCT,
    instrument_specs,
    main_contract,
    reset_api,
)
from src.risk.universe_table import (
    _as_float,
    parse_margin_rate_pct,
    product_key,
    read_universe_table,
    resolve_universe_path,
    write_universe_table,
)
from src.utils import load_config, load_secrets


# —— 品种中文名、合理的默认乘数/每跳（旧表没填到时用）——
# 数值来源：中国期货市场常用值，若和实际不符以交易所为准。
# 保证金比例天勤拿不到，默认 0，请对照期货公司或同花顺填，例如 23 表示 23%。
_DEFAULT_NAME_MULT_TICK: dict[str, tuple[str, float, float]] = {
    # 上期所 SHFE
    "cu": ("铜", 5, 10.0),
    "al": ("铝", 5, 5.0),
    "zn": ("锌", 5, 5.0),
    "pb": ("铅", 5, 5.0),
    "ni": ("镍", 1, 10.0),
    "sn": ("锡", 1, 10.0),
    "au": ("黄金", 1000, 0.02),
    "ag": ("白银", 15, 1.0),
    "rb": ("螺纹钢", 10, 1.0),
    "wr": ("线材", 10, 1.0),
    "hc": ("热轧卷板", 10, 1.0),
    "ss": ("不锈钢", 5, 5.0),
    "bu": ("沥青", 10, 2.0),
    "ru": ("橡胶", 10, 5.0),
    "sp": ("纸浆", 10, 2.0),
    "fu": ("燃油", 10, 1.0),
    "ao": ("原油期权", 1000, 0.01),
    "br": ("二十号胶", 10, 5.0),
    "ad": ("铝期权", 5, 1.0),
    "op": ("原油期权", 1000, 0.01),

    # 能源所 INE
    "sc": ("原油", 1000, 0.1),
    "lu": ("低硫燃料油", 10, 1.0),
    "nr": ("20号胶", 10, 5.0),
    "bc": ("国际铜", 5, 5.0),
    "ec": ("集运指数", 50, 1.0),

    # 大商所 DCE
    "a": ("豆一", 10, 1.0),
    "b": ("豆二", 10, 1.0),
    "m": ("豆粕", 10, 1.0),
    "y": ("豆油", 10, 2.0),
    "p": ("棕榈油", 10, 2.0),
    "c": ("玉米", 10, 1.0),
    "cs": ("玉米淀粉", 10, 1.0),
    "jd": ("鸡蛋", 10, 1.0),
    "lh": ("生猪", 16, 5.0),
    "l": ("塑料", 5, 1.0),
    "v": ("PVC", 5, 1.0),
    "pp": ("聚丙烯", 5, 1.0),
    "j": ("焦炭", 100, 0.5),
    "jm": ("焦煤", 60, 0.5),
    "i": ("铁矿石", 100, 0.5),
    "eg": ("乙二醇", 10, 1.0),
    "eb": ("苯乙烯", 5, 1.0),
    "pg": ("LPG", 20, 1.0),
    "rr": ("粳米", 10, 1.0),
    "fb": ("纤维板", 500, 1.0),
    "bb": ("胶合板", 500, 1.0),
    "bz": ("豆粕期权", 10, 0.5),
    "lg": ("塑料期权", 5, 0.5),

    # 郑商所 CZCE
    "sr": ("白糖", 10, 1.0),
    "cf": ("棉花", 5, 5.0),
    "ta": ("PTA", 5, 2.0),
    "ma": ("甲醇", 10, 1.0),
    "fg": ("玻璃", 20, 1.0),
    "rm": ("菜粕", 10, 1.0),
    "oi": ("菜油", 10, 2.0),
    "sa": ("纯碱", 20, 1.0),
    "pf": ("短纤", 5, 2.0),
    "ur": ("尿素", 20, 1.0),
    "sm": ("锰硅", 5, 2.0),
    "sf": ("硅铁", 5, 2.0),
    "ap": ("苹果", 10, 1.0),
    "cj": ("红枣", 5, 5.0),
    "pk": ("花生", 5, 1.0),
    "cy": ("棉纱", 5, 5.0),
    "sh": ("烧碱", 30, 1.0),
    "px": ("对二甲苯", 5, 2.0),
    "pl": ("液化石油气", 20, 1.0),
    "pr": ("瓶片", 15, 2.0),

    # 中金所 CFFEX
    "if": ("沪深300股指", 300, 0.2),
    "ih": ("上证50股指", 300, 0.2),
    "ic": ("中证500股指", 200, 0.2),
    "im": ("中证1000股指", 200, 0.2),
    "t": ("10年国债", 10000, 0.005),
    "tf": ("5年国债", 10000, 0.005),
    "ts": ("2年国债", 10000, 0.005),
    "tl": ("30年国债", 10000, 0.01),

    # 广期所 GFEX
    "si": ("工业硅", 5, 5.0),
    "lc": ("碳酸锂", 1, 50.0),
    "ps": ("合成橡胶", 5, 2.0),
    "pt": ("铂", 1000, 0.1),
    "pd": ("钯", 1000, 0.1),
}


def _product_of_symbol(symbol: str) -> str:
    return product_key(symbol)


def _main_bars_contract_old_to_new(old_sym: str, new_sym: str) -> bool:
    """判断旧合约和新合约是不是同一个品种（仅合约月份变了）。"""
    return _product_of_symbol(old_sym) == _product_of_symbol(new_sym)


def refresh_universe_table(
    tq_user: str | None = None,
    tq_password: str | None = None,
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """刷新品种表：遍历全部品种问天勤当前主力合约。

    返回
    ----
    新的行列表（可以直接 write_universe_table 写回）。
    """
    old_rows: list[dict[str, Any]] = []
    path = resolve_universe_path()
    if path.exists():
        try:
            old_rows = read_universe_table(path)
            logger.info("已从旧表读入 {} 行", len(old_rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("旧表读取失败，将从零开始重建：{}", exc)

    # 旧表按品种分组（同一个品种可能有多行历史主力）
    old_by_product: dict[str, dict[str, Any]] = {}
    for row in old_rows:
        p = _product_of_symbol(str(row.get("symbol") or ""))
        if p and p not in old_by_product:
            old_by_product[p] = row

    tq_user = tq_user or os.environ.get("TQ_USER") or ""
    tq_password = tq_password or os.environ.get("TQ_PASSWORD") or ""

    # 再兜底：从 config.yaml → data.tq.user 和 secrets.local.yaml → tq_user/tq_password 读
    if not tq_user or not tq_password:
        try:
            cfg = load_config()
            tq_cfg = (cfg.get("data") or {}).get("tq") or {}
            if not tq_user:
                tq_user = str(tq_cfg.get("user") or "").strip()
            if not tq_password:
                tq_password = str(tq_cfg.get("password") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    if not tq_user or not tq_password:
        try:
            sec = load_secrets()
            if not tq_user:
                tq_user = str(sec.get("tq_user") or "").strip()
            if not tq_password:
                tq_password = str(sec.get("tq_password") or "").strip()
        except Exception:  # noqa: BLE001
            pass

    if tq_user and tq_password:
        logger.info("已读取天勤账号: {} / {}…", tq_user, tq_password[:2] + "**" if len(tq_password) > 2 else "**")
    else:
        logger.warning("未检测到天勤账号密码：请在 GUI 配置页「天勤行情账号/密码」填好，或设环境变量 TQ_USER / TQ_PASSWORD")

    all_products = sorted(EXCHANGE_BY_PRODUCT.keys())
    logger.info("开始刷新，共 {} 个品种", len(all_products))

    new_rows: list[dict[str, Any]] = []
    success = 0
    no_tq = 0
    failed = 0
    fallback = 0

    for product in all_products:
        exchange = EXCHANGE_BY_PRODUCT.get(product, "")
        logger.debug("--- 品种 {} ({}) ---", product, exchange)

        # 1. 尝试问天勤当前主力
        current_main: str | None = None
        if tq_user and tq_password:
            try:
                current_main = main_contract(product, user=tq_user, password=tq_password)
                if current_main:
                    success += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("  天勤查询失败 {}: {}", product, exc)
                failed += 1
                current_main = None
        else:
            no_tq += 1
            current_main = None

        # 2. 拿旧表里已有的比例/每手数量/手续费/时段
        old = old_by_product.get(product)
        old_symbol = str(old.get("symbol") or "") if old else ""

        # 3. 决定新的合约代码
        new_symbol = current_main or old_symbol or product

        # 4. 拼新行
        if old:
            margin = max(0.0, _as_float(old.get("margin"), 0.0))
            rate = parse_margin_rate_pct(old.get("margin_rate_pct"))
            multiplier = max(0.1, _as_float(old.get("multiplier"), 10.0))
            commission = max(0.0, _as_float(old.get("commission"), 0.0))
            today = old.get("commission_close_today")
            price_tick = old.get("price_tick")
            name = str(old.get("name") or product)
            enabled = bool(old.get("enabled", True))
            trading_sessions = str(old.get("trading_sessions") or "")
            flatten_times = str(old.get("flatten_times") or "")
        else:
            name, multiplier, price_tick = _DEFAULT_NAME_MULT_TICK.get(
                product, (product, 10.0, 1.0)
            )
            margin = 0.0
            rate = 0.0
            commission = 0.0
            today = None
            enabled = False
            trading_sessions = ""
            flatten_times = ""

        # 天勤能给每手数量、每跳、最新价；给不了期货公司保证金比例。
        if current_main and tq_user and tq_password:
            try:
                specs = instrument_specs(
                    current_main, user=tq_user, password=tq_password
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("  合约资料失败 {}: {}", product, exc)
                specs = {}
            qty = float(specs.get("volume_multiple") or 0)
            tick = float(specs.get("price_tick") or 0)
            last = float(specs.get("last_price") or 0)
            if qty > 0:
                multiplier = qty
            if tick > 0:
                price_tick = tick
            # 旧表只有固定金额时，用现价估一个比例，方便以后随价格变。
            if rate <= 0 and margin > 0 and last > 0 and multiplier > 0:
                guessed = margin / (last * multiplier) * 100.0
                if 1.0 < guessed <= 80.0:
                    rate = round(guessed, 2)
                    logger.info(
                        "  {} 用旧保证金{:g}元 / (价{:g}×每手{:g}) 估比例 {:g}%",
                        product,
                        margin,
                        last,
                        multiplier,
                        rate,
                    )
                    margin = 0.0

        # 如果今天主力没变 → 保留原启用状态 + 说明
        if current_main and old and old_symbol and old_symbol.lower() == current_main.lower():
            comment = "主力未变"
        elif current_main and old and old_symbol and _product_of_symbol(old_symbol) == product:
            comment = f"主力切换：{old_symbol} → {current_main}"
        elif current_main:
            comment = "主力合约"
        else:
            comment = "主力未知（无天勤账号），保留/用品种代码占位"
            fallback += 1

        new_rows.append(
            {
                "symbol": new_symbol,
                "name": name,
                "margin_rate_pct": rate,
                "margin": margin,
                "multiplier": multiplier,
                "commission": commission,
                "commission_close_today": today,
                "price_tick": price_tick,
                "trading_sessions": trading_sessions,
                "flatten_times": flatten_times,
                "enabled": enabled,
                "_source": comment,
            }
        )

    logger.info(
        "刷新完成：天勤主力查询成功={}，无账号跳过={}，查询失败={}，占位={}",
        success,
        no_tq,
        failed,
        fallback,
    )

    if not dry_run:
        if any("_source" in r for r in new_rows):
            # _source 是内部字段，写表前删掉
            for r in new_rows:
                r.pop("_source", None)
        write_universe_table(path, new_rows)
        logger.info("新表已写入: {}", path)

    reset_api()
    return new_rows


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    rows = refresh_universe_table(dry_run=dry)
    # 打印一行汇总
    enabled = sum(1 for r in rows if r.get("enabled", True))
    print(f"\n共 {len(rows)} 个品种，启用={enabled}，停用={len(rows) - enabled}")
    if dry:
        print("（--dry-run 未写入 Excel）")
