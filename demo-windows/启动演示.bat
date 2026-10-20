@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   lan-ip-manager 前端演示（仅演示界面与交互，不改动电脑）
echo ============================================================
echo.
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py server.py
  ) else (
    echo [错误] 未找到 Python。请先安装 Python 3，再双击本文件。
  )
)
echo.
echo 服务已停止，按任意键关闭窗口。
pause >nul
