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

echo [1/2] 安装固定版本的打包工具 ...
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 (
  echo pip 安装失败
  pause
  exit /b 1
)

echo.
echo [2/2] 预检 -^> 清理 -^> 打包 -^> 收尾 -^> 验收 -^> 冒烟 ...
".venv\Scripts\python.exe" tools\build_exe.py %*
if errorlevel 1 (
  echo.
  echo 打包未通过。按上面的第一条错误修复后重试；
  echo 排查缓存问题时可用：打包成EXE.bat --no-clean
  pause
  exit /b 1
)

echo.
echo ========================================
echo 完成！分发时请拷贝整个文件夹：
echo   dist\Auto-Trading\
echo 双击 Auto-Trading.exe 即可（无需安装 Python）
echo 构建元数据：build\packaging\build-info.json
echo ========================================
explorer "dist\Auto-Trading"
pause
