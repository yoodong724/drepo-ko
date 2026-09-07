@echo off
rem Death Game Report Korean patch - install launcher.
rem Keeps this file pure ASCII; all Korean messages come from patch_release.ps1.
rem Extra arguments are forwarded, e.g. install.cmd -GameDir "D:\path\to\game".
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0patch_release.ps1" -Command install -PackageDir "%~dp0." %*
set RC=%ERRORLEVEL%
echo.
pause
endlocal & exit /b %RC%
