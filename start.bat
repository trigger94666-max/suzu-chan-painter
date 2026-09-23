@echo off
cd /d %~dp0
title 小铃跑图台
set "URL=http://127.0.0.1:8199"

set "PYEXE="
where python >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE (
  where py >nul 2>&1 && set "PYEXE=py"
)
if not defined PYEXE (
  if exist "%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\python.exe" set "PYEXE=%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
)
if not defined PYEXE (
  echo [x] 找不到 python，请把它加进 PATH 后重试。
  pause
  exit /b 1
)

netstat -ano | findstr ":8199" | findstr LISTENING >nul
if not errorlevel 1 goto open

echo 正在启动小铃跑图台...
start "suzune-imgui" /min cmd /c "%PYEXE% server.py > server.log 2>&1"

set /a n=0
:wait
netstat -ano | findstr ":8199" | findstr LISTENING >nul
if not errorlevel 1 goto open
set /a n+=1
if %n% geq 30 goto fail
ping -n 2 127.0.0.1 >nul
goto wait

:fail
echo.
echo [x] 30 秒内没起来。server.log 末尾：
powershell -NoProfile -Command "if (Test-Path server.log) { Get-Content server.log -Tail 15 }"
echo.
pause
exit /b 1

:open
echo 已就绪，正在打开 %URL%
start "" "%URL%"
if errorlevel 1 explorer "%URL%"
ping -n 2 127.0.0.1 >nul
exit
