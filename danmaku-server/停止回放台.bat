@echo off
chcp 65001 >nul
title 弹幕回放台 - 停止
cd /d "%~dp0"
echo ==============================================
echo   停止 弹幕回放台
echo ==============================================
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" stop
echo.
pause