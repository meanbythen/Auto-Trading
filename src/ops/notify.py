"""
告警外发（飞书 webhook）
功能：把熔断、断线、对账差异这类必须人看到的事，推到手机上的飞书群。
原理：只入队不阻塞调用方；后台单线程 POST，5 秒超时、失败重试 1 次；
     同一条 code 每小时限流；任何异常都降级成本地日志，不影响扫描线程。
     默认关闭（runtime.alerts.external=off），URL 只存本地配置，日志不打印。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import Any
from urllib import request

from loguru import logger

QUEUE_SIZE = 200
RETRY_TIMES = 1
RETRY_SLEEP_SEC = 0.5
RATE_WINDOW_SEC = 3600.0
_LEVEL_ORDER = {"warning": 1, "error": 2}


@dataclass(frozen=True)
class WebhookConfig:
    """飞书机器人 webhook。"""

    kind: str = "feishu"
    url: str = ""
    timeout_sec: float = 5.0


@dataclass(frozen=True)
class NotifyConfig:
    """告警外发配置。problem 非空表示配置有问题、已按关闭处理。"""

    enabled: bool = False
    min_level: str = "error"
    rate_limit_per_hour: int = 10
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    problem: str = ""

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any] | None) -> NotifyConfig:
        """读 runtime.alerts。写错任何一项都只关闭外发，不抛异常。"""
        runtime = (cfg or {}).get("runtime") or {}
        alerts = runtime.get("alerts") or {}
        if not isinstance(alerts, dict):
            return cls(problem="runtime.alerts 不是映射，已按关闭处理")
        hook = alerts.get("webhook") or {}
        if not isinstance(hook, dict):
            hook = {}
        external = str(alerts.get("external") or "off").strip().lower()
        kind = str(hook.get("kind") or "feishu").strip().lower()
        url = str(hook.get("url") or "").strip()
        try:
            timeout = float(hook.get("timeout_sec", 5) or 5)
        except (TypeError, ValueError):
            timeout = 5.0
        timeout = min(max(timeout, 1.0), 30.0)
        min_level = str(alerts.get("min_level") or "error").strip().lower()
        if min_level not in _LEVEL_ORDER:
            min_level = "error"
        try:
            rate = int(alerts.get("rate_limit_per_hour", 10))
        except (TypeError, ValueError):
            rate = 10
        rate = max(rate, 0)
        problem = ""
        if external not in ("off", "webhook"):
            problem = f"不支持的外发通道 {external}，已按关闭处理"
        elif external == "webhook" and not url:
            problem = "打开外发但 webhook.url 为空，已按关闭处理"
        elif external == "webhook" and kind != "feishu":
            problem = f"目前只支持飞书 webhook，{kind} 已按关闭处理"
        enabled = external == "webhook" and bool(url) and not problem
        return cls(
            enabled=enabled,
            min_level=min_level,
            rate_limit_per_hour=rate,
            webhook=WebhookConfig(kind=kind, url=url, timeout_sec=timeout),
            problem=problem,
        )


class _State:
    """模块级运行状态；单线程消费、测试可重置。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cfg = NotifyConfig()
        self.queue: Queue[dict[str, Any]] | None = None
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        self.sent_at: dict[str, deque[float]] = {}
        self.dropped = 0
        self.failed = 0
        self.sent = 0


_state = _State()


def configure(cfg: dict[str, Any] | None) -> NotifyConfig:
    """启动时调用一次；不调用就等于关闭。"""
    parsed = NotifyConfig.from_cfg(cfg)
    _state.cfg = parsed
    if parsed.problem:
        logger.warning("告警外发已关闭：{}", parsed.problem)
    elif parsed.enabled:
        logger.info(
            "告警外发已开启：飞书 webhook，级别≥{}，同 code 每小时最多 {} 条",
            parsed.min_level,
            parsed.rate_limit_per_hour,
        )
    return parsed


def current_config() -> NotifyConfig:
    return _state.cfg


_failure_hook: Callable[[], None] | None = None


def set_failure_hook(hook: Callable[[], None] | None) -> None:
    """失败时补一条本地告警的回调；由 alerts 注册，避免 notify 反向导入。"""
    global _failure_hook
    _failure_hook = hook


def notify(row: dict[str, Any]) -> bool:
    """
    把一条已写入 alerts.jsonl 的告警放进外发队列。

    返回是否真的排队。永远不抛异常、不等待网络，主流程只花一次入队的时间。
    """
    try:
        cfg = _state.cfg
        if not cfg.enabled:
            return False
        code = str(row.get("code") or "")
        if not code or code == "notify_failed":
            return False
        level = str(row.get("level") or "warning").strip().lower()
        if _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(cfg.min_level, 2):
            return False
        if _rate_limited(code):
            return False
        _enqueue({"text": _format(row), "timeout": cfg.webhook.timeout_sec})
        return True
    except Exception:  # noqa: BLE001
        logger.exception("告警外发入队失败")
        return False


def _format(row: dict[str, Any]) -> str:
    """外发正文。界面看得到 code 和短 run_id 就够定位。"""
    level = str(row.get("level") or "warning")
    code = str(row.get("code") or "")
    message = str(row.get("message") or "")
    run_id = str(row.get("run_id") or "")
    head = f"【{level}】{code}"
    if run_id:
        head += f" [{run_id[-6:]}]"
    stamp = str(row.get("ts") or "")
    return f"{head}\n{message}\n{stamp}".strip()


def _rate_limited(code: str) -> bool:
    """同一 code 一小时内最多 rate_limit_per_hour 条；0 表示不限。"""
    limit = _state.cfg.rate_limit_per_hour
    if limit <= 0:
        return False
    now = time.time()
    marks = _state.sent_at.setdefault(code, deque())
    while marks and now - marks[0] > RATE_WINDOW_SEC:
        marks.popleft()
    if len(marks) >= limit:
        logger.debug("告警 {} 超过每小时 {} 条，本次只记本地", code, limit)
        return True
    marks.append(now)
    return False


def _ensure_worker() -> Queue[dict[str, Any]]:
    """首次入队时启一个后台线程；已有队列就复用。"""
    with _state.lock:
        if _state.queue is None:
            _state.queue = Queue(maxsize=QUEUE_SIZE)
        if _state.worker is None or not _state.worker.is_alive():
            _state.stop = threading.Event()
            _state.worker = threading.Thread(
                target=_loop,
                args=(_state.queue, _state.stop),
                name="alert-notify",
                daemon=True,
            )
            _state.worker.start()
        return _state.queue


def _enqueue(item: dict[str, Any]) -> None:
    """队列满时丢最旧的一条，绝不阻塞调用方。"""
    queue = _ensure_worker()
    try:
        queue.put_nowait(item)
        return
    except Full:
        pass
    try:
        queue.get_nowait()
        queue.task_done()
        _state.dropped += 1
    except Empty:
        pass
    try:
        queue.put_nowait(item)
    except Full:
        _state.dropped += 1


def _loop(queue: Queue[dict[str, Any]], stop: threading.Event) -> None:
    """后台消费。单条失败只记日志，不让线程退出。"""
    while not stop.is_set():
        try:
            item = queue.get(timeout=0.5)
        except Empty:
            continue
        try:
            _deliver(item)
        except Exception:  # noqa: BLE001
            logger.exception("告警外发线程异常，继续下一条")
        finally:
            queue.task_done()


def _deliver(item: dict[str, Any]) -> None:
    """发一条；失败重试一次，仍失败写一条本地 notify_failed（不再外发）。"""
    cfg = _state.cfg
    if not cfg.enabled or not cfg.webhook.url:
        return
    payload = {"msg_type": "text", "content": {"text": str(item.get("text") or "")}}
    timeout = float(item.get("timeout") or cfg.webhook.timeout_sec)
    ok = False
    for attempt in range(RETRY_TIMES + 1):
        try:
            ok = bool(_post_json(cfg.webhook.url, payload, timeout))
        except Exception as exc:  # noqa: BLE001
            logger.warning("告警外发失败（第 {} 次）: {}", attempt + 1, exc)
            ok = False
        if ok:
            break
        if attempt < RETRY_TIMES:
            time.sleep(RETRY_SLEEP_SEC)
    if ok:
        _state.sent += 1
        return
    _state.failed += 1
    logger.warning("告警外发失败，已重试 {} 次，只保留本地记录", RETRY_TIMES)
    try:
        if _failure_hook is not None:
            _failure_hook()
    except Exception:  # noqa: BLE001
        logger.exception("写 notify_failed 告警失败")


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> bool:
    """POST 一个 JSON。飞书 HTTP 200 也可能是业务失败，要看响应里的 code。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        body = resp.read(4096)
    if not (200 <= status < 300):
        return False
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return True
    if isinstance(parsed, dict):
        code = parsed.get("code", parsed.get("StatusCode", 0))
        if isinstance(code, int) and code != 0:
            logger.warning("飞书返回业务错误 code={}", code)
            return False
    return True


def stats() -> dict[str, int]:
    """外发计数，给诊断用。"""
    return {"sent": _state.sent, "failed": _state.failed, "dropped": _state.dropped}


def reset(queue_size: int = QUEUE_SIZE) -> None:
    """测试用：停掉后台线程、清空限流与计数。"""
    with _state.lock:
        _state.stop.set()
        _state.cfg = NotifyConfig()
        _state.queue = Queue(maxsize=queue_size)
        _state.worker = None
        _state.sent_at.clear()
        _state.dropped = 0
        _state.failed = 0
        _state.sent = 0
