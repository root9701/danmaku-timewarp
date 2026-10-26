@echo off
chcp 65001 >nul
title 弹幕采集挂机中-请勿关闭本窗口
cd /d "%~dp0"
echo ==============================================
echo   弹幕采集挂机中，请勿关闭本窗口
echo   可最小化；按 Ctrl+C 可随时安全停止
echo ==============================================
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先确认环境准备已完成（见 README.md）。
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0collector.py" --config "%~dp0config.json"
echo.
echo 采集已结束。请查看上方摘要，结果文件在 danmaku-collector\data 文件夹（.jsonl.gz 与 meta.json）。
pause
