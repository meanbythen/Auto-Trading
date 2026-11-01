@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   Auto-Trading 打包为独立可运行程序
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到 .venv，请先创建虚拟环境并安装依赖：
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

echo [1/3] 安装/更新 PyInstaller ...
".venv\Scripts\python.exe" -m pip install -U "pyinstaller>=6.0" wheel
if errorlevel 1 (
  echo pip 安装失败
  pause
  exit /b 1
)

echo.
echo [2/3] 开始打包（首次可能要几分钟，请耐心等待）...
".venv\Scripts\python.exe" -m PyInstaller Auto-Trading.spec --noconfirm --clean
if errorlevel 1 (
  echo.
  echo 打包失败。可把 console=True 写进 Auto-Trading.spec 再试，便于看报错。
  pause
  exit /b 1
)

echo.
echo [3/4] 整理发布目录 ...
".venv\Scripts\python.exe" tools\finish_dist.py
if errorlevel 1 (
  echo 整理脚本失败，但 dist\Auto-Trading 可能已生成。
)

echo.
echo [4/4] 验收发布包（版本、复盘模块、备查文件）...
".venv\Scripts\python.exe" tools\verify_dist.py
if errorlevel 1 (
  echo.
  echo 验收未通过，请根据上面的提示修补后再分发。
  pause
  exit /b 1
)

echo.
echo ========================================
echo 完成！请把整个文件夹拷到目标电脑：
echo   dist\Auto-Trading\
echo 双击 Auto-Trading.exe 即可（无需安装 Python）
echo ========================================
explorer "dist\Auto-Trading"
pause
