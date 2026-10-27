@echo off
chcp 65001 >nul
title 弹幕伪直播 - 服务状态
cd /d "%~dp0"
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" status
echo.
pause