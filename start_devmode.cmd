@echo off
rem Dev mode (double-click to run).
rem   Sets AI_ROUNDTABLE_DEVMODE=1: AI seats form a fixed controller/implementer/
rem   verifier pipeline that can read and write the target project directory.
rem   Loopback-only (no Tailscale Funnel); invite/guest join is disabled.
set "AI_ROUNDTABLE_DEVMODE=1"
echo [devmode] Dev mode ON.
call "%~dp0start.cmd"
