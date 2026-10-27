@echo off
chcp 65001 >nul
title 弹幕伪直播 - 启动弹幕采集器
cd /d "%~dp0"
echo 启动 弹幕采集器（后台）...
echo.
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" start collector
echo.
echo 采集控制台: http://127.0.0.1:8787
pause