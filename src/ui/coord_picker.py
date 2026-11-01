"""
同花顺下单坐标标定窗口。

功能：点「开始记录」后倒计时 3 秒，把鼠标移到目标上，时间到自动记下坐标。
原理：
  开发时由 tools/pick_coords.py 用 Python 打开这个窗口。
  打包后的 exe 不是 Python，不能再拿它去跑那个脚本，否则窗口不会出现。
  打包后的主界面会再启动一次自己，只带 --pick-coords，单独弹出这个窗口。
  坐标写到 exe 旁边的 config，不写到打包时的临时目录。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import pyautogui
import yaml

from src.utils.paths import is_frozen, project_root

# 字段名和配置里 trade.gui 一致。确定按钮和品种框可以不录。
FIELDS = [
    ("price_input", "1 价格输入框"),
    ("volume_input", "2 手数输入框"),
    ("buy_open_btn", "3 买开（红色买多）"),
    ("sell_open_btn", "4 卖开（绿色卖空）"),
    ("close_btn", "5 平仓（蓝色平仓）"),
    ("confirm_btn", "6 确定弹窗（可选）"),
    ("symbol_input", "7 品种输入框（可选）"),
]

COUNTDOWN_SEC = 3


def picker_launch_command() -> list[str]:
    """
    标定窗口的启动命令。

    打包后的程序再开一个自己，只弹出标定窗口。
    没打包时仍用旁边的 pythonw 去跑脚本，避免多出一个黑框。
    """
    if is_frozen():
        return [sys.executable, "--pick-coords"]
    script = project_root() / "tools" / "pick_coords.py"
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    launcher = str(pythonw if pythonw.exists() else exe)
    return [launcher, str(script)]


def save_coords(captured: dict[str, list[int]]) -> Path:
    """
    把坐标写到 gui_coords.yaml，并合并进 config.yaml。

    已有的点会留下来，这次录到的点盖掉同名的那一项。
    可以只补录确定按钮。
    """
    if not captured:
        raise ValueError("还没有记录任何坐标")

    root = project_root()
    cfg_path = root / "config" / "config.yaml"
    merged: dict[str, list[int]] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        old = (cfg.get("trade") or {}).get("gui") or {}
        for key, value in old.items():
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                merged[key] = [int(value[0]), int(value[1])]
    merged.update(captured)

    out = root / "config" / "gui_coords.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"trade": {"gui": merged}}, f, allow_unicode=True, sort_keys=False)

    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("trade", {}).setdefault("gui", {}).update(merged)
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    return out


class PickCoordsApp:
    """
    标定小窗口。

    不在点下按钮的那一瞬取坐标，那时鼠标还在这个窗口上。
    倒计时结束再取鼠标位置，才是同花顺上的那个点。
    """

    def __init__(self) -> None:
        pyautogui.FAILSAFE = True
        self.captured: dict[str, list[int]] = {}
        self._busy = False

        self.root = tk.Tk()
        self.root.title("同花顺坐标标定")
        self.root.attributes("-topmost", True)
        self.root.geometry("+60+40")

        tip = (
            "正确用法（不要把本窗口盖在按钮上）：\n"
            "1. 点某一行的「开始记录」\n"
            "2. 在 3 秒内把鼠标移到同花顺对应位置正中央，停住不动\n"
            "3. 倒计时结束会自动记下那时的鼠标位置\n"
            "4. 1~5 必录；第6项「确定弹窗」可选（程序默认也会按回车关弹窗）\n"
            "5. 录完点「保存到配置」（可只补录第6项）"
        )
        tk.Label(self.root, text=tip, justify="left", anchor="w", fg="#333333").pack(
            fill="x", padx=12, pady=8
        )

        self.pos_var = tk.StringVar(value="鼠标=(?, ?)")
        tk.Label(self.root, textvariable=self.pos_var, font=("Consolas", 12)).pack(pady=4)

        self.status_var = tk.StringVar(value="已录 0 —— 先点某一行「开始记录」")
        tk.Label(self.root, textvariable=self.status_var, fg="#2E7D32", wraplength=560).pack(
            pady=2
        )

        self._btns: dict[str, tk.Button] = {}
        for field, label in FIELDS:
            row = tk.Frame(self.root)
            row.pack(fill="x", padx=12, pady=3)
            tk.Label(row, text=label, width=22, anchor="w").pack(side="left")
            btn = tk.Button(
                row,
                text="开始记录",
                command=lambda f=field, lab=label: self._start_countdown(f, lab),
            )
            btn.pack(side="left", padx=4)
            self._btns[field] = btn
            setattr(self, f"lbl_{field}", tk.Label(row, text="未录", fg="#888888"))
            getattr(self, f"lbl_{field}").pack(side="left", padx=6)

        tk.Button(
            self.root,
            text="保存到配置",
            bg="#2E7D32",
            fg="white",
            height=2,
            command=self._on_save,
        ).pack(fill="x", padx=12, pady=12)

        tk.Button(self.root, text="退出", command=self.root.destroy).pack(pady=(0, 12))
        self._fit_window()
        self._tick_mouse()

    def _fit_window(self) -> None:
        """按实际内容决定窗口大小，避免保存和退出被裁在外面。"""
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth() + 16, 640)
        height = self.root.winfo_reqheight() + 56
        self.root.geometry(f"{width}x{height}+60+40")
        self.root.minsize(width, height)

    def _tick_mouse(self) -> None:
        """定时刷新鼠标坐标，方便对准。"""
        x, y = pyautogui.position()
        self.pos_var.set(f"鼠标=({x}, {y})")
        self.root.after(80, self._tick_mouse)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in self._btns.values():
            btn.config(state=state)

    def _start_countdown(self, field: str, label: str) -> None:
        """点「开始记录」后倒计时，到期再抓鼠标位置。"""
        if self._busy:
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self._countdown(field, label, COUNTDOWN_SEC)

    def _countdown(self, field: str, label: str, left: int) -> None:
        if left > 0:
            self.status_var.set(
                f"正在录「{label}」—— 还有 {left} 秒，请把鼠标移到目标上停住！"
            )
            self.root.after(1000, lambda: self._countdown(field, label, left - 1))
            return

        x, y = pyautogui.position()
        self.captured[field] = [int(x), int(y)]
        lbl = getattr(self, f"lbl_{field}")
        lbl.config(text=f"[{x}, {y}]", fg="#1565C0")
        self.status_var.set(f"已录 {len(self.captured)} 项 ← 刚记下「{label}」= ({x}, {y})")
        self._busy = False
        self._set_buttons_enabled(True)

    def _on_save(self) -> None:
        try:
            path = save_coords(self.captured)
            messagebox.showinfo(
                "保存成功",
                f"已写入：\n{path}\n并已合并进 config.yaml 的 trade.gui\n\n"
                "下一步：界面里把执行通道改成 gui，先开着模拟模式测试。",
            )
        except ValueError as e:
            messagebox.showwarning("还没录完", str(e))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e))

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    """打开标定窗口。打包后的程序从 --pick-coords 进到这里。"""
    PickCoordsApp().run()
