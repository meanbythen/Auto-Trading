"""UI variable initialization and conversion between widgets and configuration."""

from __future__ import annotations

from typing import Any


class AppStateMixin:
    """运行期状态与配置辅助：实盘扫描用的 cfg、回测周期同步。"""

    def _cfg_for_live_scan(self) -> dict[str, Any]:
        """
        给常驻和单次扫描用的配置。

        默认只看下拉框选中的那一个合约；多品种开关打开时保留完整名单，
        让白名单里的其他合约也能一起扫描，选中品种仍排第一。
        """
        cfg = self._clone_cfg()
        current = self.watch_choice_var.get().strip()
        symbols = list((cfg.get("strategy") or {}).get("symbols") or [])
        if not current or current == "（还没有）":
            return cfg
        st = cfg.setdefault("strategy", {})
        ms_box = (cfg.get("live") or {}).get("multi_symbol") or {}
        if ms_box.get("enabled"):
            ordered = [current] + [item for item in symbols if item.lower() != current.lower()]
            st["symbols"] = ordered
            cfg.setdefault("trade", {})["symbol"] = current
            if len(ordered) > 1:
                self._append_log(
                    f"多品种已启用，本次扫描：{'、'.join(ordered)}。"
                    "真实账户还需打开「真实账户放行」才会生效。"
                )
            return cfg
        st["symbols"] = [current]
        cfg.setdefault("trade", {})["symbol"] = current
        extras = [item for item in symbols if item.lower() != current.lower()]
        if extras:
            self._append_log(
                f"本次只盯 {current}。列表里还有 {'、'.join(extras)}，不会一起挂机。"
            )
        return cfg

    def _sync_bt_periods_after_save(
        self, old_tf: str, old_trail: str, cur_tf: str, cur_trail: str, cfg: dict[str, Any]
    ) -> None:
        """旧入口：保存配置页时曾经顺手改回测周期。现在不改，避免手改的周期被冲掉。"""
        _ = (old_tf, old_trail, cur_tf, cur_trail, cfg)
        return
