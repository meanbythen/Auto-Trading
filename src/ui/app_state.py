"""UI variable initialization and conversion between widgets and configuration."""

from __future__ import annotations

from typing import Any


class AppStateMixin:
    """运行期状态与配置辅助：实盘扫描用的 cfg、回测周期同步。"""

    def _cfg_for_live_scan(self) -> dict[str, Any]:
        """
        给常驻和单次扫描用的配置。

        关注列表可以有好几个具体合约，但同花顺一次只盯下拉框里选中的那一个。
        其余合约仍保存在配置里，下次可以再选，这一次不会一起扫描。
        """
        cfg = self._clone_cfg()
        current = self.watch_choice_var.get().strip()
        symbols = list((cfg.get("strategy") or {}).get("symbols") or [])
        if not current or current == "（还没有）":
            return cfg
        st = cfg.setdefault("strategy", {})
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
