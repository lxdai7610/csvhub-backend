@echo off
setlocal
cd /d "%~dp0"
set "CSVHUB_PYTHON="
set "CSVHUB_PYTHON_ARGS="
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if exist "%~dp0.venv\Scripts\python.exe" (
    set "CSVHUB_PYTHON=%~dp0.venv\Scripts\python.exe"
    goto runtime_ready
)

if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" if exist "%~dp0..\..\work\test-deps\fastapi" (
    set "CSVHUB_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    set "PYTHONPATH=%~dp0;%~dp0..\..\work\test-deps;%PYTHONPATH%"
    goto runtime_ready
)

where python.exe >nul 2>&1
if not errorlevel 1 (
    set "CSVHUB_PYTHON=python.exe"
    goto runtime_ready
)
where py.exe >nul 2>&1
if not errorlevel 1 (
    set "CSVHUB_PYTHON=py.exe"
    set "CSVHUB_PYTHON_ARGS=-3"
    goto runtime_ready
)
echo Python is not available. Run install.cmd to prepare this project.
pause
exit /b 1

:runtime_ready
"%CSVHUB_PYTHON%" %CSVHUB_PYTHON_ARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"
if errorlevel 1 (
    echo Python 3.12 or newer is required. Run install.cmd to prepare this project.
    pause
    exit /b 1
)
"%CSVHUB_PYTHON%" %CSVHUB_PYTHON_ARGS% -c "import psutil"
if errorlevel 1 (
    echo Project dependencies are missing. Run install.cmd to prepare this project.
    pause
    exit /b 1
)
"%CSVHUB_PYTHON%" %CSVHUB_PYTHON_ARGS% -m csvhub.launch --action stop %*
set "CSVHUB_EXIT_CODE=%errorlevel%"
if not "%CSVHUB_EXIT_CODE%"=="0" pause
exit /b %CSVHUB_EXIT_CODE%
