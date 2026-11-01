@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 历史回测（按当前配置策略）
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\run_backtest.py
) else (
  python tools\run_backtest.py
)
echo.
pause
