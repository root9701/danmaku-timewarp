@echo off
title 弹幕采集挂机中-请勿关闭本窗口
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先确认环境准备已完成（见 README.md）。
    pause
    exit /b 1
)
echo ==============================================
echo   弹幕采集挂机中，请勿关闭本窗口
echo   可最小化；关闭本窗口 = 停止采集
echo   按 Ctrl+C 也可安全停止
echo ==============================================
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0collector.py" --config "%~dp0config.json"
echo.
echo 采集已结束。结果在 data 文件夹（.jsonl.gz 与 manifest.json）。
pause
