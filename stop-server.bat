@echo off
chcp 65001 >nul
title 弹幕伪直播 - 停止回放台
cd /d "%~dp0"
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" stop server
echo.
pause