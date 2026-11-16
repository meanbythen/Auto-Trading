"""
打包入口（PyInstaller）
功能：双击 exe 打开图形界面；也可带参数走命令行（--once 等）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _want_gui(argv: list[str]) -> bool:
    """无参数或显式 --gui → 开界面；--once / --sim-test / tick 命令 → 命令行。"""
    if "--gui" in argv:
        return True
    cli_flags = (
        "--once",
        "--sim-test",
        "-h",
        "--help",
        "--tick-record",
        "--tick-status",
        "--tick-bars",
        "--tick-download",
    )
    if any(a in argv for a in cli_flags):
        return False
    # 打包 exe 默认 GUI；源码直接跑本文件也默认 GUI
    return len(argv) == 0


def main() -> int:
    from src.utils.config_loader import ensure_runtime_layout

    ensure_runtime_layout()
    argv = sys.argv[1:]

    if "--pick-coords" in argv:
        # 主界面点「标定下单坐标」时会再开一个自己。这里只弹标定窗口，不再打开主界面。
        from src.ui.coord_picker import main as pick_main

        pick_main()
        return 0

    # 真正会操作交易账户的入口才拦：同一时间只能有一个进程。
    from src.utils.single_instance import acquire

    if not acquire():
        message = "程序已在运行：同一时间只能有一个实例操作交易账户，请先关闭另一个窗口。"
        print(message, file=sys.stderr)
        if _want_gui(argv):
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Auto-Trading", message)
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        return 2

    if _want_gui(argv):
        from src.ui import run_app

        run_app()
        return 0

    from src.main import main as cli_main

    cli_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
