@echo off
title 弹幕回放台 - 本机访问（关闭本窗口即停止）
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   弹幕回放台 · 仅本机可访问
echo   关闭本窗口 = 停止服务
echo ==============================================
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
echo.
echo 服务已停止。
pause
