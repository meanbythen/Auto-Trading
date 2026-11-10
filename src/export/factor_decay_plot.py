"""
多因子滚动盈亏衰减图。

对照资料《策略上线前自检清单》第二节的画图思路：
按开仓时四个因子 |得分| 分摊这一笔盈亏，再画最近 20 笔滚动盈亏和累计盈亏。
数据用本程序自己的字段（score_turtle，不是资料示例里的 don_score）。
可以读：最近一次回测、实盘分摊日志，以及资料那种 CSV。
这里只画图，不改开平仓。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.strategy.cta_factors import FACTOR_KEYS, FACTOR_LABELS, allocate_factor_pnl, scores_from_meta

ROLL_WINDOW = 20
PNL_COLS = ("pnl_ma", "pnl_turtle", "pnl_boll", "pnl_sar")
LATEST_NAME = "因子衰减图.png"


def _as_time(value: Any) -> datetime | None:
    """把开平仓时间收成可排序的时刻。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    text = str(value).replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(text).to_pydatetime()
    except Exception:  # noqa: BLE001
        return None


def _score_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """资料 CSV 用 ma_score / don_score；本程序用 score_ma / score_turtle。两种都认。"""
    out = dict(raw)
    if out.get("score_ma") is None and out.get("ma_score") is not None:
        out["score_ma"] = out.get("ma_score")
    turtle = out.get("score_turtle", out.get("turtle_score", out.get("don_score")))
    if out.get("score_turtle") is None and turtle is not None:
        out["score_turtle"] = turtle
    if out.get("score_boll") is None and out.get("boll_score") is not None:
        out["score_boll"] = out.get("boll_score")
    if out.get("score_sar") is None and out.get("sar_score") is not None:
        out["score_sar"] = out.get("sar_score")
    if not out.get("exit_time") and out.get("datetime"):
        out["exit_time"] = out.get("datetime")
    return out


def _row_from_trade(item: dict[str, Any]) -> dict[str, Any] | None:
    """没有四个因子分数的成交不进图（日内实体等其它策略的回测不会混进来）。"""
    raw = _score_aliases(item)
    scores = scores_from_meta(raw)
    has_ready_pnl = all(raw.get(col) is not None for col in PNL_COLS)
    if not has_ready_pnl and sum(abs(v) for v in scores.values()) <= 1e-12:
        return None
    pnl = float(raw.get("pnl") or 0.0)
    if has_ready_pnl:
        allocated = {key: float(raw.get(f"pnl_{key}") or 0.0) for key in FACTOR_KEYS}
    else:
        allocated = allocate_factor_pnl(scores, pnl)
    when = _as_time(raw.get("exit_time")) or _as_time(raw.get("entry_time"))
    if when is None:
        when = datetime.now()
    return {
        "datetime": when,
        "symbol": str(raw.get("symbol") or ""),
        "pnl": pnl,
        "score_ma": scores["ma"],
        "score_turtle": scores["turtle"],
        "score_boll": scores["boll"],
        "score_sar": scores["sar"],
        "pnl_ma": allocated["ma"],
        "pnl_turtle": allocated["turtle"],
        "pnl_boll": allocated["boll"],
        "pnl_sar": allocated["sar"],
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_factor_trades(
    root: Path | str,
    *,
    csv_path: Path | str | None = None,
    include_backtest: bool = True,
    include_live: bool = True,
) -> pd.DataFrame:
    """
    读出能画图的成交。

    默认：最近一次历史回测 + data/cta_factor_trades.jsonl。
    另外可以指定资料那种 CSV。
    """
    base = Path(root)
    items: list[dict[str, Any]] = []
    if include_backtest:
        last = base / "data" / "backtest_last.json"
        if last.exists():
            try:
                payload = json.loads(last.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            items.extend(payload.get("trades") or [])
    if include_live:
        from src.export.cta_decay import live_log_path

        items.extend(_read_jsonl(live_log_path(base)))
    if csv_path:
        csv_file = Path(csv_path)
        if csv_file.exists():
            frame = pd.read_csv(csv_file)
            items.extend(frame.to_dict(orient="records"))
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mapped = _row_from_trade(item)
        if mapped is not None:
            rows.append(mapped)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
    return out


def factor_summary(df: pd.DataFrame) -> pd.DataFrame:
    """四个因子的总盈亏、有分摊的笔数、赚钱笔数、胜率。"""
    lines: list[dict[str, Any]] = []
    for key in FACTOR_KEYS:
        col = f"pnl_{key}"
        series = df[col] if col in df.columns else pd.Series(dtype=float)
        trades = int((series.abs() > 1e-12).sum())
        wins = int((series > 1e-12).sum())
        lines.append(
            {
                "因子": FACTOR_LABELS[key],
                "总盈亏": float(series.sum()) if len(series) else 0.0,
                "交易次数": trades,
                "盈利次数": wins,
                "胜率": (wins / trades) if trades else 0.0,
            }
        )
    return pd.DataFrame(lines)


def plot_factor_decay(
    df: pd.DataFrame,
    dest: Path,
    *,
    window: int = ROLL_WINDOW,
) -> Path:
    """
    画出四张子图。实线=最近 window 笔滚动盈亏，虚线=累计盈亏。

    用文件保存，不弹交互窗口，方便从按钮或命令行调用。
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise ImportError("还没有安装 matplotlib。请先执行: pip install matplotlib") from exc

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True)
    fig.suptitle(f"多因子CTA 因子滚动盈亏衰减曲线（滚动窗口={int(window)}笔）", fontsize=14)
    colors = ["#1f77b4", "#ff7e0e", "#2ca02c", "#d62728"]
    for i, key in enumerate(FACTOR_KEYS):
        col = f"pnl_{key}"
        roll = df[col].rolling(window=max(1, int(window)), min_periods=1).sum()
        cum = df[col].cumsum()
        ax = axes[i]
        ax.plot(df["datetime"], roll, label=f"{FACTOR_LABELS[key]} 滚动{int(window)}笔盈亏", color=colors[i], lw=1.5)
        ax.plot(df["datetime"], cum, label=f"{FACTOR_LABELS[key]} 累计盈亏", color=colors[i], linestyle="--", lw=1.2)
        ax.axhline(y=0, color="k", lw=0.8, alpha=0.6)
        ax.set_ylabel(FACTOR_LABELS[key])
        ax.legend(loc="upper left")
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel("时间")
    fig.tight_layout()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return dest


def write_factor_decay_plot(
    root: Path | str,
    *,
    window: int = ROLL_WINDOW,
    csv_path: Path | str | None = None,
) -> tuple[Path, pd.DataFrame]:
    """
    读成交、画图、另存 PNG。

    每次带时间戳的一份不覆盖旧图；同时更新 data/因子衰减图.png 方便你找最新一张。
    """
    base = Path(root)
    df = load_factor_trades(base, csv_path=csv_path)
    if df.empty:
        raise ValueError("还没有带四因子分数的成交。请先用多因子策略跑完历史回测，或等模拟盘平过仓。")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dated = base / "data" / f"因子衰减图-{stamp}.png"
    latest = base / "data" / LATEST_NAME
    plot_factor_decay(df, dated, window=window)
    latest.write_bytes(dated.read_bytes())
    logger.info("已画出因子衰减图 {} 笔 → {}", len(df), dated)
    return dated, df


def main(argv: list[str] | None = None) -> int:
    """命令行：python -m src.export.factor_decay_plot"""
    parser = argparse.ArgumentParser(description="画出多因子四个因子的滚动盈亏衰减图")
    parser.add_argument("--csv", dest="csv_path", default="", help="可选，资料那种交易 CSV")
    parser.add_argument("--window", type=int, default=ROLL_WINDOW, help="滚动几笔，默认 20")
    args = parser.parse_args(argv)
    from src.utils.paths import project_root

    root = project_root()
    try:
        path, df = write_factor_decay_plot(
            root,
            window=max(1, int(args.window)),
            csv_path=args.csv_path or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(str(exc))
        return 1
    print(f"已保存 {path}，共 {len(df)} 笔")
    print(factor_summary(df).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
