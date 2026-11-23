"""自适应斜率阈值分布分析。

用法：
    python tools/slope_adaptive_report.py
    python tools/slope_adaptive_report.py --periods 15 --scenarios 20:4.5,60:6,120:3
    python tools/slope_adaptive_report.py --symbols lh2701,fu2701 --out data/自适应阈值.xlsx

看的是「自适应阈值」本身：在每个品种的 15 分钟历史上，过去 500 根 |角度| 的
0.8 分位、再夹到基准 0.7~1.3 倍，会落在什么区间、抖动多大、把覆盖率改成多少。
只读本地清洗库，不改配置、不下单。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.data.bar_store import BarStore  # noqa: E402
from src.data.inventory import split_stored_symbol  # noqa: E402
from src.strategy.indicators import average_true_range, ema_series  # noqa: E402
from src.strategy.slope import (  # noqa: E402
    ADAPTIVE_MAX_RATIO,
    ADAPTIVE_MIN_RATIO,
    slope_angle_series,
)


def _product(contract: str) -> str:
    match = re.match(r"^[a-zA-Z]+", contract.strip())
    return match.group(0).lower() if match else contract


def _parse_scenarios(text: str) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for part in text.replace("，", ",").split(","):
        if not part.strip():
            continue
        ema_text, _, base_text = part.partition(":")
        ema_period = max(2, int(ema_text))
        base = abs(float(base_text or "3"))
        out.append((ema_period, base))
    return out


def _pick_stored(store: BarStore, periods: set[str], wanted: set[str] | None) -> list[tuple[str, str, str]]:
    picked: dict[tuple[str, str], str] = {}
    for item in store.summarize():
        stored = str(item["symbol"])
        period = str(item["period"])
        if period not in periods:
            continue
        contract, _ = split_stored_symbol(stored)
        if wanted is not None and contract not in wanted and stored not in wanted:
            continue
        key = (contract, period)
        old = picked.get(key)
        if old is None or ("@tq" in stored and "@tq" not in old):
            picked[key] = stored
    return [(contract, period, stored) for (contract, period), stored in sorted(picked.items())]


def analyze_frame(
    frame: pd.DataFrame,
    *,
    ema_period: int,
    base_deg: float,
    lookback: int,
    quantile: float,
    window: int,
) -> dict[str, float]:
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame.get("high", close), errors="coerce")
    low = pd.to_numeric(frame.get("low", close), errors="coerce")
    ema = ema_series(close, ema_period)
    atr_s = average_true_range(high, low, close, 14)
    angles = slope_angle_series(ema, atr_s, lookback=lookback, method="reg")
    abs_ang = angles.abs()
    valid = abs_ang.notna()
    rolling = abs_ang.rolling(window, min_periods=50).quantile(quantile)
    thr = rolling.clip(lower=ADAPTIVE_MIN_RATIO * base_deg, upper=ADAPTIVE_MAX_RATIO * base_deg)
    thr = thr.where(rolling.notna(), base_deg)
    t = thr[valid]
    a = abs_ang[valid]
    eps = 1e-9
    moved = t.diff().abs().dropna()
    fixed_cov = float((a >= base_deg - eps).mean() * 100)
    adap_cov = float((a >= t - eps).mean() * 100)
    return {
        "有效根数": int(len(t)),
        "固定覆盖率%": round(fixed_cov, 2),
        "自适应覆盖率%": round(adap_cov, 2),
        "覆盖率差pp": round(adap_cov - fixed_cov, 2),
        "阈值均值": round(float(t.mean()), 2),
        "阈值中位": round(float(t.median()), 2),
        "阈值标准差": round(float(t.std(ddof=0)), 2),
        "阈值最小": round(float(t.min()), 2),
        "阈值q10": round(float(t.quantile(0.10)), 2),
        "阈值q90": round(float(t.quantile(0.90)), 2),
        "阈值最大": round(float(t.max()), 2),
        "抬高占比%": round(float((t > base_deg + eps).mean() * 100), 2),
        "降低占比%": round(float((t < base_deg - eps).mean() * 100), 2),
        "触底占比%": round(float((t <= ADAPTIVE_MIN_RATIO * base_deg + eps).mean() * 100), 2),
        "触顶占比%": round(float((t >= ADAPTIVE_MAX_RATIO * base_deg - eps).mean() * 100), 2),
        "平均单根变动": round(float(moved.mean()) if len(moved) else 0.0, 4),
        "最大单根变动": round(float(moved.max()) if len(moved) else 0.0, 2),
    }


def build_rows(
    *,
    periods: set[str],
    scenarios: list[tuple[int, float]],
    wanted: set[str] | None,
    lookback: int,
    quantile: float,
    window: int,
    min_bars: int,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    store = BarStore(ROOT / "data" / "klines" / "market.sqlite")
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for contract, period, stored in _pick_stored(store, periods, wanted):
        frame = store.load(stored, period)
        need = max(ema for ema, _ in scenarios) + lookback + 120
        if frame is None or len(frame) < max(min_bars, need):
            skipped.append(f"{stored}/{period}（{0 if frame is None else len(frame)} 根）")
            continue
        frame = frame.sort_index()
        for ema_period, base in scenarios:
            stats = analyze_frame(
                frame,
                ema_period=ema_period,
                base_deg=base,
                lookback=lookback,
                quantile=quantile,
                window=window,
            )
            rows.append(
                {
                    "场景": f"EMA{ema_period}/{base:g}°",
                    "品种": _product(contract),
                    "合约": contract,
                    "周期": period,
                    "起": str(frame.index[0]),
                    "止": str(frame.index[-1]),
                    "基准阈值": base,
                    **stats,
                }
            )
    meta = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据": str((ROOT / "data" / "klines" / "market.sqlite").resolve()),
        "周期": "、".join(sorted(periods)),
        "回看根数": lookback,
        "自适应分位": quantile,
        "自适应窗口": window,
        "夹紧区间": f"{ADAPTIVE_MIN_RATIO:g}~{ADAPTIVE_MAX_RATIO:g} × 基准",
        "口径": "reg 回归斜率，ATR14 归一化；逐根算角度，窗口含当前已收盘根",
        "未纳入": "；".join(skipped) or "无",
    }
    return rows, skipped, meta


def _conclusions(df: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    for scenario, part in df.groupby("场景", sort=False):
        base = float(part["基准阈值"].iloc[0])
        up = part["阈值均值"] >= base
        raised = ", ".join(part.loc[up, "合约"].tolist()) or "无"
        lowered = ", ".join(part.loc[~up, "合约"].tolist()) or "无"
        lo = float(part["自适应覆盖率%"].min())
        hi = float(part["自适应覆盖率%"].max())
        cap = float(part["触顶占比%"].max())
        floor = float(part["触底占比%"].max())
        lines.append(
            f"{scenario}：均值阈值高于基准的 {raised}；低于基准的 {lowered}。"
            f"自适应覆盖率 {lo:.1f}%~{hi:.1f}%，触顶最高 {cap:.1f}%，触底最高 {floor:.1f}%。"
        )
    return lines


def write_report(path: Path, df: pd.DataFrame, meta: dict[str, Any]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "说明"
    ws["A1"] = "自适应斜率阈值分布分析"
    ws["A1"].font = Font(size=14, bold=True)
    ws["A3"] = "公式"
    ws["A3"].font = Font(bold=True)
    ws["A4"] = "T_t = clip( 过去 window 根 |角度| 的 quantile 分位, 0.7×基准, 1.3×基准 )"
    ws["A5"] = "角度 = atan(EMA 近 N 根回归涨幅 / (N × ATR14))，单位度；当前根已收盘，计入窗口。"
    ws["A6"] = "有效角度不足 50 根时退回基准阈值；覆盖率=满足 |角度| ≥ 阈值的 K 线占比。"
    row = 8
    for key, value in meta.items():
        ws.cell(row=row, column=1, value=key).font = Font(bold=True)
        ws.cell(row=row, column=2, value=str(value))
        row += 1
    row += 1
    ws.cell(row=row, column=1, value="结论").font = Font(bold=True)
    row += 1
    for line in _conclusions(df):
        ws.cell(row=row, column=1, value=line)
        row += 1
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 90

    def _sheet(title: str, frame: pd.DataFrame) -> None:
        sheet = wb.create_sheet(title)
        headers = list(frame.columns)
        sheet.append(headers)
        fill = PatternFill("solid", fgColor="DDEBF7")
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for record in frame.itertuples(index=False):
            sheet.append(list(record))
        sheet.freeze_panes = "A2"
        for idx, name in enumerate(headers, start=1):
            width = 10
            if name in {"场景", "合约", "品种"}:
                width = 12
            elif name in {"起", "止"}:
                width = 20
            elif len(name) > 6:
                width = 14
            sheet.column_dimensions[get_column_letter(idx)].width = width

    _sheet("分布", df)
    summary = (
        df.groupby(["场景", "品种"], sort=False)
        .agg(
            合约数=("合约", "count"),
            有效根数=("有效根数", "sum"),
            基准阈值=("基准阈值", "first"),
            固定覆盖率=("固定覆盖率%", "mean"),
            自适应覆盖率=("自适应覆盖率%", "mean"),
            覆盖率差pp=("覆盖率差pp", "mean"),
            阈值均值=("阈值均值", "mean"),
            阈值标准差=("阈值标准差", "mean"),
            抬高占比=("抬高占比%", "mean"),
            触顶占比=("触顶占比%", "mean"),
            触底占比=("触底占比%", "mean"),
        )
        .reset_index()
    )
    for col in ("固定覆盖率", "自适应覆盖率", "覆盖率差pp", "阈值均值", "阈值标准差", "抬高占比", "触顶占比", "触底占比"):
        summary[col] = summary[col].round(2)
    _sheet("品种汇总", summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="分析自适应斜率阈值在各品种上的分布")
    parser.add_argument("--symbols", default="", help="逗号分隔的合约，留空=库里全部")
    parser.add_argument("--periods", default="15", help="逗号分隔的周期")
    parser.add_argument(
        "--scenarios",
        default="20:4.5,60:6,120:3",
        help="逗号分隔的 均线:基准角度，如 20:4.5,120:3",
    )
    parser.add_argument("--lookback", type=int, default=20, help="斜率回看根数")
    parser.add_argument("--quantile", type=float, default=0.80, help="自适应分位")
    parser.add_argument("--window", type=int, default=500, help="自适应窗口")
    parser.add_argument("--min-bars", type=int, default=1200, help="最少 K 线根数")
    parser.add_argument("--out", default=str(ROOT / "data" / "自适应斜率阈值分布.xlsx"))
    args = parser.parse_args()

    wanted = {s.strip() for s in args.symbols.replace("，", ",").split(",") if s.strip()} or None
    rows, skipped, meta = build_rows(
        periods={p.strip() for p in args.periods.split(",") if p.strip()},
        scenarios=_parse_scenarios(args.scenarios),
        wanted=wanted,
        lookback=args.lookback,
        quantile=args.quantile,
        window=args.window,
        min_bars=args.min_bars,
    )
    if not rows:
        print("没有可分析的数据：检查合约、周期和本地 K 线根数。")
        print(f"未纳入：{meta['未纳入']}")
        return 2
    df = pd.DataFrame(rows)
    out = write_report(Path(args.out), df, meta)
    print(f"已写入 {out}")
    print(f"未纳入：{meta['未纳入']}")
    show = df[df["场景"] == df["场景"].iloc[0]][
        ["合约", "有效根数", "固定覆盖率%", "自适应覆盖率%", "覆盖率差pp", "阈值均值", "阈值标准差", "抬高占比%", "触顶占比%"]
    ]
    print(show.to_string(index=False))
    for line in _conclusions(df):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
