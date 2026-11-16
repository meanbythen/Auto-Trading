"""
Auto-Trading 桌面控制台
功能：运行控制 + 配置选项卡（策略/账号/密码/软件路径等）。
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

# 允许直接 python src/ui/app.py 启动：先把项目根加入路径
_FALLBACK_ROOT = Path(__file__).resolve().parents[2]
if str(_FALLBACK_ROOT) not in sys.path:
    sys.path.insert(0, str(_FALLBACK_ROOT))

from loguru import logger

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
from src.ui.optimize_tab import OptimizeTabMixin
from src.ui.picker_tab import PickerTabMixin
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
    ResearchTabMixin,
    ReviewTabMixin,
    AccountTabMixin,
    OptimizeTabMixin,
    PickerTabMixin,
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
        self._status = "待命"
        self._show_password = ctk.BooleanVar(value=False)
        self._show_tq_password = ctk.BooleanVar(value=False)
        self._prep_busy = False
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

        # 状态卡。五列按固定比例分同一行：窗口变宽时一起变，不会再被某一句长文字单独撑开。
        self.status_row = ctk.CTkFrame(self, fg_color=T.BG)
        self.status_row.pack(fill="x", padx=28, pady=(10, 6))
        self._status_card_frames: list[ctk.CTkFrame] = []
        # 执行通道那句最长，只比旁边宽一档。其余四张一样宽，避免品种和持仓缩成一条。
        status_weights = (5, 5, 6, 5, 5)
        for i, weight in enumerate(status_weights):
            self.status_row.grid_columnconfigure(i, weight=weight, uniform="status_cards", minsize=140)
        self.card_mode = self._make_stat_card(self.status_row, 0, "运行状态", "待命")
        self.card_engine = self._make_stat_card(self.status_row, 1, "策略引擎", "-")
        self.card_exec = self._make_stat_card(self.status_row, 2, "执行通道", "-")
        self.card_symbols = self._make_stat_card(self.status_row, 3, "关注品种", "-")
        self.card_position = self._make_stat_card(self.status_row, 4, "程序持仓", "空仓")
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
        self.tabs.add("配置")
        self.tabs.add("策略编辑器")
        self.tabs.add("历史回测")
        self.tabs.add("数据管理")
        self.tabs.add("交叉测试")
        self.tabs.add("策略优化")
        self.tabs.add("复盘")
        self.tabs.add("账户分析")
        self.tabs.add("规律候选")

        self._build_run_tab(self.tabs.tab("运行"))
        self._build_config_tab(self.tabs.tab("配置"))
        self._build_rule_editor_tab(self.tabs.tab("策略编辑器"))
        self._build_backtest_tab(self.tabs.tab("历史回测"))
        self._build_data_tab(self.tabs.tab("数据管理"))
        self._build_picker_tab(self.tabs.tab("交叉测试"))
        self._build_optimize_tab(self.tabs.tab("策略优化"))
        self._build_review_tab(self.tabs.tab("复盘"))
        self._build_account_tab(self.tabs.tab("账户分析"))
        self._build_research_tab(self.tabs.tab("规律候选"))
        self._load_saved_picks()
        # 打开程序先看一眼当前关注合约是不是主力，非主力时在配置页亮提示。
        self.after(300, self._refresh_main_contract_hint)


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
