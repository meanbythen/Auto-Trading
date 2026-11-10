"""
同花顺 GUI 自动下单试跑（模拟账户专用）
功能：激活期货通 → 鼠标先移到标定位置预览 → 倒计时后真实点击「买多1手」→ 再点「平仓」。
用法:
    python tools/gui_order_test.py
    或双击 试单测试.bat

注意:
    - 会真实移动鼠标并点击（不受 config 里 dry_run 限制）
    - 请保持下单面板可见、窗口不要挪动
    - 真下单时请不要自己动鼠标；卡住可按 Ctrl+Alt+Delete
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyautogui
from loguru import logger

from src.login import AppLoginManager
from src.trader.gui_trader import GuiTrader
from src.utils.config_loader import load_config


PREVIEW_KEYS = [
    ("price_input", "价格框"),
    ("volume_input", "手数框"),
    ("buy_open_btn", "买多按钮"),
    ("sell_open_btn", "卖空按钮"),
    ("close_btn", "平仓按钮"),
]


def _preview_moves(gui: dict) -> None:
    """只移动鼠标到标定坐标，不点击，方便肉眼确认对准了没有。"""
    print("\n=== 第1步：预览坐标（只移动，不点击）===")
    for key, label in PREVIEW_KEYS:
        pt = gui.get(key)
        if not pt or len(pt) < 2:
            print(f"  缺少坐标: {key}")
            continue
        x, y = int(pt[0]), int(pt[1])
        print(f"  移向 {label} -> ({x}, {y})")
        pyautogui.moveTo(x, y, duration=0.6)
        time.sleep(0.7)
    print("预览结束。若鼠标没停在对应按钮上，请重新标定后再测。\n")


def main() -> None:
    # 四角急停会跟填价格抢鼠标，试单和下单一样关掉。
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.25

    cfg = load_config()
    # 本脚本专门测真实点击，临时关闭模拟开关（不改写 config.yaml）
    cfg["dry_run"] = False
    trade = cfg.setdefault("trade", {})
    trade["executor"] = "gui"
    gui = trade.get("gui") or {}

    print("=" * 52)
    print("同花顺自动下单 · 模拟账户试跑")
    print("流程：预览坐标 → 倒计时 → 买多1手 → 等待 → 平仓")
    print("真下单时请不要自己动鼠标；要停扫描用运行页「停止」")
    print("=" * 52)

    missing = [k for k, _ in PREVIEW_KEYS if not gui.get(k)]
    if missing:
        print(f"坐标不完整，缺少: {missing}")
        print("请先运行 标定坐标.bat")
        sys.exit(1)

    # 激活窗口（强制非 dry_run，否则 focus 会被跳过）
    login = AppLoginManager(cfg)
    if not login.focus_window():
        print("警告：未能自动置顶期货通，请手动点一下同花顺窗口，5 秒后继续…")
        time.sleep(5)
    else:
        time.sleep(1)

    _preview_moves(gui)

    print("=== 第2步：5 秒后真实点击下单（买多 1 手，不改价格）===")
    for i in range(5, 0, -1):
        print(f"  {i}…")
        time.sleep(1)

    trader = GuiTrader(cfg)
    # 只改手数=1，价格沿用面板当前值，减少点错风险
    r1 = trader.buy_open(symbol=str(trade.get("symbol", "bu")), volume=1, price=None)
    print(f"买开结果: ok={r1.ok} msg={r1.message} id={r1.order_id}")
    logger.info("试单买开: {}", r1)

    print("等待 3 秒后尝试平仓…")
    time.sleep(3)
    r2 = trader.close_position(symbol=str(trade.get("symbol", "bu")), volume=1, price=None)
    print(f"平仓结果: ok={r2.ok} msg={r2.message} id={r2.order_id}")
    logger.info("试单平仓: {}", r2)

    print("\n请到同花顺查看：委托/成交/持仓是否有变化。")
    print("若鼠标点到别处，重新标定坐标后再测。")
    print("config.yaml 的 dry_run 仍保持原值（本脚本未永久改配置）。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        logger.exception("试单失败: {}", e)
        print(f"试单失败: {e}")
        sys.exit(1)
