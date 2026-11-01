"""
挂机前自检入口
用法:
    python tools/preflight_check.py              # 配置+行情+窗口+坐标预览，询问是否点开平
    python tools/preflight_check.py --click      # 含真实买开/平仓点击
    python tools/preflight_check.py --config-only
    python tools/preflight_check.py --yes-click  # 不问，直接含点击（给界面调用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.config_loader import load_config
from src.utils.preflight import run_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="挂机前自检")
    parser.add_argument("--click", action="store_true", help="包含真实开平点击测试")
    parser.add_argument("--yes-click", action="store_true", help="不询问，直接做开平点击")
    parser.add_argument("--config-only", action="store_true", help="只检查配置和行情，不动鼠标")
    parser.add_argument("--no-preview", action="store_true", help="不做坐标鼠标预览")
    args = parser.parse_args()

    cfg = load_config()
    cfg["_project_root"] = str(ROOT)

    do_click = bool(args.click or args.yes_click)
    do_preview = not args.config_only and not args.no_preview

    if args.config_only:
        do_click = False
        do_preview = False
    elif not args.yes_click and not args.click and not args.config_only:
        print()
        print("是否做「真实点击」开平测试？（模拟账户建议做一次）")
        print("  Y = 买开1手再平仓（会动账户）")
        print("  N = 只检查配置+预览坐标")
        ans = input("请选择 [Y/N]: ").strip().lower()
        do_click = ans in ("y", "yes", "是")

    report = run_preflight(cfg, do_click_test=do_click, do_preview=do_preview)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
