@echo off
chcp 65001 >nul
title 弹幕回放台 - 局域网访问
cd /d "%~dp0"
echo ==============================================
echo   弹幕回放台 · 局域网访问模式
echo   手机/iPad 扫下方二维码即可打开
echo ==============================================
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" lan
echo.
echo 服务在后台运行；关闭本窗口不影响。
echo 要停止请运行 stop-server.bat
pause