@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" app.py %*
if errorlevel 1 pause
