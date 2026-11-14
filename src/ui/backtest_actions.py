"""Historical-backtest execution, local data tools, and exports."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from loguru import logger
from pathlib import Path
from src.data.periods import period_label
from tkinter import messagebox
import os
import subprocess
import sys
import threading


class BacktestActionsMixin:
    """Methods mixed into AutoTradingApp; UI state remains on the app instance."""

    def _format_limit_lines(self, snap: dict) -> str:
        """结果里写明这次有没有套用开仓限制，方便和上一把对比。"""
        if not snap.get("apply_limits", True):
            return "开仓限制  关（固定1手，不看止损距离、日亏损和开仓次数）"
        skipped = self._format_skipped(snap.get("skipped_opens") or {})
        return (
            f"开仓限制  开\n"
            f"仓位      {snap.get('sizing_mode')}  起始权益{float(snap.get('capital') or 0):.0f}元"
            f"  每笔{snap.get('risk_per_trade_pct')}%"
            f"  仓位上限{snap.get('margin_usage_pct', '—')}%"
            f"  固定{snap.get('fixed_volume')}手"
            f"{'  手数跟剩余权益走（真实账户口径开）' if snap.get('chain_capital') or snap.get('rollover_chain') else '  手数按起始权益（真实账户口径关）'}\n"
            f"限制      最少{snap.get('min_volume')}手"
            f"  日亏停{float(snap.get('max_daily_loss') or 0):.0f}元"
            f"  间隔{snap.get('min_order_interval_sec')}秒"
            f"  每天最多开{snap.get('max_orders_per_day')}次"
            f"  兜底每手保证金{float(snap.get('margin_per_lot') or 0):.0f}元（有比例时按价格现算）\n"
            f"凯利      {'按手填胜率/盈亏比算手数' if snap.get('kelly_in_backtest') else '关（按仓位模式和每笔风险%）'}\n"
            f"{skipped}"
        )


    def _format_skipped(self, skipped: dict) -> str:
        """把没开成的原因收成一行。没有跳过就写明这次限制没有挡掉信号。"""
        if not skipped:
            return "跳过开仓  无"
        parts = [f"{name}{count}次" for name, count in skipped.items()]
        total = sum(int(v) for v in skipped.values())
        return f"跳过开仓  {total} 次（" + "，".join(parts) + "）"


    def _on_backtest(self) -> None:
        """后台跑历史回测，结果写本页 + data/backtest_last.json。"""
        prepared = self._prepare_backtest_run()
        if prepared is None:
            return
        cfg, symbol, start, end = prepared
        st = cfg["strategy"]

        self._append_log(
            f"开始历史回测… engine={st['engine']} symbol={symbol} "
            f"行情={self.bt_source_var.get()} "
            f"区间={start or '起'}~{end or '止'} "
            f"换月={'按主力时段' if self.bt_rollover_var.get() else '否'} "
            f"口径={'真实账户' if self.bt_rollover_chain_var.get() else '固定起始资金'}"
        )
        self.tabs.set("历史回测")
        if hasattr(self, "bt_result_box"):
            self._set_result_text(
                self.bt_result_box,
                "历史回测进行中，请稍候…\n正在下载行情。网络慢时最多大约 20 秒，然后改用电脑里已经有的K线。\n",
            )

        def _run() -> None:
            try:
                from src.backtest.engine import run_backtest, save_backtest_report
                from src.backtest.evaluation import EvaluationThresholds, format_evaluation_lines
                from src.backtest.rollover import run_backtest_rollover, format_rollover_lines
                from src.backtest.stress import evaluate_with_stress

                chain = bool(self.bt_rollover_chain_var.get())
                use_rollover = bool(self.bt_rollover_var.get())
                if use_rollover:
                    result = run_backtest_rollover(
                        cfg,
                        symbol=symbol,
                        start_date=start,
                        end_date=end,
                        engine=st["engine"],
                        chain_capital=chain,
                    )
                else:
                    result = run_backtest(
                        cfg,
                        symbol=symbol,
                        start_date=start,
                        end_date=end,
                        engine=st["engine"],
                        chain_capital=chain,
                    )
                p = result.performance
                th = EvaluationThresholds.from_cfg(cfg)

                def _runner(local_cfg, **kwargs):
                    if use_rollover:
                        return run_backtest_rollover(local_cfg, **kwargs)
                    return run_backtest(local_cfg, **kwargs)

                # 评估、滑点压力、参数扰动和历史派生口径共用一套，别在这里另写一遍。
                result.evaluation, _slip, _pert, stress_note = evaluate_with_stress(
                    cfg,
                    result,
                    symbol=symbol,
                    engine=st["engine"],
                    start_date=start,
                    end_date=end,
                    chain_capital=chain,
                    thresholds=th,
                    runner=_runner,
                    on_start=lambda: self._post_to_main(
                        lambda: self._append_log("历史回测完成，开始滑点压力测试与参数扰动…")
                    ),
                )
                path = self.root_dir / "data" / "backtest_last.json"
                save_backtest_report(result, path)
                snap = result.params_snapshot or {}
                rollover_text = format_rollover_lines(snap)
                note = ""
                if p.trades < 3:
                    note = (
                        f"\n注意：K线 {result.bars} 根、成交仅 {p.trades} 笔，"
                        "胜率参考价值低（可改成「全部可用」再试）。"
                    )
                from src.backtest.metrics import format_expectancy_line, format_payoff_lines
                from src.screen.picker import format_cover_lines

                # 摘要 + 最近几笔成交。盈利覆盖等和交叉测试同一套算法，方便对照。
                lines = [
                    f"历史回测完成  {result.symbol}",
                    f"跑完时间  {result.ran_at}",
                    f"策略      {snap.get('engine', st['engine'])}",
                    "收盘清仓  开（到点强平）"
                    if snap.get("flatten_at_close", True)
                    else "收盘清仓  关（可留过收盘；区间最后一根仍会按收盘价结算未平的仓）",
                    f"周期      信号{period_label(result.timeframe)} / 止损{period_label(result.trail_timeframe)}",
                    self._format_limit_lines(snap),
                    f"你选的区间  {snap.get('start_date') or '不限'} ~ {snap.get('end_date') or '到最新'}",
                    f"行情能给的  {str(snap.get('available_from', ''))[:16]}  →  {str(snap.get('available_to', ''))[:16]}",
                    f"实际区间    {str(snap.get('data_from', ''))[:16]}  →  {str(snap.get('data_to', ''))[:16]}",
                    str(snap.get("coverage_note") or "").strip(),
                    *([rollover_text] if rollover_text else []),
                    str(snap.get("fill_note") or "").strip(),
                    f"K线根数   {result.bars}",
                    f"成交      {p.trades} 笔（赢{p.wins} / 亏{p.losses}）",
                    f"没开成    {len(result.rejected)} 条开仓信号（被策略过滤或风控挡住，明细在下面）"
                    if result.rejected
                    else "没开成    无（每个开仓信号都成交了）",
                    f"胜率      {p.win_rate:.0%}",
                    *format_payoff_lines(p.avg_win, p.avg_loss, p.payoff),
                    format_expectancy_line(p.win_rate, p.avg_win, p.avg_loss, p.expectancy),
                    f"净盈亏    {p.net_pnl:.0f} 元",
                    f"盈利因子  {p.profit_factor:.2f}",
                    f"最大回撤  {p.max_drawdown:.0f} 元（{p.max_drawdown_pct:.2%}）",
                    *format_cover_lines(
                        p.net_pnl,
                        p.max_drawdown,
                        p.trades,
                        str(snap.get("data_from") or ""),
                        str(snap.get("data_to") or ""),
                    ),
                    stress_note,
                    *format_evaluation_lines(result.evaluation),
                    f"报告      {path}",
                    note.strip(),
                    "",
                    "—— 成交明细（最多显示最近 15 笔）——",
                ]
                for t in result.trades[-15:]:
                    lines.append(
                        f"{t.entry_time[:16]}→{t.exit_time[:16]} {t.side}{t.volume}手 "
                        f"{t.entry_price:.1f}->{t.exit_price:.1f} "
                        f"pnl={t.pnl:.0f} | {t.reason_exit[:40]}"
                    )
                if result.rejected:
                    lines.append("")
                    lines.append(
                        f"—— 有开仓信号但没做（同一交易时段同类原因只记一次，共 "
                        f"{len(result.rejected)} 条，最多显示最近 20 条）——"
                    )
                    for item in result.rejected[-20:]:
                        price = f"{item.price:.2f}" if item.price else "—"
                        stop = f"{item.stop:.2f}" if item.stop else "—"
                        lines.append(
                            f"{item.time[:16]} {item.side} 价{price} 止损{stop} "
                            f"| {item.stage}：{item.reason[:80]}"
                        )
                text = "\n".join(x for x in lines if x is not None)
                self._post_to_main(lambda: self._append_log(
                    f"历史回测完成 {result.symbol} | {result.bars}根 | "
                    f"{p.trades}笔 胜率{p.win_rate:.0%} 盈亏比{p.payoff:.2f} "
                    f"期望{p.expectancy:.2f} 净盈亏{p.net_pnl:.0f}"
                ))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("历史回测失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"历史回测失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.bt_result_box, f"历史回测失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("历史回测失败", m))

        threading.Thread(target=_run, daemon=True).start()


    def _main_table_path(self) -> Path:
        """当前回测会用的主力合约表路径。"""
        from src.backtest.rollover import resolve_main_table

        found = resolve_main_table({"_project_root": str(self.root_dir)})
        return found if found else (self.root_dir / "config" / "主力合约表.xlsx")


    def _refresh_main_table_state(self) -> None:
        """按钮旁显示表的最后更新时间，方便确认表是不是旧的。"""
        if not hasattr(self, "bt_table_state"):
            return
        path = self._main_table_path()
        if path.exists():
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self.bt_table_state.configure(text=f"更新于 {stamp}")
        else:
            self.bt_table_state.configure(text="还没有表，点「更新」生成")


    def _on_open_main_table(self) -> None:
        """用系统默认程序打开主力合约表，方便直接改「手动修正」页。"""
        from tkinter import messagebox

        path = self._main_table_path()
        if not path.exists():
            messagebox.showinfo(
                "还没有主力合约表",
                "先点「更新主力合约表」生成，再打开。\n生成后请在「手动修正」页里加行。",
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开", f"打不开主力合约表。\n\n{exc}")


    def _on_update_main_table(self) -> None:
        """手动更新主力合约表：只有点这个按钮才会重新拉行情，平时不会自动更新。"""
        from tkinter import messagebox

        if getattr(self, "_main_table_busy", False):
            return
        path = self._main_table_path()
        if not messagebox.askyesno(
            "更新主力合约表",
            f"现在重新下载日行情并重算主力时段？\n\n输出：{path}\n\n"
            "「手动修正」页会原样保留，不会被覆盖。\n"
            "大商所品种要逐个合约拉日线，会慢一些，期间可以切到其它页面。",
        ):
            return
        self._main_table_busy = True
        if hasattr(self, "bt_table_btn"):
            self.bt_table_btn.configure(state="disabled", text="更新中…")
        if hasattr(self, "bt_table_state"):
            self.bt_table_state.configure(text="正在更新，请稍候…")
        self._append_log("开始更新主力合约表（手动触发）…")
        start = f"{date.today().year}-01-01"

        def _progress(msg: str) -> None:
            self._post_to_main(lambda m=msg: self._append_log(m))

        def _run() -> None:
            ok = False
            try:
                from src.backtest.main_table import update_table

                summary = update_table(self.root_dir, start=start, out=path, progress=_progress)
                self._post_to_main(lambda s=summary: self._append_log(
                    f"主力合约表已更新：{s['products']} 个品种、{s['periods']} 段 | "
                    f"区间 {s['start']}~{s['end']}"
                ))
                ok = True
            except Exception as e:  # noqa: BLE001
                logger.exception("主力合约表更新失败")
                err = str(e)
                if isinstance(e, PermissionError):
                    err = f"写不进表，文件正被占用。请先关闭 Excel 里打开的{path.name}再更新。\n（{e}）"
                self._post_to_main(lambda m=err: self._append_log(f"主力合约表更新失败: {m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("主力合约表更新失败", m))
            finally:
                self._post_to_main(lambda good=ok: self._finish_main_table_update(good))

        threading.Thread(target=_run, daemon=True).start()


    def _finish_main_table_update(self, ok: bool) -> None:
        """恢复按钮，并刷新表的时间戳。"""
        self._main_table_busy = False
        if hasattr(self, "bt_table_btn"):
            self.bt_table_btn.configure(state="normal", text="更新主力合约表")
        if not ok and hasattr(self, "bt_table_state"):
            self.bt_table_state.configure(text="更新失败，表没有改动")
            return
        self._refresh_main_table_state()


    def _on_apply_slippage_hint(self) -> None:
        """把实盘滑点 70 分位建议填进回测滑点框。要点确认，不会自己改。"""
        from tkinter import messagebox

        from src.account.slippage_log import summarize

        info = summarize(self.root_dir)
        if not info["n"]:
            messagebox.showinfo(
                "实盘滑点",
                "还没有滑点记录。有真实成交（尤其是 CTP 回报）之后才会给出建议。",
            )
            return
        suggested = float(info["suggested"] or 0)
        scope = "真实成交" if info["used_real"] else "全部记录（含点击假定成交，仅供参考）"
        if not messagebox.askyesno(
            "填入回测滑点",
            f"样本 {info['n']} 笔（{scope}）。\n"
            f"中位 {info['median']:+.2f} 跳，70分位 {info['p70']:+.2f} 跳。\n"
            f"建议回测填 {suggested:g} 跳。\n\n"
            "只改历史回测页这个数字，不改开平仓规则。要填入吗？",
        ):
            return
        self.bt_slippage_var.set(str(suggested))
        self.cfg.setdefault("backtest", {})["slippage_ticks"] = suggested
        self._append_log(f"回测滑点已改成建议值 {suggested:g} 跳（未自动保存配置文件）")


    def _on_slippage_scan(self) -> None:
        """滑点灵敏度：同一份K线换几档滑点重跑，看利润边缘和盈亏分水岭。"""
        from tkinter import messagebox

        prepared = self._prepare_backtest_run()
        if prepared is None:
            return
        cfg, symbol, start, end = prepared
        st = cfg["strategy"]

        self._append_log(f"开始滑点灵敏度… {symbol} 区间={start or '起'}~{end or '止'}")
        self.tabs.set("历史回测")
        if hasattr(self, "bt_result_box"):
            self._set_result_text(
                self.bt_result_box,
                "滑点灵敏度进行中…\nK线只拉一次，之后每档滑点各跑一遍，大约比单次回测多几倍时间。\n",
            )

        def _run() -> None:
            try:
                from src.backtest.engine import FillModel
                from src.backtest.sensitivity import format_slip_scan, run_slippage_scan
                from src.risk.sizing import PositionSizer

                scan = run_slippage_scan(
                    cfg,
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                    engine=st.get("engine"),
                )
                try:
                    current = float(self.bt_slippage_var.get() or 0)
                except (TypeError, ValueError):
                    current = 1.0
                text = format_slip_scan(
                    scan,
                    symbol=symbol,
                    current_slip=current,
                    tick=FillModel.from_cfg(cfg, symbol).price_tick,
                    mult=PositionSizer(cfg).multiplier_for(symbol),
                )
                self._post_to_main(lambda: self._append_log(f"滑点灵敏度完成 {symbol}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda t=text: self._set_result_text(self.bt_result_box, t))
            except Exception as e:  # noqa: BLE001
                logger.exception("滑点灵敏度失败")
                err = str(e)
                self._post_to_main(lambda: self._append_log(f"滑点灵敏度失败: {err}"))
                if hasattr(self, "bt_result_box"):
                    self._post_to_main(lambda m=err: self._set_result_text(self.bt_result_box, f"滑点灵敏度失败:\n{m}"))
                self._post_to_main(lambda m=err: messagebox.showerror("滑点灵敏度失败", m))

        threading.Thread(target=_run, daemon=True).start()


    def _on_export_backtest(self) -> None:
        """把最近一次回测追加到统一 Excel 的「回测摘要」「回测成交」「回测信号」，不盖掉以前的行。"""
        from tkinter import filedialog, messagebox

        from src.export.book import default_workbook_path
        from src.export.bundle import export_backtest

        report = self.root_dir / "data" / "backtest_last.json"
        if not report.exists():
            messagebox.showwarning("还不能导出", "请先点「开始历史回测」，跑完后再导出。")
            return
        default_path = default_workbook_path(self.root_dir)
        path = filedialog.asksaveasfilename(
            title="导出历史回测到统一 Excel",
            defaultextension=".xlsx",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            filetypes=[("Excel 文件", "*.xlsx")],
        )
        if not path:
            return

        def _run() -> None:
            try:
                out, summary_added, trade_added, summary_total, trade_total = export_backtest(
                    self.root_dir, path
                )
            except ValueError as e:
                self._post_to_main(lambda m=str(e): messagebox.showwarning("还不能导出", m))
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("导出历史回测失败")
                text = self._export_fail_text(path, e)
                self._post_to_main(lambda t=text: messagebox.showerror("导出失败", t))
                return
            note = self._export_saved_note(path, out)
            message = (
                f"已追加到三张表，原来的记录还在：\n"
                f"回测摘要 新增 {summary_added} 行，合计 {summary_total} 行\n"
                f"回测成交 新增 {trade_added} 行，合计 {trade_total} 行\n"
                f"回测信号 已追加本次没开成的信号和原因\n"
                f"第一列「回测时间」相同、而且内容也相同的一行不会重复写。\n"
                f"多因子还会在 data 里另存一份「多因子衰减监控-策略-时间.xlsx」，不覆盖以前的。\n\n"
                f"文件：\n{out}{note}"
            )
            self._post_to_main(lambda: messagebox.showinfo("已导出", message))
            self._post_to_main(
                lambda: self._append_log(
                    f"历史回测已追加到 {out} | 摘要新增{summary_added}合计{summary_total}"
                    f" | 成交新增{trade_added}合计{trade_total}"
                )
            )

        # 写 Excel 可能要几秒：放后台线程，弹窗和日志都回主线程再碰控件。
        threading.Thread(target=_run, daemon=True).start()


    def _on_plot_factor_decay(self) -> None:
        """按资料第二节的思路画衰减图：只读已有成交，不改开平仓。"""
        from tkinter import messagebox

        from src.export.factor_decay_plot import factor_summary, write_factor_decay_plot

        try:
            path, df = write_factor_decay_plot(self.root_dir)
        except ImportError as e:
            messagebox.showwarning("还不能画图", str(e))
            return
        except ValueError as e:
            messagebox.showwarning("还不能画图", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("画出因子衰减图失败")
            messagebox.showerror("画图失败", str(e))
            return
        lines = [f"已保存 {path}", f"一共 {len(df)} 笔带四因子分数的成交。"]
        summary = factor_summary(df)
        for row in summary.to_dict(orient="records"):
            lines.append(
                f"{row['因子']}：总盈亏 {row['总盈亏']:.2f}，有分摊 {row['交易次数']} 笔，"
                f"赚钱 {row['盈利次数']} 笔"
            )
        messagebox.showinfo("因子衰减图", "\n".join(lines))
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("打不开衰减图 {}: {}", path, exc)
        self._append_log(f"已画出因子衰减图 {path} | {len(df)} 笔")
