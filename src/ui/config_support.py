"""Configuration-tab integration, presets, watch lists, and contract-table actions."""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from src.ui import theme as T
from src.ui.app_common import ENGINE_CHOICES
from src.ui.app_common import _same_contract
from src.ui.app_common import _ui_engine
from src.strategy.rule_dsl import rule_engine_stem
from src.utils import load_config
from src.utils import save_config
from tkinter import messagebox
import customtkinter as ctk
import os
import subprocess
import sys
import threading


class ConfigSupportMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _build_config_tab(self, tab: ctk.CTkFrame) -> None:
        """配置页已拆到 src/ui/config_tab.py，这里只保留入口。"""
        from src.ui.config_tab import build_config_tab

        build_config_tab(self, tab, ENGINE_CHOICES)

    def _build_rule_editor_tab(self, tab: ctk.CTkFrame) -> None:
        """「策略编辑器」页签，界面在 src/ui/rule_editor_page.py。"""
        from src.ui.rule_editor_page import build_rule_editor_tab

        build_rule_editor_tab(self, tab)


    def _pack_watch_editor(self, parent: ctk.CTkFrame) -> None:
        """
        配置页的关注品种。

        下拉框是已经加入的具体合约，选中的那一个会显示在运行页顶部。
        右边的文本框用来手工填写品种加合约，例如 bu2612，点添加后才进入下拉框。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row, text="关注品种", font=T.FONT_BODY, text_color=T.TEXT_DIM, width=140, anchor="w"
        ).pack(side="left", padx=(0, 8))
        values = self._watch_symbols or ["（还没有）"]
        self.watch_menu = self._option(row, self.watch_choice_var, values, width=140)
        self.watch_menu.configure(command=self._on_watch_picked)
        self.watch_menu.pack(side="left")
        ctk.CTkEntry(
            row,
            textvariable=self.watch_add_var,
            width=110,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
            placeholder_text="如 bu2612",
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="添加",
            width=64,
            height=32,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._on_watch_add,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="移出",
            width=64,
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.BORDER,
            border_width=1,
            border_color=T.BORDER,
            text_color=T.TEXT,
            command=self._on_watch_remove,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            parent,
            text="下拉是当前启用的合约。添加/移出会同步进「品种与合约」总表。保证金、乘数、手续费、交易时段、清仓时刻请在总表里改；同品种必须填成一样。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        # 当前合约不是主力时，在这里亮一行黄底提示。平时高度压到 1 像素，不占地方。
        self.main_contract_hint = ctk.CTkLabel(
            parent,
            text="",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            fg_color="transparent",
            corner_radius=8,
            height=1,
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.main_contract_hint.pack(fill="x", pady=(0, 4))


    def _refresh_strategy_param_panel(self, *_args: object) -> None:
        """
        右边只显示当前策略的参数面板。

        原理：每套策略的输入控件一开始就建好并绑在 StringVar 上；
        换引擎时只 pack / pack_forget，不销毁控件，所以没展示的策略数值也不会丢。
        换完后按内容重算右侧高度，避免「少参数策略」还留着一大块空白。
        """
        panels = getattr(self, "_strategy_detail_panels", None) or {}
        if not panels:
            return
        current = _ui_engine(self.engine_var.get())
        # 自定义规则（rule_dsl:<文件名>）共用同一块面板，面板里可以换文件。
        is_rule_engine = bool(rule_engine_stem(current))
        if is_rule_engine and "rule_dsl" in panels:
            current = "rule_dsl"
        # 别名都指到同一块面板（如 ma_cross / dual_ma）。
        show = panels.get(current)
        if show is None:
            show = panels.get("daily_body")
            current = "daily_body"
        meta = getattr(self, "_strategy_detail_meta", {}) or {}
        title, note = meta.get(current, ("当前策略参数", ""))
        try:
            self._strategy_detail_title.configure(text=title)
            self._strategy_detail_note.configure(text=note)
        except Exception:  # noqa: BLE001
            pass
        for panel in panels.values():
            try:
                panel.pack_forget()
            except Exception:  # noqa: BLE001
                pass
        if show is not None:
            show.pack(fill="x", anchor="n")
        # 切到自定义规则时，把对应的 YAML 读进编辑框。
        if is_rule_engine:
            self._refresh_rule_editor()
        # 换策略后说明文字换行宽度跟着右侧实际宽度走。
        try:
            self.after_idle(self._sync_strategy_param_wrap)
        except Exception:  # noqa: BLE001
            self._sync_strategy_param_wrap()

    def _refresh_generic_stop_param_panel(self, *_args: object) -> None:
        """
        通用止损：左边选方式，右边只显示这种方式要填的参数。

        和策略引擎一样，各方式的面板一开始就建好并绑在 StringVar 上；
        切换时只 pack / pack_forget，不销毁控件，其它方式的数值也不会丢。
        """
        panels = getattr(self, "_generic_stop_detail_panels", None) or {}
        if not panels:
            return
        from src.strategy.generic_stop import generic_stop_from_label

        kind = generic_stop_from_label(self.generic_stop_var.get())
        show = panels.get(kind) or panels.get("off")
        meta = getattr(self, "_generic_stop_detail_meta", {}) or {}
        title, note = meta.get(kind, ("通用止损参数", ""))
        try:
            self._generic_stop_detail_title.configure(text=title)
            self._generic_stop_detail_note.configure(text=note)
        except Exception:  # noqa: BLE001
            pass
        for panel in panels.values():
            try:
                panel.pack_forget()
            except Exception:  # noqa: BLE001
                pass
        if show is not None:
            show.pack(fill="x", anchor="n")


    def _sync_strategy_param_wrap(self) -> None:
        """
        按右侧参数框当前实际宽度，刷新整段说明的换行。

        只改 wraplength，不改控件宽高，避免 Tk 用非法尺寸画背景图时报 CreateDIBSection。
        """
        shell = getattr(self, "_strategy_detail_shell", None)
        if shell is None:
            return
        try:
            shell.update_idletasks()
            w = int(shell.winfo_width() or 0)
            min_w = int(getattr(self, "_strategy_param_min_width", 360) or 360)
            # 尚未完成布局时宽度可能是 1，先用最小宽，等 Configure 再校正。
            if w < min_w:
                w = min_w
            # 防止异常超大宽度把 wraplength 撑爆。
            w = min(w, 1600)
            # 说明文字固定不超过 420，避免窗口一拉宽就把字画出蓝框。
            wrap = max(200, min(420, w - 40))
            self._strategy_param_hint_wrap = 200
            try:
                self._strategy_detail_note.configure(wraplength=wrap)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


    def _on_strategy_split_configure(self, event: object | None = None) -> None:
        """
        分栏宽度变化时，只同步右侧说明换行。

        横向铺满由 grid weight=1 完成；这里不再手写 width/height，避免启动时尺寸非法。
        """
        split = getattr(self, "_strategy_split", None)
        if split is None:
            return
        try:
            split_w = int(split.winfo_width() or 0)
            if split_w <= 1:
                return
            last_w = int(getattr(self, "_strategy_split_last_w", 0) or 0)
            if abs(split_w - last_w) < 4:
                return
            self._strategy_split_last_w = split_w
            self._sync_strategy_param_wrap()
        except Exception:  # noqa: BLE001
            pass


    def _toggle_password(self) -> None:
        self.pwd_entry.configure(show="" if self._show_password.get() else "*")


    def _toggle_tq_password(self) -> None:
        self.tq_pwd_entry.configure(show="" if self._show_tq_password.get() else "*")


    def _refresh_tq_hint(self) -> None:
        """
        选天勤时说明能拉什么周期。日线原生可用；周线用日线合成，不是直接要一周。
        """
        label = getattr(self, "tq_hint_label", None)
        if label is None:
            return
        from src.data.quote_check import normalize_source

        if normalize_source(self.source_var.get()) != "tq":
            label.configure(text="", text_color=T.TEXT_DIM)
            return
        label.configure(
            text="天勤免费接口：分钟、小时、日线可直接拉；周线用日线合成。多因子的日线方向也能用。账号密码填在上面「账号与登录」。",
            text_color=T.TEXT_DIM,
        )


    def _on_save(self) -> None:
        # 记下配置页和回测页各自的信号/止损周期，保存后把「还跟着配置页」的那项一起换掉。
        old_st = self.cfg.get("strategy") or {}
        old_tf = str(old_st.get("timeframe") or "")
        old_trail = str(old_st.get("trail_timeframe") or "")
        try:
            old_limit = float((self.cfg.get("risk") or {}).get("max_daily_loss") or 0)
        except (TypeError, ValueError):
            old_limit = 0.0
        cur_tf = self._period_code(self.bt_tf_var, self.bt_tf_custom_var)
        cur_trail = self._period_code(self.bt_trail_var, self.bt_trail_custom_var)
        # 保存前用最新余额填上账户权益。这个数不能在配置页手改。
        self._pull_signal_balance()
        # 扫描间隔夹到 5~600 秒，并把夹过的值显示回输入框，避免界面和配置对不上。
        self.scan_var.set(str(self._scan_interval_value()))
        cfg = self._merge_disk_gui_coords(self._apply_ui_to_cfg())
        path = save_config(cfg)
        self._sync_bt_periods_after_save(old_tf, old_trail, cur_tf, cur_trail, cfg)
        self._status = "已保存"
        self._append_log(f"配置已保存: {path}")
        # 常驻正在跑时，把新间隔推给它：主循环每轮都读这个属性，不用重启就能生效。
        bot = getattr(self.worker, "bot", None)
        if bot is not None and bool(getattr(bot, "is_running", False)):
            bot.scan_interval = int((cfg.get("schedule") or {}).get("scan_interval_sec") or 30)
            if hasattr(bot, "cfg"):
                bot.cfg.setdefault("schedule", {})["trading_sessions"] = list(
                    (cfg.get("schedule") or {}).get("trading_sessions") or []
                )
                risk_box = (cfg.get("risk") or {}).get("contracts")
                if isinstance(risk_box, dict):
                    bot.cfg.setdefault("risk", {})["contracts"] = risk_box
            if hasattr(bot, "_refresh_sessions"):
                bot._refresh_sessions()
            # 日亏限额也一起推给运行中的风控，否则它仍按启动时的旧限额熔断。
            bot.sync_risk_limits(float((cfg.get("risk") or {}).get("max_daily_loss") or 0))
            self._append_log(
                f"扫描间隔已改为 {bot.scan_interval} 秒；当前合约时段已同步，下一轮扫描起生效。"
            )
        try:
            new_limit = float((cfg.get("risk") or {}).get("max_daily_loss") or 0)
        except (TypeError, ValueError):
            new_limit = 0.0
        if new_limit > 0 and abs(new_limit - old_limit) > 0.5:
            try:
                from src.ops.alerts import prune_stale_halt_alerts

                prune_stale_halt_alerts(self.root_dir, new_limit)
            except OSError as exc:
                logger.debug("旧限额熔断告警没清掉: {}", exc)
        if self.password_var.get():
            self._append_log("密码已写入 config/secrets.local.yaml（本地私密）")
        try:
            from src.ui.config_presets import add_preset

            entry = add_preset(self.root_dir, cfg)
            self._refresh_config_presets(select_id=str(entry.get("id") or ""))
            self._append_log(f"配置存档已留下: {entry.get('name')}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("留下配置存档失败")
            self._append_log(f"配置文件已保存，但存档没有留下: {exc}")
        self._refresh_status_cards()


    def _refresh_config_presets(self, select_id: str | None = None) -> None:
        """刷新配置页的存档下拉。select_id 有值时停在刚保存的那一份。"""
        if not hasattr(self, "cfg_preset_menu"):
            return
        from src.ui.config_presets import load_presets

        items = load_presets(self.root_dir)
        self._cfg_preset_ids = {}
        labels: list[str] = []
        chosen = "（还没有保存过配置）"
        for item in items:
            label = str(item.get("name") or item.get("id") or "")
            vid = str(item.get("id") or "")
            if not label or not vid:
                continue
            if label in self._cfg_preset_ids:
                label = f"{label} ({vid})"
            self._cfg_preset_ids[label] = vid
            labels.append(label)
            if select_id and vid == select_id:
                chosen = label
        if not labels:
            labels = ["（还没有保存过配置）"]
        elif select_id is None:
            chosen = labels[0]
        self.cfg_preset_menu.configure(values=labels)
        self.cfg_preset_var.set(chosen if chosen in labels else labels[0])


    def _on_apply_config_preset(self) -> None:
        """把选中的存档写回配置页和配置文件。常驻正在跑时不应用。"""
        from tkinter import messagebox

        from src.ui.config_presets import apply_preset_to_config, get_preset, switch_summary
        from src.utils.config_loader import load_config

        label = self.cfg_preset_var.get()
        vid = (self._cfg_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("还没有存档", "请先点「保存全部配置」，再来一键应用。")
            return
        if self.worker.running:
            messagebox.showwarning(
                "请先停止常驻",
                "程序正在跑，不会自己换成新开关。\n请先点「停止常驻」，再一键应用，然后重新启动。",
            )
            return
        entry = get_preset(self.root_dir, vid)
        saved = (entry or {}).get("config")
        if not isinstance(saved, dict):
            messagebox.showerror("找不到存档", "这一份配置读不出来，请重新保存一次。")
            return
        if not messagebox.askyesno(
            "一键应用配置",
            f"将用这一份盖住当前配置页，并写入配置文件：\n\n{label}\n\n{switch_summary(saved)}\n\n确定？",
        ):
            return
        merged = apply_preset_to_config(load_config(), saved)
        save_config(self._merge_disk_gui_coords(merged))
        self._reload_values_only()
        self._refresh_status_cards()
        self._append_log(f"已一键应用配置: {label}")
        messagebox.showinfo("已应用", "配置页已经换成这一份。请再看一眼开关，然后启动常驻。")


    def _on_delete_config_preset(self) -> None:
        """删掉下拉框里选中的那一份配置存档。不改当前正在用的配置。"""
        from tkinter import messagebox

        from src.ui.config_presets import delete_preset

        label = self.cfg_preset_var.get()
        vid = (self._cfg_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("没有可删的存档", "请先在下拉框里选一份已经保存的配置。")
            return
        if not messagebox.askyesno("删除配置存档", f"只删除这一份备份，当前配置不会变：\n\n{label}\n\n确定删除？"):
            return
        try:
            removed = delete_preset(self.root_dir, vid)
        except ValueError as exc:
            messagebox.showwarning("没有删掉", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除配置存档失败")
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_config_presets()
        self._append_log(f"已删除配置存档: {removed}")


    def _refresh_coord_presets(self, select_id: str | None = None) -> None:
        """刷新下单坐标列表。select_id 有值时停在刚保存或刚改名的那一份。"""
        if not hasattr(self, "coord_preset_menu"):
            return
        from src.ui.gui_coord_presets import load_coord_presets

        items = load_coord_presets(self.root_dir)
        self._coord_preset_ids = {}
        labels: list[str] = []
        chosen = "（还没有保存过坐标）"
        for item in items:
            label = str(item.get("name") or item.get("id") or "")
            vid = str(item.get("id") or "")
            if not label or not vid:
                continue
            if label in self._coord_preset_ids:
                label = f"{label} ({vid})"
            self._coord_preset_ids[label] = vid
            labels.append(label)
            if select_id and vid == select_id:
                chosen = label
        if not labels:
            labels = ["（还没有保存过坐标）"]
        elif select_id is None:
            chosen = labels[0]
        self.coord_preset_menu.configure(values=labels)
        self.coord_preset_var.set(chosen if chosen in labels else labels[0])
        self._sync_coord_name_box()


    def _sync_coord_name_box(self, _label: str | None = None) -> None:
        """下拉换了一套时，名字框跟着换成这一套现在的名字。"""
        if not hasattr(self, "coord_name_var"):
            return
        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label, "")
        if not vid:
            self.coord_name_var.set("")
            return
        from src.ui.gui_coord_presets import get_coord_preset

        entry = get_coord_preset(self.root_dir, vid) or {}
        self.coord_name_var.set(str(entry.get("name") or label))


    def _on_save_coord_preset(self) -> None:
        """把配置文件里现在的下单点存成一份。名字空着就用时间。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import add_coord_preset, get_coord_preset
        from src.utils.config_loader import load_config

        gui = ((load_config().get("trade") or {}).get("gui") or {})
        # 名字框里如果还是上一套的名字，就按时间新存一份，避免盖成「家里显示器-2」。
        typed = self.coord_name_var.get().strip()
        selected = (self._coord_preset_ids or {}).get(self.coord_preset_var.get(), "")
        current_name = str((get_coord_preset(self.root_dir, selected) or {}).get("name") or "") if selected else ""
        name = "" if typed == current_name else typed
        try:
            entry = add_coord_preset(self.root_dir, gui, name=name)
        except ValueError as exc:
            messagebox.showwarning("还不能保存", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存下单坐标失败")
            messagebox.showerror("保存失败", str(exc))
            return
        self._refresh_coord_presets(select_id=str(entry.get("id") or ""))
        self._append_log(f"已保存下单坐标: {entry.get('name')}")


    def _on_apply_coord_preset(self) -> None:
        """把选中的一套点写成当前下单坐标。常驻正在跑时不换。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import apply_coords_to_config, get_coord_preset, write_gui_coords_file
        from src.utils.config_loader import load_config

        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("还没有坐标", "请先标定并点「保存当前坐标」。")
            return
        if self.worker.running:
            messagebox.showwarning("请先停止常驻", "程序正在跑，不会中途换点击位置。\n请先点「停止常驻」，再选用坐标，然后重新启动。")
            return
        entry = get_coord_preset(self.root_dir, vid) or {}
        coords = entry.get("coords")
        if not isinstance(coords, dict):
            messagebox.showerror("找不到坐标", "这一份读不出来，请重新保存一次。")
            return
        if not messagebox.askyesno("使用这套坐标", f"接下来下单会点这一套位置：\n\n{label}\n\n确定？"):
            return
        merged = apply_coords_to_config(load_config(), coords)
        save_config(merged)
        write_gui_coords_file(self.root_dir, coords)
        self.cfg = load_config()
        self._append_log(f"已改用下单坐标: {label}")
        messagebox.showinfo("已换上", "当前下单坐标已经换成这一套。请再启动常驻。")


    def _on_rename_coord_preset(self) -> None:
        """把选中的坐标改成输入框里的名字。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import rename_coord_preset

        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("没有可改的坐标", "请先保存一套坐标。")
            return
        try:
            new_name = rename_coord_preset(self.root_dir, vid, self.coord_name_var.get())
        except ValueError as exc:
            messagebox.showwarning("改名没有成功", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("下单坐标改名失败")
            messagebox.showerror("改名失败", str(exc))
            return
        self._refresh_coord_presets(select_id=vid)
        self._append_log(f"下单坐标已改名: {new_name}")


    def _on_delete_coord_preset(self) -> None:
        """删掉选中的坐标备份。正在用的点不受影响。"""
        from tkinter import messagebox

        from src.ui.gui_coord_presets import delete_coord_preset

        label = self.coord_preset_var.get()
        vid = (self._coord_preset_ids or {}).get(label)
        if not vid:
            messagebox.showwarning("没有可删的坐标", "请先在列表里选一份已经保存的坐标。")
            return
        if not messagebox.askyesno("删除坐标", f"只删除这一份备份，当前下单位置不会变：\n\n{label}\n\n确定删除？"):
            return
        try:
            removed = delete_coord_preset(self.root_dir, vid)
        except ValueError as exc:
            messagebox.showwarning("没有删掉", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("删除下单坐标失败")
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_coord_presets()
        self._append_log(f"已删除下单坐标: {removed}")


    def _on_reload(self) -> None:
        self._reload_values_only()
        self._status = "已重载"
        self._append_log("已从磁盘重新加载配置与本地密码。")
        self._refresh_status_cards()


    def _pack_margin_table_row(self, parent: ctk.CTkFrame) -> None:
        """
        品种与合约总表：路径 + 打开 + 重新读取 + 摘要。

        一张表管关注合约、保证金/乘数，以及每个品种自己的交易时段和清仓时刻。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row,
            text="品种与合约表",
            font=T.FONT_BODY,
            text_color=T.TEXT_DIM,
            width=140,
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(
            row,
            textvariable=self.margin_table_path_var,
            width=220,
            fg_color=T.BG_ELEVATED,
            border_color=T.BORDER,
            text_color=T.TEXT,
        ).pack(side="left")
        ctk.CTkButton(
            row,
            text="打开表格",
            width=84,
            height=32,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._open_margin_table,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="重新读取",
            width=84,
            height=32,
            fg_color=T.BG_ELEVATED,
            hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
            border_width=1,
            border_color=T.BORDER,
            command=lambda: self._reload_margin_table(silent=False),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            row,
            text="刷新全市场主力",
            width=132,
            height=32,
            fg_color=T.OK,
            hover_color=T.ACCENT_HOVER,
            text_color=T.ACCENT_ON,
            command=self._refresh_universe_from_tq,
        ).pack(side="left", padx=(8, 0))
        self.margin_table_summary = ctk.CTkLabel(
            parent,
            text="",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=420,
            justify="left",
            anchor="w",
        )
        self.margin_table_summary.pack(anchor="w", pady=(0, 4))
        self._refresh_margin_table_summary()


    def _margin_table_abs_path(self) -> Path:
        """按配置页路径算出总表绝对路径。"""
        from src.risk.universe_table import DEFAULT_REL_PATH, resolve_universe_path

        rel = str(self.margin_table_path_var.get() or "").strip() or DEFAULT_REL_PATH
        risk = self.cfg.setdefault("risk", {})
        risk["universe_table"] = rel
        risk["margin_table"] = rel
        return resolve_universe_path(self.cfg)


    def _refresh_universe_from_tq(self) -> None:
        """从天勤刷新全市场 81 个主力合约；独立线程跑，不卡界面。"""
        import threading

        def _thread() -> None:
            try:
                from tools.refresh_universe import refresh_universe_table

                rows = refresh_universe_table(dry_run=False)

                # 主线程弹结果
                def _on_done() -> None:
                    total = len(rows)
                    enabled = sum(1 for r in rows if r.get("enabled", True))
                    if hasattr(self, "margin_table_summary"):
                        self._reload_margin_table(silent=True)
                    messagebox.showinfo(
                        "主力合约刷新完成",
                        f"天勤主力查询 {total} 个品种全部成功。\n"
                        f"启用={enabled}（沿用原有关注品种），停用={total - enabled}。\n\n"
                        f"如果某个月分主力变了，品种代码已自动更新。\n"
                        f"每手数量已尽量从天勤行情写入。保证金比例天勤没有，"
                        f"请在表里手填（燃油常见 23），或从交易台合约页抄。\n"
                        f"想启用新品种，在 Excel 里把「启用」改成「是」即可。",
                    )

                self.after(0, _on_done)
            except Exception as exc:  # noqa: BLE001
                logger.error("刷新全市场主力失败: {}", exc)
                # except 块结束后 Python 会删掉 exc，闭包稍后在主线程跑时就取不到了，
                # 先转成字符串存起来。
                fail_text = f"{exc}"

                def _on_fail() -> None:
                    messagebox.showerror(
                        "刷新失败",
                        f"{fail_text}\n\n"
                        "可能原因：天勤 tqsdk 未安装（pip install tqsdk），"
                        "或账号密码没填（配置页 → 账号与登录 → 天勤行情账号/密码）。",
                    )

                self._post_to_main(_on_fail)

        threading.Thread(target=_thread, name="UniverseRefresh", daemon=True).start()
        messagebox.showinfo(
            "正在刷新",
            "正在问天勤查询 81 个品种的当前主力合约和每手数量，大约要一两分钟。\n"
            "点确定后请稍等，完成后会再弹结果。",
        )


    def _refresh_margin_table_summary(self) -> None:
        """刷新总表说明文字；并刷新上方关注下拉（不罗列全部合约）。"""
        from src.risk.margin_table import summarize_contracts

        symbols = list((self.cfg.get("strategy") or {}).get("symbols") or [])
        contracts = (self.cfg.get("risk") or {}).get("contracts") or {}
        # 回测页抄「当前品种保证金」仍用摘要字符串，只是界面下方不再展示长名单。
        self.margin_map_var.set(summarize_contracts(contracts))
        if symbols and symbols != list(self._watch_symbols):
            try:
                self._set_watch_codes(symbols, select=symbols[0], save=False)
            except Exception:  # noqa: BLE001
                pass
        text = (
            "改表后请先关掉 Excel，再点「重新读取」。保证金比例填 23 表示 23%。"
            "每手数量就是合约乘数。同品种比例/每手数量/手续费必须相同。"
            "刷新全市场主力能自动填每手数量；比例天勤拿不到，请对照期货公司或交易台合约页手填。"
        )
        try:
            self.margin_table_summary.configure(text=text)
        except Exception:  # noqa: BLE001
            pass


    def _reload_margin_table(self, *, silent: bool = False) -> bool:
        """
        从品种与合约总表载入关注名单和保证金，并写回配置文件。

        silent=True 用于保存流程；False 时弹窗提示。
        """
        from tkinter import messagebox

        from src.risk.universe_table import apply_universe_table_to_cfg

        try:
            path = self._margin_table_abs_path()
            symbols, contracts = apply_universe_table_to_cfg(
                self.cfg, path=path, create_if_missing=True
            )
            if symbols:
                self._set_watch_codes(symbols, select=symbols[0], save=False)
            self._refresh_margin_table_summary()
            self._sync_bt_margin_from_table()
            if not silent:
                try:
                    save_config(self.cfg)
                except Exception as save_exc:  # noqa: BLE001
                    logger.warning("总表已读入内存，但写回配置文件失败: {}", save_exc)
                    messagebox.showwarning(
                        "已读取，未写回文件",
                        "表格已读进程序，但 config.yaml 没写成功。\n"
                        "请再点一次「保存全部配置」。\n\n"
                        f"{save_exc}",
                    )
                    return True
                messagebox.showinfo(
                    "品种与合约",
                    f"已读取关注 {len(symbols)} 个合约、{len(contracts)} 个品种，并已写进配置。\n{path}",
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取品种与合约表失败: {}", exc)
            if not silent:
                messagebox.showerror(
                    "读取失败",
                    f"读不到品种与合约表。\n请先关掉 Excel 再试。\n同品种保证金必须相同。\n\n{exc}",
                )
            return False


    def _open_margin_table(self) -> None:
        """打开品种与合约总表；没有文件时先按当前配置生成。"""
        from tkinter import messagebox

        from src.risk.universe_table import ensure_universe_table

        try:
            path = self._margin_table_abs_path()
            ensure_universe_table(path, self.cfg)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开", f"打不开品种与合约表。\n\n{exc}")


    def _set_watch_codes(self, codes: list[str], select: str | None = None, save: bool = True) -> None:
        """
        用一份合约列表刷新关注下拉框。

        选中的那一项排到最前。添加、移出或换当前合约时马上写入配置文件，
        重新打开程序时整份名单还在。save=False 只用在从文件读回来的时候。
        """
        if getattr(self, "_watch_busy", False):
            return
        self._watch_busy = True
        try:
            self._apply_watch_codes(codes, select)
        finally:
            self._watch_busy = False
        if save:
            self._persist_watch_symbols()


    def _apply_watch_codes(self, codes: list[str], select: str | None = None) -> None:
        """真正改下拉框。外面的 _set_watch_codes 负责挡住重复刷新。"""
        cleaned: list[str] = []
        seen: set[str] = set()
        for code in codes:
            text = str(code).strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        if not cleaned:
            self._watch_symbols = []
            self.symbols_var.set("")
            self.watch_choice_var.set("（还没有）")
            if hasattr(self, "watch_menu"):
                self.watch_menu.configure(values=["（还没有）"])
            if hasattr(self, "card_symbols"):
                self.card_symbols.configure(text="-")
            self._sync_bt_symbol_menu()
            self._sync_extra_symbol_menus()
            self._refresh_cross_lists()
            self._refresh_main_contract_hint("")
            return
        chosen = select or cleaned[0]
        match = next((item for item in cleaned if item.lower() == chosen.lower()), cleaned[0])
        ordered = [match] + [item for item in cleaned if item.lower() != match.lower()]
        self._watch_symbols = ordered
        self.symbols_var.set(",".join(ordered))
        self.watch_choice_var.set(match)
        if hasattr(self, "watch_menu"):
            self.watch_menu.configure(values=ordered)
        if hasattr(self, "card_symbols"):
            self.card_symbols.configure(text=match)
        # 列表变了就让回测页的下拉一起变。回测页已经选中的合约还在，就先不改它。
        self._sync_bt_symbol_menu()
        self._sync_extra_symbol_menus()
        self._refresh_cross_lists()
        self._refresh_main_contract_hint(match)


    def _persist_watch_symbols(self) -> None:
        """
        把关注名单写进总表和配置文件。

        添加/移出后立刻改 Excel「启用」列，再写 yaml，保证只维护一处源头。
        """
        from src.risk.universe_table import sync_watch_symbols_to_table

        symbols = list(self._watch_symbols)
        self.cfg.setdefault("strategy", {})["symbols"] = symbols
        if symbols:
            self.cfg.setdefault("trade", {})["symbol"] = symbols[0]
        try:
            path = self._margin_table_abs_path()
            sync_watch_symbols_to_table(
                path,
                symbols,
                contracts=(self.cfg.get("risk") or {}).get("contracts") or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("关注名单没有写入品种与合约表: {}", exc)
        try:
            disk = load_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取配置失败，关注名单先留在窗口里: {}", exc)
            return
        disk.setdefault("strategy", {})["symbols"] = symbols
        if symbols:
            disk.setdefault("trade", {})["symbol"] = symbols[0]
        try:
            save_config(self._merge_disk_gui_coords(disk))
        except Exception as exc:  # noqa: BLE001
            logger.warning("关注名单没有写入配置文件: {}", exc)


    def _config_symbol_now(self) -> str:
        """配置页关注品种下拉框里，现在选中的那一个合约。"""
        current = self.watch_choice_var.get().strip()
        if current and current != "（还没有）":
            return current
        if self._watch_symbols:
            return self._watch_symbols[0]
        return ""


    def _set_main_contract_hint(self, text: str, *, warn: bool = False) -> None:
        """写关注品种下面那行提示。空文本时把高度压到 1 像素。"""
        label = getattr(self, "main_contract_hint", None)
        if label is None:
            return
        if not text:
            label.configure(
                text="", fg_color="transparent", text_color=T.TEXT_DIM, height=1, border_width=0
            )
        elif warn:
            label.configure(
                text=text,
                fg_color="#3A2A12",
                text_color=T.WARN,
                height=48,
                corner_radius=8,
                border_width=1,
                border_color=T.WARN,
            )
        else:
            label.configure(
                text=text, fg_color="transparent", text_color=T.TEXT_DIM, height=40, border_width=0
            )


    def _delivery_hint_for(self, code: str) -> tuple[str, bool]:
        """关注合约的交割月提示。认不出年月时空着。"""
        try:
            from src.risk.delivery import live_hint

            return live_hint(code)
        except Exception:  # noqa: BLE001
            return "", False


    def _refresh_main_contract_hint(self, symbol: str | None = None) -> None:
        """
        关注品种下面提示交割月，以及（选天勤时）是不是主力。

        交割月按合约代码的年月本地就能算，不联网。主力合约向天勤的主连要。
        查询放在后台线程；换合约后旧结果直接丢掉。
        只有行情来源选「天勤」才登录天勤；选新浪/东方财富时不连。
        """
        label = getattr(self, "main_contract_hint", None)
        if label is None:
            return
        from src.data.quote_check import is_tq_source

        code = (symbol or self._config_symbol_now()).strip()
        self._main_contract_seq = getattr(self, "_main_contract_seq", 0) + 1
        seq = self._main_contract_seq
        if not code or code == "（还没有）" or not any(ch.isdigit() for ch in code):
            self._set_main_contract_hint("")
            return
        delivery_text, delivery_warn = self._delivery_hint_for(code)
        if not is_tq_source(self.source_var.get()):
            self._set_main_contract_hint(delivery_text, warn=delivery_warn)
            return
        tq_user = self.tq_user_var.get().strip()
        tq_password = self.tq_password_var.get().strip()
        if not tq_user or not tq_password:
            self._set_main_contract_hint(delivery_text, warn=delivery_warn)
            return
        waiting = "正在查询主力合约…"
        shown = f"{delivery_text} {waiting}".strip() if delivery_text else waiting
        self._set_main_contract_hint(shown, warn=delivery_warn)

        def run() -> None:
            from src.data.tq_source import main_contract

            try:
                found = main_contract(code, user=tq_user, password=tq_password)
            except Exception as exc:  # noqa: BLE001
                logger.debug("查询主力合约失败 {}: {}", code, exc)
                found = ""
            try:
                self._post_to_main(lambda: self._apply_main_contract_result(seq, code, found))
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=run, name="main-contract", daemon=True).start()


    def _apply_main_contract_result(self, seq: int, code: str, found: str) -> None:
        """后台查到主力后回到界面。慢一步的旧查询结果不再显示。"""
        if seq != getattr(self, "_main_contract_seq", 0):
            return
        if self._config_symbol_now().lower() != code.lower():
            return
        delivery_text, delivery_warn = self._delivery_hint_for(code)
        main_text = ""
        if not found:
            main_text = "暂时查不到主力合约（天勤未登录或网络不可用）"
        elif not _same_contract(code, found):
            main_text = f"当前合约非主力合约，主力合约为：{found}"
        combined = " ".join(part for part in (delivery_text, main_text) if part).strip()
        self._set_main_contract_hint(combined, warn=bool(delivery_warn or (found and main_text)))

    # ---------------------------------------------------------- 自定义规则编辑器

    def _refresh_rule_list(self, highlight: str = "") -> None:
        page = getattr(self, "rule_page", None)
        if page is not None:
            page.refresh_list(highlight)

    def _refresh_rule_editor(self, select_stem: str = "") -> None:
        """刷新左侧文件列表，并把选中的规则读进编辑框。"""
        from src.strategy.rule_dsl import list_rule_files, rule_engine_stem

        if getattr(self, "rule_editor", None) is None:
            return
        stems = [str(info["stem"]) for info in list_rule_files()]
        stem = select_stem or rule_engine_stem(self.engine_var.get())
        if not stem:
            configured = (self.cfg.get("strategy") or {}).get("rule_dsl") or {}
            stem = Path(str(configured.get("file") or "")).stem
        if stem not in stems:
            stem = stems[0] if stems else ""
        if not stem:
            self._rule_loaded_stem = ""
            self.rule_name_var.set("")
            self._rule_saved_text = ""
            self._rule_dirty = False
            self._set_rule_dirty(False, "")
            self._refresh_rule_list()
            self.rule_status_var.set("还没有规则文件，点左下角「新建规则」写一条。")
            return
        self._load_rule_text(stem)

    def _load_rule_text(self, stem: str) -> None:
        from src.strategy.rule_dsl import NEW_RULE_TEXT, is_builtin_rule, rules_dir

        self._rule_loaded_stem = stem
        self.rule_name_var.set(stem)
        try:
            text = (rules_dir() / f"{stem}.yaml").read_text(encoding="utf-8")
        except OSError:
            text = NEW_RULE_TEXT
        self.rule_editor.delete("1.0", "end")
        self.rule_editor.insert("1.0", text)
        self._rule_saved_text = text
        self._rule_dirty = False
        try:
            self.rule_editor.edit_modified(False)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_rule_list(highlight=stem)
        self._set_rule_dirty(False, stem)
        builtin = is_builtin_rule(stem)
        tag = "（程序自带，不可删改，可另存为）" if builtin else ""
        self.rule_status_var.set(f"正在编辑 {stem}.yaml{tag}；改完点「保存」。")

    def _on_rule_open(self, stem: str) -> None:
        """点左侧文件列表；有未保存修改先确认。"""
        if not stem or stem == getattr(self, "_rule_loaded_stem", ""):
            return
        if not self._confirm_discard_rule():
            return
        self._load_rule_text(stem)

    def _on_rule_new(self) -> None:
        from src.strategy.rule_dsl import NEW_RULE_TEXT

        if not self._confirm_discard_rule():
            return
        self._rule_loaded_stem = ""
        self.rule_name_var.set("")
        self.rule_editor.delete("1.0", "end")
        self.rule_editor.insert("1.0", NEW_RULE_TEXT)
        self._rule_saved_text = ""
        self._rule_dirty = False
        try:
            self.rule_editor.edit_modified(False)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_rule_list()
        self._set_rule_dirty(False, "")
        self.rule_status_var.set("填个文件名（小写字母/数字/下划线），写好后点「保存」。")

    def _on_rule_modified(self) -> None:
        """编辑框变动时维护未保存标记，标题上的 * 跟着变。"""
        editor = getattr(self, "rule_editor", None)
        if editor is None:
            return
        try:
            if not editor.edit_modified():
                return
            editor.edit_modified(False)
            text = editor.get("1.0", "end")
        except Exception:  # noqa: BLE001
            return
        dirty = text.strip() != (self._rule_saved_text or "").strip()
        self._set_rule_dirty(dirty, getattr(self, "_rule_loaded_stem", ""))

    def _set_rule_dirty(self, dirty: bool, stem: str) -> None:
        self._rule_dirty = dirty
        page = getattr(self, "rule_page", None)
        if page is not None:
            page.set_dirty(dirty, stem)

    def _confirm_discard_rule(self) -> bool:
        if not getattr(self, "_rule_dirty", False):
            return True
        return bool(
            messagebox.askyesno("未保存的修改", "当前规则有未保存的修改，放弃并继续？", parent=self)
        )

    def _on_rule_check(self) -> None:
        """只编译不写盘。"""
        from src.strategy.rule_dsl import RuleConfigError, compile_rule_text

        text = self.rule_editor.get("1.0", "end").strip()
        try:
            rule = compile_rule_text(text)
        except RuleConfigError as exc:
            self.rule_status_var.set(f"校验没通过：{exc}")
            return
        self.rule_status_var.set(f"校验通过：{rule.name}。还没写盘，点「保存」才会存成文件。")

    def _save_rule_from_editor(self) -> str:
        """校验并写盘当前编辑内容。成功返回文件名，失败返回空串并更新状态行。"""
        from src.strategy.rule_dsl import (
            RuleConfigError,
            is_rule_stem,
            rules_dir,
            save_rule_text,
        )
        from src.ui.app_common import register_rule_engines

        stem = self.rule_name_var.get().strip().lower()
        if not is_rule_stem(stem):
            self.rule_status_var.set("文件名只能用小写字母、数字、下划线或短横线，且以字母或数字开头。")
            return ""
        text = self.rule_editor.get("1.0", "end").strip()
        try:
            rule = save_rule_text(rules_dir() / f"{stem}.yaml", text)
        except RuleConfigError as exc:
            self.rule_status_var.set(f"没有保存：{exc}")
            return ""
        register_rule_engines()
        try:
            self.engine_menu.configure(values=list(ENGINE_CHOICES))
        except Exception:  # noqa: BLE001
            pass
        self._refresh_rule_editor(select_stem=stem)
        self.rule_status_var.set(
            f"已保存：{stem}.yaml（{rule.name}）。要在运行/回测里用它，"
            "到「配置」页的策略引擎下拉里选择；保存本身不会切换运行策略。"
        )
        return stem


    def _on_rule_save(self) -> None:
        """校验通过才写盘；只注册进策略下拉，不自动切换当前运行策略。"""
        self._save_rule_from_editor()

    def _on_rule_backtest(self) -> None:
        """用「历史回测」页的条件跑当前规则并生成绩效报告。

        不另写回测：品种、周期、日期、行情来源、滑点和开仓限制都读历史回测页，
        只把策略换成正在编辑的规则；报告照旧写到 data/backtest_last.json，
        同时显示在编辑框下方的报告框里。
        """
        from src.strategy.rule_dsl import (
            RuleConfigError,
            compile_rule_text,
            is_builtin_rule,
            is_rule_stem,
            rule_engine_code,
            rules_dir,
        )

        stem = self._current_rule_stem()
        if not is_rule_stem(stem):
            self.rule_status_var.set("先填一个合法的文件名（小写字母/数字/下划线），再回测当前规则。")
            return
        text = self.rule_editor.get("1.0", "end").strip()
        try:
            compile_rule_text(text)
        except RuleConfigError as exc:
            self.rule_status_var.set(f"规则没通过校验，未开始回测：{exc}")
            return
        path = rules_dir() / f"{stem}.yaml"
        if text != (self._rule_saved_text or "").strip() or not path.exists():
            if is_builtin_rule(stem):
                messagebox.showwarning(
                    "内置规则不能覆盖",
                    f"{stem}.yaml 是程序自带规则。要回测改动后的内容，"
                    "请先在文件名里填新名字并「保存」为自己的规则。",
                )
                return
            if not messagebox.askyesno(
                "回测前先保存",
                f"当前规则有未保存的修改。\n回测读的是磁盘上的 {stem}.yaml，先保存再跑？",
            ):
                return
            if not self._save_rule_from_editor():
                messagebox.showwarning("没有保存", "规则没写盘，回测没有开始。看编辑框下方的提示。")
                return
        self._start_backtest(
            engine_override=rule_engine_code(stem),
            result_box=getattr(self, "rule_bt_result_box", None),
            switch_tab=False,
            header="规则回测",
        )

    def _update_rule_summary(self) -> None:
        """配置页里「当前运行策略对应哪个规则文件」的摘要。"""
        from src.strategy.rule_dsl import is_builtin_rule, rule_engine_stem

        stem = rule_engine_stem(self.engine_var.get())
        if stem:
            if is_builtin_rule(stem):
                self.rule_summary_var.set(f"当前运行策略：{stem}.yaml（程序自带只读，可另存为）")
            else:
                self.rule_summary_var.set(f"当前运行策略：{stem}.yaml（自己的规则，可改可删）")
            return
        if str(self.engine_var.get()).startswith("rule_dsl:"):
            self.rule_summary_var.set("这条规则文件已不存在；运行时会回退到「日内实体」。")
        else:
            self.rule_summary_var.set("当前运行策略不是 DSL 规则；要写自定义策略，点下面按钮。")

    def _open_rule_editor(self) -> None:
        """从配置页跳到策略编辑器，并顺手载入当前运行策略对应的规则。"""
        from src.strategy.rule_dsl import rule_engine_stem

        try:
            self.tabs.set("策略编辑器")
        except Exception:  # noqa: BLE001
            pass
        stem = rule_engine_stem(self.engine_var.get())
        if stem and stem != getattr(self, "_rule_loaded_stem", ""):
            if self._confirm_discard_rule():
                self._load_rule_text(stem)
        elif not getattr(self, "_rule_loaded_stem", ""):
            self._refresh_rule_editor()

    def _current_rule_stem(self) -> str:
        """取当前编辑的规则文件名：优先已加载的规则，其次文件名字段。"""
        loaded = getattr(self, "_rule_loaded_stem", "") or ""
        if loaded:
            return loaded
        return self.rule_name_var.get().strip().lower()

    def _on_rule_delete(self) -> None:
        """删除当前用户规则；内置规则拒绝。"""
        from src.strategy.rule_dsl import (
            RuleConfigError,
            delete_rule_file,
            is_builtin_rule,
        )
        from src.ui.app_common import register_rule_engines

        stem = self._current_rule_stem()
        if not stem:
            self.rule_status_var.set("先选一条要删除的规则（非内置）。")
            return
        if is_builtin_rule(stem):
            self.rule_status_var.set(f"{stem}.yaml 是程序自带规则，不能删除；想改就「另存为」自己的规则。")
            return
        try:
            delete_rule_file(stem)
        except RuleConfigError as exc:
            self.rule_status_var.set(f"没有删除：{exc}")
            return
        register_rule_engines()
        try:
            self.engine_menu.configure(values=list(ENGINE_CHOICES))
        except Exception:  # noqa: BLE001
            pass
        # 删的是当前引擎就回退到日内实体，避免界面引用已删除的策略。
        if rule_engine_stem(self.engine_var.get()) == stem:
            self.engine_var.set("daily_body")
        self._rule_loaded_stem = ""
        self._refresh_rule_editor()
        self.rule_status_var.set(f"已删除 {stem}.yaml，并从策略下拉移除。")

    def _on_rule_rename(self) -> None:
        """把当前加载的规则改名为文件名字段里的新名字；内置规则拒绝。"""
        from src.strategy.rule_dsl import (
            RuleConfigError,
            is_builtin_rule,
            rename_rule_file,
            rule_engine_code,
            rule_engine_stem,
        )
        from src.ui.app_common import register_rule_engines

        old = getattr(self, "_rule_loaded_stem", "") or ""
        new = self.rule_name_var.get().strip().lower()
        if not old:
            self.rule_status_var.set("先加载一条用户规则，再在文件名字段填新名字后点重命名。")
            return
        if not new:
            self.rule_status_var.set("重命名需要在文件名字段填上新名字。")
            return
        if old == new:
            self.rule_status_var.set("新旧文件名一样，没有改。")
            return
        if is_builtin_rule(old):
            self.rule_status_var.set(f"{old}.yaml 是程序自带规则，不能改名；想改就「另存为」自己的规则。")
            return
        try:
            rename_rule_file(old, new)
        except RuleConfigError as exc:
            self.rule_status_var.set(f"没有改名：{exc}")
            return
        register_rule_engines()
        try:
            self.engine_menu.configure(values=list(ENGINE_CHOICES))
        except Exception:  # noqa: BLE001
            pass
        # 当前引擎指向旧名字时切到新名字。
        if rule_engine_stem(self.engine_var.get()) == old:
            self.engine_var.set(rule_engine_code(new))
        self._rule_loaded_stem = new
        self._refresh_rule_editor(select_stem=new)
        self.rule_status_var.set(f"已把 {old}.yaml 改名为 {new}.yaml。")

    def _sync_bt_symbol_menu(self, prefer: str | None = None, force: bool = False) -> None:
        """
        把历史回测品种下拉刷成配置页现在的关注列表。

        force 为真时，选中项改成 prefer，也就是配置页当前合约。
        平时只更新列表，回测页自己选过的合约还在列表里就留着。
        """
        from src.ui.backtest_fill import choose_backtest_symbol

        values = list(self._watch_symbols) or ["（还没有）"]
        chosen = choose_backtest_symbol(
            values,
            self.bt_symbol_var.get(),
            prefer=prefer,
            force=force,
        )
        if hasattr(self, "bt_symbol_menu"):
            self.bt_symbol_menu.configure(values=values)
            self.bt_symbol_menu.set(chosen)
        else:
            self.bt_symbol_var.set(chosen)


    def _sync_symbol_choice(self, menu_name: str, var, prefer: str | None = None, force: bool = False) -> None:
        """把某一个下拉刷成关注名单。force 为真时改成 prefer，否则已选中的还在就留着。"""
        from src.ui.backtest_fill import choose_backtest_symbol

        values = list(self._watch_symbols) or ["（还没有）"]
        chosen = choose_backtest_symbol(values, var.get(), prefer=prefer, force=force)
        menu = getattr(self, menu_name, None)
        if menu is not None:
            menu.configure(values=values)
            menu.set(chosen)
        else:
            var.set(chosen)


    def _sync_extra_symbol_menus(self, prefer: str | None = None, force: bool = False) -> None:
        """
        策略优化和复盘的品种下拉跟关注名单一起变。

        平时不改已经选中的合约。force 为真时改成 prefer，也就是配置页或回测页当前那一个。
        """
        self._sync_symbol_choice("opt_symbol_menu", self.opt_symbol_var, prefer=prefer, force=force)
        self._sync_symbol_choice("rv_symbol_menu", self.rv_symbol_var, prefer=prefer, force=force)
        self._sync_symbol_choice("rs_symbol_menu", self.rs_symbol_var, prefer=prefer, force=force)


    def _on_watch_picked(self, choice: str) -> None:
        """下拉框换了当前合约。运行页顶部跟着换，列表里的其他合约还留着。"""
        if choice in ("", "（还没有）"):
            return
        self._set_watch_codes(self._watch_symbols, select=choice)


    def _on_watch_add(self) -> None:
        """把文本框里的品种加合约放进关注列表，并选中它。"""
        from tkinter import messagebox

        raw = self.watch_add_var.get().strip().replace(" ", "")
        if not raw:
            messagebox.showwarning("还没填写", "请填写品种加合约，例如 bu2612。")
            return
        letters = "".join(ch for ch in raw if ch.isalpha())
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not letters or not digits or letters + digits != raw:
            messagebox.showwarning("格式不对", "请填写品种加合约，例如 bu2612。不要只写品种，也不要写中文。")
            return
        if any(item.lower() == raw.lower() for item in self._watch_symbols):
            self._set_watch_codes(self._watch_symbols, select=raw)
            self.watch_add_var.set("")
            self._append_log(f"{raw} 已经在关注列表里，已把它选为当前品种，并写入配置")
            return
        self._set_watch_codes(self._watch_symbols + [raw], select=raw)
        self.watch_add_var.set("")
        self._append_log(f"已把 {raw} 加入关注列表，并选为当前品种。名单已经写入配置，重新打开还在。")


    def _on_watch_remove(self) -> None:
        """把下拉框里当前选中的合约从关注列表移出。"""
        from tkinter import messagebox

        current = self.watch_choice_var.get().strip()
        if not self._watch_symbols or current in ("", "（还没有）"):
            messagebox.showinfo("没有可移出的", "关注列表还是空的。")
            return
        if len(self._watch_symbols) == 1:
            messagebox.showinfo("先留一个", "至少保留一个关注品种，不然没有合约可看。")
            return
        kept = [item for item in self._watch_symbols if item.lower() != current.lower()]
        self._set_watch_codes(kept)
        self._append_log(f"已把 {current} 移出关注列表。名单已经写入配置。")

