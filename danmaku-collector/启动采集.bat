@echo off
title 弹幕采集挂机中-请勿关闭本窗口
cd /d "%~dp0"
rem 首次运行自动建 .venv 并装依赖(约 1-3 分钟, 只发生一次)。
call "%~dp0_ensure_venv.bat" "%~dp0"
if errorlevel 1 exit /b 1
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
