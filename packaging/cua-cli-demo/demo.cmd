@echo off
setlocal
if not exist "%~dp0runtime\python\python.exe" (
    echo Bundled Python is missing. Extract the entire demo ZIP before running it.
    pause
    exit /b 1
)
"%~dp0runtime\python\python.exe" -I "%~dp0launch.py" %*
set "demo_exit=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %demo_exit%
