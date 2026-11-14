"""
挂机前自检
功能：检查配置是否适合自动监测/下单，并可选实测「买开→平仓」点击是否准确。
原理：分三关——配置静态检查 → 窗口/坐标预览 →（可选）真实点1手开平。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from src.data.periods import period_label

LogFn = Callable[[str], None]


@dataclass
class CheckItem:
    """单项检查结果。"""

    name: str
    ok: bool
    level: str  # pass | warn | fail
    detail: str


@dataclass
class PreflightReport:
    """自检总报告。"""

    ok: bool
    started_at: str
    items: list[CheckItem] = field(default_factory=list)
    click_tested: bool = False
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, level: str | None = None) -> None:
        if level is None:
            level = "pass" if ok else "fail"
        self.items.append(CheckItem(name=name, ok=ok, level=level, detail=detail))
        if level == "fail":
            self.ok = False


def _log(msg: str, log: LogFn | None) -> None:
    logger.info(msg)
    if log:
        log(msg)


# 标定窗口留下的点，和配置页正在用的点，差在这个范围里只提醒。
# 换过显示器时通常会差几十上百像素，那种才算没通过。
_COORD_WARN_PIXELS = 20

_COORD_LABELS = {
    "price_input": "价格框",
    "volume_input": "手数框",
    "buy_open_btn": "买多",
    "sell_open_btn": "卖空",
    "close_btn": "平仓",
}


def describe_coord_mismatch(current: dict[str, Any], saved: dict[str, Any]) -> tuple[str, str] | None:
    """
    比较配置里正在用的坐标，和标定文件里留下的坐标。

    完全一样就不用提。差几个像素只提醒，程序仍用配置里的点。
    差得远就判没通过，避免拿着另一块屏幕的点去点。
    """
    gaps: list[tuple[str, int]] = []
    for key, label in _COORD_LABELS.items():
        now = current.get(key)
        old = saved.get(key)
        if not now or not old or len(now) < 2 or len(old) < 2:
            continue
        gap = max(abs(int(now[0]) - int(old[0])), abs(int(now[1]) - int(old[1])))
        if gap:
            gaps.append((label, gap))
    if not gaps:
        return None
    largest = max(gap for _label, gap in gaps)
    names = "、".join(label for label, _gap in gaps)
    if largest <= _COORD_WARN_PIXELS:
        return (
            "warn",
            f"{names}和上次标定差了最多 {largest} 像素。程序用的是配置里现在这套点，刚才鼠标也是按这套走的。",
        )
    return (
        "fail",
        f"{names}和上次标定差了最多 {largest} 像素。这通常是换了屏幕。请用现在这块屏幕再标定一次，或在列表里选对的那一份。",
    )


def check_config(cfg: dict[str, Any], report: PreflightReport, log: LogFn | None = None) -> None:
    """静态检查配置：通道、模拟开关、坐标、资金、时段等。"""
    trade = cfg.get("trade") or {}
    gui = trade.get("gui") or {}
    risk = cfg.get("risk") or {}
    st = cfg.get("strategy") or {}
    app = cfg.get("app") or {}

    executor = str(trade.get("executor", "sim"))
    dry = bool(cfg.get("dry_run", True))

    if executor == "gui":
        report.add("执行通道", True, "executor=gui（会点击同花顺）")
    elif executor == "sim":
        report.add("执行通道", True, "executor=sim（只模拟成交，不点同花顺）", "warn")
    elif executor == "ctp":
        from src.trader.ctp_logic import ctp_kind_label, overlay_ctp_profile

        ctp = overlay_ctp_profile(trade.get("ctp") or {}, account=cfg.get("account"))
        missing = []
        if not str(ctp.get("td_front") or "").strip():
            missing.append("交易前置")
        if not str(ctp.get("broker_id") or "").strip():
            missing.append("BrokerID")
        if not str(ctp.get("user_id") or "").strip():
            missing.append("资金账号")
        if not str(ctp.get("resolved_auth_code") or "").strip():
            missing.append("认证码")
        kind_txt = ctp_kind_label(ctp.get("account_kind"))
        if missing:
            report.add("执行通道", False, f"CTP {kind_txt}还缺：" + "、".join(missing) + "。填好前不要切换。")
        else:
            report.add("执行通道", True, f"executor=ctp（{kind_txt}，连接华泰柜台报单）")
    else:
        report.add("执行通道", False, f"executor={executor}（不认识的执行通道）")

    if executor == "gui" and dry:
        report.add(
            "模拟开关",
            False,
            "dry_run=true：只会监测，不会真实点击。挂机真下单请关掉模拟。",
        )
    elif dry:
        report.add("模拟开关", True, "dry_run=true（安全，不点单）", "warn")
    else:
        report.add("模拟开关", True, "dry_run=false（允许真实点击）")

    need_keys = ["price_input", "volume_input", "buy_open_btn", "sell_open_btn", "close_btn"]
    missing = [k for k in need_keys if not gui.get(k) or len(gui.get(k) or []) < 2]
    if missing:
        report.add("下单坐标", False, f"缺少标定: {', '.join(missing)}，请运行标定坐标")
    else:
        pts = ", ".join(f"{k}={tuple(gui[k][:2])}" for k in need_keys)
        report.add("下单坐标", True, pts)
        # 若存在 gui_coords.yaml 且与主配置不一致，提醒以标定文件为准
        try:
            import yaml

            root = Path(cfg.get("_project_root") or Path(__file__).resolve().parents[2])
            alt = root / "config" / "gui_coords.yaml"
            if alt.exists():
                with alt.open("r", encoding="utf-8") as f:
                    alt_gui = ((yaml.safe_load(f) or {}).get("trade") or {}).get("gui") or {}
                status = describe_coord_mismatch(gui, alt_gui)
                if status is not None:
                    level, detail = status
                    report.add("坐标一致性", level != "fail", detail, level)
        except Exception:  # noqa: BLE001
            pass

    confirm_times = int(gui.get("confirm_times", 0) or 0)
    if confirm_times <= 0:
        report.add(
            "确认框",
            True,
            "点完买开/卖空/平仓即结束，不按回车（期货通无确认弹窗；能成交就成交，否则进委托单）",
        )
    elif gui.get("confirm_btn"):
        report.add("确认框坐标", True, f"已标定 confirm_btn={tuple(gui['confirm_btn'][:2])}")
    else:
        report.add(
            "确认框坐标",
            True,
            f"未标定确定按钮，将依赖回车关闭弹窗（confirm_mode={gui.get('confirm_mode', 'both')}）",
            "warn",
        )

    symbols = st.get("symbols") or [trade.get("symbol")]
    report.add(
        "策略",
        True,
        f"engine={st.get('engine')} 周期={period_label(str(st.get('timeframe', '15')))}/"
        f"{period_label(str(st.get('trail_timeframe', '30')))} 品种={symbols}",
    )

    mode = str(risk.get("sizing_mode", "fixed"))
    report.add(
        "资金管理",
        True,
        f"mode={mode} 权益={risk.get('capital')} 每笔风险%={risk.get('risk_per_trade_pct')} "
        f"仓位上限%={risk.get('margin_usage_pct', 80)} 日最大亏损={risk.get('max_daily_loss')}",
    )

    path = str(app.get("path", ""))
    if path and Path(path).exists():
        report.add("软件路径", True, path)
    elif path:
        report.add("软件路径", True, f"路径不存在（若软件已打开可忽略）: {path}", "warn")
    else:
        report.add("软件路径", True, "未配置 app.path（软件已打开时可忽略）", "warn")

    acc = cfg.get("account") or {}
    if bool(acc.get("skip_auto_login", True)):
        report.add(
            "登录方式",
            True,
            "自动登录是关的。请先手动登录同花顺，能看到下单面板后再挂机",
        )
    else:
        pwd = str(acc.get("resolved_password", "") or "")
        if not pwd:
            report.add("登录方式", False, "自动登录已打开，但密码是空的。请在配置页填写密码，或关掉自动登录")
        else:
            report.add("登录方式", True, "启动时会打开交易登录框，填入密码并点登录")

    for item in report.items:
        # 用 ASCII 标记，避免 Windows 控制台 GBK 无法打印 ✓✗
        mark = {"pass": "OK", "warn": "!!", "fail": "XX"}.get(item.level, "??")
        _log(f"  [{mark}] {item.name}: {item.detail}", log)


def check_window(cfg: dict[str, Any], report: PreflightReport, log: LogFn | None = None) -> bool:
    """检查能否找到并激活同花顺窗口。"""
    from src.login import AppLoginManager

    # 临时关闭 dry_run，否则 focus 会被跳过
    cfg2 = dict(cfg)
    cfg2["dry_run"] = False
    mgr = AppLoginManager(cfg2)
    running = mgr.is_running()
    if running:
        report.add("软件进程", True, "检测到期货通相关进程在运行")
    else:
        report.add("软件进程", True, "未检测到配置的 exe 进程（窗口标题仍可能找得到）", "warn")

    ok = mgr.focus_window()
    if ok:
        report.add("窗口激活", True, "已尝试置顶期货通窗口")
        _log("  [OK] 窗口激活成功", log)
    else:
        report.add("窗口激活", False, "找不到期货通窗口，请先打开并登录同花顺")
        _log("  [XX] 窗口激活失败", log)
    return ok


def preview_coords(cfg: dict[str, Any], report: PreflightReport, log: LogFn | None = None) -> None:
    """鼠标移到各标定坐标（不点击），方便肉眼确认。"""
    import pyautogui

    # 预览也会划过屏幕，四角急停开着会自己把自己掐掉。
    pyautogui.FAILSAFE = False
    gui = (cfg.get("trade") or {}).get("gui") or {}
    keys = [
        ("price_input", "价格框"),
        ("volume_input", "手数框"),
        ("buy_open_btn", "买开"),
        ("sell_open_btn", "卖开"),
        ("close_btn", "平仓"),
    ]
    if gui.get("confirm_btn"):
        keys.append(("confirm_btn", "确定弹窗"))

    _log("=== 坐标预览（只移动鼠标，不点击）===", log)
    bad = False
    for key, label in keys:
        pt = gui.get(key)
        if not pt or len(pt) < 2:
            continue
        x, y = int(pt[0]), int(pt[1])
        _log(f"  移向 {label} ({x},{y})", log)
        try:
            pyautogui.moveTo(x, y, duration=0.45)
            time.sleep(0.55)
        except Exception as e:  # noqa: BLE001
            bad = True
            _log(f"  移动失败: {e}", log)
    if bad:
        report.add("坐标预览", False, "鼠标移动过程出错")
    else:
        report.add("坐标预览", True, "已依次移到各按钮位置，请确认停在正确控件上")


def check_data_feed(cfg: dict[str, Any], report: PreflightReport, log: LogFn | None = None) -> None:
    """试拉一根K线，确认行情数据可用。"""
    try:
        from src.data import KlineProvider

        sym = str((cfg.get("trade") or {}).get("symbol", "bu"))
        tf = str((cfg.get("strategy") or {}).get("timeframe", "15"))
        df = KlineProvider(cfg).fetch(sym, period=tf)
        n = len(df) if df is not None else 0
        if n >= 30:
            report.add("行情数据", True, f"{sym} {tf}m 拉到 {n} 根K线")
            _log(f"  [OK] 行情数据 OK ({n} bars)", log)
        else:
            report.add("行情数据", False, f"{sym} K线不足 ({n})，策略可能无法开仓")
            _log(f"  [XX] K线不足: {n}", log)
    except Exception as e:  # noqa: BLE001
        report.add("行情数据", False, f"拉取失败: {e}")
        _log(f"  [XX] 行情失败: {e}", log)


def click_open_close_test(cfg: dict[str, Any], report: PreflightReport, log: LogFn | None = None) -> None:
    """
    真实点击：买开1手 → 等确认 → 平仓。
    注意：会改动模拟/实盘账户持仓；挂机前建议在模拟账户做。
    """
    import pyautogui

    from src.trader.gui_trader import GuiTrader

    pyautogui.FAILSAFE = False

    cfg2 = dict(cfg)
    cfg2["dry_run"] = False
    trade = cfg2.setdefault("trade", {})
    trade["executor"] = "gui"

    _log("=== 点击实测：5秒后买开1手，再平仓 ===", log)
    _log("点同花顺时请不要自己动鼠标；要停扫描请用运行页「停止」", log)
    for i in range(5, 0, -1):
        _log(f"  {i}…", log)
        time.sleep(1)

    trader = GuiTrader(cfg2)
    sym = str(trade.get("symbol", "bu"))
    r1 = trader.buy_open(sym, 1, price=None)
    _log(f"  买开: ok={r1.ok} {r1.message}", log)
    time.sleep(2.5)
    r2 = trader.close_position(sym, 1, price=None)
    _log(f"  平仓: ok={r2.ok} {r2.message}", log)

    report.click_tested = True
    if r1.ok and r2.ok:
        report.add(
            "点击开平实测",
            True,
            "程序已发出买开+平仓点击；请到同花顺核对委托/持仓是否正确",
        )
        report.notes.append("请人工确认：价格框应是对手价；若有挂单请检查撤单。")
    else:
        report.add(
            "点击开平实测",
            False,
            f"买开ok={r1.ok} 平仓ok={r2.ok}，请重标定或检查弹窗",
        )


def run_preflight(
    cfg: dict[str, Any],
    *,
    do_click_test: bool = False,
    do_preview: bool = True,
    log: LogFn | None = None,
) -> PreflightReport:
    """
    执行完整自检。

    参数:
        do_click_test: 是否真实点买开/平仓
        do_preview: 是否鼠标预览坐标
    """
    report = PreflightReport(ok=True, started_at=datetime.now().isoformat(timespec="seconds"))
    _log("=" * 52, log)
    _log("挂机前自检开始", log)
    _log("=" * 52, log)

    _log("【1/4】配置检查", log)
    check_config(cfg, report, log)

    _log("【2/4】行情检查", log)
    check_data_feed(cfg, report, log)

    _log("【3/4】窗口与坐标", log)
    focused = check_window(cfg, report, log)
    if focused and do_preview:
        preview_coords(cfg, report, log)

    if do_click_test:
        if not focused:
            report.add("点击开平实测", False, "窗口未激活，跳过点击实测")
        else:
            _log("【4/4】点击开平实测", log)
            try:
                click_open_close_test(cfg, report, log)
            except Exception as e:  # noqa: BLE001
                report.add("点击开平实测", False, str(e))
                logger.exception("点击实测失败")
    else:
        report.add("点击开平实测", True, "本次未做真实点击（仅配置/预览）", "warn")
        report.notes.append("挂机真下单前建议加一次点击实测：选「含开平点击」。")

    # 汇总
    fails = [i for i in report.items if i.level == "fail"]
    warns = [i for i in report.items if i.level == "warn"]
    report.ok = len(fails) == 0
    _log("=" * 52, log)
    if report.ok:
        _log("自检通过，可以挂机" if not warns else f"自检通过，有 {len(warns)} 条提醒", log)
    else:
        _log(f"自检没通过，有 {len(fails)} 项要先处理", log)
    for item in fails:
        _log(f"没通过 {item.name}：{item.detail}", log)
    for item in warns:
        _log(f"提醒 {item.name}：{item.detail}", log)
    for n in report.notes:
        _log(f"备注: {n}", log)
    _log("=" * 52, log)

    # 落盘报告
    root = Path(cfg.get("_project_root") or Path(__file__).resolve().parents[2])
    out = root / "data" / "preflight_last.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": report.ok,
        "started_at": report.started_at,
        "click_tested": report.click_tested,
        "notes": report.notes,
        "items": [asdict(i) for i in report.items],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"报告已保存: {out}", log)
    return report
