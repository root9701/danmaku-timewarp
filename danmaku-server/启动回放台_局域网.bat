@echo off
title 弹幕回放台 - 局域网访问（关闭本窗口即停止）
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   弹幕回放台 · 局域网访问
echo   手机/iPad 和电脑连同一个 WiFi，扫下方二维码
echo   关闭本窗口 = 停止服务
echo ==============================================
echo.
set DANMAKU_HOST=0.0.0.0
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
echo.
echo 服务已停止。
pause
