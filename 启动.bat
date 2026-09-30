@echo off
rem 媒体浏览器启动脚本（Windows）
rem   双击本文件即可启动；要开放局域网就把下面这行改成 set MEDIA_BROWSER_LAN=1
setlocal
chcp 65001 >nul
set MEDIA_BROWSER_LAN=0
set HERE=%~dp0
cd /d "%HERE%"

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" (where python >nul 2>nul && set PY=python)
if "%PY%"=="" (
  echo 没找到 Python。请先安装：https://www.python.org/downloads/
  echo 安装时记得勾选 "Add python.exe to PATH"
  pause
  exit /b 1
)

%PY% "%HERE%启动.py" %*
if errorlevel 1 pause
endlocal
