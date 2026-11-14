"""UI variable initialization and conversion between widgets and configuration."""

from __future__ import annotations

from typing import Any

from src.data.periods import period_label


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
        """
        配置页保存后，把回测页还跟着旧配置的信号/止损周期一起换过来。

        回测页有自己的周期选择器（故意和实盘分开）。只有它当前值还是配置页上一次
        的值——也就是没在回测页单独改过——才跟着换，免得把特意选的周期冲掉。
        """
        st = cfg.get("strategy") or {}
        pairs = (
            ("信号周期", old_tf, cur_tf, str(st.get("timeframe") or ""),
             self.bt_tf_var, self.bt_tf_custom_var),
            ("止损周期", old_trail, cur_trail, str(st.get("trail_timeframe") or ""),
             self.bt_trail_var, self.bt_trail_custom_var),
        )
        for name, old, cur, new, var, custom in pairs:
            if new and old and cur == old and new != old:
                self._set_period_vars(new, var, custom)
                self._append_log(f"回测页{name}跟着配置页改成 {period_label(new)}。")
