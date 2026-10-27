@echo off
rem ============================================================
rem  环境自举: 首次运行自动创建 .venv 并装依赖
rem
rem  为什么需要: .venv 不入版本库(体积 150MB+, 且含平台相关二进制),
rem  用户 clone 下来是没有的。原启动脚本只会报"未找到 .venv, 见 README",
rem  但 README 里没有"环境准备"这一节 —— 新用户会卡在第一条命令。
rem
rem  用法(在本脚本目录下): call _ensure_venv.bat ..\danmaku-server
rem  参数: 目标目录(相对本脚本或绝对), 该目录下需有 requirements.txt
rem
rem  退出码: 0 = 环境就绪, 1 = 失败(已打印原因)
rem ============================================================
setlocal
set "TARGET_DIR=%~1"
if "%TARGET_DIR%"=="" (
    echo [错误] _ensure_venv.bat 需要一个参数: 目标目录^(含 requirements.txt^)
    exit /b 1
)
pushd "%TARGET_DIR%" 2>nul
if errorlevel 1 (
    echo [错误] 目录不存在: %TARGET_DIR%
    exit /b 1
)
set "VENV_PY=%CD%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    rem 已存在: 检查依赖是否齐(用 import 试探, 比比对文件可靠 —— 文件可能被手改)
    "%VENV_PY%" -c "import aiohttp, numpy" >nul 2>&1
    if not errorlevel 1 (
        popd
        exit /b 0
    )
    echo [信息] .venv 存在但缺少依赖, 正在安装...
) else (
    echo ============================================================
    echo   首次运行: 正在创建 Python 虚拟环境
    echo   这只会发生一次, 约需 1-3 分钟^(取决于网速^)
    echo ============================================================
    echo.
    rem 优先用 py 启动器(官方安装器会装), 退回 python
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo [错误] 没找到 Python。
            echo        请先安装 Python 3.12+: https://www.python.org/downloads/
            echo        安装时务必勾选 "Add python.exe to PATH"。
            popd
            pause
            exit /b 1
        )
        python -m venv .venv
    )
    if not exist "%VENV_PY%" (
        echo.
        echo [错误] 创建虚拟环境失败。
        echo        常见原因: Python 版本过低^(需 3.12+^), 或没有写权限。
        popd
        pause
        exit /b 1
    )
)

echo [信息] 正在安装依赖^(首次约 1-2 分钟^)...
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。请检查网络后重试。
    echo        如需国内镜像: pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    popd
    pause
    exit /b 1
)
echo.
echo [完成] 环境已就绪。
popd
exit /b 0
