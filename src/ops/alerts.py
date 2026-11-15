"""
运行告警（记下来，方便以后接微信/邮件）

功能：熔断、窗口丢失、柜台没连上这类「人必须看一眼」的事，写成 data/alerts.jsonl。
原理：同一条告警十分钟内不重复写，避免扫描每几秒刷爆文件。
     这一层只记事，不改开平仓。界面运行卡可以从快照里读最近一条。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

FILE_NAME = "alerts.jsonl"
_COOLDOWN_SEC = 600.0
_last_emit: dict[str, float] = {}
# 当前这次运行的标识，由 TradingBot 启动时设置；没设置时写空串。
_run_id: str = ""
# 熔断告警正文里「已超过限额 100」这一段数字，用来对照现在配置页填的日最大亏损。
_HALT_LIMIT_RE = re.compile(r"已超过限额\s*([0-9]+(?:\.[0-9]+)?)")


def alerts_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


def set_run_id(run_id: str | None) -> None:
    """设置当前运行标识，之后写的告警都会带上它。"""
    global _run_id
    _run_id = str(run_id or "")


def current_run_id() -> str:
    """当前运行标识；还没设置时是空串。"""
    return _run_id


def emit_alert(
    code: str,
    message: str,
    *,
    level: str = "warning",
    root: Path | str | None = None,
) -> bool:
    """
    记一条告警。返回 True 表示这次真的写进了文件。

    code 用英文短名（halt / window_lost / ctp_down / reconcile），方便以后筛选。
    """
    from src.utils.paths import project_root

    key = f"{code}|{message}"
    now = datetime.now().timestamp()
    prev = _last_emit.get(key, 0.0)
    if now - prev < _COOLDOWN_SEC:
        return False
    _last_emit[key] = now
    base = Path(root) if root else project_root()
    path = alerts_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": str(level or "warning"),
        "code": str(code or ""),
        "message": str(message or ""),
        "run_id": _run_id,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.warning("告警 [{}] {}", code, message)
    # 外发只入队，失败由 notify 自己降级，不能影响告警写入。
    try:
        from src.ops.notify import notify

        notify(row)
    except Exception:  # noqa: BLE001
        logger.debug("告警外发入队失败，忽略")
    return True


def halt_limit_in_message(message: str) -> float | None:
    """
    从日亏熔断那句话里取出当时的限额。

    正文类似「当日亏损 -150.00 已超过限额 100」。对不上现在配置的，
    就当过期记录，不再拿来吓人。
    """
    match = _HALT_LIMIT_RE.search(str(message or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def is_stale_halt(row: dict[str, Any] | None, max_daily_loss: float | None) -> bool:
    """
    是不是「当时限额」已经不是配置页上那个数的熔断告警。

    只动 code=halt 的行。对账、窗口丢失这类不删。没传现在限额时也不删，
    避免误清。
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("code") or "") != "halt":
        return False
    if max_daily_loss is None:
        return False
    parsed = halt_limit_in_message(str(row.get("message") or ""))
    if parsed is None:
        return False
    # 50 和 50.0 算同一个数；100 对 3000 就算过期。
    return abs(parsed - float(max_daily_loss)) > 0.5


def prune_stale_halt_alerts(root: Path | str | None, max_daily_loss: float | None) -> int:
    """
    从 alerts.jsonl 里删掉限额已经对不上现在配置的日亏熔断行。

    返回删了几条。文件没有、或现在限额读不出来时，什么也不改。
    原理：一行一行读，过期 halt 丢掉，其它原样写回。只在真有删减时才覆盖，
    避免运行台每几秒空写一遍。
    """
    if max_daily_loss is None:
        return 0
    from src.utils.paths import project_root

    path = alerts_path(Path(root) if root else project_root())
    if not path.exists():
        return 0
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    dropped = 0
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)
            continue
        if isinstance(row, dict) and is_stale_halt(row, max_daily_loss):
            dropped += 1
            continue
        kept.append(json.dumps(row, ensure_ascii=False) if isinstance(row, dict) else stripped)
    if dropped <= 0:
        return 0
    text = "\n".join(kept)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    logger.debug("已删掉 {} 条限额已过期的日亏熔断告警，现限额 {:.0f}", dropped, float(max_daily_loss))
    return dropped


def last_alert(
    root: Path | str | None = None,
    *,
    max_daily_loss: float | None = None,
) -> dict[str, Any] | None:
    """
    读文件里最后一条给运行台显示。

    纯读：只跳过限额对不上的熔断行，不改文件。历史旧行由启动时或
    保存配置后的 prune_stale_halt_alerts 统一清理。
    """
    from src.utils.paths import project_root

    base = Path(root) if root else project_root()
    path = alerts_path(base)
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if is_stale_halt(row, max_daily_loss):
            continue
        last = row
    return last
