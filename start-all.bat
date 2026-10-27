@echo off
chcp 65001 >nul
title 弹幕伪直播 - 启动全部服务
cd /d "%~dp0"
echo ==============================================
echo   启动 弹幕采集器 + 弹幕回放台（后台运行）
echo ==============================================
echo.
"%~dp0danmaku-server\.venv\Scripts\python.exe" "%~dp0ctl.py" start all
echo.
echo 浏览器打开 http://127.0.0.1:8789 使用回放台
echo 采集控制台:   http://127.0.0.1:8787
echo.
echo 关闭本窗口不影响服务运行；要停止请运行 stop-all.bat
pause