@echo off
chcp 65001 >nul
title 弹幕采集 - 状态
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" status
echo.
pause