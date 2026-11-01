"""
界面主题
功能：统一配色与字体，做成「深蓝交易台」气质。
原理：深蓝底 + 亮蓝强调 + 浅色字，保证下拉框/输入框上的字清晰可读；
      主按钮亮蓝配白字，危险操作用珊瑚红，运行中用清爽绿。
"""

from __future__ import annotations

# 主色：深蓝夜台（可读性优先）
BG = "#0B1628"           # 深蓝整窗背景
BG_PANEL = "#132338"     # 略亮面板（状态卡、选项卡区）
BG_ELEVATED = "#1A2F4A"  # 抬起卡片 / 次要按钮底
BORDER = "#2A4566"       # 蓝灰边线
TEXT = "#E8F1FA"         # 主文字（浅色，深底上清晰）
TEXT_DIM = "#8FA8C4"     # 次要文字 / 标签说明
ACCENT = "#3B8EFF"       # 亮蓝强调（品牌、主按钮、选中项）
ACCENT_HOVER = "#5AA0FF" # 主按钮悬停（略亮）
ACCENT_ON = "#FFFFFF"    # 主按钮上的字色（可点时）
ACCENT_MUTED = "#4A5568" # 启动按钮运行中禁用：灰底（与停止待命同色逻辑）
ACCENT_TEXT_DISABLED = "#CBD5E1"  # 启动禁用时字色（浅灰白）
DANGER = "#D32F2F"       # 停止 / 紧急清仓（运行中可点：鲜红）
DANGER_HOVER = "#E53935"
DANGER_MUTED = "#4A5568" # 停止按钮待命时：灰色底，表示当前点不了
DANGER_TEXT = "#FFFFFF"  # 危险按钮字色（启用）
DANGER_TEXT_DISABLED = "#CBD5E1"  # 待命禁用时字色（浅灰白，配灰底清晰）
OK = "#2ECC8A"           # 运行中 / 成功
WARN = "#E6A23C"         # 警告提示

FONT_DISPLAY = ("Segoe UI Semibold", 28)
FONT_TITLE = ("Segoe UI Semibold", 16)
FONT_BODY = ("Segoe UI", 13)
FONT_SMALL = ("Segoe UI", 11)
FONT_MONO = ("Consolas", 12)
