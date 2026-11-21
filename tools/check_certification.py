"""合约参数体检 / 再认证检查。

用法：
    python tools/check_certification.py             # 检查并更新快照
    python tools/check_certification.py --no-write  # 只看结果，不动快照

退出码：0 = 没有错误；1 = 保证金 / 乘数缺失等错误，适合定时任务报警。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.review.certification import format_certification_lines, run_certification
from src.risk.universe_table import apply_universe_table_to_cfg
from src.utils import load_config


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    try:
        apply_universe_table_to_cfg(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"品种与合约表载入失败：{exc}")
        return 1
    report = run_certification(cfg, root=_ROOT, write="--no-write" not in args)
    print("\n".join(format_certification_lines(report)))
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
