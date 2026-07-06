@echo off
rem Stop the public demo: turn off Tailscale Funnel.
rem (Close the server window yourself.)
echo [demo] Turning off Tailscale Funnel ...
tailscale funnel reset
tailscale funnel status
echo [demo] Done. Funnel is off.
pause
