@echo off
chcp 65001 >nul
title 弹幕伪直播 - 停止弹幕采集器
cd /d "%~dp0"
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" stop collector
echo.
pause