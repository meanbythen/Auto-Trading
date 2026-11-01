@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo  挂机前自检
echo  1) 检查配置 / 行情 / 窗口
echo  2) 鼠标预览下单坐标
echo  3) 可选：真实点买开+平仓
echo  请先打开同花顺并露出下单面板
echo  紧急停止：鼠标甩到屏幕左上角
echo ========================================
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\preflight_check.py
) else (
  python tools\preflight_check.py
)
echo.
pause
