@echo off
chcp 65001 >nul
title 弹幕采集 - 停止
cd /d "%~dp0"
echo ==============================================
echo   停止 弹幕采集器
echo ==============================================
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" stop
echo.
pause