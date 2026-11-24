"""testkit 纯函数：汇总解析、覆盖率行解析、目标判定与步骤编排。"""

from __future__ import annotations

from tools.testkit import (
    gate_steps,
    parse_baseline,
    parse_coverage_rows,
    parse_coverage_total,
    slug,
    statement_cover,
)

_BRANCH_REPORT = """\
Name                                  Stmts   Miss Branch BrPart  Cover   Missing
---------------------------------------------------------------------------------
src/scheduler/catch_up.py               161     38     42      8    73%   87-93, 102
src/scheduler/order_state.py            200      0     60      0   100%
TOTAL                                  3574    620    851     88    83%
"""

_PLAIN_REPORT = """\
Name              Stmts   Miss  Cover   Missing
-----------------------------------------------
src/account/x.py     10      2    80%   1-2
TOTAL                10      2    80%
"""


def test_parse_baseline_with_deselected() -> None:
    output = "2410/2415 tests collected (5 deselected) in 12.34s"
    assert parse_baseline(output) == (2410, 2415, 5)


def test_parse_baseline_without_deselected() -> None:
    assert parse_baseline("93 tests collected in 1.02s") == (93, 93, 0)


def test_parse_baseline_returns_none_for_unrelated_output() -> None:
    assert parse_baseline("no tests ran") is None


def test_parse_coverage_rows_with_branch_columns() -> None:
    rows = parse_coverage_rows(_BRANCH_REPORT, branch=True)
    assert [row.path for row in rows] == [
        "src/scheduler/catch_up.py",
        "src/scheduler/order_state.py",
    ]
    first = rows[0]
    assert (first.stmt, first.miss, first.branch, first.brpart, first.cover) == (161, 38, 42, 8, 73)
    assert first.missing == "87-93, 102"
    assert rows[1].missing == ""


def test_parse_coverage_rows_without_branch_columns() -> None:
    rows = parse_coverage_rows(_PLAIN_REPORT, branch=False)
    assert len(rows) == 1
    assert (rows[0].path, rows[0].stmt, rows[0].miss, rows[0].cover) == ("src/account/x.py", 10, 2, 80)
    assert rows[0].branch == 0


def test_parse_coverage_total_reads_total_row() -> None:
    total = parse_coverage_total(_BRANCH_REPORT, branch=True)
    assert total is not None
    assert (total.stmt, total.miss, total.cover) == (3574, 620, 83)
    assert total.statement_cover == 82.65


def test_statement_cover_handles_zero_statements() -> None:
    assert statement_cover(0, 0) == 100.0
    assert statement_cover(200, 0) == 100.0
    assert statement_cover(200, 50) == 75.0


def test_slug_normalizes_paths() -> None:
    assert slug("src/scheduler") == "src_scheduler"
    assert slug("src\\account") == "src_account"
    assert slug("src/a-b.c") == "src_a_b_c"


def test_gate_steps_order_without_coverage() -> None:
    names = [name for name, _ in gate_steps(with_coverage=False)]
    assert names == ["pytest", "ruff", "mypy", "lint-imports", "docs"]


def test_gate_steps_with_coverage_runs_coverage_report_after_pytest() -> None:
    steps = gate_steps(with_coverage=True)
    names = [name for name, _ in steps]
    assert names[:2] == ["pytest", "coverage"]
    pytest_cmd = steps[0][1]
    assert "coverage" in pytest_cmd and "run" in pytest_cmd and "--branch" in pytest_cmd
    assert steps[1][1][-2:] == ["report", "--show-missing"]
