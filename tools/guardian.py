"""
冷备守护（可选，默认关闭）

用法:
    python tools/guardian.py            # 常驻，默认读 config/config.yaml
    python tools/guardian.py --once     # 只检查一次，配合人工确认环境
    python tools/guardian.py --force    # 临时忽略 runtime.guardian.enabled

功能：主程序进程消失后，在交易时段内把持仓平掉，避免「人不在、程序也没了、仓还在」。
原理：每 poll_sec 读一次 data/bot_state.json。只有「有仓 + 心跳过期 + 主进程 PID 不存在
     + 在交易时段 + CTP 配置齐全」同时满足才独立连柜台平仓；PID 还活着只告警，
     绝不连接（同账号会把主进程踢下线）。
边界：整机断电、系统休眠、网络中断时本进程同样不在，救不了；那些场景只能靠券商侧条件单。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.state_store import default_state_path  # noqa: E402

ACTIONS_FILE = "guardian_actions.jsonl"


@dataclass(frozen=True)
class GuardianConfig:
    """runtime.guardian 段。"""

    enabled: bool = False
    poll_sec: float = 10.0
    stale_after_sec: float = 180.0
    max_close_attempts: int = 3

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any] | None) -> GuardianConfig:
        raw = ((cfg or {}).get("runtime") or {}).get("guardian") or {}
        if not isinstance(raw, dict):
            return cls()

        def _num(key: str, default: float, low: float) -> float:
            try:
                return max(float(raw.get(key, default)), low)
            except (TypeError, ValueError):
                return default

        try:
            attempts = max(int(raw.get("max_close_attempts", 3)), 1)
        except (TypeError, ValueError):
            attempts = 3
        flag = raw.get("enabled", False)
        if isinstance(flag, str):
            flag = flag.strip().lower() in ("1", "true", "yes", "on")
        return cls(
            enabled=bool(flag),
            poll_sec=_num("poll_sec", 10.0, 1.0),
            stale_after_sec=_num("stale_after_sec", 180.0, 30.0),
            max_close_attempts=attempts,
        )


def pid_alive(pid: int) -> bool:
    """进程还在不在。Windows 用 OpenProcess，其它平台用 kill 0。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            # 打不开退出码时按「还活着」处理，宁可不动作。
            return True
        return int(code.value) == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _parse_stamp(text: Any) -> datetime | None:
    """心跳时间可能是 ISO（带 T）或空格分隔，两种都认。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _open_positions(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """程序账上还有仓的品种。"""
    out: dict[str, dict[str, Any]] = {}
    for symbol, raw in (state.get("positions") or {}).items():
        if not isinstance(raw, dict):
            continue
        side = str(raw.get("side") or "flat")
        try:
            volume = int(raw.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0
        if side in ("long", "short") and volume > 0:
            out[str(symbol)] = raw
    return out


class Guardian:
    """读状态、判断该不该接管、必要时平仓。依赖都可注入，方便测试。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        root: Path | str | None = None,
        *,
        pid_alive_fn: Callable[[int], bool] | None = None,
        flatten_fn: Callable[[list[str]], dict[str, str]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        alert_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = Path(root or cfg.get("_project_root", "."))
        self.config = GuardianConfig.from_cfg(cfg)
        self.state_path = default_state_path(self.root)
        self.actions_path = self.root / "data" / ACTIONS_FILE
        self._pid_alive = pid_alive_fn or pid_alive
        self._flatten = flatten_fn or self._ctp_flatten
        self._now = now_fn or datetime.now
        self._alert = alert_fn or self._emit_alert
        # 平过的 run_id 落盘；只告警过的只留在内存，主进程真的死了还要能接管。
        self._handled = self._load_handled()
        self._alerted: set[str] = set()

    # ---------- 主判断 ----------

    def check_once(self) -> str:
        """跑一轮。返回本轮结论，方便 --once 和日志排查。"""
        if not self.config.enabled:
            return "disabled"
        state = self._read_state()
        if state is None:
            return "no_state"
        positions = _open_positions(state)
        if not positions:
            return "flat"
        now = self._now()
        age = self._heartbeat_age(state, now)
        if age is None or age <= self.config.stale_after_sec:
            return "fresh"
        run_id = str(state.get("run_id") or "") or self._fallback_key(state)
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and self._pid_alive(pid):
            # 主进程还活着，只是卡住了。连上去会把主进程踢下线，只能告警。
            if run_id not in self._alerted:
                self._alerted.add(run_id)
                self._alert(
                    "guardian_stale_main",
                    f"主进程 PID {pid} 仍在但心跳已停 {age:.0f} 秒，"
                    f"守护不连接柜台；持仓 {self._positions_text(positions)}",
                )
            return "stale_main"
        symbols = list(positions)
        if not self._in_session(symbols, now):
            return "off_session"
        if not self._ctp_ready():
            return "no_ctp"
        if run_id in self._handled:
            return "already_handled"

        results = self._flatten(symbols)
        self._write_action(state, run_id, pid, positions, results)
        self._handled.add(run_id)
        self._alert(
            "guardian_flatten",
            f"主进程 PID {pid or '未知'} 已退出、心跳停 {age:.0f} 秒，"
            f"守护已尝试平仓：{json.dumps(results, ensure_ascii=False)}",
        )
        return "flattened"

    # ---------- 读状态 ----------

    def _read_state(self) -> dict[str, Any] | None:
        path = self.state_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("守护读状态文件失败: {}", exc)
            return None
        return data if isinstance(data, dict) else None

    def _heartbeat_age(self, state: dict[str, Any], now: datetime) -> float | None:
        stamp = _parse_stamp(state.get("heartbeat_at"))
        if stamp is None:
            return None
        return max((now - stamp).total_seconds(), 0.0)

    @staticmethod
    def _fallback_key(state: dict[str, Any]) -> str:
        return f"{state.get('pid') or 0}|{state.get('heartbeat_at') or ''}"

    @staticmethod
    def _positions_text(positions: dict[str, dict[str, Any]]) -> str:
        return "、".join(
            f"{symbol} {'多' if str(raw.get('side')) == 'long' else '空'}{int(raw.get('volume') or 0)}"
            for symbol, raw in positions.items()
        )

    # ---------- 前置条件 ----------

    def _in_session(self, symbols: list[str], now: datetime) -> bool:
        """按每个品种自己的交易时段判断，任一在场就可以动手。"""
        from src.utils.session_clock import in_trading_sessions, sessions_for_symbol

        for symbol in symbols:
            try:
                if in_trading_sessions(sessions_for_symbol(self.cfg, symbol), now):
                    return True
            except Exception:  # noqa: BLE001
                logger.exception("守护判断交易时段失败: {}", symbol)
        return False

    def _ctp_ready(self) -> bool:
        try:
            from src.trader.ctp_gateway import CtpSettings

            return not CtpSettings.from_cfg(self.cfg).missing()
        except Exception:  # noqa: BLE001
            logger.exception("守护读取 CTP 配置失败")
            return False

    # ---------- 记录 ----------

    def _load_handled(self) -> set[str]:
        """守护自己重启后，不重复平同一个 run_id 的仓。"""
        handled: set[str] = set()
        if not self.actions_path.exists():
            return handled
        try:
            for line in self.actions_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and str(row.get("action") or "") == "flatten":
                    key = str(row.get("run_id") or "")
                    if key:
                        handled.add(key)
        except OSError:
            pass
        return handled

    def _write_action(
        self,
        state: dict[str, Any],
        run_id: str,
        pid: int,
        positions: dict[str, dict[str, Any]],
        results: dict[str, str],
    ) -> None:
        row = {
            "ts": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": "flatten",
            "run_id": run_id,
            "main_pid": pid,
            "heartbeat_at": str(state.get("heartbeat_at") or ""),
            "positions": positions,
            "results": results,
        }
        try:
            self.actions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.actions_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("写守护动作记录失败: {}", exc)

    def _emit_alert(self, code: str, message: str) -> None:
        try:
            from src.ops.alerts import emit_alert

            emit_alert(code, message, level="error", root=self.root)
        except Exception:  # noqa: BLE001
            logger.exception("守护写告警失败")

    # ---------- 真正平仓 ----------

    def _ctp_flatten(self, symbols: list[str]) -> dict[str, str]:
        """独立连柜台，按对手价把柜台上的持仓平掉。"""
        from src.trader import create_trader
        from src.trader.ctp_logic import symbol_key

        cfg = dict(self.cfg)
        cfg["dry_run"] = False
        cfg["trade"] = {**(self.cfg.get("trade") or {}), "executor": "ctp"}
        results: dict[str, str] = {}
        trader = create_trader(cfg)
        try:
            ok, message = trader.connect()
            if not ok:
                return {symbol: f"连接失败：{message}" for symbol in symbols}
            if not trader.gateway.refresh():
                return {symbol: "柜台持仓查询失败" for symbol in symbols}
            broker = {
                symbol_key(name): int(getattr(view, "net", 0) or 0)
                for name, view in trader.gateway.positions.items()
            }
            from src.data import KlineProvider

            prices = KlineProvider(cfg)
            for symbol in symbols:
                net = broker.get(symbol_key(symbol), 0)
                if net == 0:
                    results[symbol] = "柜台已无持仓"
                    continue
                price = prices.latest_price(symbol)
                if price <= 0:
                    results[symbol] = "取不到现价，未平仓"
                    continue
                last = ""
                for attempt in range(1, self.config.max_close_attempts + 1):
                    result = trader.close_position(symbol, abs(net), price)
                    last = f"第 {attempt} 次：{result.message}"
                    if result.ok:
                        break
                    time.sleep(1.0)
                results[symbol] = last
        except Exception as exc:  # noqa: BLE001
            logger.exception("守护平仓失败")
            return {symbol: f"平仓异常：{exc}" for symbol in symbols}
        finally:
            try:
                trader.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("守护释放交易接口失败")
        return results

    # ---------- 常驻 ----------

    def run_forever(self) -> None:
        logger.info(
            "冷备守护启动：轮询 {} 秒，心跳超过 {} 秒且主进程 PID 不存在才接管",
            self.config.poll_sec,
            self.config.stale_after_sec,
        )
        while True:
            try:
                outcome = self.check_once()
                if outcome not in ("fresh", "flat"):
                    logger.info("守护检查：{}", outcome)
            except KeyboardInterrupt:
                logger.info("冷备守护退出")
                return
            except Exception:  # noqa: BLE001
                logger.exception("守护检查异常，继续下一轮")
            time.sleep(self.config.poll_sec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冷备守护：主进程消失后平掉持仓")
    parser.add_argument("--config", default="", help="配置文件；默认 config/config.yaml")
    parser.add_argument("--once", action="store_true", help="只检查一次")
    parser.add_argument("--force", action="store_true", help="忽略 runtime.guardian.enabled")
    args = parser.parse_args(argv)

    from src.utils.config_loader import load_config

    cfg = load_config(args.config or None)
    cfg["_project_root"] = str(ROOT)
    from src.ops.notify import configure

    configure(cfg)
    guard = Guardian(cfg, ROOT)
    if not guard.config.enabled and not args.force:
        print("runtime.guardian.enabled=false，冷备守护未启动（加 --force 可临时运行）")
        return 0
    if args.once:
        print(f"守护检查结果：{guard.check_once()}")
        return 0
    guard.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
