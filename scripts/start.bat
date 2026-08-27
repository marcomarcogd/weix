@echo off
chcp 65001 >nul
REM ============================================================
REM Weix - Windows 一键启动脚本
REM ============================================================
setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo  Weix - 启动服务 (Windows)
echo ============================================================

REM 切换到项目根目录
cd /d "%~dp0\.."
set "PROJECT_DIR=%CD%"

REM 检查虚拟环境
if not exist "venv\Scripts\python.exe" (
    echo [错误] 虚拟环境不存在，请先运行 scripts\setup.bat
    pause
    exit /b 1
)

REM 检查前端运行环境。直接调用 Node + Vite，避免 npm 启动器异常。
set "NODE_EXE="
for /f "delims=" %%I in ('where node.exe 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%I"
if not defined NODE_EXE (
    echo [错误] 未找到 Node.js，请先安装 Node.js 20 或更高版本
    pause
    exit /b 1
)

if not exist "frontend\node_modules\vite\bin\vite.js" (
    echo [错误] 前端依赖不存在，请先运行 scripts\setup.bat
    pause
    exit /b 1
)

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 curl.exe，无法执行启动健康检查
    pause
    exit /b 1
)

REM 防止误连到旧服务或启动重复进程。
netstat -ano -p TCP | findstr /C:":8000 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo [错误] 端口 8000 已被占用，请先关闭旧的 Weix 后端窗口
    pause
    exit /b 1
)

netstat -ano -p TCP | findstr /C:":5173 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo [错误] 端口 5173 已被占用，请先关闭旧的 Weix 前端窗口
    pause
    exit /b 1
)

echo [启动] FastAPI 后端 (端口 8000)...
start "Weix-Backend" /D "%PROJECT_DIR%\backend" "%ComSpec%" /d /k "chcp 65001 >nul && ""%PROJECT_DIR%\venv\Scripts\python.exe"" -m app.main"

echo [启动] 前端开发服务器 (端口 5173)...
start "Weix-Frontend" /D "%PROJECT_DIR%\frontend" "%NODE_EXE%" "node_modules\vite\bin\vite.js" --host 127.0.0.1 --port 5173 --strictPort

echo [检查] 等待前端启动...
set /a FRONTEND_WAIT=0
:WAIT_FRONTEND
curl.exe -fsS --max-time 2 "http://127.0.0.1:5173/" >nul 2>&1
if not errorlevel 1 goto FRONTEND_READY
set /a FRONTEND_WAIT+=1
if %FRONTEND_WAIT% GEQ 30 goto FRONTEND_FAILED
timeout /t 1 /nobreak >nul
goto WAIT_FRONTEND

:FRONTEND_READY
echo [检查] 前端已就绪，等待后端初始化...
set /a BACKEND_WAIT=0
:WAIT_BACKEND
curl.exe -fsS --max-time 2 "http://127.0.0.1:8000/api/health" >nul 2>&1
if not errorlevel 1 goto ALL_READY
set /a BACKEND_WAIT+=1
if %BACKEND_WAIT% GEQ 120 goto BACKEND_FAILED
timeout /t 1 /nobreak >nul
goto WAIT_BACKEND

:ALL_READY
echo [检查] 前后端均已就绪，正在打开管理页面...
if /I not "%WEIX_NO_BROWSER%"=="1" start "" "http://127.0.0.1:5173/"

echo.
echo ============================================================
echo  Weix 服务已启动
echo   后端: http://localhost:8000
echo   后端文档: http://localhost:8000/docs
echo   前端: http://localhost:5173
echo.
echo   关闭 Weix-Backend 和 Weix-Frontend 窗口可停止服务
echo ============================================================

REM 保持窗口打开
pause
exit /b 0

:FRONTEND_FAILED
echo.
echo [错误] 前端在 30 秒内未能启动。
echo 请查看 Weix-Frontend 窗口中的错误信息；窗口会保持打开，不会再闪退。
pause
exit /b 1

:BACKEND_FAILED
echo.
echo [错误] 后端在 120 秒内未完成初始化。
echo 前端已启动，请查看 Weix-Backend 窗口中的错误信息。
pause
exit /b 1
