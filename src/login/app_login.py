"""
同花顺期货通登录模块
功能：启动软件、激活窗口；可选自动输入账号密码。
原理：优先认窗口标题（你已打开并能下单就够）；不要因进程名对不上就再开一次启动器弹登录框。
注意：模拟账户不知道密码时，设 skip_auto_login=true（默认），只置顶窗口即可挂机。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pyautogui
from loguru import logger

try:
    import pygetwindow as gw
except ImportError:  # pragma: no cover
    gw = None


# 同花顺期货通常见进程名（启动器 ≠ 交易主程序）
_EXTRA_PROCESS_NAMES = {
    "hexinlauncher.exe",
    "xiadan.exe",
    "futures.exe",
    "qhtong.exe",
    "ths.exe",
}


def force_foreground(hwnd: int) -> bool:
    """
    把窗口放到最前。

    Windows 不允许后台程序随便抢焦点。pygetwindow 一失败就报错，
    错误码 0 的意思其实是「操作成功完成」，错误码 2 经常是上一步启动程序留下的旧错误。
    这里先挂到当前前台窗口的输入队列上，再置顶，最后只看前台是不是已经变成这个窗口。
    """
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE，先从最小化还原
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    if user32.GetForegroundWindow() == hwnd:
        return True

    our_thread = kernel32.GetCurrentThreadId()
    process_id = ctypes.c_ulong()
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id)) if foreground else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    attached: list[int] = []
    for other in (int(foreground_thread or 0), int(target_thread or 0)):
        if other and other != our_thread and other not in attached:
            if user32.AttachThreadInput(our_thread, other, True):
                attached.append(other)
    user32.BringWindowToTop(hwnd)
    kernel32.SetLastError(0)
    user32.SetForegroundWindow(hwnd)
    for other in attached:
        user32.AttachThreadInput(our_thread, other, False)
    return user32.GetForegroundWindow() == hwnd


def pin_topmost(hwnd: int) -> None:
    """
    下单这一小段把同花顺钉在所有窗口上面。

    程序窗口一刷新日志，Windows 有时会把它重新放到前面。
    钉住之后，鼠标点到的仍是同花顺的价格框和买卖按钮。
    下单结束要立刻取消，平时扫描不要一直盖住控制台。
    """
    if not hwnd:
        return
    # HWND_TOPMOST = -1。只改叠放顺序，不改位置和大小。
    ctypes.windll.user32.SetWindowPos(int(hwnd), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)


def unpin_topmost(hwnd: int) -> None:
    """取消钉住。HWND_NOTOPMOST = -2，窗口回到普通叠放，不再一直盖住别的程序。"""
    if not hwnd:
        return
    ctypes.windll.user32.SetWindowPos(int(hwnd), -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)


class AppLoginManager:
    """负责「打开软件 + 登录账号」的管理器。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.app_cfg = cfg.get("app", {})
        self.account = cfg.get("account", {})
        self.trade_gui = (cfg.get("trade") or {}).get("gui") or {}
        # 最近一次准备激活的同花顺窗口。下单时用它钉在最前，平时为 0。
        self.last_hwnd = 0
        self.dry_run = bool(cfg.get("dry_run", True))
        # 默认跳过自动填密码：你手动登录后挂机最稳；不知道密码也能用
        self.skip_auto_login = bool(self.account.get("skip_auto_login", True))
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.3

    def _title_key(self) -> str:
        return str(self.app_cfg.get("window_title", "期货通"))

    def find_windows(self) -> list[Any]:
        """
        查找期货通相关窗口。

        行情主窗口标题含「期货通」。交易窗口标题是「CTP主席」，不含这几个字，也要算进来。
        下单时必须能选中交易窗口，不能只反复激活行情主窗口。
        """
        if gw is None:
            return []
        from src.login.ths_login import looks_like_order_window

        key = self._title_key()
        found = []
        for win in gw.getAllWindows():
            title = win.title or ""
            if key in title or looks_like_order_window(title):
                found.append(win)
        return found

    def window_exists(self) -> bool:
        """是否已有期货通窗口（比进程名更可靠）。"""
        return bool(self.find_windows())

    def is_running(self) -> bool:
        """
        检查软件是否已在跑。
        优先：窗口已存在；其次：进程名匹配启动器或常见主程序名。
        """
        if self.window_exists():
            return True

        names = set(_EXTRA_PROCESS_NAMES)
        exe_name = Path(self.app_cfg.get("path", "")).name.lower()
        if exe_name:
            names.add(exe_name)

        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in names:
                return True
        return False

    def _launch_exe(self, exe: Path) -> None:
        """
        真正拉起安装包里的 exe。

        原理：
        - 工作目录必须设为 exe 所在 bin 目录，否则同花顺启动器找不到 DLL，表现为「点了没反应」
        - 打包成无控制台 exe 后，简单的 Popen([path]) 更容易失败，需多备几手
        """
        workdir = str(exe.parent)
        exe_s = str(exe)
        last_err: Exception | None = None

        # 1) 推荐：指定 cwd 的 Popen（与手动双击效果接近）
        try:
            flags = 0
            if sys.platform == "win32":
                # 独立进程组，避免随本程序退出被带走
                flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0x00000008
                )
            subprocess.Popen(
                [exe_s],
                cwd=workdir,
                shell=False,
                close_fds=False,
                creationflags=flags if sys.platform == "win32" else 0,
            )
            logger.info("已用 Popen 启动（cwd={}）", workdir)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Popen 启动失败，尝试 os.startfile: {}", e)

        # 2) Windows 壳启动（对中文路径通常更稳）
        if sys.platform == "win32":
            try:
                os.startfile(exe_s)  # type: ignore[attr-defined]
                logger.info("已用 os.startfile 启动: {}", exe_s)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("os.startfile 失败，尝试 cmd start: {}", e)

            try:
                subprocess.Popen(
                    f'start "" "{exe_s}"',
                    cwd=workdir,
                    shell=True,
                )
                logger.info("已用 cmd start 启动: {}", exe_s)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e

        raise RuntimeError(f"无法启动同花顺: {exe_s} | 最后错误: {last_err}")

    def start_app(self) -> bool:
        """
        启动同花顺期货通。
        若窗口已在，绝不重复启动（重复启动容易再弹一个登录框）。

        说明：即使 dry_run=模拟下单，也会真实打开软件（方便你对照盘面）；
        模拟只影响「是否真点击下单」，不影响拉起期货通。
        """
        if self.is_running() or self.window_exists():
            logger.info("软件已在运行（窗口或进程已存在），跳过启动")
            return True

        path = str(self.app_cfg.get("path", "") or "").strip()
        exe = Path(path) if path else None
        if not exe or not exe.exists():
            logger.error(
                "找不到软件路径: {}。请在「配置」里填对同花顺启动程序（如 D:\\同花顺期货通\\bin\\hexinlauncher.exe）",
                path,
            )
            # 模拟模式也要提示失败，避免误以为已打开
            return False

        logger.info("正在启动: {} （工作目录 {}）", exe, exe.parent)
        try:
            self._launch_exe(exe)
        except Exception as e:  # noqa: BLE001
            logger.exception("启动同花顺失败: {}", e)
            return False

        wait_sec = int(self.app_cfg.get("startup_wait_sec", 15))
        logger.info("等待软件加载 {} 秒...", wait_sec)
        time.sleep(wait_sec)
        if self.window_exists() or self.is_running():
            logger.info("同花顺已拉起（检测到窗口或进程）")
        else:
            logger.warning(
                "已执行启动，但尚未检测到「{}」窗口。"
                "若本机弹出了登录框请手动登录；若完全没弹出，请检查路径是否为 hexinlauncher.exe",
                self._title_key(),
            )
        return True

    def focus_window(self) -> bool:
        """把期货通窗口放到最前面。优先选标题更像「已登录交易」的窗口。"""
        title_key = self._title_key()
        if self.dry_run:
            logger.info("[模拟] 假设已激活窗口: {}", title_key)
            return True

        if gw is None:
            logger.warning("未安装 pygetwindow，无法自动激活窗口")
            return False

        windows = self.find_windows()
        if not windows:
            logger.warning("未找到标题含「{}」的窗口", title_key)
            return False

        from src.login.ths_login import trade_window_rank

        # 交易窗口优先于行情主窗口。宽度只作同分时的参考，不能让主窗口压过交易窗口。
        def _score(w: Any) -> tuple[int, int]:
            width = 0
            try:
                width = int(w.width or 0)
            except Exception:  # noqa: BLE001
                width = 0
            return (trade_window_rank(w.title or ""), width)

        windows.sort(key=_score, reverse=True)
        win = windows[0]
        hwnd = int(getattr(win, "_hWnd", 0) or 0)
        self.last_hwnd = hwnd
        # 已经是这个窗口时不要再激活。反复激活主窗口会把同花顺界面切来切去。
        front = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if hwnd and front == hwnd:
            return True
        # 刚拉起时窗口句柄可能还不稳定，多试几次，不再把「操作成功完成」打成错误。
        for _attempt in range(3):
            if force_foreground(hwnd):
                self.last_hwnd = hwnd
                time.sleep(0.3)
                logger.info("已激活窗口: {}", win.title)
                return True
            time.sleep(0.4)
            fresh = self.find_windows()
            if fresh:
                fresh.sort(key=_score, reverse=True)
                win = fresh[0]
                hwnd = int(getattr(win, "_hWnd", 0) or 0)
                self.last_hwnd = hwnd
        logger.warning("没能把同花顺窗口放到最前: {}", getattr(win, "title", ""))
        return False

    def login(self) -> bool:
        """
        打开同花顺，并按配置决定要不要自动登录。

        自动登录关掉时：窗口已经在就直接用，不填密码。
        自动登录打开时：点开「期货交易(主界面)」。如果弹出登录框，就填入密码并点登录。
        没有登录框，说明已经登录，不再输入。
        """
        logger.info(
            "开始登录流程 dry_run={} skip_auto_login={}",
            self.dry_run,
            self.skip_auto_login,
        )

        force_relogin = bool(self.account.get("force_relogin", False))
        had_window = self.window_exists()

        if not self.start_app():
            return False

        # 关掉自动登录：不碰密码。窗口本来就在时也不抢焦点，避免挡住自己的控制台。
        # 结算单窗口仍要点掉，这一下只点那个按钮，不点买卖。
        if self.skip_auto_login and not force_relogin:
            self._dismiss_settlement(2)
            if had_window:
                logger.info("期货通窗口已在，已跳过自动登录。下单前会再激活同花顺。")
            else:
                self.focus_window()
                logger.info("已打开同花顺，但自动登录是关的。请手动登录后再挂机。")
            return self.window_exists()

        from src.login.ths_login import login_trade_account, order_probe_points, order_window_is_open

        # 先把同花顺拿到最前，再点掉可能盖住坐标的结算单窗口，然后才看价格框。
        self.focus_window()
        self._dismiss_settlement(2)
        points = order_probe_points(self.trade_gui)
        try:
            panel_open = order_window_is_open(points)
        except Exception as exc:  # noqa: BLE001
            logger.warning("登录前自检失败，继续登录: {}", exc)
            panel_open = False
        if panel_open:
            logger.info("下单窗口还在，跳过这次登录，避免把窗口关掉。")
            return True

        user_id = str(self.account.get("user_id", "") or "")
        password = str(self.account.get("resolved_password", "") or "")
        if user_id in ("你的资金账号", "your_account", "账号"):
            user_id = ""
        if not password:
            logger.warning("自动登录已打开，但没有读到交易密码。请在配置页填写并保存。")
            return False

        try:
            from src.login.ths_login import login_trade_account

            # 模拟下单只决定会不会去点买卖按钮。登录框仍按配置去填。
            ok, message = login_trade_account(
                password,
                user_id=user_id,
                captcha_pause=str(self.account.get("captcha_mode", "pause")) == "pause",
                probe_points=points,
            )
            if user_id:
                logger.info("登录使用的资金账号: {}", user_id)
            logger.info(message)
            if ok:
                # 点完登录后，结算单窗口往往还要再等一两秒才出现。
                self._dismiss_settlement(5)
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception("登录失败: {}", e)
            return False

    def _dismiss_settlement(self, wait_sec: float) -> None:
        """点掉「确认结算单」。出错只记日志，不让整个登录中断。"""
        if self.dry_run:
            return
        try:
            from src.login.ths_login import confirm_settlement_if_present

            confirm_settlement_if_present(wait_sec=wait_sec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("确认结算单时出错，继续后面的步骤: {}", exc)
