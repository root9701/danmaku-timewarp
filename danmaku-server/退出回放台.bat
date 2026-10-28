@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 退出回放台 - 停止服务
cd /d "%~dp0"

echo ==============================================
echo   退出 弹幕时光机
echo ==============================================
echo.

rem 只认"正在监听 8789 的那个进程", 不按进程名乱杀 ——
rem 本机可能同时跑着采集端控制台和别的 python, 按名字杀会误伤。
rem 注: 先落临时文件再 findstr, 比在 for /f 里嵌套管道+引号可靠得多
rem     (实测 `for /f ... in ('netstat^|findstr /r /c:"LISTENING"')` 会报
rem      "was unexpected at this time", cmd 对多层引号转义处理很脆)。
set TMPF=%TEMP%\danmaku_8789_pids.txt
netstat -ano > "%TMPF%" 2>nul
set FOUND=0
for /f "tokens=5" %%P in ('findstr ":8789" "%TMPF%" ^| findstr "LISTENING"') do (
    if not "%%P"=="0" (
        set FOUND=1
        echo   正在停止回放台 ^(PID %%P^) ...
        taskkill /PID %%P /T /F >nul 2>&1
    )
)
del "%TMPF%" >nul 2>&1

if "!FOUND!"=="0" (
    echo   回放台没在运行 ^(端口 8789 无监听^)。
) else (
    echo.
    echo   已停止，端口 8789 已释放。
)

echo.
pause
