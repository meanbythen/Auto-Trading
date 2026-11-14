"""只读审计 data/backtest_last.json、data/optimize_last.json 和当前配置。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.audit import audit_results, format_audit_text


def _read_dict(path: Path, file_kind: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if file_kind == "json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 的根节点不是字典")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读审计最近一次回测、优化结果和当前配置")
    parser.add_argument("--root", type=Path, default=ROOT, help="项目根目录，默认按脚本位置推导")
    parser.add_argument("--json", action="store_true", dest="json_output", help="输出结构化 JSON")
    parser.add_argument("--strict", action="store_true", help="发现 critical 时退出码为 1")
    args = parser.parse_args(argv)

    try:
        backtest = _read_dict(args.root / "data" / "backtest_last.json", "json")
        optimize = _read_dict(args.root / "data" / "optimize_last.json", "json")
        config = _read_dict(args.root / "config" / "config.yaml", "yaml")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"审计输入错误：{exc}", file=sys.stderr)
        return 2

    report = audit_results(backtest, optimize, config)
    if args.json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(format_audit_text(report))
    return 1 if args.strict and report.counts["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
