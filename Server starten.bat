@echo off
rem Weisslochhaus: Server fuers Zusammenspielen starten.
rem Holt zuerst den neuesten Stand von GitHub, dann laeuft der Server in diesem Fenster.
cd /d "%~dp0"
title Weisslochhaus-Server
echo Hole den neuesten Stand ...
git pull
echo.
python server\netz_server.py --port 8070
echo.
echo Der Server wurde beendet.
pause
