@echo off
chcp 65001 >nul
title 伪直播服务-请勿关闭
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
echo.
echo 伪直播服务已退出。
pause
