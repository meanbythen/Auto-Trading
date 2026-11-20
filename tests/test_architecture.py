"""架构约束回归：依赖方向、模块环、界面不得直改运行对象。

这些规则由 import-linter 与人工评审共同保证，这里把最容易回退的几处钉住：
- 领域/内核不依赖业务，业务不依赖调度与界面，调度不依赖界面（见 pyproject.toml）；
- 全仓只允许两组已知的设计级循环；
- 界面层不得直接写 bot 的运行字段，也不得调它的私有方法。
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

from src.scheduler.runner import TradingBot

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 已知豁免：都是函数内延迟导入掩盖的历史反向依赖，R9 已登记，R10 再拆。
ALLOWED_CYCLES = {
    frozenset(
        {
            "src.backtest.param_history",
            "src.export.cta_decay",
            "src.strategy.engine_labels",
            "src.strategy.factory",
            "src.strategy.multi_factor",
            "src.strategy.rule_dsl",
            "src.trader.ctp_fronts",
            "src.trader.ctp_gateway",
            "src.trader.ctp_logic",
            "src.trader.ctp_md",
            "src.trader.ctp_profile",
            "src.trader.ctp_td",
            "src.utils.config_loader",
        }
    ),
    # flatten_manager 的时段判断转调 runner，是为了兼容测试对 runner.in_trading_session 的替换。
    frozenset({"src.scheduler.flatten_manager", "src.scheduler.runner"}),
}

BOT_WRITE_FIELDS = {"cfg", "sizer", "scan_interval", "strategy", "risk"}


def _modules() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in SRC.rglob("*.py"):
        if path.name == "__init__.py":
            rel = path.relative_to(SRC).parent
            name = "src" if str(rel) == "." else "src." + ".".join(rel.parts)
        else:
            name = "src." + ".".join(path.relative_to(SRC).with_suffix("").parts)
        found[name] = path
    return found


def _is_type_checking(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and any(
        isinstance(test, ast.Name) and test.id == "TYPE_CHECKING" for test in ast.walk(node.test)
    )


def _import_edges(module: str, path: Path, names: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    edges: set[str] = set()

    def add(target: str) -> None:
        if target in names and target != module:
            edges.add(target)

    def walk(node: ast.AST) -> None:
        if _is_type_checking(node):
            return
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")
                if path.name != "__init__.py":
                    base = base[:-1]
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ((node.module or "").split(".") if node.module else []))
            else:
                target = node.module or ""
            if target in names:
                for alias in node.names:
                    for candidate in (f"{target}.{alias.name}", target):
                        if candidate in names:
                            add(candidate)
                            break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return edges


def _strongly_connected(graph: dict[str, set[str]], names: set[str]) -> list[set[str]]:
    sys.setrecursionlimit(10000)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[set[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        for nxt in graph.get(node, set()):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif on_stack.get(nxt):
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            comp: set[str] = set()
            while True:
                item = stack.pop()
                on_stack[item] = False
                comp.add(item)
                if item == node:
                    break
            result.append(comp)

    for name in sorted(names):
        if name not in index:
            visit(name)
    return result


def test_only_documented_import_cycles_remain() -> None:
    modules = _modules()
    names = set(modules)
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        graph[module] = _import_edges(module, path, names)

    actual = {frozenset(comp) for comp in _strongly_connected(graph, names) if len(comp) > 1}
    unexpected = actual - ALLOWED_CYCLES
    assert not unexpected, f"出现新的模块循环: {sorted(sorted(c) for c in unexpected)}"


def _assignment_targets(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            for sub in ast.walk(target):
                ids.add(id(sub))
    return ids


def _runtime_write_hits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets = _assignment_targets(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or id(node) not in targets:
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id == "bot" and node.attr in BOT_WRITE_FIELDS:
            hits.append(f"{path.name}:{node.lineno} 直接写 bot.{node.attr}")
        elif (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "bot"
            and value.attr in BOT_WRITE_FIELDS
        ):
            hits.append(f"{path.name}:{node.lineno} 直接写 bot.{value.attr}.{node.attr}")
    return hits


def test_ui_has_no_direct_runtime_writes() -> None:
    problems: list[str] = []
    for path in (SRC / "ui").rglob("*.py"):
        problems.extend(_runtime_write_hits(path))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "bot"
                and node.func.attr.startswith("_")
            ):
                problems.append(f"{path.name}:{node.lineno} 直接调 bot.{node.func.attr}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "positions"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "strategy"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "bot"
            ):
                problems.append(f"{path.name}:{node.lineno} 直接读 bot.strategy.positions")
    assert not problems, "\n".join(problems)


def test_runtime_commands_exist() -> None:
    for name in ("apply_runtime_config", "positions_snapshot", "note_force_close"):
        assert callable(getattr(TradingBot, name, None)), name
    assert isinstance(getattr(TradingBot, "orders_paused", None), property)
