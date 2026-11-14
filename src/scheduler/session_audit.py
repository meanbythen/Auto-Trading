"""盘前行情适配审计的定时触发与后台执行。"""

from __future__ import annotations

import threading
from datetime import datetime

from loguru import logger


class SessionAuditMixin:
    """盘前审计方法；共享运行状态仍保存在 TradingBot 实例上。"""

    _last_audit_at: datetime | None
    _audit_thread: threading.Thread | None

    def _maybe_run_session_audit(self) -> None:
        """定时触发：盘前 08:55 / 13:25 / 20:25 各跑一次审计。"""
        from src.strategy.session_audit import (
            should_run_session_audit,
        )

        now = datetime.now()
        if should_run_session_audit(now, self._last_audit_at):
            self._run_session_audit(f"盘前定时（{now.strftime('%H:%M')}）")

    def _audit_bars_provider(self):
        """复用 runner 的单例 KlineProvider，避免审计每轮新建连接。"""
        from src.strategy.session_audit import make_bars_provider

        return make_bars_provider(self.cfg, provider=self.kline)

    def _run_session_audit(self, trigger: str = "") -> None:
        """立即跑一次盘前行情适配审计，结果写日志。

        独立线程执行，K 线拉取有网络延迟时不阻塞主扫描循环。
        """

        def _thread() -> None:
            try:
                from src.strategy.session_audit import (
                    format_audit_report,
                    run_pre_session_audit,
                )

                rows = run_pre_session_audit(
                    self.cfg,
                    bars_provider=self._audit_bars_provider(),
                )
                for line in format_audit_report(rows):
                    logger.info(line)
            except Exception as exc:  # noqa: BLE001
                logger.warning("盘前行情审计跳过（不影响主流程）：{}", exc)

        if self._audit_thread is not None and self._audit_thread.is_alive():
            # 上一次还没跑完，这次不再开第二条线程，也不刷新触发时间，稍后自然重试。
            logger.info("上一次盘前审计还在跑，这次跳过。")
            return
        self._last_audit_at = datetime.now()
        self._audit_thread = threading.Thread(target=_thread, name="SessionAudit", daemon=True)
        self._audit_thread.start()
