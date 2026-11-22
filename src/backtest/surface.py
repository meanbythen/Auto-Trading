"""参数曲面：把优化 trials 的得分按两个参数摆成二维表。

只读已有 trials，不重跑回测。同一格有多组 trial 时取最高分，空格保留为 None。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable


def _trials_box(trials: Iterable[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in trials or []:
        box = item if isinstance(item, dict) else {}
        params = box.get("params")
        if isinstance(params, dict):
            out.append(box)
    return out


def numeric_param_keys(trials: Iterable[Any] | None) -> list[str]:
    """trials 里出现过的数值参数名，按首次出现顺序。"""
    keys: list[str] = []
    for trial in _trials_box(trials):
        for key, value in (trial.get("params") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and key not in keys:
                keys.append(str(key))
    return keys


def build_surface(
    trials: Iterable[Any] | None,
    x_key: str,
    y_key: str,
) -> dict[str, Any]:
    """生成二维曲面。返回的 cells 是 [{x, y, score, runs}]，空格不出现。"""
    x_key = str(x_key or "").strip()
    y_key = str(y_key or "").strip()
    if not x_key or not y_key or x_key == y_key:
        return {"ok": False, "error": "横纵轴要选两个不同的数值参数", "cells": []}

    boxes = _trials_box(trials)
    xs: set[float] = set()
    ys: set[float] = set()
    best: dict[tuple[float, float], tuple[float, int]] = {}
    for trial in boxes:
        params = trial.get("params") or {}
        if x_key not in params or y_key not in params:
            continue
        try:
            x = float(params[x_key])
            y = float(params[y_key])
            score = float(trial.get("score") or 0)
        except (TypeError, ValueError):
            continue
        xs.add(x)
        ys.add(y)
        old = best.get((x, y))
        if old is None or score > old[0]:
            best[(x, y)] = (score, 1)
        else:
            best[(x, y)] = (old[0], old[1] + 1)

    if len(xs) < 2 or len(ys) < 2:
        return {
            "ok": False,
            "error": "这两个参数的取值不足两档，画不出曲面",
            "x_key": x_key,
            "y_key": y_key,
            "cells": [],
        }

    cells = [
        {"x": x, "y": y, "score": round(score, 6), "runs": runs}
        for (x, y), (score, runs) in sorted(best.items())
    ]
    top = max(cells, key=lambda item: item["score"]) if cells else None
    return {
        "ok": True,
        "error": "",
        "x_key": x_key,
        "y_key": y_key,
        "x_values": sorted(xs),
        "y_values": sorted(ys),
        "cells": cells,
        "best": top,
        "runs": len(boxes),
    }


def save_surface_csv(surface: dict[str, Any], path: str | Path) -> Path:
    """把曲面写成「行=纵轴、列=横轴」的 CSV，空格留空。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    xs = [float(value) for value in surface.get("x_values") or []]
    ys = [float(value) for value in surface.get("y_values") or []]
    lookup = {
        (float(cell["x"]), float(cell["y"])): cell["score"]
        for cell in surface.get("cells") or []
    }
    x_key = str(surface.get("x_key") or "x")
    y_key = str(surface.get("y_key") or "y")
    with out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"{y_key}\\{x_key}", *[f"{value:g}" for value in xs]])
        for y in ys:
            row: list[Any] = [f"{y:g}"]
            for x in xs:
                value = lookup.get((x, y))
                row.append("" if value is None else f"{float(value):.6f}")
            writer.writerow(row)
    return out
