@echo off

rem cachereach启动器(由 setup.py 生成)

cd /d "%~dp0"

set "PY=C:\Users\fylqjzh\WorkBuddy\2026-10-18-16-56-26\tools\.venv\Scripts\python.exe"

if not exist "%PY%" set "PY=python"

"%PY%" cachereach.py %*

