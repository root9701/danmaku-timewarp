@echo off
title 弹幕回放台 - 本机访问（关闭本窗口即停止）
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   弹幕回放台 · 仅本机可访问
echo   关闭本窗口 = 停止服务
echo ==============================================
echo.
rem 必须显式覆盖监听地址: config.json 的 host 可能是 0.0.0.0(局域网模式保存过),
rem 若不覆盖, 这个"仅本机"的脚本实际会绑 0.0.0.0 并打印二维码, 与标题自相矛盾。
set DANMAKU_HOST=127.0.0.1
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
echo.
echo 服务已停止。
pause
