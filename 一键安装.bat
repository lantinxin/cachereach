@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title cachereach 一键安装

rem ============================================================
rem  cachereach 一键安装（Windows  CMD 版）
rem  双击本文件即可运行
rem ============================================================

cd /d "%~dp0"

echo(
echo ============================================================
echo   cachereach 环境安装
echo   从浏览器缓存导出网页游戏本机数据
echo ============================================================
echo(

rem ---------- 1. 定位 Python ----------
set "PY="

rem 优先用已建好的虚拟环境
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
    echo [1/2] 使用已有虚拟环境
    goto :run
)

rem 优先 py 启动器（Windows 官方，常能拿到最新版）
where py >nul 2>&1 && (
    set "PY=py -3"
    echo [1/2] 使用 py 启动器
    goto :run
)

rem 其次 PATH 里的 python
where python >nul 2>&1 && (
    set "PY=python"
    echo [1/2] 使用系统 Python
    goto :run
)

rem 常见安装位置兜底
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
) do (
    if exist %%P (
        set "PY=%%~P"
        echo [1/2] 使用 %%~P
        goto :run
    )
)

echo(
echo   [X] 未找到 Python
echo(
echo   请先安装 Python 3.8 或更高版本：
echo     https://www.python.org/downloads/
echo(
echo   安装时请勾选 "Add Python to PATH"
echo(
pause
exit /b 1

:run
echo(

rem ---------- 2. 运行安装 ----------
echo [2/2] 开始检查与安装...
echo(

%PY% "%~dp0setup.py" %*
set "RC=%ERRORLEVEL%"

echo(
if "%RC%"=="0" (
    echo ============================================================
    echo   安装流程结束
    echo ============================================================
) else (
    echo ============================================================
    echo   安装过程出现问题，退出码 %RC%
    echo ============================================================
)
echo(

rem 失败时保留窗口便于查看提示
if not "%RC%"=="0" (
    echo 按任意键关闭...
    pause >nul
) else (
    rem 成功时 3 秒后自动关闭，便于阅读结果
    echo 3 秒后自动关闭...
    timeout /t 3 /nobreak >nul
)

endlocal
exit /b %RC%
