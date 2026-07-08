@echo off
echo [stop] Turning off Tailscale Funnel ...
tailscale funnel reset
tailscale funnel status
echo.
powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue; if ($conns) { $procIds = $conns.OwningProcess | Sort-Object -Unique; foreach ($pid_ in $procIds) { Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue; Write-Host ('[stop] roundtable server stopped (PID ' + $pid_ + ')') } } else { Write-Host '[stop] server is not running' }"
pause
