@echo off
rem Start ONLY the real-CLI engine bridge (codex / claude).
rem Use start-panel.cmd when you also want the group-chat panel.
setlocal
set WS=%~dp0
set PY=python
where python >nul 2>&1 || set PY=py

%PY% -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/health',timeout=2).status==200 else 1)" >nul 2>&1
if errorlevel 1 (
  echo Starting engine server...
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%PY%' -ArgumentList ('%WS%engine_server.py'),'--port','8899' -WorkingDirectory '%WS%' -WindowStyle Hidden"
  powershell -NoProfile -Command "Start-Sleep -Seconds 2"
) else (
  echo Engine server already running.
)

%PY% -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8899/health',timeout=3).read().decode('utf-8'))"
pause
