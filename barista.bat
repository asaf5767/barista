@echo off
:: De'Longhi Barista CLI
:: Usage: barista [start|stop|status|restart|logs|ui|scan]

setlocal
set "PROJECT=C:\Users\assafakiva\projects\delonghi-coffee-server"

if "%1"=="" goto help
if "%1"=="start" goto start
if "%1"=="stop" goto stop
if "%1"=="status" goto status
if "%1"=="restart" goto restart
if "%1"=="logs" goto logs
if "%1"=="ui" goto ui
if "%1"=="scan" goto scan
if "%1"=="brew" goto brew
if "%1"=="help" goto help
goto help

:start
cd /d "%PROJECT%"
for /f "tokens=2" %%a in ('tasklist /FI "WINDOWTITLE eq Barista*" /NH 2^>nul ^| findstr python') do taskkill /PID %%a /F >nul 2>&1
start "Barista Server" /min python -m barista start --address 00:A0:50:2A:D2:8F --port 8080
timeout /t 3 /nobreak >nul
echo   Barista started: http://localhost:8080
start http://localhost:8080
goto :eof

:stop
taskkill /FI "WINDOWTITLE eq Barista Server*" /F >nul 2>&1
echo   Barista stopped.
goto :eof

:restart
call :stop
timeout /t 2 /nobreak >nul
call :start
goto :eof

:status
curl -s http://localhost:8080/api/status 2>nul
if errorlevel 1 (
    echo   Barista is NOT running.
    echo   Start: barista start
) else (
    echo.
)
goto :eof

:logs
if exist "%PROJECT%\server.log" (
    type "%PROJECT%\server.log" | more
) else (
    echo   No logs yet.
)
goto :eof

:ui
start http://localhost:8080
goto :eof

:scan
cd /d "%PROJECT%"
python -m barista scan
goto :eof

:brew
cd /d "%PROJECT%"
if "%2"=="" (
    curl -s -X POST http://localhost:8080/api/brew -H "Content-Type: application/json" -d "{\"beverage\": \"espresso\"}" 2>nul
) else (
    curl -s -X POST http://localhost:8080/api/brew -H "Content-Type: application/json" -d "{\"beverage\": \"%2\"}" 2>nul
)
echo.
goto :eof

:help
echo.
echo   barista - De'Longhi Coffee Machine CLI
echo.
echo   barista start       Start the server
echo   barista stop        Stop the server
echo   barista restart     Restart
echo   barista status      Machine status (JSON)
echo   barista ui          Open web UI in browser
echo   barista brew        Brew espresso (default)
echo   barista brew coffee Brew a specific drink
echo   barista scan        Scan for BLE machines
echo   barista logs        View server logs
echo   barista help        This message
echo.
goto :eof
