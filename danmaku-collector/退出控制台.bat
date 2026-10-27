@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 退出采集控制台 - 停止控制台与采集
cd /d "%~dp0"

echo ==============================================
echo   退出 弹幕采集控制台
echo ==============================================
echo.

rem 只认"正在监听 8787 的那个进程", 不按进程名乱杀(本机还有别的 python)。
rem 采集任务跑在控制台进程内部(dashboard 用 asyncio task 调 collector.amain),
rem 所以结束控制台进程树 = 控制台和采集一起停。
rem 注: 先落临时文件再 findstr, 避免 for /f 里嵌套管道+引号导致
rem     "was unexpected at this time"。
set TMPF=%TEMP%\danmaku_8787_pids.txt
netstat -ano > "%TMPF%" 2>nul
set FOUND=0
for /f "tokens=5" %%P in ('findstr ":8787" "%TMPF%" ^| findstr "LISTENING"') do (
    if not "%%P"=="0" (
        set FOUND=1
        echo   正在停止控制台 ^(PID %%P^) ...
        taskkill /PID %%P /T /F >nul 2>&1
    )
)
del "%TMPF%" >nul 2>&1

if "!FOUND!"=="0" (
    echo   控制台没在运行 ^(端口 8787 无监听^)。
) else (
    echo.
    echo   已停止，端口 8787 已释放。
)

echo.
echo   提示: 控制台进程退出时会触发采集收尾(gzip + 写 meta/manifest)。
echo         如果你刚点过"停止"就关窗口，建议重开控制台确认一次收尾结果。
echo.
pause
