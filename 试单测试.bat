@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo  同花顺自动下单试跑（模拟账户）
echo  请先让期货通下单面板露出来
echo  5秒预览后会真实点击：买多1手 -^> 平仓
echo  紧急停止：鼠标甩到屏幕左上角
echo ========================================
pause
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\gui_order_test.py
) else (
  python tools\gui_order_test.py
)
echo.
pause
