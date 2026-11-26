"""
Auto-Trading 桌面控制台
功能：运行控制 + 账号页 + 配置选项卡（策略/软件路径等）。
原理：CustomTkinter Tabview；BotWorker 后台跑策略；密码存 secrets.local.yaml。

启动（任选其一）:
    python -m src.ui
    python -m src.main --gui
    双击打包后的 Auto-Trading.exe
    在 Cursor 中运行 src/ui/__main__.py（已兼容）
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path
from typing import Callable

# 允许直接 python src/ui/app.py 启动：先把项目根加入路径
_FALLBACK_ROOT = Path(__file__).resolve().parents[2]
if str(_FALLBACK_ROOT) not in sys.path:
    sys.path.insert(0, str(_FALLBACK_ROOT))

from loguru import logger

from src.kernel.events import (
    BACKTEST_DONE,
    CROSS_TEST_DONE,
    CROSS_TEST_FAILED,
    CROSS_TEST_PROGRESS,
    CROSS_TEST_START,
    CROSS_TEST_STOPPED,
    Event,
    EventBus,
)
from src.ui import theme as T
from src.ui.bot_worker import BotWorker
from src.utils import load_config, setup_logger
from src.utils.paths import project_root
from src.version import __version__

try:
    import customtkinter as ctk
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "缺少 customtkinter，请先执行:\n  pip install customtkinter\n"
        f"原始错误: {e}"
    ) from e

# 保留旧的常量与辅助函数导入入口；实现统一放在 app_common。
from src.ui.app_common import (
    ENGINE_CHOICES,
    ENGINE_LABELS,
    EXEC_LABELS,
    QueueLogHandler,
    _clock_text,
    _duration_text,
    _eth_minutes_from_strategy,
    _same_contract,
    _ui_engine,
)
from src.ui.account_tab import AccountTabMixin
from src.ui.app_state import AppStateMixin
from src.ui.backtest_tab import BacktestTabMixin
from src.ui.config_collect import ConfigCollectMixin
from src.ui.config_support import ConfigSupportMixin
from src.ui.config_vars import ConfigVarsMixin
from src.ui.dashboard import DashboardMixin
from src.ui.data_tab import DataTabMixin
from src.ui.governance_tab import GovernanceTabMixin
from src.ui.optimize_tab import OptimizeTabMixin
from src.ui.picker_tab import PickerTabMixin
from src.ui.research_hub import ResearchHubMixin
from src.ui.research_tab import ResearchTabMixin
from src.ui.review_tab import ReviewTabMixin
from src.ui.run_tab import RunTabMixin
from src.ui.widgets import WidgetsMixin

__all__ = [
    "AutoTradingApp",
    "main",
    "ENGINE_CHOICES",
    "ENGINE_LABELS",
    "EXEC_LABELS",
    "QueueLogHandler",
    "_clock_text",
    "_duration_text",
    "_eth_minutes_from_strategy",
    "_same_contract",
    "_ui_engine",
]


class AutoTradingApp(
    WidgetsMixin,
    AppStateMixin,
    ConfigVarsMixin,
    ConfigCollectMixin,
    ConfigSupportMixin,
    DashboardMixin,
    RunTabMixin,
    BacktestTabMixin,
    DataTabMixin,
    GovernanceTabMixin,
    ResearchTabMixin,
    ReviewTabMixin,
    AccountTabMixin,
    OptimizeTabMixin,
    PickerTabMixin,
    ResearchHubMixin,
    ctk.CTk,
):
    """主窗口：运行台 + 配置选项卡。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"Auto-Trading {__version__}  ·  期货自动交易台")
        self.geometry("1240x820")
        self.minsize(1020, 700)

        # 深蓝主题：系统用 dark，具体色值看 theme.py（保证下拉/输入框字色够亮）
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.configure(fg_color=T.BG)

        self.root_dir = project_root()
        from src.utils.config_loader import ensure_runtime_layout

        ensure_runtime_layout()
        # 自定义规则也是一套策略：把 config/rules/*.yaml 注册进引擎下拉框。
        from src.ui.app_common import register_rule_engines

        register_rule_engines()
        self.cfg = load_config()
        setup_logger(
            log_dir=str(self.root_dir / self.cfg.get("logging", {}).get("dir", "logs")),
            level="INFO",
        )
        # 只在这里清一次历史遗留的旧限额熔断行；之后改限额保存时再清，
        # 不在界面每轮刷新里读写告警文件。
        try:
            from src.ops.alerts import prune_stale_halt_alerts

            limit = float((self.cfg.get("risk") or {}).get("max_daily_loss") or 0)
            if limit > 0:
                prune_stale_halt_alerts(self.root_dir, limit)
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("启动时清理旧限额熔断告警跳过: {}", exc)
        self.worker = BotWorker()
        # 各层只发事件，界面在这里订阅；后台线程发来的事件由处理函数切回主线程。
        self.events = EventBus()
        self.events.subscribe(BACKTEST_DONE, self._on_backtest_done)
        self.events.subscribe(CROSS_TEST_DONE, self._on_cross_test_done)
        self.events.subscribe(CROSS_TEST_START, self._on_cross_test_start)
        self.events.subscribe(CROSS_TEST_PROGRESS, self._on_cross_test_progress)
        self.events.subscribe(CROSS_TEST_STOPPED, self._on_cross_test_stopped)
        self.events.subscribe(CROSS_TEST_FAILED, self._on_cross_test_failed)
        self._status = "待命"
        self._show_tq_password = ctk.BooleanVar(value=False)
        self._show_ctp_sim_password = ctk.BooleanVar(value=False)
        self._show_ctp_auth = ctk.BooleanVar(value=False)
        self._prep_busy = False
        # 历史回测等研究任务占用天勤连接时的名称；空字符串表示没有。
        self._research_busy = ""
        # 天勤 tick 常驻采集器。界面上点启动/停止，关窗口时要先停掉。
        self._tick_recorder = None
        self._tick_synth_busy = False
        self._login_fired: set[str] = set()
        # 准备按钮已经登录成功时，下一次启动常驻不再登录。
        self._skip_next_bot_login = False
        # 正在点击同花顺时先攒着的日志。点击结束前不写进窗口，避免把本程序拉到前面。
        self._held_logs: list[str] = []
        # 后台线程写日志只进队列，主线程定时取出，避免跨线程调用 Tk。
        self._log_queue: queue.Queue[str] = queue.Queue()
        # 后台线程要执行的主线程回调也只入队，由同一个 100ms 泵取出执行。
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        # 定时刷新的 after id。关窗口时先 after_cancel，避免窗口销毁后回调还在跑。
        self._closing = False
        self._tick_after_id: str | None = None
        self._log_after_id: str | None = None
        self._login_tick_after_id: str | None = None
        # 运行日志最多留这么多行，长时间挂机不会把文本框越撑越大。
        self._LOG_MAX_LINES = 2000

        self._init_vars()
        self._build()
        self._hook_logger()
        self._refresh_status_cards()
        self._tick_after_id = self.after(800, self._tick)
        self._login_tick_after_id = self.after(5000, self._tick_login_schedule)
        self.after(2000, self._warn_unprotected_resume)
        # 界面建好后再按配置自动开 tick 采集，避免启动时被天勤连接卡住。
        self.after(1600, self._auto_start_tick_if_enabled)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._dm_scraping = False
        # 读同花顺图时焦点在期货通上，窗口快捷键可能收不到；工作线程还会另外查 ESC。
        self.bind_all("<Escape>", self._on_escape_key)

    def _build(self) -> None:
        hero = ctk.CTkFrame(self, fg_color=T.BG)
        hero.pack(fill="x", padx=28, pady=(20, 4))
        ctk.CTkLabel(
            hero, text=f"Auto-Trading {__version__}", font=T.FONT_DISPLAY, text_color=T.ACCENT, anchor="w"
        ).pack(anchor="w")
        # 副标题按当前券商、关注品种和模拟开关生成，不再写死某几个品种。
        self.hero_sub = ctk.CTkLabel(
            hero,
            text="-",
            font=T.FONT_SMALL,
            text_color=T.TEXT_DIM,
            anchor="w",
        )
        self.hero_sub.pack(anchor="w", pady=(2, 0))

        # 状态卡。六列按固定比例分同一行：窗口变宽时一起变，不会再被某一句长文字单独撑开。
        self.status_row = ctk.CTkFrame(self, fg_color=T.BG)
        self.status_row.pack(fill="x", padx=28, pady=(10, 6))
        self._status_card_frames: list[ctk.CTkFrame] = []
        # 信号周期字短，收窄一档；执行通道、策略名、持仓略宽。
        status_weights = (5, 5, 3, 5, 4, 5)
        for i, weight in enumerate(status_weights):
            self.status_row.grid_columnconfigure(i, weight=weight, uniform="status_cards", minsize=108)
        self.card_mode = self._make_stat_card(self.status_row, 0, "运行状态", "待命")
        self.card_engine = self._make_stat_card(self.status_row, 1, "策略引擎", "-")
        self.card_timeframe = self._make_stat_card(self.status_row, 2, "信号周期", "-")
        self.card_exec = self._make_stat_card(self.status_row, 3, "执行通道", "-")
        self.card_symbols = self._make_stat_card(self.status_row, 4, "关注品种", "-")
        self.card_position = self._make_stat_card(self.status_row, 5, "程序持仓", "空仓")
        # 持仓句子可能写成「bu2612 空1手 @4662 止损4700」，用小一号字，避免一有仓就把整排撑高。
        self.card_position.configure(font=T.FONT_BODY)
        self.status_row.bind("<Configure>", self._fit_status_card_text)
        # 告警 / 健康 / 动态减仓：三句经常很长，不挤成一行。行距压紧，没字的行收起来。
        self.status_notes = ctk.CTkFrame(self, fg_color="transparent")
        self.status_notes.pack(fill="x", padx=28, pady=(0, 4))
        note_kw = {
            "font": T.FONT_SMALL,
            "anchor": "w",
            "justify": "left",
            "height": 18,
        }
        self.alert_line = ctk.CTkLabel(self.status_notes, text="", text_color=T.DANGER, **note_kw)
        self.health_line = ctk.CTkLabel(self.status_notes, text="", text_color=T.TEXT_DIM, **note_kw)
        self.dyn_line = ctk.CTkLabel(self.status_notes, text="", text_color=T.TEXT_DIM, **note_kw)
        self.status_notes.bind("<Configure>", self._fit_status_note_wrap)

        self.tabs = ctk.CTkTabview(
            self,
            fg_color=T.BG_PANEL,
            segmented_button_fg_color=T.BG_ELEVATED,
            segmented_button_selected_color=T.ACCENT,
            segmented_button_selected_hover_color=T.ACCENT_HOVER,
            segmented_button_unselected_color=T.BG_ELEVATED,
            segmented_button_unselected_hover_color=T.BORDER,
            text_color=T.TEXT,
            corner_radius=16,
        )
        self.tabs.pack(fill="both", expand=True, padx=28, pady=(6, 22))
        self.tabs.add("运行")
        self.tabs.add("交易账号")
        self.tabs.add("交易配置")
        self.tabs.add("策略编辑器")
        self.tabs.add("策略研究")
        self.tabs.add("数据管理")
        self.tabs.add("账户分析")

        self._build_run_tab(self.tabs.tab("运行"))
        self._build_credentials_tab(self.tabs.tab("交易账号"))
        self._build_config_tab(self.tabs.tab("交易配置"))
        self._build_rule_editor_tab(self.tabs.tab("策略编辑器"))
        self._build_research_hub(self.tabs.tab("策略研究"))
        self._build_data_tab(self.tabs.tab("数据管理"))
        self._build_account_tab(self.tabs.tab("账户分析"))
        self._load_saved_picks()
        # 切到“策略研究”时按内层页签重画名单，避免还停在启动那一份。
        self.tabs.configure(command=self._on_main_tab_change)
        # 打开程序先看一眼当前关注合约是不是主力，非主力时在配置页亮提示。
        self.after(300, self._refresh_main_contract_hint)

    def _on_main_tab_change(self) -> None:
        """顶部分页切换。研究页内部跟着当前选中的二级页签刷新。"""
        try:
            name = self.tabs.get()
        except Exception:
            return
        if name == "策略研究":
            self._on_research_tab_change()
        elif name == "交易配置":
            # 账户权益只读，打开配置页就按当前账本对一次，别让用户看到旧余额。
            try:
                self._pull_signal_balance(force=True)
            except Exception as exc:  # noqa: BLE001 - 对不上不影响切页
                logger.warning("切到交易配置时对账户权益跳过: {}", exc)


    def _on_backtest_done(self, event: Event) -> None:
        """历史回测完成。摘要仍写进运行日志，界面更新切回 Tk 主线程。"""
        d = event.data
        text = (
            f"{d.get('header') or '历史回测'}完成 {d.get('symbol')} | {d.get('bars')}根 | "
            f"{d.get('trades')}笔 胜率{float(d.get('win_rate') or 0):.0%} "
            f"盈亏比{float(d.get('payoff') or 0):.2f} 期望{float(d.get('expectancy') or 0):.2f} "
            f"净盈亏{float(d.get('net_pnl') or 0):.0f}"
        )
        self._post_to_main(lambda: self._append_log(text))


    def _on_cross_test_done(self, event: Event) -> None:
        """交叉测试完成。只报测了多少组、其中较好多少组。"""
        d = event.data
        text = f"交叉测试完成，测了 {int(d.get('rows') or 0)} 个组合，较好 {int(d.get('better') or 0)} 个"
        self._post_to_main(lambda: self._append_log(text))


    def _on_cross_test_start(self, event: Event) -> None:
        """交叉测试开跑。文案由发起方拼好，这里只负责写进运行日志。"""
        text = str(event.data.get("text") or "开始交叉测试")
        self._post_to_main(lambda: self._append_log(text))


    def _on_cross_test_progress(self, event: Event) -> None:
        """交叉测试进度。交给主线程记账，界面最多每 100 毫秒重画一次。"""
        d = event.data
        index = int(d.get("index") or 0)
        total = int(d.get("total") or 0)
        label = str(d.get("label") or "")
        self._post_to_main(lambda: self._note_pick_progress(index, total, label))


    def _on_cross_test_stopped(self, event: Event) -> None:
        """点了停止。真正的收尾仍在跑测线程完成后再做。"""
        text = str(event.data.get("text") or "已请求停止交叉测试")
        self._post_to_main(lambda: self._append_log(text))


    def _on_cross_test_failed(self, event: Event) -> None:
        """交叉测试失败。只写日志，弹窗和结果区由发起方处理。"""
        message = str(event.data.get("message") or "")
        self._post_to_main(lambda: self._append_log(f"交叉测试失败: {message}"))


def main() -> None:
    """GUI 入口。"""
    try:
        app = AutoTradingApp()
        app.mainloop()
    except KeyboardInterrupt:
        # 终端里按 Ctrl+C，或 Cursor 点停止/重复运行时都会走到这里。
        # 属于人为中断，不是程序出错，不用打印一大段 traceback。
        logger.info("界面被键盘中断（Ctrl+C）关闭")
    except BaseException:
        logger.exception("图形界面异常退出")
        raise


if __name__ == "__main__":
    main()
