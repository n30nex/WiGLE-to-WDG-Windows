@echo off
setlocal
cd /d "%~dp0"
call run_wigle_to_wdg.bat --dry-run --verbose
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo Dry test failed. See wigle_to_wdg.log for details.
) else (
    echo Dry test finished successfully. No WDG upload was made.
)
pause
exit /b %EXITCODE%
