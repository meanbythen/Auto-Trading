"""清理 data/param_history.json 和 data/config_presets.json 里已删策略的残留。

运行: python tools/cleanup_deleted_engines.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DELETED_ENGINES = {
    # 9.40.0 删除的两套
    "single_ma",
    "Single_ma",
    "ma20_trend",
    "ma20",
    "morning_structure_b",
    "morning_b",
    # 19.0.0 删除早盘箱体突破 A
    "morning_box_a",
    "morning_a",
    # 23.0.0 删除 Jesse Casper
    "jesse_casper",
    "casper",
    "jesse",
    # 24.0.0 删除多因子自适应权重
    "multi_factor_adapt",
    # 25.0.0 删除多因子混合
    "multi_hybrid",
    "hybrid",
    # 26.0.0 删除多因子投票
    "multi_vote",
    "vote",
    # 27.0.0 删除日内实体·扩张
    "daily_body_expand",
    # 更早删除的（顺手清，让文件更干净）
    "morning_gap_c",
    "morning_c",
    "sar_flip",
    "mean_reversion",
    "liquidity_exhaustion",
    "liquidity_signs",
    "macd_divergence",
    "macd_top_bottom",
    "macd_ths",
    "macd_zero",
    "macd",
    "reversal_pivot",
    "fvg",
    "intraday_breakout",
    "intraday",
    "pinbar_sar",
    "ema_pin_momentum",
    "ema_pin",
    "price_action",
    "pa_liquidity",
    "custom",
}
DELETED_LOWER = {e.lower() for e in DELETED_ENGINES}


def _clean_dict_keys(d: dict) -> tuple[dict, int]:
    """删掉字典 key 命中 DELETED_ENGINES 的顶层条目，子字典递归。"""
    removed = 0
    out: dict = {}
    for k, v in d.items():
        kl = k.lower() if isinstance(k, str) else ""
        if kl in DELETED_LOWER:
            removed += 1
            continue
        if isinstance(v, dict):
            v2, r2 = _clean_dict_keys(v)
            removed += r2
            out[k] = v2
        elif isinstance(v, list):
            v2, r2 = _clean_list(v)
            removed += r2
            out[k] = v2
        else:
            out[k] = v
    return out, removed


def _clean_list(items: list) -> tuple[list, int]:
    removed = 0
    out = []
    for item in items:
        if isinstance(item, dict):
            d2, r2 = _clean_dict_keys(item)
            removed += r2
            out.append(d2)
        elif isinstance(item, list):
            l2, r2 = _clean_list(item)
            removed += r2
            out.append(l2)
        elif isinstance(item, str) and item.lower() in DELETED_LOWER:
            # 列表里的引擎名字符串（如 cross_engines、参数网格引擎列）
            removed += 1
            continue
        else:
            out.append(item)
    return out, removed


def _is_deleted_engine(obj) -> bool:
    """判断一条历史记录本身就是已删引擎（engine 字段命中），整条丢掉。"""
    if isinstance(obj, dict):
        eng = obj.get("engine") or obj.get("engine_normalized") or ""
        if isinstance(eng, str) and eng.lower() in DELETED_LOWER:
            return True
    return False


def cleanup_param_history(path: Path, dry_run: bool) -> int:
    if not path.exists():
        print(f"skip {path} (not found)")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    versions = data.get("versions", [])
    before = len(versions)

    # 1) 整条 engine 命中已删引擎 → 直接过滤掉
    new_versions = [v for v in versions if not _is_deleted_engine(v)]
    dropped_entries = before - len(new_versions)

    # 2) 剩余条目里，snapshot / opt_params / 其它嵌套字典里的已删引擎小节 → 清掉
    removed_sections = 0
    for v in new_versions:
        for key in ("snapshot", "opt_params", "params"):
            val = v.get(key)
            if isinstance(val, dict):
                cleaned, r = _clean_dict_keys(val)
                removed_sections += r
                v[key] = cleaned
            elif isinstance(val, list):
                cleaned, r = _clean_list(val)
                removed_sections += r
                v[key] = cleaned
        # strategy 字段（部分旧版本可能是完整 dict）
        st = v.get("strategy")
        if isinstance(st, dict):
            cleaned, r = _clean_dict_keys(st)
            removed_sections += r
            v["strategy"] = cleaned
        elif isinstance(st, str) and st.lower() in DELETED_LOWER:
            v["strategy"] = "daily_body"

    print(f"param_history: {before} -> {len(new_versions)} entries, dropped {dropped_entries} entries, removed {removed_sections} sections")

    if not dry_run:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        data["versions"] = new_versions
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  wrote {path} (backup at {backup})")
    return dropped_entries + removed_sections


def cleanup_config_presets(path: Path, dry_run: bool) -> int:
    if not path.exists():
        print(f"skip {path} (not found)")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    presets = data.get("presets", [])
    before = len(presets)

    # 1) 整条 engine 命中已删引擎 → 直接过滤掉
    new_presets = [p for p in presets if not _is_deleted_engine(p)]
    dropped_entries = before - len(new_presets)

    # 2) 剩余 preset 里嵌套的 strategy 小节、cross_engines 列表里的已删名清掉
    removed_sections = 0
    for p in new_presets:
        st = p.get("strategy")
        if isinstance(st, dict):
            # engine 本身可能在已删列表 → 改成 daily_body（preset 名保留，内容修正）
            eng = st.get("engine") or ""
            if isinstance(eng, str) and eng.lower() in DELETED_LOWER:
                st["engine"] = "daily_body"
            cleaned, r = _clean_dict_keys(st)
            removed_sections += r
            p["strategy"] = cleaned
        # cross_engines 列表
        cross = p.get("cross_engines")
        if isinstance(cross, list):
            new_cross = [e for e in cross if not (isinstance(e, str) and e.lower() in DELETED_LOWER)]
            removed_sections += len(cross) - len(new_cross)
            p["cross_engines"] = new_cross
        # 其它嵌套
        for key in ("config", "params"):
            val = p.get(key)
            if isinstance(val, dict):
                cleaned, r = _clean_dict_keys(val)
                removed_sections += r
                p[key] = cleaned
            elif isinstance(val, list):
                cleaned, r = _clean_list(val)
                removed_sections += r
                p[key] = cleaned

    print(f"config_presets: {before} -> {len(new_presets)} presets, dropped {dropped_entries} entries, removed {removed_sections} sections")

    if not dry_run:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        data["presets"] = new_presets
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  wrote {path} (backup at {backup})")
    return dropped_entries + removed_sections


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印改动数量，不写文件")
    args = ap.parse_args()

    hist = ROOT / "data" / "param_history.json"
    presets = ROOT / "data" / "config_presets.json"

    total = 0
    total += cleanup_param_history(hist, args.dry_run)
    total += cleanup_config_presets(presets, args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] 共将清理 {total} 处残留，未写文件。")
    else:
        print(f"\n完成：共清理 {total} 处残留。原文件已另存为 .bak。")


if __name__ == "__main__":
    main()
