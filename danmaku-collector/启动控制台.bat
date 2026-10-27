@echo off
chcp 65001 >nul
title 弹幕采集控制台 - 请勿关闭本窗口
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   弹幕采集控制台
echo   浏览器打开 http://127.0.0.1:8787 即可操作
echo   可最小化，请勿关闭本窗口（关闭即停止控制台）
echo ==============================================
"%~dp0.venv\Scripts\python.exe" "%~dp0dashboard.py"
echo.
echo 控制台已退出。
pause