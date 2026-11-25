"""
天勤实时 tick 常驻采集。

独立线程、独立 TqApi 连接，不跟 K 线拉取抢同一个事件循环。
订阅真实合约（品种代码先解析当前主力），去重后攒进内存缓冲，
定时/定量走 vnpy save_tick_data() 入库。断线按 5→10→30→60 秒退避重连，
心跳写到 data/ticks/recorder_status.json 供界面和命令行查看。

tick 入库失败只告警、保留缓冲，不向交易主循环抛异常。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from src.data.tick.vnpy_store import (
    VnpyUnavailable,
    format_tick_time,
    save_ticks,
    split_instrument,
    to_tick_data,
)
from src.data.tq_source import main_contract, tq_symbol
from src.utils import load_secrets
from src.utils.holidays import calendar_from_config
from src.utils.session_clock import in_trading_sessions, parse_hhmm, sessions_for_symbol
from src.utils.trading_day import futures_trading_day, is_trading_day

_BACKOFF = (5.0, 10.0, 30.0, 60.0)


class TickConfigError(RuntimeError):
    """配置写错了，重连也没用，应直接停采集。"""


def _watch_symbols(cfg: dict[str, Any]) -> list[str]:
    """配置页关注品种。tick.symbols 空时用这份，和说明书一致。"""
    strategy = cfg.get("strategy") or {}
    return [str(item).strip() for item in (strategy.get("symbols") or []) if str(item).strip()]


def tick_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """把 data.tick 配置收成实际要用的值和绝对路径。旧配置缺项也能跑。"""
    root = Path(cfg.get("_project_root", "."))
    data_cfg = cfg.get("data", {}) or {}
    tick_cfg = data_cfg.get("tick", {}) or {}
    tq_cfg = data_cfg.get("tq", {}) or {}
    secrets = load_secrets()
    symbols = [str(item).strip() for item in (tick_cfg.get("symbols") or []) if str(item).strip()]
    if not symbols:
        symbols = _watch_symbols(cfg)
    return {
        "root": root,
        "symbols": symbols,
        "resolve_main": bool(tick_cfg.get("resolve_main", True)),
        "enabled": bool(tick_cfg.get("enabled", False)),
        "session_only": bool(tick_cfg.get("session_only", True)),
        "open_lead_seconds": float(tick_cfg.get("open_lead_seconds", 30)),
        "close_grace_seconds": float(tick_cfg.get("close_grace_seconds", 60)),
        "idle_poll_seconds": max(5.0, float(tick_cfg.get("idle_poll_seconds", 30))),
        "auto_synth": bool(tick_cfg.get("auto_synth", True)),
        "synth_delay_seconds": max(0.0, float(tick_cfg.get("synth_delay_seconds", 120))),
        "retention_days": max(0, int(tick_cfg.get("retention_days", 30))),
        "pause_record_during_download": bool(
            tick_cfg.get("pause_record_during_download", True)
        ),
        "lock_file": root / str(tick_cfg.get("lock_file", "data/ticks/recorder.lock")),
        "synth_state_file": root
        / str(tick_cfg.get("synth_state_file", "data/ticks/synth_state.json")),
        "flush_seconds": float(tick_cfg.get("flush_seconds", 5)),
        "batch_size": int(tick_cfg.get("batch_size", 2000)),
        "database": root / str(tick_cfg.get("database", "data/ticks/vnpy_data.db")),
        "status_file": root / str(tick_cfg.get("status_file", "data/ticks/recorder_status.json")),
        "bar_store": root / str(tick_cfg.get("bar_store", "data/klines/tick_bars.sqlite")),
        "synth_periods": [str(item) for item in (tick_cfg.get("synth_periods") or ["1", "5", "15", "30", "60", "D"])],
        "raw_csv": bool(tick_cfg.get("raw_csv", False)),
        "reconnect_seconds": float(tick_cfg.get("reconnect_seconds", 5)),
        "max_backoff_seconds": float(tick_cfg.get("max_backoff_seconds", 60)),
        "user": str(tq_cfg.get("user") or secrets.get("tq_user") or os.environ.get("TQ_USER") or ""),
        "password": str(
            tq_cfg.get("password") or os.environ.get("TQ_PASSWORD") or secrets.get("tq_password") or ""
        ),
    }


def read_status(path: str | Path) -> dict[str, Any]:
    """读心跳文件。没跑过或文件坏了都返回空字典。"""
    file = Path(path)
    if not file.exists():
        return {}
    try:
        return dict(json.loads(file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _wait_update(client: Any) -> None:
    """最多等一秒，主循环才能及时响应停止。老版本 tqsdk 没有 deadline 参数。"""
    try:
        client.wait_update(deadline=time.time() + 1.0)
    except TypeError:
        client.wait_update()


_SELF_CREATED: str | None = None


def _process_created(pid: int) -> str | None:
    """进程创建时间（Windows 用 FILETIME 计数，Linux 用 /proc 第 22 列）。

    Windows 会回收复用 PID，只比对 PID 会把「已经退出的旧进程」误判成还在跑。
    把创建时间和 PID 一起记进锁里，才能确认锁的主人到底还是不是原来那个进程。
    """
    global _SELF_CREATED
    if pid <= 0:
        return None
    if pid == os.getpid() and _SELF_CREATED is not None:
        return _SELF_CREATED
    value: str | None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = getattr(ctypes, "windll").kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
                if not ok:
                    return None
                ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                value = str(ticks)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return None
    else:
        try:
            value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
        except (OSError, IndexError):
            return None
    if pid == os.getpid():
        _SELF_CREATED = value
    return value


class TickRecorder:
    """常驻采集器。start() 起线程，stop() 先落盘再关连接。"""

    def __init__(self, cfg: dict[str, Any], *, api_factory: Any | None = None) -> None:
        settings = tick_settings(cfg)
        self.cfg = cfg
        self.symbols_in = settings["symbols"]
        self.resolve_main = bool(settings["resolve_main"])
        self.session_only = bool(settings["session_only"])
        self.open_lead_seconds = float(settings["open_lead_seconds"])
        self.close_grace_seconds = float(settings["close_grace_seconds"])
        self.idle_poll_seconds = float(settings["idle_poll_seconds"])
        self.auto_synth = bool(settings["auto_synth"])
        self.synth_delay_seconds = float(settings["synth_delay_seconds"])
        self.retention_days = int(settings["retention_days"])
        self.lock_file = Path(settings["lock_file"])
        self.synth_state_file = Path(settings["synth_state_file"])
        self.flush_seconds = float(settings["flush_seconds"])
        self.batch_size = max(1, int(settings["batch_size"]))
        self.database = Path(settings["database"])
        self.status_file = Path(settings["status_file"])
        self.bar_store = Path(settings["bar_store"])
        self.synth_periods = list(settings["synth_periods"])
        self.reconnect_seconds = float(settings["reconnect_seconds"])
        self.max_backoff_seconds = float(settings["max_backoff_seconds"])
        self.user = str(settings["user"])
        self.password = str(settings["password"])
        self._api_factory = api_factory
        self._calendar = calendar_from_config(cfg, settings["root"])

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._api: Any | None = None
        self._buffer: list[Any] = []
        self._buffer_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._seen: dict[str, str] = {}
        self._symbols: list[str] = []
        self._state = "idle"
        self._error = ""
        self._connected_at = ""
        self._last_tick_at = ""
        self._ticks_today = 0
        self._today = date.today()
        self._saved = 0
        self._next_open = ""
        self._last_synth_day = self._read_synth_state()
        self._locked = False

    # ---------- 对外 ----------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """起采集线程。已经在跑就直接返回。"""
        if self.running:
            return
        self._acquire_lock()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="TickRecorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """通知线程退出，等它关连接，再把缓冲落盘。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self.flush()
        self._release_lock()
        self._write_status("stopped")

    def flush(self) -> int:
        """把缓冲写进 vnpy 库。失败保留缓冲，下一轮再试。"""
        # 停止按钮和工作线程可能同时 flush。串行复制/删除，避免后一次删掉前一次保存期间新增的 tick。
        with self._flush_lock:
            with self._buffer_lock:
                batch = list(self._buffer)
            if not batch:
                return 0
            try:
                saved = save_ticks(batch, database=self.database, batch_size=self.batch_size)
            except Exception as exc:  # noqa: BLE001
                self._error = f"tick 入库失败：{exc}"
                logger.warning("tick 入库失败，缓冲保留（{} 条）等下次重试：{}", len(batch), exc)
                self._write_status(self._state or "running")
                return 0
            with self._buffer_lock:
                del self._buffer[: len(batch)]
                self._saved += saved
            self._error = ""
            logger.debug("tick 入库 {} 条 → {}", saved, self.database)
            return saved

    def status(self) -> dict[str, Any]:
        """当前状态，字段和心跳文件一致。"""
        with self._buffer_lock:
            buffered = len(self._buffer)
        return {
            "state": self._state,
            "session_only": self.session_only,
            "next_open": self._next_open,
            "last_synth_day": self._last_synth_day,
            "database": str(self.database),
            "symbols": list(self._symbols),
            "buffered": buffered,
            "ticks_today": self._ticks_today,
            "ticks_total": self._saved,
            "last_tick_at": self._last_tick_at,
            "connected_at": self._connected_at,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "error": self._error,
            "pid": os.getpid(),
        }

    # ---------- 线程 ----------

    def _run(self) -> None:
        attempt = 0
        if not self.symbols_in:
            self._error = (
                "没有要采集的合约：请在配置页填关注品种，或在 data.tick.symbols 里写真实合约"
            )
            self._state = "stopped"
            logger.error("tick 采集停住：{}", self._error)
            self._write_status("stopped")
            self._release_lock()
            return
        while not self._stop.is_set():
            if self.session_only and not self._wait_for_open():
                break
            try:
                self._session()
                attempt = 0
            except (TickConfigError, VnpyUnavailable) as exc:
                # 没装库、没填合约：再连也一样，停住让界面把原因显示出来。
                self._error = str(exc)
                self._state = "stopped"
                logger.error("tick 采集停住：{}", exc)
                break
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self._error = str(exc)
                self._state = "reconnecting"
                self._write_status("reconnecting")
                wait = self._backoff(attempt)
                logger.warning("tick 采集中断：{}；{:.0f} 秒后重连", exc, wait)
                attempt += 1
                if self._stop.wait(wait):
                    break
                continue
            # 正常收盘退出：落盘、补合成、清旧数据，然后回去等下一次开盘。
            if self.session_only and not self._stop.is_set():
                self.flush()
                self._state = "closed"
                self._write_status("closed")
                self._after_session_close()
        self.flush()
        self._release_lock()
        self._write_status("stopped")

    def _after_session_close(self) -> None:
        """收盘后的收尾：等最后几笔落库，再合成 K 线、清旧 tick。"""
        synth_ok = True
        if self.auto_synth and not self._stop.is_set():
            if self._stop.wait(self.synth_delay_seconds):
                return
            synth_ok = self._synth_recent()
        # 合成失败时保留原始 tick，下次收盘重试；不能没合成成就先删掉。
        if synth_ok and not self._stop.is_set():
            self._cleanup_retention()

    def _synth_recent(self) -> bool:
        """收盘后合成 tick K 线。全部合约成功才记状态；重复运行幂等。"""
        day = futures_trading_day(datetime.now()).isoformat()
        from src.data.tick.synthesize import synthesize_symbol
        from src.data.tq_source import config_symbol_from_tq

        succeeded = True
        for instrument in list(self._symbols):
            symbol = config_symbol_from_tq(instrument)
            if not symbol:
                continue
            try:
                report = synthesize_symbol(
                    symbol,
                    database=self.database,
                    bar_store=self.bar_store,
                    periods=self.synth_periods,
                )
                logger.info(
                    "收盘 tick 合成 {}：tick {} 条（丢 {}），写库 {}",
                    symbol,
                    report["ticks"],
                    report["dropped"],
                    report["bars"],
                )
            except Exception as exc:  # noqa: BLE001
                succeeded = False
                logger.warning("收盘 tick 合成 {} 失败：{}", symbol, exc)
        if succeeded:
            self._last_synth_day = day
            self._write_synth_state(day)
        return succeeded

    def _cleanup_retention(self) -> None:
        """只保留最近 N 天原始 tick；汇总表一起重建，界面库存才不会撒谎。"""
        if self.retention_days <= 0:
            return
        import sqlite3

        cutoff = (datetime.now() - timedelta(days=self.retention_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            conn = sqlite3.connect(self.database)
            try:
                removed = conn.execute(
                    "DELETE FROM dbtickdata WHERE datetime < ?", (cutoff,)
                ).rowcount
                conn.execute("DELETE FROM dbtickoverview")
                conn.execute(
                    "INSERT INTO dbtickoverview (symbol, exchange, count, start, end) "
                    "SELECT symbol, exchange, COUNT(*), MIN(datetime), MAX(datetime) "
                    "FROM dbtickdata GROUP BY symbol, exchange"
                )
                conn.commit()
            finally:
                conn.close()
            logger.info("tick 保留 {} 天，清掉 {} 条旧数据", self.retention_days, removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tick 旧数据清理失败：{}", exc)

    def _read_synth_state(self) -> str:
        try:
            payload = json.loads(self.synth_state_file.read_text(encoding="utf-8"))
            return str(payload.get("last_trading_day") or "")
        except (OSError, ValueError):
            return ""

    def _write_synth_state(self, day: str) -> None:
        try:
            self.synth_state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.synth_state_file.with_suffix(self.synth_state_file.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"last_trading_day": day}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.synth_state_file)
        except OSError as exc:
            logger.debug("tick 合成状态写入失败：{}", exc)

    def _lock_payload(self) -> str:
        """锁内容：PID + 进程创建时间，两者一起才能确认身份。"""
        pid = os.getpid()
        return json.dumps(
            {
                "pid": pid,
                "created": _process_created(pid) or "",
                "started_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _lock_owner(path: Path) -> tuple[int, str] | None:
        """读锁：新格式返回 (pid, 创建时间)，旧格式（纯 PID）创建时间给空串。"""
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        if raw.isdigit():
            return int(raw), ""
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        return (pid, str(payload.get("created") or "")) if pid > 0 else None

    def _acquire_lock(self) -> None:
        """同一时间只允许一个进程采 tick，避免两条天勤连接互挤。

        锁里同时记 PID 和进程创建时间：Windows 会回收复用 PID，只比 PID 会把
        「已经退出的旧进程」误判成还在跑，新实例就永远起不来，会整整停掉一天。
        """
        path = self.lock_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            owner = self._lock_owner(path)
            if owner is not None:
                pid, created = owner
                if created and _process_created(pid) == created:
                    raise TickConfigError(f"另一个 tick 采集进程正在运行（PID {pid}）")
                if created:
                    logger.info("接管陈旧 tick 锁（PID {}），原进程已退出", pid)
                else:
                    logger.info("接管旧格式 tick 锁（PID {}），没法确认身份按陈旧处理", pid)
            path.write_text(self._lock_payload(), encoding="utf-8")
            self._locked = True
        except TickConfigError:
            raise
        except OSError as exc:
            logger.debug("tick 锁文件写入失败（继续运行）：{}", exc)

    def _release_lock(self) -> None:
        if not self._locked:
            return
        try:
            owner = self._lock_owner(self.lock_file)
            if owner is None or owner[0] == os.getpid():
                self.lock_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("tick 锁文件删除失败：{}", exc)
        self._locked = False

    # ---------- 交易时段 ----------

    def _sessions(self) -> list[list[str]]:
        out: list[list[str]] = []
        for symbol in self.symbols_in:
            for item in sessions_for_symbol(self.cfg, symbol):
                if item not in out:
                    out.append(item)
        return out

    def _calendar_day_ok(self, moment: datetime) -> bool:
        """凌晨 06:00 前算前一天的夜盘，周五夜盘跨到周六也仍有效。"""
        day = moment.date()
        if moment.time() < dtime(6, 0):
            day -= timedelta(days=1)
        return is_trading_day(day, calendar=self._calendar)

    def _in_session(self, moment: datetime, *, grace_sec: float = 90.0) -> bool:
        if not self._calendar_day_ok(moment):
            return False
        return in_trading_sessions(self._sessions(), moment, grace_sec=grace_sec)

    def _wait_for_open(self) -> bool:
        """平常只等，不连天勤；开盘前 open_lead_seconds 才开始连。"""
        while not self._stop.is_set():
            now = datetime.now()
            if self._in_session(now) or self._in_session(
                now + timedelta(seconds=self.open_lead_seconds)
            ):
                return True
            self._next_open = self._format_next_open(now)
            self._write_status("waiting_open")
            wait = min(self.idle_poll_seconds, max(5.0, self.open_lead_seconds))
            if self._stop.wait(wait):
                return False
        return False

    def _format_next_open(self, now: datetime) -> str:
        sessions = self._sessions()
        if not sessions:
            return ""
        for offset in range(0, 9):
            day = now.date() + timedelta(days=offset)
            if not is_trading_day(day, calendar=self._calendar):
                continue
            for start, _end in sessions:
                moment = datetime.combine(day, parse_hhmm(start))
                if moment > now:
                    return moment.isoformat(timespec="minutes")
        return ""

    # ---------- 连接 ----------

    def _session(self) -> None:
        if not self.symbols_in:
            raise TickConfigError(
                "没有要采集的合约：请在配置页填关注品种，或在 data.tick.symbols 里写真实合约"
            )
        client = self._api_factory() if self._api_factory else self._default_api()
        self._api = client
        try:
            instruments = self._resolve_instruments(client)
            serials = {name: client.get_tick_serial(name, data_length=2) for name in instruments}
            self._symbols = list(serials)
            self._state = "running"
            self._next_open = ""
            self._connected_at = datetime.now().isoformat(timespec="seconds")
            logger.info("tick 采集已连接：{}", ", ".join(self._symbols))
            for instrument, serial in serials.items():
                self._append_serial(instrument, serial)
            self.flush()
            self._write_status("running")

            last_flush = time.monotonic()
            last_status = last_flush
            refreshed = date.today()
            seen_session = False
            while not self._stop.is_set():
                if self.session_only:
                    now = datetime.now()
                    if self._in_session(now, grace_sec=0) or self._in_session(
                        now - timedelta(seconds=self.close_grace_seconds), grace_sec=0
                    ):
                        seen_session = True
                    elif seen_session:
                        # 开盘前提前连上的那几十秒还没算进时段，只有真正开过盘才判收盘。
                        self._state = "closed"
                        break
                _wait_update(client)
                for instrument, serial in list(serials.items()):
                    self._append_serial(instrument, serial)

                now = time.monotonic()
                with self._buffer_lock:
                    buffered = len(self._buffer)
                if buffered and (buffered >= self.batch_size or now - last_flush >= self.flush_seconds):
                    self.flush()
                    last_flush = now
                if now - last_status >= 2:
                    self._write_status(self._state)
                    last_status = now

                if date.today() != refreshed:
                    refreshed = date.today()
                    self._refresh_subscriptions(client, serials)
        finally:
            self._close(client)

    def _default_api(self) -> Any:
        if not self.user or not self.password:
            raise TickConfigError(
                "天勤需要账号和密码：请填 data.tq.user，密码放 config/secrets.local.yaml 的 tq_password"
            )
        from tqsdk import TqApi, TqAuth

        return TqApi(auth=TqAuth(self.user, self.password))

    def _close(self, client: Any) -> None:
        self._api = None
        try:
            client.close()
        except BaseException:  # noqa: BLE001
            logger.debug("关闭天勤采集连接时出错，已忽略")

    def _resolve_instruments(self, client: Any) -> list[str]:
        """品种代码解析成当前主力真实合约；具体合约原样订阅。"""
        raw_list = self.symbols_in
        if not raw_list:
            raise TickConfigError(
                "没有要采集的合约：请在配置页填关注品种，或在 data.tick.symbols 里写真实合约"
            )
        out: list[str] = []
        for raw in raw_list:
            text = str(raw).strip()
            if not text:
                continue
            if "." in text or any(ch.isdigit() for ch in text):
                instrument = tq_symbol(text)
            elif self.resolve_main:
                contract = main_contract(text, user=self.user, password=self.password, api=client)
                instrument = tq_symbol(contract)
            else:
                raise RuntimeError(
                    f"tick 采集只存真实合约：{text} 只写了品种。请填具体合约，或打开 data.tick.resolve_main"
                )
            if instrument not in out:
                out.append(instrument)
        if not out:
            raise RuntimeError("data.tick.symbols 里没有有效合约")
        return out

    def _refresh_subscriptions(self, client: Any, serials: dict[str, Any]) -> None:
        """每天重解析一次主力，换月后顺手订阅新合约。失败继续用旧合约。"""
        try:
            instruments = self._resolve_instruments(client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tick 主力重解析失败，继续用旧合约：{}", exc)
            return
        for instrument in instruments:
            if instrument in serials:
                continue
            try:
                serials[instrument] = client.get_tick_serial(instrument, data_length=2)
            except Exception as exc:  # noqa: BLE001
                logger.warning("订阅新合约 {} 失败：{}", instrument, exc)
                continue
            logger.info("tick 采集追加订阅：{}", instrument)
        self._symbols = list(serials)
        self._write_status(self._state)

    def _append_serial(self, instrument: str, serial: Any) -> None:
        if serial is None or len(serial) == 0:
            return
        row = serial.iloc[-1]
        raw_moment = row.get("datetime")
        moment = str(raw_moment or "")
        if not moment or moment == "NaT":
            return
        if self._seen.get(instrument) == moment:
            return
        self._seen[instrument] = moment
        symbol, exchange = split_instrument(instrument)
        try:
            tick = to_tick_data(row, symbol, exchange)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tick 数据转换失败（{}）：{}", instrument, exc)
            return
        today = date.today()
        if today != self._today:
            self._today = today
            self._ticks_today = 0
        with self._buffer_lock:
            self._buffer.append(tick)
        self._ticks_today += 1
        self._last_tick_at = format_tick_time(raw_moment)

    # ---------- 心跳 ----------

    def _write_status(self, state: str) -> None:
        self._state = state
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
            tmp.write_text(json.dumps(self.status(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.status_file)
        except OSError as exc:
            logger.debug("tick 心跳写入失败：{}", exc)
