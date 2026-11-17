@echo off
setlocal
cd /d "%~dp0"
if exist "dist-amount-word\AmountTool\AmountTool.exe" goto packaged
if not exist ".venv\Scripts\python.exe" goto missing_environment
".venv\Scripts\python.exe" -I -m number_app.amount_mode gui --data-dir "%~dp0data_amount"
set "numberAmountExitCode=%errorlevel%"
if not "%numberAmountExitCode%"=="0" pause
exit /b %numberAmountExitCode%

:packaged
start "" "%~dp0dist-amount-word\AmountTool\AmountTool.exe" gui --data-dir "%~dp0data_amount"
exit /b 0

:missing_environment
echo Run setup.cmd once before starting the application.
pause
exit /b 2
