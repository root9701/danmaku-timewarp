@echo off
chcp 65001 >nul
title 弹幕采集 - 启动（后台运行）
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先完成环境准备（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   启动 弹幕采集器（后台运行，关掉本窗口不影响）
echo ==============================================
"%~dp0.venv\Scripts\python.exe" "%~dp0svc.py" start
if errorlevel 1 (
    echo.
    echo 启动失败，请检查上面的提示。
    pause
    exit /b 1
)
echo.
echo 采集器已在后台运行。
echo   查看进度: http://127.0.0.1:8787/    （需要用 启动控制台.bat 启动）
echo   停止采集: 双击 停止采集.bat
echo.
pause