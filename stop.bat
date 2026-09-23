@echo off
title 关闭铃酱跑图台
echo 正在关闭铃酱跑图台...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8199" ^| findstr LISTENING') do (
  echo   结束进程 %%p
  taskkill /PID %%p /F >nul 2>&1
)
echo 完成（ComfyUI 保持不动）。
ping -n 3 127.0.0.1 >nul
exit
