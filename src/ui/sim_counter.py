"""运行页的程序自测仿真柜台控制面板。

柜台是独立本机进程；这里只负责进程管理和查看柜台原始状态，不参与报单状态迁移。
关闭本窗口会保留柜台，退出主程序时只回收由本程序启动的进程。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk

from src.trader.sim_counter import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    SCENARIO_LABELS,
    SERVICE_NAME,
    request_once,
)
from src.ui import theme as T
from src.utils.paths import project_root


class SimCounterController:
    """管理一个回环地址柜台；process 非空表示进程由本控制器启动。"""

    def __init__(self, root: str | Path, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.root = Path(root).resolve()
        self.host = host
        self.port = int(port)
        self.process: subprocess.Popen[str] | None = None

    def _checked(self, data: Any) -> dict[str, Any]:
        row = dict(data or {})
        if row.get("service") != SERVICE_NAME or row.get("protocol") != PROTOCOL_VERSION:
            raise RuntimeError(f"{self.host}:{self.port} 上不是本程序的仿真柜台")
        return row

    def status(self) -> dict[str, Any]:
        return self._checked(
            request_once("status", host=self.host, port=self.port, timeout=1.0)
        )

    def start(self) -> dict[str, Any]:
        if self.process is not None and self.process.poll() is None:
            return self.status()
        self.process = None
        try:
            # 已经运行时直接复用，但不把它标记成本程序拥有的子进程。
            return self.status()
        except OSError:
            pass
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(f"{self.host}:{self.port} 上的服务不是本程序的仿真柜台：{exc}") from exc

        source_root = Path(__file__).resolve().parents[2]
        if getattr(sys, "frozen", False):
            # 打包后 sys.executable 是主程序本身；由 app_entry 识别 --sim-counter。
            command = [sys.executable, "--sim-counter"]
        else:
            command = [sys.executable, "-m", "src.trader.sim_counter"]
        command.extend(
            [
                "--root",
                str(self.root),
                "--host",
                self.host,
                "--port",
                str(self.port),
            ]
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            command,
            cwd=source_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # 首次启动可能被杀毒软件扫描拖慢，等久一点，别把慢当成失败。
        deadline = time.monotonic() + 20.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self._read_output()
                self.process = None
                raise RuntimeError(f"仿真柜台启动失败：{output or '子进程已退出'}")
            try:
                return self.status()
            except (OSError, RuntimeError, ValueError) as exc:
                last_error = exc
                time.sleep(0.1)
        output = self._stop_and_read_output()
        raise RuntimeError(
            f"仿真柜台启动超时：{last_error or '没有收到就绪应答'}"
            + (f"。进程输出：{output}" if output else "")
        )

    def _read_output(self) -> str:
        process = self.process
        if process is None or process.stdout is None:
            return ""
        try:
            if process.poll() is None:
                return ""
            return (process.stdout.read() or "").strip()
        except (OSError, ValueError):
            return ""

    def _stop_and_read_output(self) -> str:
        process = self.process
        self.process = None
        if process is None:
            return ""
        try:
            if process.poll() is None:
                process.terminate()
            output, _ = process.communicate(timeout=2)
            return (output or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            try:
                if process.poll() is None:
                    process.kill()
                output, _ = process.communicate(timeout=1)
                return (output or "").strip()
            except (OSError, subprocess.TimeoutExpired):
                return ""

    def stop(self) -> None:
        self.status()
        request_once("stop", host=self.host, port=self.port, timeout=1.0)
        process = self.process
        if process is not None:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=4)
        self.process = None

    def close_owned(self) -> None:
        """主程序退出时调用；检测到但未由本程序启动的实例不在此回收。"""
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            request_once("stop", host=self.host, port=self.port, timeout=1.0)
            process.wait(timeout=4)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)


def counter_for(master: ctk.CTk) -> SimCounterController:
    controller = getattr(master, "_sim_counter_controller", None)
    if controller is None:
        root = getattr(master, "root_dir", None) or project_root()
        controller = SimCounterController(root)
        master._sim_counter_controller = controller  # type: ignore[attr-defined]
    return controller


def shutdown_owned_counter(master: ctk.CTk) -> None:
    controller = getattr(master, "_sim_counter_controller", None)
    if controller is not None:
        controller.close_owned()


def open_sim_counter(master: ctk.CTk) -> None:
    existing = getattr(master, "_sim_counter_window", None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        existing.focus_force()
        return
    window = SimCounterWindow(master, counter_for(master))
    master._sim_counter_window = window  # type: ignore[attr-defined]


class SimCounterWindow(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, controller: SimCounterController) -> None:
        super().__init__(master)
        self.controller = controller
        self.title("程序自测 · 仿真柜台")
        self.geometry("980x760")
        self.minsize(840, 640)
        self.configure(fg_color=T.BG)
        self._closed = False
        self._refreshing = False
        self._action_busy = False
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.status_var = ctk.StringVar(value="正在检测本机柜台…")
        self.account_var = ctk.StringVar(value="")
        self.message_var = ctk.StringVar(value="")
        self.hint_var = ctk.StringVar(value="")
        self.scenario_var = ctk.StringVar(value=SCENARIO_LABELS["immediate"])
        self.symbol_var = ctk.StringVar(value="fu2701")
        self.last_var = ctk.StringVar(value="2500")
        self.bid_var = ctk.StringVar(value="2499")
        self.ask_var = ctk.StringVar(value="2501")
        self._scenario_keys = {label: key for key, label in SCENARIO_LABELS.items()}
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(80, self._drain_ui)
        self._refresh()

    def _build(self) -> None:
        ctk.CTkLabel(self, text="程序自测仿真柜台", font=T.FONT_TITLE, text_color=T.TEXT).pack(
            anchor="w", padx=18, pady=(16, 4)
        )
        ctk.CTkLabel(
            self,
            text=(
                "独立进程只监听本机回环地址，随时可测，不依赖仿真账号开放时段。"
                "启动后到账号页选择“程序自测仿真柜台”，再打开交易台连接登录，用原有按钮测开平撤和止损请求。"
            ),
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=920,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 10))

        controls = ctk.CTkFrame(self, fg_color=T.BG_PANEL, corner_radius=10)
        controls.pack(fill="x", padx=18, pady=(0, 8))
        for text, command, color in (
            ("启动 / 检测", self._start, T.ACCENT),
            ("停止柜台", self._stop, T.DANGER),
            ("刷新状态", self._refresh, T.BG_ELEVATED),
        ):
            ctk.CTkButton(
                controls,
                text=text,
                command=command,
                fg_color=color,
                hover_color=T.ACCENT_HOVER if color == T.ACCENT else T.BORDER,
                width=110,
            ).pack(side="left", padx=(10, 0), pady=10)
        ctk.CTkLabel(controls, textvariable=self.status_var, font=T.FONT_BODY, text_color=T.TEXT).pack(
            side="left", padx=16
        )
        ctk.CTkLabel(self, textvariable=self.account_var, font=T.FONT_SMALL, text_color=T.TEXT).pack(
            anchor="w", padx=20, pady=(0, 4)
        )
        ctk.CTkLabel(
            self,
            textvariable=self.message_var,
            font=T.FONT_SMALL,
            text_color=T.WARN,
            wraplength=920,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 2))
        ctk.CTkLabel(
            self,
            textvariable=self.hint_var,
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            wraplength=920,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 6))

        settings = ctk.CTkFrame(self, fg_color=T.BG_ELEVATED, corner_radius=10)
        settings.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(settings, text="后续报单场景", font=T.FONT_BODY, text_color=T.TEXT).grid(
            row=0, column=0, padx=(12, 8), pady=10, sticky="w"
        )
        ctk.CTkOptionMenu(
            settings,
            variable=self.scenario_var,
            values=list(SCENARIO_LABELS.values()),
            command=self._set_scenario,
            width=210,
            fg_color=T.BG,
            button_color=T.ACCENT,
            button_hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
        ).grid(row=0, column=1, padx=(0, 14), pady=10, sticky="w")
        ctk.CTkLabel(
            settings,
            text="无回报/断线用于暴露等待与恢复问题；拒单用于检查程序是否显示真实阻断原因。",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
        ).grid(row=0, column=2, columnspan=5, padx=4, pady=10, sticky="w")
        for column, (label, variable, width) in enumerate(
            (
                ("合约", self.symbol_var, 100),
                ("最新", self.last_var, 80),
                ("买一", self.bid_var, 80),
                ("卖一", self.ask_var, 80),
            )
        ):
            ctk.CTkLabel(settings, text=label, font=T.FONT_SMALL, text_color=T.TEXT_DIM).grid(
                row=1, column=column * 2, padx=(12, 4), pady=(0, 10), sticky="w"
            )
            ctk.CTkEntry(
                settings,
                textvariable=variable,
                width=width,
                fg_color=T.BG,
                border_color=T.BORDER,
                text_color=T.TEXT,
            ).grid(row=1, column=column * 2 + 1, padx=(0, 8), pady=(0, 10), sticky="w")
        ctk.CTkButton(
            settings,
            text="设置行情 / 加入合约",
            command=self._set_quote,
            fg_color=T.ACCENT,
            hover_color=T.ACCENT_HOVER,
        ).grid(row=1, column=8, padx=10, pady=(0, 10), sticky="w")

        self.tabs = ctk.CTkTabview(
            self,
            fg_color=T.BG_PANEL,
            segmented_button_fg_color=T.BG_ELEVATED,
            segmented_button_selected_color=T.ACCENT,
            segmented_button_selected_hover_color=T.ACCENT_HOVER,
            text_color=T.TEXT,
        )
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.boxes: dict[str, ctk.CTkTextbox] = {}
        for name in ("委托", "成交", "持仓 / 行情", "柜台事件（含错误）"):
            self.tabs.add(name)
            box = ctk.CTkTextbox(
                self.tabs.tab(name),
                font=T.FONT_MONO,
                fg_color=T.BG,
                text_color=T.TEXT,
                wrap="none",
            )
            box.pack(fill="both", expand=True, padx=6, pady=6)
            box.configure(state="disabled")
            self.boxes[name] = box

    def _async(self, work: Callable[[], Any], done: Callable[[Any, Exception | None], None]) -> None:
        def job() -> None:
            try:
                result, error = work(), None
            except Exception as exc:  # noqa: BLE001 - 管理请求错误要显示在面板上
                result, error = None, exc
            self._ui_queue.put(lambda: done(result, error))

        threading.Thread(target=job, daemon=True, name="sim-counter-ui").start()

    def _drain_ui(self) -> None:
        if self._closed:
            return
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._drain_ui)

    def _refresh(self) -> None:
        if self._closed or self._refreshing:
            return
        self._refreshing = True

        def done(data: Any, error: Exception | None) -> None:
            self._refreshing = False
            if self._closed:
                return
            if error is not None:
                self.status_var.set(f"未运行 · {self.controller.host}:{self.controller.port}")
                self.account_var.set("交易台连接时会自动启动柜台；也可以点“启动 / 检测”。")
                # 自动刷新只写提示，不覆盖“启动 / 检测”留下的真实错误。
                self.hint_var.set(str(error))
            else:
                self._paint(data)
            self.after(1000, self._refresh)

        self._async(self.controller.status, done)

    def _start(self) -> None:
        if self._action_busy:
            return
        self._action_busy = True
        self.message_var.set("正在启动或检测已有实例…")

        def done(data: Any, error: Exception | None) -> None:
            self._action_busy = False
            if error is not None:
                self.message_var.set(str(error))
            else:
                self.hint_var.set("")
                self._paint(data)
                self.message_var.set("柜台已就绪；已有实例会直接复用，不会重复启动。")

        self._async(self.controller.start, done)

    def _stop(self) -> None:
        if self._action_busy:
            return
        self._action_busy = True

        def done(_data: Any, error: Exception | None) -> None:
            self._action_busy = False
            if error is None:
                self.hint_var.set("")
            self.message_var.set("柜台已停止；账本文件保留，下次启动可恢复。" if error is None else str(error))
            self._refresh()

        self._async(self.controller.stop, done)

    def _set_scenario(self, label: str) -> None:
        scenario = self._scenario_keys.get(label)
        if not scenario:
            return

        def work() -> Any:
            return request_once(
                "set_scenario",
                {"scenario": scenario},
                host=self.controller.host,
                port=self.controller.port,
                timeout=1.0,
            )

        def done(_data: Any, error: Exception | None) -> None:
            self.message_var.set(f"后续报单场景：{label}" if error is None else str(error))

        self._async(work, done)

    def _set_quote(self) -> None:
        payload = {
            "symbol": self.symbol_var.get().strip(),
            "last": self.last_var.get().strip(),
            "bid": self.bid_var.get().strip(),
            "ask": self.ask_var.get().strip(),
        }

        def work() -> Any:
            return request_once(
                "set_quote",
                payload,
                host=self.controller.host,
                port=self.controller.port,
                timeout=1.0,
            )

        def done(data: Any, error: Exception | None) -> None:
            if error is not None:
                self.message_var.set(str(error))
            else:
                self.message_var.set(f"已设置 {data['symbol']} 最新价 {data['last']}；交易台轮询会读到新盘口。")
                self._refresh()

        self._async(work, done)

    def _paint(self, data: dict[str, Any]) -> None:
        self.hint_var.set("")
        account = data.get("account") or {}
        self.status_var.set(
            f"运行中 · {data.get('host')}:{data.get('port')} · 交易连接 {data.get('connections', 0)}"
        )
        self.account_var.set(
            f"权益 {float(account.get('balance') or 0):,.2f} · "
            f"可用 {float(account.get('available') or 0):,.2f} · "
            f"保证金 {float(account.get('margin') or 0):,.2f} · 当前场景 {data.get('scenario_label')}"
        )
        label = str(data.get("scenario_label") or "")
        if label in self._scenario_keys:
            self.scenario_var.set(label)
        orders = data.get("orders") or []
        self._set_box(
            "委托",
            "编号       合约         方向/开平  价格        手数/成交  状态     说明\n"
            + "-" * 96 + "\n"
            + ("\n".join(
                f"{str(row.get('order_ref')):>10}  {str(row.get('symbol')):<12} "
                f"{str(row.get('direction'))}/{str(row.get('offset'))}       "
                f"{float(row.get('price') or 0):>10g}  {int(row.get('volume') or 0):>4}/{int(row.get('filled') or 0):<4} "
                f"{str(row.get('status_code')):<8} {row.get('message') or ''}"
                for row in reversed(orders[-200:])
            ) or "暂无委托"),
        )
        trades = data.get("trades") or []
        self._set_box(
            "成交",
            "时间       合约         方向/开平  价格        手数  成交编号\n" + "-" * 88 + "\n"
            + ("\n".join(
                f"{str(row.get('trade_time') or row.get('time') or ''):<10}  {str(row.get('symbol')):<12} "
                f"{str(row.get('direction'))}/{str(row.get('offset'))}       {float(row.get('price') or 0):>10g}  "
                f"{int(row.get('volume') or 0):>4}  {row.get('trade_id') or ''}"
                for row in reversed(trades[-200:])
            ) or "暂无成交"),
        )
        positions = data.get("positions") or []
        instruments = data.get("instruments") or []
        position_text = "\n".join(
            f"{row.get('symbol'):<12} 方向 {row.get('direction')}  今 {int(row.get('today') or 0):>4}  "
            f"昨 {int(row.get('yesterday') or 0):>4}  保证金 {float(row.get('use_margin') or 0):,.2f}"
            for row in positions
        ) or "暂无持仓"
        quote_text = "\n".join(
            f"{row.get('symbol'):<12} 最新 {float(row.get('last') or 0):>10g}  "
            f"买一 {float(row.get('bid') or 0):>10g}  卖一 {float(row.get('ask') or 0):>10g}  "
            f"涨停 {float(row.get('upper') or 0):>10g}  跌停 {float(row.get('lower') or 0):>10g}"
            for row in sorted(instruments, key=lambda item: str(item.get("symbol")))
        )
        self._set_box("持仓 / 行情", "【持仓】\n" + position_text + "\n\n【合约行情】\n" + quote_text)
        events = data.get("events") or []
        self._set_box(
            "柜台事件（含错误）",
            "\n".join(
                f"{row.get('ts')}  [{row.get('kind')}]  {row.get('message')}"
                for row in reversed(events[-200:])
            ) or "暂无事件",
        )

    def _set_box(self, name: str, text: str) -> None:
        box = self.boxes[name]
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _close(self) -> None:
        self._closed = True
        self.destroy()
