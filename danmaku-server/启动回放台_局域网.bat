@echo off
chcp 65001 >nul
title 弹幕回放台 - 启动（局域网 + 二维码）
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   启动 弹幕回放台 · 局域网访问
echo   手机/iPad 和电脑连同一个 WiFi，扫下方二维码
echo ==============================================
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" lan
echo.
pause