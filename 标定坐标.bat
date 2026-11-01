@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 请先打开同花顺期货通并露出下单面板，然后按任意键开始标定...
pause >nul
REM 用 pythonw 打开标定窗口。按完键后这个黑框会关掉，不再留在旁边。
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" tools\pick_coords.py
) else if exist ".venv\Scripts\python.exe" (
  start "" ".venv\Scripts\python.exe" tools\pick_coords.py
) else (
  start "" pythonw tools\pick_coords.py
)
