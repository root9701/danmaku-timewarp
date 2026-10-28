@echo off
title 弹幕时光机 - 本机访问（关闭本窗口即停止）
cd /d "%~dp0"
rem 首次运行自动建 .venv 并装依赖(约 1-3 分钟, 只发生一次)。
rem 失败时脚本已打印原因并 pause, 这里直接退出, 不重复报错。
call "%~dp0_ensure_venv.bat" "%~dp0" 
if errorlevel 1 exit /b 1
echo ==============================================
echo   弹幕时光机 · 仅本机可访问
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
