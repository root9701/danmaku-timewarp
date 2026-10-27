@echo off
chcp 65001 >nul
title 弹幕回放台 - 启动（本机）
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   启动 弹幕回放台（后台运行，关掉本窗口不影响）
echo   仅本机可访问
echo ==============================================
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" start
if errorlevel 1 (
    echo.
    echo 启动失败，请检查上面的提示。
    pause
    exit /b 1
)
echo.
echo 浏览器打开: http://127.0.0.1:8789
echo 停止服务:   双击 停止回放台.bat
echo.
pause