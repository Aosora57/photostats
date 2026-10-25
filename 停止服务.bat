@echo off
chcp 65001 >nul
title 停止 PhotoStats 服务
echo 正在查找 PhotoStats(端口 8765)...
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo   结束进程 PID %%a
    taskkill /F /PID %%a >nul 2>&1
    set FOUND=1
)
if %FOUND%==0 (echo 没有正在运行的服务。) else (echo 已停止。)
pause
