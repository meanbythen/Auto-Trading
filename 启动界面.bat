@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 优先用 pythonw：无黑色命令行窗口，避免和图形界面抢焦点
REM 原理：python.exe 会带控制台；启动常驻后同花顺再置顶时，用户常误点到这个黑框
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m src.ui
  exit /b 0
)
if exist ".venv\Scripts\python.exe" (
  start "" ".venv\Scripts\python.exe" -m src.ui
  exit /b 0
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw -m src.ui
  exit /b 0
)

python -m src.ui
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装依赖： pip install -r requirements.txt
  pause
)
