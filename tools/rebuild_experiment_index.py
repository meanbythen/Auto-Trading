"""
重建回测实验索引：扫描 data/experiments/*/manifest.json，重写 index.json。

功能：
    1. 为历史实验补齐 data/experiments/index.json；
    2. 只读 manifest，只写 index.json，不搬动、不删除任何实验目录。

用法：
    python tools/rebuild_experiment_index.py
    python tools/rebuild_experiment_index.py --root .
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保项目根在路径上
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.experiment import rebuild_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建 data/experiments/index.json")
    parser.add_argument("--root", default=str(_ROOT), help="项目根目录（默认仓库根）")
    args = parser.parse_args(argv)

    root = Path(args.root)
    exp_dir = root / "data" / "experiments"
    try:
        count = rebuild_index(root / "data")
    except OSError as exc:
        print(f"重建实验索引失败: {exc}")
        return 1
    if count:
        print(f"已重建 {exp_dir / 'index.json'}，共 {count} 条实验。")
    else:
        print(f"没有可用实验，未写入索引（{exp_dir}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
