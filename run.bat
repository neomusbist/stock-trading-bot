@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "ENTRY=main.py"
if "%~1"=="" set "ENTRY=supervisor.py"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" %ENTRY% %*
) else if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" %ENTRY% %*
) else (
  py -3 %ENTRY% %*
)
set RESULT=%ERRORLEVEL%
if not "%RESULT%"=="0" pause
exit /b %RESULT%
