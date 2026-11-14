"""Top status cards, health notes, position summaries, and periodic status refresh."""

from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger
from src.data.periods import period_label
from src.ui import theme as T
from src.ui.app_common import ENGINE_LABELS
from src.ui.app_common import EXEC_LABELS
import customtkinter as ctk


class DashboardMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _make_stat_card(self, parent: ctk.CTkFrame, col: int, title: str, value: str) -> ctk.CTkLabel:
        """做顶部的一张状态卡。宽度由列的比例决定，文字在卡片里面换行，不把旁边的卡片挤窄。"""
        # 宽高只给一个很小的起点。真正的宽由上面的列比例拉开，高由里面的字撑开。
        # 不写的话，卡片会按默认 200 像素去抢宽度，窗口一窄就会被挤变形。
        card = ctk.CTkFrame(
            parent,
            fg_color=T.BG_PANEL,
            corner_radius=14,
            border_width=1,
            border_color=T.BORDER,
            width=40,
            height=36,
        )
        # nsew：五张卡同一行里一样高，某一张换成两行字时，旁边的卡不会显得矮一截。
        card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0 if col == 4 else 6))
        # 宽高写小。不写的话这一行会按默认 200 像素把整张状态卡撑高。
        title_row = ctk.CTkFrame(card, fg_color="transparent", width=20, height=16)
        title_row.pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(title_row, text=title, font=T.FONT_SMALL, text_color=T.TEXT_DIM).pack(side="left")
        # 日期放在「运行状态」这张卡的右上角。这张卡本来就在说现在，标题又短，不会把心跳时间挤成两行。
        if col == 0:
            self.card_date = ctk.CTkLabel(
                title_row, text=date.today().isoformat(), font=T.FONT_SMALL, text_color=T.TEXT_DIM,
            )
            self.card_date.pack(side="right")
        val = ctk.CTkLabel(
            card,
            text=value,
            font=T.FONT_TITLE,
            text_color=T.TEXT,
            anchor="w",
            justify="left",
            width=40,
            wraplength=160,
        )
        val.pack(anchor="w", fill="x", padx=14, pady=(4, 14))
        self._status_card_frames.append(card)
        return val


    def _fit_status_card_text(self, _event: object = None) -> None:
        """窗口变宽或变窄时，让每张卡里的字按卡片实际宽度换行。"""
        labels = (
            self.card_mode,
            self.card_engine,
            self.card_exec,
            self.card_symbols,
            self.card_position,
        )
        for card, label in zip(self._status_card_frames, labels):
            width = card.winfo_width()
            if width < 80:
                continue
            # 左右各留 14 像素。界面有屏幕放大时，换行宽度要先换回未放大的像素，否则字会画出边框。
            inner = max(96, width - 28)
            wrap = int(round(card._reverse_widget_scaling(inner)))
            current = int(label.cget("wraplength") or 0)
            if abs(current - wrap) <= 4:
                continue
            label.configure(wraplength=wrap)


    def _refresh_status_cards(self) -> None:
        running = self.worker.running
        snap = self.worker.status_snapshot() if running else {}
        # 过了零点也要换成新的一天。这里用的就是换日清零所用的日历日期。
        if hasattr(self, "card_date"):
            self.card_date.configure(text=date.today().isoformat())
        if running and snap.get("heartbeat_at"):
            mode_txt = f"运行中 · 心跳{str(snap['heartbeat_at'])[-8:]}"
            if snap.get("halted"):
                mode_txt = "已熔断"
            self.card_mode.configure(text=mode_txt, text_color=T.DANGER if snap.get("halted") else T.OK)
        else:
            self.card_mode.configure(
                text="运行中" if running else self._status,
                text_color=T.OK if running else T.TEXT,
            )
        eng = self.engine_var.get()
        self.card_engine.configure(text=ENGINE_LABELS.get(eng, eng))
        ex = self.exec_var.get()
        pnl = snap.get("daily_pnl")
        if running and pnl is not None:
            self.card_exec.configure(text=f"{EXEC_LABELS.get(ex, ex)} · 今日{pnl:+.0f}元")
        else:
            self.card_exec.configure(text=EXEC_LABELS.get(ex, ex))
        current = self.watch_choice_var.get().strip()
        if current in ("", "（还没有）"):
            current = self._watch_symbols[0] if self._watch_symbols else "-"
        self.card_symbols.configure(text=current)
        if hasattr(self, "hero_sub"):
            broker = str((self.cfg.get("account") or {}).get("broker") or "期货公司").strip()
            syms = self._watch_symbols
            sym_txt = " / ".join(syms[:3]) + (f" 等{len(syms)}个" if len(syms) > 3 else "")
            dry_txt = "模拟" if self.dry_var.get() else "实盘"
            from src.account.workspace import profile_of

            book_txt = profile_of(root=self.root_dir)["banner"]
            self.hero_sub.configure(
                text=f"{broker} · {sym_txt or '未选品种'} · 当前{dry_txt}（开关在下方控制台） · {book_txt}"
            )
        position_text = self._position_card_text(snap if running else None)
        self.card_position.configure(
            text=position_text,
            text_color=T.TEXT if position_text == "空仓" else T.OK,
        )
        if hasattr(self, "alert_line"):
            # 始终按配置页现在的日最大亏损去读文件：限额已经改过的旧熔断会删掉，不在红字里留「限额 100」。
            try:
                now_limit = float(self.max_loss_var.get())
            except (TypeError, ValueError):
                now_limit = None
            alert = None
            try:
                from src.ops.alerts import last_alert

                alert = last_alert(self.root_dir, max_daily_loss=now_limit)
            except Exception:  # noqa: BLE001
                alert = snap.get("last_alert") if running else None
            if isinstance(alert, dict) and alert.get("message"):
                alert_text = f"最近告警 {alert.get('ts', '')}  {alert.get('message')}"
            else:
                alert_text = ""
        else:
            alert_text = ""
        health_text = ""
        health_tone = T.TEXT_DIM
        if hasattr(self, "health_line"):
            health = snap.get("health") if running else None
            if not isinstance(health, dict):
                try:
                    from src.ops.health import evaluate_health

                    health = evaluate_health(self.root_dir).as_dict()
                except Exception:  # noqa: BLE001
                    health = None
            if isinstance(health, dict) and health.get("message"):
                health_text = str(health.get("message") or "")
                health_tone = T.DANGER if health.get("status") == "watch" else T.TEXT_DIM
        dyn_text = ""
        if hasattr(self, "dyn_line"):
            note = snap.get("dynamic") if running else None
            if not note:
                try:
                    from src.risk.dynamic import describe_status, live_equity_and_peak

                    preview = dict(self.cfg)
                    preview_risk = dict(preview.get("risk") or {})
                    preview_risk["dynamic"] = self._dynamic_cfg_from_ui()
                    preview["risk"] = preview_risk
                    equity, peak = live_equity_and_peak(self.root_dir)
                    note = describe_status(preview, equity=equity, peak=peak)
                except Exception:  # noqa: BLE001
                    note = ""
            dyn_text = str(note or "")
        flow = str(snap.get("ctp_flow") or "") if running else ""
        if flow:
            dyn_text = f"{flow}\n{dyn_text}" if dyn_text else flow
        if running and snap.get("orders_paused"):
            alert_text = (alert_text + "  " if alert_text else "") + "已暂停报单，开平仓不会发出去"
        self._show_status_notes(alert_text, health_text, health_tone, dyn_text)
        # 跑着：左边停止常驻，右边暂停/恢复报单。没跑：只能点启动。
        if running:
            paused = bool(snap.get("orders_paused"))
            self.btn_start.configure(
                state="normal",
                text="停止常驻",
                fg_color=T.DANGER,
                hover_color=T.DANGER_HOVER,
                text_color=T.DANGER_TEXT,
                command=self._on_halt_resident,
            )
            self.btn_stop.configure(
                state="normal",
                text="恢复报单" if paused else "暂停报单",
                fg_color=T.ACCENT if paused else T.DANGER,
                hover_color=T.ACCENT_HOVER if paused else T.DANGER_HOVER,
                text_color=T.ACCENT_ON if paused else T.DANGER_TEXT,
            )
        else:
            self.btn_start.configure(
                state="normal",
                text="启动常驻",
                fg_color=T.ACCENT,
                hover_color=T.ACCENT_HOVER,
                text_color=T.ACCENT_ON,
                command=self._on_start,
            )
            self.btn_stop.configure(
                state="disabled",
                text="暂停报单",
                fg_color=T.DANGER_MUTED,
                text_color_disabled=T.DANGER_TEXT_DISABLED,
            )
        # 运行页只补顶部没有的周期和开关。策略、通道、品种已经在状态栏。
        if hasattr(self, "route_summary"):
            bits = [
                f"信号{period_label(self._period_code(self.tf_var, self.tf_custom_var))}",
                f"止损{period_label(self._period_code(self.trail_var, self.trail_custom_var))}",
            ]
            self.route_summary.configure(text=" · ".join(bits))


    def _fit_status_note_wrap(self, _event: Any = None) -> None:
        """窗口拉宽时，三行提示跟着折行，避免字跑出右边缘。"""
        box = getattr(self, "status_notes", None)
        if box is None:
            return
        width = max(240, int(box.winfo_width() or 0) - 4)
        for name in ("alert_line", "health_line", "dyn_line"):
            label = getattr(self, name, None)
            if label is not None:
                label.configure(wraplength=width)


    def _show_status_notes(
        self,
        alert_text: str,
        health_text: str,
        health_tone: str,
        dyn_text: str,
    ) -> None:
        """
        有字才占一行。三句都可能很长，不并成一行，免得挤成看不清的一段。
        """
        if not hasattr(self, "alert_line"):
            return
        width = 900
        box = getattr(self, "status_notes", None)
        if box is not None:
            width = max(240, int(box.winfo_width() or 0) - 4) or 900
        rows = (
            (self.alert_line, (alert_text or "").strip(), T.DANGER),
            (self.health_line, (health_text or "").strip(), health_tone or T.TEXT_DIM),
            (self.dyn_line, (dyn_text or "").strip(), T.TEXT_DIM),
        )
        for label, _, _ in rows:
            label.pack_forget()
        for label, text, color in rows:
            if not text:
                label.configure(text="")
                continue
            label.configure(text=text, text_color=color, wraplength=width, height=18)
            label.pack(fill="x", pady=0)


    def _position_card_text(self, snap: dict[str, Any] | None) -> str:
        """
        顶部「程序持仓」的字。

        常驻开着时用内存里的仓，这样刚下完单马上能看见。
        没开常驻时读 data/bot_state.json，关掉再打开也能看到上次记下的仓。
        """
        from src.utils.state_store import format_position_text, load_state

        positions = (snap or {}).get("positions") if snap else None
        if positions is None:
            try:
                from src.utils.state_store import default_state_path

                positions = load_state(default_state_path(self.root_dir)).get("positions") or {}
            except Exception:  # noqa: BLE001
                positions = {}
        return format_position_text(positions)


    def _tick(self) -> None:
        """每 800ms 刷状态卡和账户余额。内部任何一步异常都不能让心跳链断掉。"""
        if self._closing:
            return
        try:
            self._refresh_status_cards()
        except Exception as exc:  # noqa: BLE001
            logger.warning("状态卡刷新跳过一轮: {}", exc)
        try:
            self._pull_signal_balance()
        except Exception as exc:  # noqa: BLE001
            logger.warning("账户余额刷新跳过一轮: {}", exc)
        finally:
            if not self._closing:
                try:
                    self._tick_after_id = self.after(800, self._tick)
                except Exception:  # noqa: BLE001
                    pass

