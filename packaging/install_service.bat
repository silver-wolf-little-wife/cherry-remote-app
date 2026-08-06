@echo off
rem ============================================================
rem  Cherry Remote App - install as Windows service via NSSM
rem  Prereq:
rem    1) run build_exe.bat first (produces dist\cherry-remote-app.exe)
rem    2) put nssm.exe into this packaging\ dir (https://nssm.cc/download)
rem    3) put config.yaml next to the exe (dist\config.yaml)
rem  Run as Administrator.
rem ============================================================
setlocal
set SRV=cherry-remote-app
set BASE=%~dp0..
set EXE=%BASE%\dist\cherry-remote-app.exe
set CFG=%BASE%\dist\config.yaml
set NSSM=%~dp0nssm.exe

if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found in packaging\. Download from https://nssm.cc/download
    exit /b 1
)
if not exist "%EXE%" (
    echo [ERROR] dist\cherry-remote-app.exe not found. Run build_exe.bat first.
    exit /b 1
)
if not exist "%CFG%" (
    echo [ERROR] dist\config.yaml not found. Copy config.example.yaml next to the exe and edit it.
    exit /b 1
)

"%NSSM%" install %SRV% "%EXE%" -c "%CFG%"
"%NSSM%" set %SRV% AppDirectory "%BASE%\dist"
"%NSSM%" set %SRV% Start SERVICE_AUTO_START
"%NSSM%" set %SRV% AppStdout "%BASE%\dist\logs\service-out.log"
"%NSSM%" set %SRV% AppStderr "%BASE%\dist\logs\service-err.log"
"%NSSM%" set %SRV% AppRotateFiles 1
"%NSSM%" set %SRV% AppRotateBytes 10485760
"%NSSM%" set %SRV% AppExit Default Restart
"%NSSM%" set %SRV% AppRestartDelay 5000

echo.
echo Service installed as: %SRV%
echo Start:  nssm start %SRV%
echo Stop:   nssm stop %SRV%
echo Remove: nssm stop %SRV% ^& nssm remove %SRV% confirm
endlocal
