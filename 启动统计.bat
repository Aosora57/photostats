@echo off
chcp 65001 >nul
title PhotoStats 服务 - 关闭此窗口即停止服务
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python photostats.py
) else (
  py photostats.py
)
echo 服务已停止。
pause
