"""
运行告警（记下来，方便以后接微信/邮件）

功能：熔断、窗口丢失、柜台没连上这类「人必须看一眼」的事，写成 data/alerts.jsonl。
原理：同一条告警十分钟内不重复写，避免扫描每几秒刷爆文件。
     这一层只记事，不改开平仓。界面运行卡可以从快照里读最近一条。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

FILE_NAME = "alerts.jsonl"
_COOLDOWN_SEC = 600.0
_last_emit: dict[str, float] = {}


def alerts_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


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
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.warning("告警 [{}] {}", code, message)
    return True


def last_alert(root: Path | str | None = None) -> dict[str, Any] | None:
    """读文件里最后一条，给运行台显示。"""
    from src.utils.paths import project_root

    path = alerts_path(Path(root) if root else project_root())
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last if isinstance(last, dict) else None
