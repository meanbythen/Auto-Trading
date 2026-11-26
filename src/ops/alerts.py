"""
运行告警（记下来，方便以后接微信/邮件）

功能：熔断、窗口丢失、柜台没连上这类「人必须看一眼」的事，写成 data/alerts.jsonl。
原理：同一条告警十分钟内不重复写，避免扫描每几秒刷爆文件。
     这一层只记事，不改开平仓。界面运行卡可以从快照里读最近一条。
     条件类告警（CTP 断线、窗口丢失）在恢复时补一条 info 恢复行，
     读「最近告警」时自动标记为已恢复，避免状态已经好了、红字还挂着。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

FILE_NAME = "alerts.jsonl"
# 告警文件只保留一代：超过这个大小先改名为 .1 再继续写。
ALERTS_MAX_BYTES = 5 * 1024 * 1024
_COOLDOWN_SEC = 600.0
_last_emit: dict[str, float] = {}
# 当前这次运行的标识，由 TradingBot 启动时设置；没设置时写空串。
_run_id: str = ""
# 没显式传 root 时写哪个目录；TradingBot 启动时指向自己的根目录，避免测试/脚本
# 把告警写进正在使用的实盘数据目录。
_default_root: Path | None = None
# 条件类告警的恢复记录：写一条 info 行，表示这些 code 已经恢复。
# 例：ctp_up 一旦写入，「最近告警」就不再显示更早的 ctp_down / ctp_book_mismatch。
RECOVERY_OF: dict[str, tuple[str, ...]] = {
    "ctp_up": ("ctp_down", "ctp_book_mismatch"),
    "window_up": ("window_lost",),
    "leg_alloc_ok": ("leg_margin_missing", "leg_alloc_overflow"),
    "reconcile_ok": ("exit_with_positions",),
}
# 熔断告警正文里「已超过限额 100」这一段数字，用来对照现在配置页填的日最大亏损。
_HALT_LIMIT_RE = re.compile(r"已超过限额\s*([0-9]+(?:\.[0-9]+)?)")


def alerts_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


def _rotate_if_needed(path: Path) -> None:
    """超过上限就把当前告警文件改名为 .1（覆盖旧 .1），保持只留一代。"""
    try:
        if not path.exists() or path.stat().st_size < ALERTS_MAX_BYTES:
            return
    except OSError:
        return
    backup = path.with_name(path.name + ".1")
    try:
        backup.unlink(missing_ok=True)
        path.replace(backup)
    except OSError as exc:
        logger.warning("轮转告警文件 {} 失败，继续追加: {}", path, exc)


def set_run_id(run_id: str | None) -> None:
    """设置当前运行标识，之后写的告警都会带上它。"""
    global _run_id
    _run_id = str(run_id or "")


def current_run_id() -> str:
    """当前运行标识；还没设置时是空串。"""
    return _run_id


def set_default_root(root: Path | str | None) -> None:
    """设置告警默认写哪个根目录；TradingBot 启动时指向自己的数据目录。"""
    global _default_root
    _default_root = Path(root) if root else None


def _root_of(root: Path | str | None) -> Path:
    """显式 root 优先；其次本进程设置的默认根；最后才回退到项目根。"""
    if root:
        return Path(root)
    if _default_root is not None:
        return _default_root
    from src.utils.paths import project_root

    return project_root()


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
    key = f"{code}|{message}"
    now = datetime.now().timestamp()
    prev = _last_emit.get(key, 0.0)
    if now - prev < _COOLDOWN_SEC:
        return False
    _last_emit[key] = now
    base = _root_of(root)
    path = alerts_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
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
        from src.ops import notify as notify_mod

        notify_mod.set_failure_hook(
            lambda: emit_alert("notify_failed", "飞书告警外发失败，只保留本地记录", level="warning")
        )
        notify_mod.notify(row)
    except Exception:  # noqa: BLE001
        logger.debug("告警外发入队失败，忽略")
    return True


def emit_recovery(
    code: str,
    message: str,
    *,
    root: Path | str | None = None,
) -> bool:
    """
    记一条「已恢复」。只进告警文件和历史，不参与「最近告警」显示。

    原理：写一条 level=info 的行并带上 resolves，last_alert 读到后就把对应的
    条件类告警从「仍然有效」里撤掉，避免 CTP 已经连上、面板还挂着连不上。
    """
    resolved = RECOVERY_OF.get(str(code or ""))
    if not resolved:
        return False
    base = _root_of(root)
    path = alerts_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": "info",
        "code": str(code or ""),
        "message": str(message or ""),
        "run_id": _run_id,
        "resolves": list(resolved),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("告警恢复 [{}] {}", code, message)
    # 恢复不外发消息，避免白天恢复也推一条打扰。
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


def _same_run(row: dict[str, Any], run_id: str | None) -> bool:
    """给了 run_id 就只认本进程写的行；没标 run_id 的旧行算历史，不再当当前告警。"""
    if not run_id:
        return True
    return str(row.get("run_id") or "") == run_id


def last_alert(
    root: Path | str | None = None,
    *,
    max_daily_loss: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """
    读最近一条告警给运行台显示；已经恢复的那条会带 recovered=True。

    纯读，不改文件。规则：
    - 只跳过限额对不上的熔断行（历史旧行由 prune_stale_halt_alerts 统一清理）；
    - 给了 run_id 就只看本次运行的告警，别的进程/测试写进来的不参与显示；
    - info 恢复行本身不当告警显示，只用来给更新的恢复行做标记；
    - 同一条告警之后若出现了能恢复它的 info 行，返回的行会多一个 recovered=True，
      界面显示「（已恢复）」并用弱化颜色，不再像当前故障那样吓人。
    """
    base = _root_of(root)
    path = alerts_path(base)
    if not path.exists():
        return None
    parsed: list[tuple[int, dict[str, Any]]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and _same_run(row, run_id):
            parsed.append((index, row))
    latest: dict[str, Any] | None = None
    latest_index = -1
    for index, row in parsed:
        if str(row.get("level") or "") == "info":
            continue
        if is_stale_halt(row, max_daily_loss):
            continue
        latest, latest_index = row, index
    if latest is None:
        return None
    code = str(latest.get("code") or "")
    if not code:
        return latest
    for index, row in parsed:
        if index <= latest_index or str(row.get("level") or "") != "info":
            continue
        resolved = row.get("resolves") or RECOVERY_OF.get(str(row.get("code") or ""), ())
        if code in {str(item or "") for item in resolved}:
            return {**latest, "recovered": True}
    return latest


def active_alert(
    root: Path | str | None = None,
    *,
    codes: tuple[str, ...] | None = None,
    max_daily_loss: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """
    最近一条「还没有被恢复」的告警；codes 给了就只在指定 code 里找。

    纯读，不改文件。给组合分配补比例这类恢复操作用：只在旧的缺比例告警
    仍然有效时才需要写恢复行，已经恢复过的不要重复写。
    """
    base = _root_of(root)
    path = alerts_path(base)
    if not path.exists():
        return None
    parsed: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and _same_run(row, run_id):
            parsed.append(row)
    recovered: set[str] = set()
    for row in reversed(parsed):
        code = str(row.get("code") or "")
        if str(row.get("level") or "") == "info":
            for item in row.get("resolves") or RECOVERY_OF.get(code, ()):
                recovered.add(str(item or ""))
            continue
        if code and code in recovered:
            continue
        if is_stale_halt(row, max_daily_loss):
            continue
        if codes and code not in codes:
            continue
        return row
    return None
