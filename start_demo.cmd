@echo off
rem Public demo mode (double-click to run).
rem   1) Starts Tailscale Funnel in the background (public HTTPS -> 127.0.0.1:8787)
rem   2) Launches the server in public mode (loopback auto-HOST disabled)
rem When it asks for the project folder, enter a throwaway folder.
rem The browser opens to the HOST link automatically; use the "invite" button
rem to create guest links. Stop everything afterwards with stop.cmd.
echo [demo] Starting Tailscale Funnel (background) on port 8787 ...
tailscale funnel --bg 8787
if errorlevel 1 echo [demo] WARNING: funnel failed - check Tailscale is up and Funnel is enabled.
tailscale funnel status
set "AI_ROUNDTABLE_PUBLIC=1"
echo [demo] Public mode ON.
call "%~dp0start.cmd"
