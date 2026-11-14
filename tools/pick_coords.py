"""
同花顺下单坐标标定工具。

用法:
    python tools/pick_coords.py
    或在图形界面配置页点「标定下单坐标」

窗口本体在 src/ui/coord_picker.py。这里只负责从项目根目录启动，
这样双击脚本时也能找到配置。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ui.coord_picker import main


if __name__ == "__main__":
    main()
