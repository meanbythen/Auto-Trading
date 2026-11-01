@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 策略优化（网格搜索，可能较慢）
echo 完成后不会自动改配置；要写入请用: python tools\run_optimize.py --apply
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\run_optimize.py
) else (
  python tools\run_optimize.py
)
echo.
pause
