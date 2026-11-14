@echo off
rem One-click launcher: group-chat panel + real CLI engine bridge.
rem Works from any folder: everything is resolved relative to this script.
setlocal
set WS=%~dp0
set PY=python
where python >nul 2>&1 || set PY=py

rem ---------- 1) panel server (127.0.0.1:8765) ----------
%PY% -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/state',timeout=2).status==200 else 1)" >nul 2>&1
if errorlevel 1 (
  echo Starting panel server...
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%PY%' -ArgumentList ('%WS%panel_server.py'),'--port','8765' -WorkingDirectory '%WS%' -WindowStyle Hidden"
  powershell -NoProfile -Command "Start-Sleep -Seconds 3"
) else (
  echo Panel server already running.
)

rem ---------- 2) engine bridge (codex / claude, 127.0.0.1:8899) ----------
%PY% -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/health',timeout=2).status==200 else 1)" >nul 2>&1
if errorlevel 1 (
  echo Starting engine server...
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%PY%' -ArgumentList ('%WS%engine_server.py'),'--port','8899' -WorkingDirectory '%WS%' -WindowStyle Hidden"
  powershell -NoProfile -Command "Start-Sleep -Seconds 2"
) else (
  echo Engine server already running.
)

rem ---------- 3) open the panel ----------
if /i "%~1"=="--no-open" goto :status
start "" "http://127.0.0.1:8765/"

:status
echo Panel URL:   http://127.0.0.1:8765/
echo Engines API: http://127.0.0.1:8899/health
pause
