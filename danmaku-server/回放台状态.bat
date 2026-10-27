@echo off
chcp 65001 >nul
title 弹幕回放台 - 状态
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" status
echo.
pause