"""研究治理页签：假设登记 / 追踪链 / 决策变更 / 失败实验 / 再认证体检 / 容量估算。

只调用治理、再认证和容量模块的函数，不在界面里拼文件路径，也不触发回测或交易动作。
再认证只读配置、写一份快照；容量只取最近 K 线成交量做估算。
所有调用都 try/except 兜底，出错只在对应区域提示。
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from src.research import governance as gov
from src.ui import theme as T
import customtkinter as ctk

_PLACEHOLDER_SYMBOL = "（还没有）"
# 和 tools/check_capacity.py 保持一致：没填保证金比例时按 10% 估算。
_FALLBACK_MARGIN_RATE = 0.1
_DEFAULT_PARTICIPATION_PCT = "1.0"
_DEFAULT_LOOKBACK = "20"


class GovernanceTabMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    # ------------------------------ 构建 ------------------------------

    def _build_governance_tab(self, tab: ctk.CTkFrame) -> None:
        """纵向分区：假设、追踪链、决策、变更、失败实验。"""
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        ctk.CTkLabel(scroll, text="研究治理", font=T.FONT_TITLE, text_color=T.TEXT).pack(
            anchor="w", padx=4, pady=(4, 2)
        )
        ctk.CTkLabel(
            scroll,
            text="把研究过程留痕：假设先登记、再用实验验证，上线走评审单与变更冻结期。"
            "这里只做登记、体检和查询，不改交易配置、不启动回测、不下单；"
            "容量估算会读取关注品种的 K 线，只做估算。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=640,
            justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 8))

        self._gov_build_hypothesis_section(scroll)
        self._gov_build_trace_section(scroll)
        self._gov_build_decisions_section(scroll)
        self._gov_build_changes_section(scroll)
        self._gov_build_failed_section(scroll)
        self._gov_build_certification_section(scroll)
        self._gov_build_capacity_section(scroll)

        # 首次加载数据；空目录也不会报错，治理层已兜底。
        self._gov_refresh_all()

    def _gov_build_hypothesis_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "① 假设登记")
        list_card = self._card(parent)
        self.gov_hyp_box = self._gov_textbox(list_card, height=130)

        form = self._card(parent)
        self.gov_hyp_title_var = ctk.StringVar()
        self.gov_hyp_statement_var = ctk.StringVar()
        self.gov_hyp_symbol_var = ctk.StringVar()
        self.gov_hyp_period_var = ctk.StringVar()
        self.gov_hyp_param_var = ctk.StringVar()
        self.gov_hyp_accept_var = ctk.StringVar()

        self._entry_row(form, "标题", self.gov_hyp_title_var, width=300)
        self._entry_row(form, "陈述", self.gov_hyp_statement_var, hint="这条假设想说什么", width=300)
        symbol_values = self._gov_symbol_values()
        self.gov_hyp_symbol_var.set(self._gov_pick_symbol(symbol_values))
        self.gov_hyp_symbol_menu = self._option_row(form, "品种", self.gov_hyp_symbol_var, symbol_values, width=180)
        self._entry_row(form, "周期", self.gov_hyp_period_var, hint="如 15m / D", width=120)
        self._entry_row(form, "参数范围", self.gov_hyp_param_var, hint='JSON，如 {"fast":[5,20]}（可空）', width=260)
        self._entry_row(form, "验收标准", self.gov_hyp_accept_var, width=300)

        btn_row = ctk.CTkFrame(form, fg_color="transparent")
        btn_row.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(
            btn_row,
            text="登记假设",
            height=34,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._gov_register_hypothesis,
        ).pack(side="left")
        ctk.CTkButton(
            btn_row,
            text="刷新列表",
            height=34,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._gov_refresh_hypotheses,
        ).pack(side="left", padx=8)
        self.gov_hyp_status = ctk.CTkLabel(btn_row, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM)
        self.gov_hyp_status.pack(side="left", padx=8)

    def _gov_build_trace_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "② 追踪链查询")
        card = self._card(parent)
        self.gov_trace_strategy_var = ctk.StringVar(value=self._gov_default_strategy())
        self._entry_row(card, "策略", self.gov_trace_strategy_var, width=260)
        symbol_values = self._gov_symbol_values()
        self.gov_trace_symbol_var = ctk.StringVar(value=self._gov_pick_symbol(symbol_values))
        self.gov_trace_symbol_menu = self._option_row(
            card, "品种", self.gov_trace_symbol_var, symbol_values, width=180
        )
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(
            btn_row,
            text="查询追踪链",
            height=34,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._gov_query_trace,
        ).pack(side="left")
        self.gov_trace_frozen_label = ctk.CTkLabel(
            btn_row, text="未查询", font=T.FONT_BODY, text_color=T.TEXT_DIM
        )
        self.gov_trace_frozen_label.pack(side="left", padx=12)
        self.gov_trace_box = self._gov_textbox(card, height=180)

    def _gov_build_decisions_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "③ 最近决策")
        card = self._card(parent)
        self._gov_refresh_button(card, "刷新决策", self._gov_refresh_decisions)
        self.gov_decisions_box = self._gov_textbox(card, height=120)

    def _gov_build_changes_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "④ 最近变更")
        card = self._card(parent)
        self._gov_refresh_button(card, "刷新变更", self._gov_refresh_changes)
        self.gov_changes_box = self._gov_textbox(card, height=120)

    def _gov_build_failed_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "⑤ 失败实验")
        card = self._card(parent)
        self._gov_refresh_button(card, "刷新失败实验", self._gov_refresh_failed)
        self.gov_failed_box = self._gov_textbox(card, height=120)

    def _gov_build_certification_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "⑥ 再认证体检")
        card = self._card(parent)
        ctk.CTkLabel(
            card,
            text="把策略、风控和关注合约参数收成指纹，和上次体检比对。保证金比例、乘数、"
            "乘数缺失会直接标错；手续费、每跳缺失只提示。只写 data/review/certification.json，不改配置。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=640,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            btn_row,
            text="体检并记快照",
            height=30,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=lambda: self._gov_run_certification(write=True),
        ).pack(side="left")
        ctk.CTkButton(
            btn_row,
            text="只看不写",
            height=30,
            width=96,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=lambda: self._gov_run_certification(write=False),
        ).pack(side="left", padx=8)
        self.gov_cert_status = ctk.CTkLabel(btn_row, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM)
        self.gov_cert_status.pack(side="left", padx=8)
        self.gov_cert_box = self._gov_textbox(card, height=110)

    def _gov_build_capacity_section(self, parent: ctk.CTkScrollableFrame) -> None:
        self._gov_section_label(parent, "⑦ 容量估算")
        card = self._card(parent)
        self.gov_cap_participation_var = ctk.StringVar(value=_DEFAULT_PARTICIPATION_PCT)
        self.gov_cap_lookback_var = ctk.StringVar(value=_DEFAULT_LOOKBACK)
        self._entry_row(
            card,
            "单笔参与率(%)",
            self.gov_cap_participation_var,
            hint="占同期均量的比例，默认 1.0",
            width=90,
        )
        self._entry_row(
            card,
            "回看根数",
            self.gov_cap_lookback_var,
            hint="取最近多少根同周期 K 线，默认 20",
            width=90,
        )
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", pady=(4, 4))
        self.gov_cap_button = ctk.CTkButton(
            btn_row,
            text="估算关注品种容量",
            height=30,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._gov_run_capacity,
        )
        self.gov_cap_button.pack(side="left")
        self.gov_cap_status = ctk.CTkLabel(btn_row, text="", font=T.FONT_SMALL, text_color=T.TEXT_DIM)
        self.gov_cap_status.pack(side="left", padx=8)
        self.gov_cap_box = self._gov_textbox(card, height=110)
        self._gov_cap_running = False

    # ------------------------------ 通用小组件 ------------------------------

    def _gov_section_label(self, parent: ctk.CTkScrollableFrame, text: str) -> None:
        ctk.CTkLabel(parent, text=text, font=T.FONT_BODY, text_color=T.ACCENT).pack(
            anchor="w", padx=4, pady=(10, 2)
        )

    def _gov_textbox(self, parent: ctk.CTkFrame, *, height: int) -> ctk.CTkTextbox:
        box = ctk.CTkTextbox(
            parent,
            height=height,
            font=T.FONT_MONO,
            fg_color=T.BG,
            text_color=T.TEXT,
            corner_radius=8,
            wrap="word",
        )
        box.pack(fill="x", pady=(0, 2))
        return box

    def _gov_refresh_button(self, parent: ctk.CTkFrame, text: str, command) -> None:
        ctk.CTkButton(
            parent,
            text=text,
            height=28,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=command,
        ).pack(anchor="w", pady=(0, 4))

    def _gov_set_status(self, label: ctk.CTkLabel, text: str, *, warn: bool = False) -> None:
        label.configure(text=text, text_color=T.DANGER if warn else T.OK)

    # ------------------------------ 默认值 ------------------------------

    def _gov_default_strategy(self) -> str:
        var = getattr(self, "bt_engine_var", None)
        if var is not None:
            val = str(var.get() or "").strip()
            if val:
                return val
        cfg = getattr(self, "cfg", None) or {}
        strategy = cfg.get("strategy") if isinstance(cfg, dict) else None
        if isinstance(strategy, dict):
            return str(strategy.get("engine") or "")
        return ""

    def _gov_symbol_values(self) -> list[str]:
        watch = list(getattr(self, "_watch_symbols", None) or [])
        watch = [s for s in watch if str(s).strip()]
        return watch or [_PLACEHOLDER_SYMBOL]

    def _gov_pick_symbol(self, values: list[str]) -> str:
        try:
            current = self._config_symbol_now()
        except Exception:  # noqa: BLE001
            current = ""
        if current and current in values:
            return current
        return values[0] if values else ""

    @staticmethod
    def _gov_clean_symbol(value: str) -> str:
        text = str(value or "").strip()
        return "" if text == _PLACEHOLDER_SYMBOL else text

    # ------------------------------ 刷新 ------------------------------

    def _gov_refresh_all(self) -> None:
        self._gov_refresh_hypotheses()
        self._gov_refresh_decisions()
        self._gov_refresh_changes()
        self._gov_refresh_failed()

    def _gov_root(self):
        return getattr(self, "root_dir", None)

    def _gov_safe(self, box: ctk.CTkTextbox, action, fallback: str) -> None:
        try:
            text = action()
        except Exception as exc:  # noqa: BLE001 - 治理是辅助，出错不能拖垮界面
            self._set_readonly_box(box, f"读取失败：{exc}")
            return
        self._set_readonly_box(box, text or fallback)

    def _gov_refresh_hypotheses(self) -> None:
        box = getattr(self, "gov_hyp_box", None)
        if box is None:
            return

        def _load() -> str:
            rows = gov.list_hypotheses(self._gov_root())
            if not rows:
                return "还没有登记假设。在下面填好点「登记假设」。"
            lines: list[str] = []
            for hyp in rows:
                hyp_id = str(hyp.get("id") or "")
                title = str(hyp.get("title") or "")
                symbol = str(hyp.get("symbol") or "—")
                period = str(hyp.get("period") or "—")
                status = str(hyp.get("status") or "—")
                conclusion = str(hyp.get("conclusion") or "")
                lines.append(f"{hyp_id}  {title}  [{symbol}/{period}]  状态:{status}" + (f"  结论:{conclusion}" if conclusion else ""))
            return "\n".join(lines)

        self._gov_safe(box, _load, "还没有登记假设。")

    def _gov_refresh_decisions(self) -> None:
        box = getattr(self, "gov_decisions_box", None)
        if box is None:
            return

        def _load() -> str:
            rows = gov.list_decisions(self._gov_root(), limit=20)
            if not rows:
                return "暂无决策记录。"
            lines: list[str] = []
            for row in rows:
                ts = str(row.get("ts") or "")[:19]
                decision_type = str(row.get("decision_type") or "")
                decision = str(row.get("decision") or "")
                strategy = str(row.get("strategy") or "—")
                symbol = str(row.get("symbol") or "—")
                lines.append(f"{ts}  [{decision_type}] {strategy}/{symbol}  {decision}")
            return "\n".join(lines)

        self._gov_safe(box, _load, "暂无决策记录。")

    def _gov_refresh_changes(self) -> None:
        box = getattr(self, "gov_changes_box", None)
        if box is None:
            return

        def _load() -> str:
            rows = gov.list_changes(self._gov_root(), limit=20)
            if not rows:
                return "暂无变更记录。"
            lines: list[str] = []
            for row in rows:
                effective = str(row.get("effective_at") or row.get("ts") or "")[:10]
                strategy = str(row.get("strategy") or "—")
                symbol = str(row.get("symbol") or "—")
                what = str(row.get("what") or "")
                why = str(row.get("why") or "")
                line = f"{effective}  {strategy}/{symbol}  {what}"
                if why:
                    line += f"（{why}）"
                lines.append(line)
            return "\n".join(lines)

        self._gov_safe(box, _load, "暂无变更记录。")

    def _gov_refresh_failed(self) -> None:
        box = getattr(self, "gov_failed_box", None)
        if box is None:
            return

        def _load() -> str:
            rows = gov.list_failed_experiments(self._gov_root())
            if not rows:
                return "没有失败实验。"
            lines: list[str] = []
            for row in rows:
                exp_id = str(row.get("experiment_id") or "")
                symbol = str(row.get("symbol") or "—")
                failed_count = row.get("failed_count")
                reason = str(row.get("failure_reason") or "")
                lines.append(f"{exp_id}  {symbol}  失败 {failed_count} 次" + (f"  原因:{reason}" if reason else ""))
            return "\n".join(lines)

        self._gov_safe(box, _load, "没有失败实验。")

    # ------------------------------ 动作 ------------------------------

    def _gov_register_hypothesis(self) -> None:
        status = getattr(self, "gov_hyp_status", None)
        title = str(self.gov_hyp_title_var.get() or "").strip()
        if not title:
            self._gov_set_status(status, "标题不能为空", warn=True)
            return
        symbol = self._gov_clean_symbol(self.gov_hyp_symbol_var.get())
        period = str(self.gov_hyp_period_var.get() or "").strip()
        statement = str(self.gov_hyp_statement_var.get() or "").strip()
        acceptance = str(self.gov_hyp_accept_var.get() or "").strip()
        param_raw = str(self.gov_hyp_param_var.get() or "").strip()
        param_ranges: Any
        if param_raw:
            import json

            try:
                param_ranges = json.loads(param_raw)
            except ValueError:
                param_ranges = param_raw  # 解析不了就原样存，治理层不限制类型
        else:
            param_ranges = {}
        try:
            hyp = gov.register_hypothesis(
                self._gov_root(),
                title=title,
                statement=statement,
                symbol=symbol,
                period=period,
                param_ranges=param_ranges,
                acceptance=acceptance,
            )
        except Exception as exc:  # noqa: BLE001
            self._gov_set_status(status, f"登记失败：{exc}", warn=True)
            return
        # 清空表单，保留品种方便连续登记
        self.gov_hyp_title_var.set("")
        self.gov_hyp_statement_var.set("")
        self.gov_hyp_period_var.set("")
        self.gov_hyp_param_var.set("")
        self.gov_hyp_accept_var.set("")
        self._gov_refresh_hypotheses()
        self._gov_set_status(status, f"已登记 {hyp.get('id', '')}", warn=False)
        try:
            self._append_log(f"研究治理：已登记假设 {hyp.get('id', '')} {title}")
        except Exception:  # noqa: BLE001
            pass

    def _gov_query_trace(self) -> None:
        box = getattr(self, "gov_trace_box", None)
        frozen_label = getattr(self, "gov_trace_frozen_label", None)
        strategy = str(self.gov_trace_strategy_var.get() or "").strip()
        if not strategy:
            self._set_readonly_box(box, "请先填策略名。")
            if frozen_label is not None:
                frozen_label.configure(text="缺策略", text_color=T.WARN)
            return
        symbol = self._gov_clean_symbol(self.gov_trace_symbol_var.get())
        try:
            trace = gov.trace_strategy(self._gov_root(), strategy, symbol)
            lines = gov.format_trace_lines(trace)
            self._set_readonly_box(box, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self._set_readonly_box(box, f"查询失败：{exc}")
            if frozen_label is not None:
                frozen_label.configure(text=f"查询失败：{exc}", text_color=T.DANGER)
            return
        try:
            frozen_flag, frozen_msg = gov.is_frozen(self._gov_root(), strategy, symbol)
        except Exception as exc:  # noqa: BLE001
            if frozen_label is not None:
                frozen_label.configure(text=f"冻结查询失败：{exc}", text_color=T.WARN)
            return
        if frozen_label is not None:
            if frozen_flag:
                frozen_label.configure(text=f"冻结中：{frozen_msg}", text_color=T.DANGER)
            else:
                frozen_label.configure(text=frozen_msg or "未冻结", text_color=T.OK)
        try:
            self._append_log(f"研究治理：追踪链 {strategy}/{symbol} 冻结={'是' if frozen_flag else '否'}")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------ 再认证 / 容量 ------------------------------

    def _gov_clone_cfg(self) -> dict[str, Any]:
        """拿一份配置副本。优先用界面当前填的内容；取不到时退回启动时加载的配置。"""
        try:
            cfg = self._clone_cfg()
        except Exception:  # noqa: BLE001
            cfg = None
        if isinstance(cfg, dict) and cfg:
            return cfg
        base = getattr(self, "cfg", None)
        return copy.deepcopy(base) if isinstance(base, dict) else {}

    def _gov_run_certification(self, *, write: bool = True) -> None:
        """体检关注合约参数并和上次快照比对。只写快照文件，不改配置。"""
        box = getattr(self, "gov_cert_box", None)
        status = getattr(self, "gov_cert_status", None)
        if box is None:
            return
        try:
            from src.review.certification import format_certification_lines, run_certification

            report = run_certification(self._gov_clone_cfg(), root=self._gov_root(), write=write)
            text = "\n".join(format_certification_lines(report))
        except Exception as exc:  # noqa: BLE001 - 体检失败不能拖垮界面
            self._set_readonly_box(box, f"再认证失败：{exc}")
            self._gov_set_status(status, f"再认证失败：{exc}", warn=True)
            return
        self._set_readonly_box(box, text)
        errors = int(report.get("errors") or 0)
        state = str(report.get("status") or "已体检")
        if errors:
            self._gov_set_status(status, f"{state} · {errors} 项错误，先补参数", warn=True)
        else:
            self._gov_set_status(status, state)
        try:
            self._append_log(f"研究治理：再认证 {state}，错误 {errors} 项，指纹 {report.get('fingerprint') or '—'}")
        except Exception:  # noqa: BLE001
            pass

    def _gov_run_capacity(self) -> None:
        """按关注品种取最近 K 线成交量估算容量。取数在后台线程，界面只显示结果。"""
        if getattr(self, "_gov_cap_running", False):
            return
        box = getattr(self, "gov_cap_box", None)
        status = getattr(self, "gov_cap_status", None)
        if box is None:
            return
        try:
            pct = float(str(self.gov_cap_participation_var.get() or "").strip())
            lookback = int(float(str(self.gov_cap_lookback_var.get() or "").strip()))
        except ValueError:
            self._set_readonly_box(box, "单笔参与率和回看根数要填数字。")
            self._gov_set_status(status, "数字格式不对", warn=True)
            return
        if pct <= 0 or pct > 100:
            self._set_readonly_box(box, "单笔参与率要在 0～100 之间（百分数，如 1.0 表示 1%）。")
            self._gov_set_status(status, "参与率超出范围", warn=True)
            return
        if lookback < 1:
            self._set_readonly_box(box, "回看根数至少为 1。")
            self._gov_set_status(status, "回看根数不对", warn=True)
            return

        cfg = self._gov_clone_cfg()
        try:
            from src.review.certification import watch_symbols

            symbols = watch_symbols(cfg) or list(getattr(self, "_watch_symbols", None) or [])
        except Exception:  # noqa: BLE001
            symbols = list(getattr(self, "_watch_symbols", None) or [])
        symbols = [str(item).strip() for item in symbols if str(item).strip()]
        if not symbols:
            self._set_readonly_box(box, "关注列表为空，先在配置页加合约。")
            self._gov_set_status(status, "没有关注品种", warn=True)
            return

        participation = pct / 100.0
        self._gov_cap_running = True
        try:
            self.gov_cap_button.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass
        self._gov_set_status(status, "正在取 K 线…")
        self._set_readonly_box(box, f"正在按 {len(symbols)} 个关注品种取最近 K 线估算容量…")

        def _run() -> None:
            try:
                lines, ok, total = self._gov_collect_capacity(cfg, symbols, participation, lookback)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self._post_to_main(lambda: self._gov_capacity_failed(err))
                return
            self._post_to_main(lambda: self._gov_capacity_done(lines, ok, total))

        threading.Thread(target=_run, daemon=True).start()

    def _gov_collect_capacity(
        self,
        cfg: dict[str, Any],
        symbols: list[str],
        participation: float,
        lookback: int,
    ) -> tuple[list[str], int, int]:
        """逐个关注品种取 K 线并估算容量。阻塞调用，只在后台线程里用。

        返回 (每品种一行结果, 成功个数, 品种总数)。取 K 线失败不抛异常，写进结果行。
        """
        from src.backtest.capacity import estimate_capacity, format_capacity_lines
        from src.data.kline import KlineProvider
        from src.risk.universe_table import product_key

        st = cfg.get("strategy") if isinstance(cfg, dict) else None
        st = st if isinstance(st, dict) else {}
        contracts = (cfg.get("risk") or {}).get("contracts") if isinstance(cfg, dict) else None
        contracts = contracts if isinstance(contracts, dict) else {}
        period = str(st.get("timeframe") or "15")
        provider = KlineProvider(cfg)
        lines: list[str] = []
        ok = 0
        for symbol in symbols:
            box = contracts.get(product_key(symbol)) or {}
            rate = float(box.get("margin_rate_pct") or 0) / 100.0
            try:
                frame = provider.fetch(symbol, period=period, keep_history=True)
            except Exception as exc:  # noqa: BLE001 - 单个品种失败不影响其它品种
                lines.append(f"{symbol} 容量     取 K 线失败：{exc}")
                continue
            estimate = estimate_capacity(
                frame,
                participation=participation,
                lookback=lookback,
                multiplier=float(box.get("multiplier") or 10.0),
                margin_rate=rate if rate > 0 else _FALLBACK_MARGIN_RATE,
            )
            lines.extend(format_capacity_lines(symbol, estimate))
            ok += 1
        return lines, ok, len(symbols)

    def _gov_capacity_done(self, lines: list[str], ok: int, total: int) -> None:
        self._gov_cap_running = False
        try:
            self.gov_cap_button.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        text = "\n".join(lines) if lines else "没有可估算的品种。"
        self._set_readonly_box(self.gov_cap_box, text)
        if ok > 0:
            self._gov_set_status(self.gov_cap_status, f"{ok}/{total} 个品种已估算")
        else:
            self._gov_set_status(self.gov_cap_status, "全部取数失败", warn=True)
        try:
            self._append_log(f"研究治理：容量估算 {ok}/{total} 个品种完成")
        except Exception:  # noqa: BLE001
            pass

    def _gov_capacity_failed(self, message: str) -> None:
        self._gov_cap_running = False
        try:
            self.gov_cap_button.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass
        self._set_readonly_box(self.gov_cap_box, f"容量估算失败：{message}")
        self._gov_set_status(self.gov_cap_status, f"容量估算失败：{message}", warn=True)
