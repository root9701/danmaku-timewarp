@echo off
chcp 65001 >nul
title 弹幕伪直播 - 停止全部服务
cd /d "%~dp0"
echo ==============================================
echo   停止 弹幕采集器 + 弹幕回放台
echo ==============================================
echo.
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" stop all
echo.
pause