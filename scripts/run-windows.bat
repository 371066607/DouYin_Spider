@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0.."

echo ============================================
echo    LiangBaShuaZi - Windows Launcher
echo ============================================
echo.

REM ---- Pre-check: Python ----
where python >nul 2>nul
if errorlevel 1 goto :no_python
where npm >nul 2>nul
if errorlevel 1 goto :no_node

REM ---- 1) venv + Python deps + chromium ----
if exist ".venv\Scripts\python.exe" goto :venv_ready
echo [1/3] First run: creating venv and installing Python deps...
python -m venv .venv
if errorlevel 1 goto :venv_failed
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
echo [1/3] Downloading chromium browser (~150MB, please wait)...
python -m playwright install chromium
goto :node_deps

:venv_ready
call ".venv\Scripts\activate.bat"

:node_deps
REM ---- 2) Node signing deps. Skip optional deps (canvas) — not needed for
REM signing and it fails to compile on newer Node without VS build tools. ----
if exist "node_modules\jsdom" goto :launch
echo [2/3] Installing Node signing deps (npm install, slow on first run)...
if exist node_modules rmdir /s /q node_modules
REM npm is npm.cmd on Windows; MUST use "call" or control never returns here.
call npm install --omit=optional
if errorlevel 1 goto :npm_failed

:launch
REM ---- 3) Launch client; log stderr so a crash is visible ----
echo [3/3] Starting client...
echo.
set "ERRLOG=%TEMP%\liangbashuazi-error.log"
python -m desktop.client 2>"%ERRLOG%"
set "EXITCODE=%ERRLEVEL%"
echo.
if not "%EXITCODE%"=="0" goto :crashed
echo Program exited normally.
goto :end

:no_python
echo [ERROR] Python not found. Install Python 3.11 or 3.12 (check "Add Python to PATH"):
echo         https://www.python.org/downloads/windows/
goto :end

:no_node
echo [ERROR] Node.js / npm not found. Install Node.js LTS:
echo         https://nodejs.org/
goto :end

:venv_failed
echo [ERROR] Failed to create virtual environment. Is Python installed correctly?
goto :end

:pip_failed
echo [ERROR] pip install failed. Check your network and requirements.txt.
goto :end

:npm_failed
echo [ERROR] npm install failed. Check your network / build tools.
goto :end

:crashed
echo ============================================
echo   The program crashed. Error details below:
echo ============================================
if exist "%ERRLOG%" type "%ERRLOG%"
echo.
echo (Full log saved at: %ERRLOG%)
echo Please screenshot the lines above and send them over.
goto :end

:end
echo.
pause
endlocal
