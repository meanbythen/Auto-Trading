"""批次3 Task 4：研究治理 UI 页签。

覆盖：
- 静态断言：_RESEARCH_TABS 含「研究治理」、原有 5 个顺序不变；Mixin 有构建与动作方法。
- 运行时冒烟（需要可用显示；无显示时 conftest 会 skip）：页签能构建、跳页路由正确。
- monkeypatch 验证：登记假设 / 查询追踪链的调用落到 ``src.research.governance``。
- 再认证 / 容量：调用落到 ``src.review.certification`` 与容量模块，后台线程结果回到界面。
- 治理函数冒烟（不依赖 UI）：import 与基本读写可用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.research import governance as gov
from src.ui import governance_tab
from src.ui import research_hub
from src.ui import theme as T
from src.ui.governance_tab import GovernanceTabMixin
from src.ui.research_hub import ResearchHubMixin

ORIGINAL_TABS = ["历史回测", "交叉测试", "策略优化", "复盘", "因子挖掘"]


def test_governance_tab_registered_in_research_tabs() -> None:
    tabs = ResearchHubMixin._RESEARCH_TABS
    assert "研究治理" in tabs
    # 原有 5 个保持不变、顺序在前，研究治理追加为第 6 个
    assert list(tabs[:5]) == ORIGINAL_TABS
    assert tabs[-1] == "研究治理"
    assert len(tabs) == 6
    # _open_tab 的 set 判断仍可用：名字在元组里
    assert "研究治理" in set(tabs)


def test_governance_mixin_exposes_build_and_actions() -> None:
    assert hasattr(ResearchHubMixin, "_build_research_hub")
    assert hasattr(GovernanceTabMixin, "_build_governance_tab")
    assert callable(getattr(GovernanceTabMixin, "_build_governance_tab"))
    for name in (
        "_gov_register_hypothesis",
        "_gov_query_trace",
        "_gov_refresh_all",
        "_gov_refresh_hypotheses",
        "_gov_refresh_decisions",
        "_gov_refresh_changes",
        "_gov_refresh_failed",
        "_gov_run_certification",
        "_gov_run_capacity",
        "_gov_collect_capacity",
        "_gov_capacity_done",
        "_gov_capacity_failed",
    ):
        assert hasattr(GovernanceTabMixin, name)


def test_ui_modules_import_without_display() -> None:
    """导入界面模块不需要已存在的窗口；能导入即说明没有语法 / 顶层错误。"""
    import importlib

    importlib.reload(research_hub)
    importlib.reload(governance_tab)


def test_governance_functions_smoke(tmp_path: Path) -> None:
    """不依赖 UI：治理函数可正常 import 与读写，UI 层只做转发。"""
    gov.ensure_policy(tmp_path)
    hyp = gov.register_hypothesis(
        tmp_path, title="冒烟假设", statement="s", symbol="rb", period="15m"
    )
    assert hyp["id"].startswith("H-")
    assert gov.list_hypotheses(tmp_path)

    trace = gov.trace_strategy(tmp_path, "daily_body", "rb")
    lines = gov.format_trace_lines(trace)
    assert isinstance(lines, list) and lines
    frozen_flag, _frozen_msg = gov.is_frozen(tmp_path, "daily_body", "rb")
    assert isinstance(frozen_flag, bool)


# ------------------------------ 运行时构建（需要显示） ------------------------------


def test_governance_subtab_is_built_at_startup(real_app) -> None:
    tab = real_app.research_tabs.tab("研究治理")
    assert tab.winfo_children(), "研究治理页签没有构建出任何子控件"
    # 各区域文本框都已创建
    for attr in (
        "gov_hyp_box",
        "gov_trace_box",
        "gov_decisions_box",
        "gov_changes_box",
        "gov_failed_box",
        "gov_cert_box",
        "gov_cap_box",
        "gov_cert_status",
        "gov_cap_status",
    ):
        assert getattr(real_app, attr) is not None
    # 容量默认参数已填好，避免空白输入直接报错
    assert real_app.gov_cap_participation_var.get() == "1.0"
    assert real_app.gov_cap_lookback_var.get() == "20"
    # 策略 / 品种默认值已从当前上下文填入
    assert real_app.gov_trace_strategy_var.get()


def test_open_tab_routes_to_governance(real_app) -> None:
    real_app._open_tab("研究治理")
    assert real_app.tabs.get() == "策略研究"
    assert real_app.research_tabs.get() == "研究治理"
    real_app.update_idletasks()


def test_register_hypothesis_dispatches_to_governance(real_app, monkeypatch) -> None:
    calls: list[dict] = []

    def fake_register(root, *, title, statement, symbol, period, param_ranges, acceptance):
        calls.append(
            {
                "root": root,
                "title": title,
                "statement": statement,
                "symbol": symbol,
                "period": period,
                "param_ranges": param_ranges,
                "acceptance": acceptance,
            }
        )
        return {"id": "H-20260101-999"}

    monkeypatch.setattr(gov, "register_hypothesis", fake_register)
    monkeypatch.setattr(gov, "list_hypotheses", lambda root: [])

    real_app.gov_hyp_title_var.set("测试假设")
    real_app.gov_hyp_statement_var.set("一段陈述")
    real_app.gov_hyp_symbol_var.set("rb2605")
    real_app.gov_hyp_period_var.set("15m")
    real_app.gov_hyp_param_var.set('{"fast":[5,20]}')
    real_app.gov_hyp_accept_var.set("夏普>1")

    real_app._gov_register_hypothesis()

    assert len(calls) == 1
    payload = calls[0]
    assert payload["title"] == "测试假设"
    assert payload["symbol"] == "rb2605"
    assert payload["period"] == "15m"
    assert payload["param_ranges"] == {"fast": [5, 20]}
    assert payload["acceptance"] == "夏普>1"


def test_query_trace_dispatches_to_governance_and_shows_freeze(real_app, monkeypatch) -> None:
    trace_calls: list[tuple] = []

    def fake_trace(root, strategy, symbol):
        trace_calls.append((strategy, symbol))
        return {"hypothesis": "未记录", "frozen": (True, "冻结提示")}

    monkeypatch.setattr(gov, "trace_strategy", fake_trace)
    monkeypatch.setattr(gov, "format_trace_lines", lambda trace: ["假设：未记录", "冻结：是"])
    monkeypatch.setattr(gov, "is_frozen", lambda root, strategy, symbol: (True, "处于变更冻结期"))

    real_app.gov_trace_strategy_var.set("daily_body")
    real_app.gov_trace_symbol_var.set("rb2605")
    real_app._gov_query_trace()

    assert trace_calls == [("daily_body", "rb2605")]
    # 冻结时给醒目提示色
    assert real_app.gov_trace_frozen_label.cget("text_color") == T.DANGER


def test_query_trace_handles_governance_errors_without_crash(real_app, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("治理层故意报错")

    monkeypatch.setattr(gov, "trace_strategy", boom)
    real_app.gov_trace_strategy_var.set("daily_body")
    real_app.gov_trace_symbol_var.set("rb2605")
    # 不应抛异常
    real_app._gov_query_trace()
    assert "失败" in real_app.gov_trace_box.get("1.0", "end")


class _FakeWidget:
    """最小控件替身：只实现 configure / cget / get / set，测试不依赖真实窗口。"""

    def __init__(self, **state):
        self._state = dict(state)

    def configure(self, **kwargs):
        self._state.update(kwargs)

    def cget(self, key):
        return self._state.get(key)

    def get(self, *_args):
        return self._state.get("value", "")

    def set(self, value):
        self._state["value"] = value


class _FakeBox(_FakeWidget):
    """只读文本框替身：get 返回当前正文。"""

    def __init__(self):
        super().__init__(text="")

    def get(self, *_args):
        return self._state.get("text", "")


class _GovStub(GovernanceTabMixin):
    """不建窗口，只验证治理页的转发逻辑与结果回填。"""

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self._watch_symbols = list(((cfg.get("strategy") or {}).get("symbols") or []))
        self.gov_cert_box = _FakeBox()
        self.gov_cert_status = _FakeWidget(text="")
        self.gov_cap_box = _FakeBox()
        self.gov_cap_status = _FakeWidget(text="")
        self.gov_cap_button = _FakeWidget(state="normal")
        self.gov_cap_participation_var = _FakeWidget(value="1.0")
        self.gov_cap_lookback_var = _FakeWidget(value="20")
        self._gov_cap_running = False

    def _gov_clone_cfg(self) -> dict:
        return self._cfg

    def _gov_root(self):
        return Path(".")

    def _set_readonly_box(self, box, text: str) -> None:
        box.configure(text=text)

    def _append_log(self, _text: str) -> None:
        pass

    def _post_to_main(self, callback) -> None:
        callback()


class _ImmediateThread:
    """测试里同步执行目标函数，避免后台线程与窗口事件循环互相等待。"""

    def __init__(self, *, target, daemon=False, **kwargs):
        self.target = target

    def start(self) -> None:
        self.target()


def test_certification_dispatches_to_review_module(monkeypatch) -> None:
    """再认证走模块函数，界面只负责显示；「只看不写」要把 write=False 传下去。"""
    from src.review import certification as cert

    calls: list[bool] = []

    def fake_run(cfg, *, root=None, write=True, now=None):
        calls.append(write)
        return {
            "status": "需要再认证",
            "fingerprint": "abc123",
            "checked_at": "2026-11-22 10:00:00",
            "changes": ["risk.capital: 80000 → 83000"],
            "issues": [],
            "errors": 0,
            "warnings": 0,
        }

    monkeypatch.setattr(cert, "run_certification", fake_run)
    app = _GovStub({"strategy": {"symbols": []}})

    app._gov_run_certification(write=False)

    assert calls == [False]
    text = app.gov_cert_box.get("1.0", "end")
    assert "需要再认证" in text
    assert "risk.capital" in text


def test_certification_reports_errors_in_status(monkeypatch) -> None:
    from src.review import certification as cert

    monkeypatch.setattr(
        cert,
        "run_certification",
        lambda cfg, **kwargs: {
            "status": "首次认证",
            "fingerprint": "abc123",
            "checked_at": "2026-11-22 10:00:00",
            "changes": [],
            "issues": [{"symbol": "lh2701", "field": "margin_rate_pct", "severity": "error", "message": "保证金比例没填"}],
            "errors": 1,
            "warnings": 0,
        },
    )
    app = _GovStub({})

    app._gov_run_certification(write=True)

    assert "1 项错误" in app.gov_cert_status.cget("text")
    assert app.gov_cert_status.cget("text_color") == T.DANGER


def test_certification_failure_does_not_crash(monkeypatch) -> None:
    from src.review import certification as cert

    def boom(*_args, **_kwargs):
        raise RuntimeError("快照目录不可写")

    monkeypatch.setattr(cert, "run_certification", boom)
    app = _GovStub({})

    app._gov_run_certification(write=True)

    assert "失败" in app.gov_cert_box.get("1.0", "end")


def test_capacity_worker_estimates_with_fetched_bars(monkeypatch) -> None:
    """取 K 线成功时按成交量和参与率给出最大手数；乘数与保证金比例从合约表取。"""
    from src.data import kline as kline_mod

    rows = [{"close": 3000.0, "volume": 1000.0} for _ in range(30)]
    monkeypatch.setattr(kline_mod.KlineProvider, "fetch", lambda self, symbol, **kwargs: rows)
    cfg = {
        "strategy": {"symbols": ["lh2701"], "timeframe": "15"},
        "risk": {"contracts": {"lh": {"multiplier": 16, "margin_rate_pct": 12}}},
    }
    app = _GovStub(cfg)

    lines, ok, total = app._gov_collect_capacity(cfg, ["lh2701"], 0.01, 20)

    assert (ok, total) == (1, 1)
    assert "建议最多 10 手" in lines[0]
    assert "1%" in lines[0]


def test_capacity_worker_keeps_going_when_one_symbol_fails(monkeypatch) -> None:
    from src.data import kline as kline_mod

    rows = [{"close": 3000.0, "volume": 1000.0} for _ in range(30)]

    def fake_fetch(self, symbol, **kwargs):
        if symbol == "fu2701":
            raise RuntimeError("取数超时")
        return rows

    monkeypatch.setattr(kline_mod.KlineProvider, "fetch", fake_fetch)
    cfg = {
        "strategy": {"symbols": ["lh2701", "fu2701"]},
        "risk": {"contracts": {"lh": {"multiplier": 16, "margin_rate_pct": 12}}},
    }
    app = _GovStub(cfg)

    lines, ok, total = app._gov_collect_capacity(cfg, ["lh2701", "fu2701"], 0.01, 20)

    assert (ok, total) == (1, 2)
    assert any("取 K 线失败" in line for line in lines)


def test_capacity_run_posts_result_back_to_ui(monkeypatch) -> None:
    """后台线程算完后必须回到主线程刷新结果区，不能在子线程直接改控件。"""
    import threading

    cfg = {"strategy": {"symbols": ["lh2701"]}}
    app = _GovStub(cfg)
    monkeypatch.setattr(
        app,
        "_gov_collect_capacity",
        lambda cfg, symbols, participation, lookback: (["lh2701 容量     建议最多 8 手"], 1, 1),
    )
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    app._gov_run_capacity()

    assert "建议最多 8 手" in app.gov_cap_box.get("1.0", "end")
    assert "1/1" in app.gov_cap_status.cget("text")
    assert app.gov_cap_button.cget("state") == "normal"


def test_capacity_run_rejects_bad_numbers() -> None:
    app = _GovStub({"strategy": {"symbols": ["lh2701"]}})

    app.gov_cap_participation_var.set("abc")
    app._gov_run_capacity()
    assert "数字" in app.gov_cap_box.get("1.0", "end")

    app.gov_cap_participation_var.set("150")
    app._gov_run_capacity()
    assert "0～100" in app.gov_cap_box.get("1.0", "end")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
