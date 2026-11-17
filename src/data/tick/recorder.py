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
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.data.tick.vnpy_store import VnpyUnavailable, save_ticks, split_instrument, to_tick_data
from src.data.tq_source import main_contract, tq_symbol
from src.utils import load_secrets

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


class TickRecorder:
    """常驻采集器。start() 起线程，stop() 先落盘再关连接。"""

    def __init__(self, cfg: dict[str, Any], *, api_factory: Any | None = None) -> None:
        settings = tick_settings(cfg)
        self.symbols_in = settings["symbols"]
        self.resolve_main = bool(settings["resolve_main"])
        self.flush_seconds = float(settings["flush_seconds"])
        self.batch_size = max(1, int(settings["batch_size"]))
        self.database = Path(settings["database"])
        self.status_file = Path(settings["status_file"])
        self.reconnect_seconds = float(settings["reconnect_seconds"])
        self.max_backoff_seconds = float(settings["max_backoff_seconds"])
        self.user = str(settings["user"])
        self.password = str(settings["password"])
        self._api_factory = api_factory

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._api: Any | None = None
        self._buffer: list[Any] = []
        self._buffer_lock = threading.Lock()
        self._seen: dict[str, str] = {}
        self._symbols: list[str] = []
        self._state = "idle"
        self._error = ""
        self._connected_at = ""
        self._last_tick_at = ""
        self._ticks_today = 0
        self._today = date.today()
        self._saved = 0

    # ---------- 对外 ----------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """起采集线程。已经在跑就直接返回。"""
        if self.running:
            return
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
        self._write_status("stopped")

    def flush(self) -> int:
        """把缓冲写进 vnpy 库。失败保留缓冲，下一轮再试。"""
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
        while not self._stop.is_set():
            try:
                self._session()
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
        self.flush()
        self._write_status("stopped")

    def _backoff(self, attempt: int) -> float:
        step = _BACKOFF[min(max(0, attempt), len(_BACKOFF) - 1)]
        return min(max(self.reconnect_seconds, step), self.max_backoff_seconds)

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
            self._connected_at = datetime.now().isoformat(timespec="seconds")
            logger.info("tick 采集已连接：{}", ", ".join(self._symbols))
            for instrument, serial in serials.items():
                self._append_serial(instrument, serial)
            self.flush()
            self._write_status("running")

            last_flush = time.monotonic()
            last_status = last_flush
            refreshed = date.today()
            while not self._stop.is_set():
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
        moment = str(row.get("datetime") or "")
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
        self._last_tick_at = moment

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
