@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist .env (
    copy .env.example .env >nul
    echo Created .env. Add your LLM_API_KEY, save it, then close Notepad.
    start /wait notepad .env
)
.venv\Scripts\python.exe run.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Install Python 3.12 and check the error above.
pause
exit /b 1
