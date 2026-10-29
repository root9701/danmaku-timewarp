@echo off
title 弹幕时光机 - 局域网访问（关闭本窗口即停止）
cd /d "%~dp0"
rem 首次运行自动建 .venv 并装依赖(约 1-3 分钟, 只发生一次)。
call "%~dp0_ensure_venv.bat" "%~dp0"
if errorlevel 1 exit /b 1
echo ==============================================
echo   弹幕时光机
echo   模式  局域网（手机或平板需连接同一 WiFi）
echo   停止  关闭本窗口，或按 Ctrl+C
echo ==============================================
echo.
set DANMAKU_HOST=0.0.0.0
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
echo.
echo 服务已停止。
pause
